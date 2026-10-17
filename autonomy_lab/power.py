"""Monte-Carlo power simulation for the Autonomy Lab design (LOOP_SPEC.md §2.2, §2.3, §3.1, §3.2; T5, D7).

Usage: python3 -m autonomy_lab.power [--sims N] [--n-boot N] [--jobs N] [--seed S] [--json PATH]
       [--only e1,e2,e3] [--refine-sims N] [--refine-boot N] [grid options]

Each simulated E1 replicate builds the exact nested design: tasks with the draft bank's decision mix
(arbitrary, inferable and preserve decisions), and the balanced channel/filler assignment of §2.3 (filler
halves, per-decision channel permutations, P4-exposure acceptance over every decision). Outcomes come from a
logistic model with per-task, per-decision and per-trial random effects, separately for the arbitrary and
inferable strata. Preserve decisions take part in the design (they count towards sibling-check fractions)
but are not simulated, because no prediction uses them. The simulated observations then go through the same
estimand and verdict functions as autonomy_lab.analysis, so the power reported here is the power of the
frozen analysis, including its stratum rules.

E2 replicates (P5) follow §3.1: breadth 1, every decision unsettled, arms `plain` and `assume` balanced
within task. True disclosure and satisfaction come from the same kind of logistic model. The T6 judge's label
is the true disclosure flipped with probability 1 - judge accuracy, and P5(a)/(b) use the judge's labels.
The audit draws a random 20% of eligible judgements and computes Cohen's kappa between the judge and the
auditor. Assumption: the auditor labels without error, so kappa uncertainty comes from judge error and the
audit sample size only.

E3 replicates draw per-task single-task success rates, a seeded balanced subset design per breadth, and run
the §3.2 P6 bootstrap at B=2 and B=4. E3 is sized on joint H4 power: P6 supported at both breadths in the
same replicate.

Every power figure is reported with its Monte Carlo standard error, sqrt(p (1 - p) / sims). After the first
pass, every cell whose target power (see REFINE_TARGETS) is within 2 standard errors of 0.80 is re-simulated
with at least --refine-sims simulations (default 1,000) and --refine-boot bootstrap resamples (default
10,000, the production count) before any recommendation uses it. For this rule only, the standard error
uses the Agresti-Coull adjusted rate (k + 2) / (n + 4), so a cell at 0% or 100% is not treated as exact.

Scenario rates and gaps are marginal population values on the probability scale: base logits and
symmetric logit shifts are calibrated by numerical integration over the normal random effects, so averaging
over tasks, decisions and trials reproduces the stated rates. Heterogeneity is stated on the logit scale.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path

from autonomy_lab.analysis import (
    CHANNELS,
    N_BOOT,
    STRATA,
    Obs,
    compute_p1,
    compute_p2,
    compute_p3,
    compute_p4,
    compute_p5,
    p6_breadth,
    p6_inputs,
)

E1_TASKS = (10, 12, 16)
E1_TRIALS = (8, 16, 24, 32)
E2_TASKS = (10, 12, 16)
E2_PER_ARM = (4, 8, 12)
E3_TASKS = (6, 12)
E3_SINGLE = (4, 8)
E3_MULTI = (12, 24, 36, 48, 72, 96, 144)
DESIGN_POOL = 32
MAX_DRAWS = 100_000
E3_BUDGET = 90
TARGET = 0.8
REFINE_SIMS = 1000
REFINE_BOOT = N_BOOT
CHUNK = 50  # simulations per worker job; results do not depend on it

# (arbitrary, inferable, preserve) per task, cycled over tasks. The draft bank (T4, 9 tasks) has five tasks
# with 4/1/1, three with 3/1/1 and one with 4/2/0; this cycle keeps its means (about 3.7 arbitrary,
# 1.2 inferable, 1 preserve, 5.8 decisions per task).
DRAFT_MIX = ((4, 1, 1), (3, 1, 1), (4, 1, 1), (4, 2, 1), (3, 1, 1), (4, 1, 1))
LEGACY_MIX = ((2, 3, 1),)  # the structure of the first T5 simulation: 6 decisions, 2 arbitrary

# Which power figure decides each experiment's sizing, and so which cells are refined near 0.80.
REFINE_TARGETS = {
    "e1": {"strong": ("P1", "P2", "P3", "P4")},
    "e2": {"effective": ("P5",)},
    "e3": {"independent": ("H4",)},
}


def logit(p):
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


# Midpoint rule over +-6 SD for E[expit(x + sd * Z)], Z standard normal; accurate to ~1e-6 here.
_GRID = [-6 + 12 * (i + 0.5) / 400 for i in range(400)]
_WEIGHTS = [math.exp(-z * z / 2) for z in _GRID]
_WSUM = sum(_WEIGHTS)


def marginal(x, sd):
    if sd == 0:
        return expit(x)
    return sum(w * expit(x + sd * z) for w, z in zip(_WEIGHTS, _GRID)) / _WSUM


@cache
def calibrate(p, sd):
    """Logit location whose marginal rate under N(0, sd) random effects is p."""
    lo, hi = -30.0, 30.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if marginal(mid, sd) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@cache
def symmetric_shift(p, gap, sd=0.0):
    """Logit shift h so that, around the location with marginal rate p, the marginal rates at +h/2 and -h/2
    differ by gap."""
    if gap == 0:
        return 0.0
    x = calibrate(p, sd)
    lo, hi = 0.0, 20.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if marginal(x + mid / 2, sd) - marginal(x - mid / 2, sd) < abs(gap):
            lo = mid
        else:
            hi = mid
    return math.copysign((lo + hi) / 2, gap)


def mc_se(p, n):
    """Monte Carlo standard error of a simulated proportion."""
    return math.sqrt(p * (1 - p) / n) if n else 0.0


def near_target(k, n, target=TARGET):
    """True when k/n is within 2 (Agresti-Coull adjusted) standard errors of the target."""
    adjusted = (k + 2) / (n + 4)
    return abs(k / n - target) <= 2 * math.sqrt(adjusted * (1 - adjusted) / n)


# ---------------------------------------------------------------- balanced E1 design (§2.3)


def sibling_fraction(channels, index):
    others = len(channels) - 1
    return sum(1 for j, c in enumerate(channels) if j != index and c in ("check", "both")) / others


def p4_exposed(trials):
    """True when every decision's `none` cells include sibling-check fractions <= 0.4 and >= 0.6."""
    for d in range(len(trials[0][1])):
        fractions = [round(sibling_fraction(ch, d), 12) for _, ch in trials if ch[d] == "none"]
        if not (any(f <= 0.4 for f in fractions) and any(f >= 0.6 for f in fractions)):
            return False
    return True


def generate_design(n_decisions, n_trials, rng, max_draws=MAX_DRAWS):
    """Balanced design for one task: [(filler, (channel per decision))], plus the number of draws used."""
    if n_trials % 8:
        raise ValueError("trials per task must be a multiple of 8")
    half = n_trials // 2
    for draw in range(1, max_draws + 1):
        trials = []
        for filler in (True, False):
            columns = []
            for _ in range(n_decisions):
                cells = list(CHANNELS) * (half // 4)
                rng.shuffle(cells)
                columns.append(cells)
            trials += [(filler, tuple(col[i] for col in columns)) for i in range(half)]
        if p4_exposed(trials):
            return trials, draw
    raise RuntimeError(f"no P4-exposed design in {max_draws} draws ({n_decisions} decisions, {n_trials} trials)")


@cache
def design_pool(n_decisions, n_trials, seed):
    rng = random.Random(f"design-pool:{seed}:{n_decisions}:{n_trials}")
    return tuple(generate_design(n_decisions, n_trials, rng)[0] for _ in range(DESIGN_POOL))


# ---------------------------------------------------------------- E1 scenarios and simulation


def _rates(settled, unsettled, check=None):
    return {"none": unsettled, "contract": settled, "check": settled if check is None else check, "both": settled}


INFERABLE = _rates(0.95, 0.80)


@dataclass(frozen=True)
class E1Scenario:
    name: str
    description: str
    rates: dict  # population satisfaction rate of arbitrary decisions per channel
    inferable: dict = field(default_factory=lambda: dict(INFERABLE))  # same, for inferable decisions
    mix: tuple = DRAFT_MIX
    task_sd: float = 0.8  # logit SD of task effects (separate draws for settled and unsettled, per stratum)
    decision_sd: float = 0.5  # logit SD of decision effects within a task
    trial_sd: float = 0.5  # logit SD of a trial effect shared by its decisions
    filler_gap: dict = field(default_factory=dict)  # {label: unsettled rate, filler on minus off}
    spill_gap: dict = field(default_factory=dict)  # {label: unsettled rate, sibling-check >= 0.6 minus <= 0.4}
    expect: dict = field(default_factory=dict)


NULL_EXPECT = {"P1": "supported", "P2": "supported", "P3": "supported", "P4": "supported"}

E1_SCENARIOS = (
    E1Scenario(
        "strong",
        "arbitrary settled 0.95 on every channel, unsettled 0.30; inferable settled 0.95, unsettled 0.80; "
        "no channel, filler or spillover effect",
        _rates(0.95, 0.30),
        expect=NULL_EXPECT,
    ),
    E1Scenario(
        "strong-legacy",
        "as strong, with the first simulation's structure: 6 decisions, 2 arbitrary, 3 inferable, 1 preserve",
        _rates(0.95, 0.30),
        mix=LEGACY_MIX,
        expect=NULL_EXPECT,
    ),
    E1Scenario(
        "moderate",
        "arbitrary settled 0.93, unsettled 0.45 (+48 pp, close to the P1(b)/(c) thresholds)",
        _rates(0.93, 0.45),
        expect={"P1": "supported"},
    ),
    E1Scenario(
        "null",
        "no coverage effect: every arbitrary channel 0.85 (unsettled decisions are not arbitrary for this agent)",
        _rates(0.85, 0.85),
        expect={"P1": "falsified"},
    ),
    E1Scenario(
        "execution",
        "arbitrary settled 0.70, unsettled 0.20: a large effect produced by poor execution",
        _rates(0.70, 0.20),
        expect={"P1": "falsified"},
    ),
    E1Scenario(
        "channel-gap",
        "arbitrary contract/both 0.95, check-only 0.65, unsettled 0.30 (+30 pp contract minus check)",
        _rates(0.95, 0.30, check=0.65),
        expect={"P2": "falsified"},
    ),
    E1Scenario(
        "channel-gap-inferable",
        "as strong, but inferable check-only 0.65 (+30 pp contract minus check, inferable only)",
        _rates(0.95, 0.30),
        inferable=_rates(0.95, 0.80, check=0.65),
        expect={"P2": "falsified"},
    ),
    E1Scenario(
        "filler",
        "as strong, plus filler raises the unsettled arbitrary rate by 25 pp (on minus off)",
        _rates(0.95, 0.30),
        filler_gap={"arbitrary": 0.25},
        expect={"P3": "falsified"},
    ),
    E1Scenario(
        "filler-inferable",
        "as strong, plus filler raises the unsettled inferable rate by 25 pp (inferable only)",
        _rates(0.95, 0.30),
        filler_gap={"inferable": 0.25},
        expect={"P3": "falsified"},
    ),
    E1Scenario(
        "spillover",
        "as strong, plus unsettled arbitrary rate 25 pp higher at sibling-check >= 0.6 than at <= 0.4",
        _rates(0.95, 0.30),
        spill_gap={"arbitrary": 0.25},
        expect={"P4": "falsified"},
    ),
    E1Scenario(
        "spillover-inferable",
        "as strong, plus the same 25 pp spillover on unsettled inferable decisions only",
        _rates(0.95, 0.30),
        spill_gap={"inferable": 0.25},
        expect={"P4": "falsified"},
    ),
)


def e1_parameters(scenario):
    """{label: (base logit per channel, filler shift, spill shift)}, calibrated so stated rates are marginal."""
    sd = math.sqrt(scenario.task_sd**2 + scenario.decision_sd**2 + scenario.trial_sd**2)
    out = {}
    for label, rates in (("arbitrary", scenario.rates), ("inferable", scenario.inferable)):
        base = {c: calibrate(p, sd) for c, p in rates.items()}
        filler_h = symmetric_shift(rates["none"], scenario.filler_gap.get(label, 0.0), sd)
        spill_h = symmetric_shift(rates["none"], scenario.spill_gap.get(label, 0.0), sd)
        out[label] = (base, filler_h, spill_h)
    return out


def simulate_e1(scenario, n_tasks, n_trials, rng, pool_seed=0, params=None):
    params = params or e1_parameters(scenario)
    gauss = rng.gauss
    obs = []
    trial_index = 0
    for t in range(n_tasks):
        task = f"t{t:02d}"
        n_arb, n_inf, n_pres = scenario.mix[t % len(scenario.mix)]
        labels = ["arbitrary"] * n_arb + ["inferable"] * n_inf
        pool = design_pool(n_arb + n_inf + n_pres, n_trials, pool_seed)
        design = pool[rng.randrange(len(pool))]
        task_effect = {label: (gauss(0, scenario.task_sd), gauss(0, scenario.task_sd)) for label in STRATA}
        dec = [(gauss(0, scenario.decision_sd), gauss(0, scenario.decision_sd)) for _ in labels]
        for filler, channels in design:
            shared = gauss(0, scenario.trial_sd)
            for d, label in enumerate(labels):
                base, filler_h, spill_h = params[label]
                channel = channels[d]
                sib = sibling_fraction(channels, d)
                x = base[channel] + shared
                if channel == "none":
                    x += task_effect[label][1] + dec[d][1]
                    x += filler_h / 2 if filler else -filler_h / 2
                    s = round(sib, 12)
                    x += spill_h / 2 if s >= 0.6 else -spill_h / 2 if s <= 0.4 else 0.0
                else:
                    x += task_effect[label][0] + dec[d][0]
                result = rng.random() < expit(x)
                obs.append(Obs(trial_index, task, f"d{d}", label, channel, filler, sib, result))
            trial_index += 1
    return obs


E1_KEYS = ("P1", "P1a", "P2", "P3", "P4") + tuple(f"{p}[{s}]" for p in ("P2", "P3", "P4") for s in STRATA)


def e1_verdicts(obs, bseed, n_boot):
    p1 = compute_p1(obs, bseed, n_boot)
    out = {"P1": p1["verdict"], "P1a": p1["components"][0]["verdict"]}
    for key, fn in (("P2", compute_p2), ("P3", compute_p3), ("P4", compute_p4)):
        p = fn(obs, bseed, n_boot)
        out[key] = p["verdict"]
        for label, sub in p["components"][0]["strata"].items():
            out[f"{key}[{label}]"] = sub["verdict"]
    return out


def e1_chunk(scenario, design, start, stop, n_boot, seed):
    n_tasks, n_trials = design
    params = e1_parameters(scenario)
    for sim in range(start, stop):
        rng = random.Random(f"e1:{seed}:{scenario.name}:{n_tasks}:{n_trials}:{sim}")
        obs = simulate_e1(scenario, n_tasks, n_trials, rng, pool_seed=seed, params=params)
        yield e1_verdicts(obs, f"{seed}:{scenario.name}:{n_tasks}:{n_trials}:{sim}", n_boot)


# ---------------------------------------------------------------- E2 (P5) scenarios and simulation


@dataclass(frozen=True)
class E2Scenario:
    name: str
    description: str
    plain: float = 0.10  # true disclosure rate of unsettled arbitrary decisions, `plain`
    assume: float = 0.75  # same, `assume`
    satisfaction: dict = field(default_factory=lambda: {"arbitrary": 0.30, "inferable": 0.80})  # `plain` rates
    satisfaction_gap: dict = field(default_factory=dict)  # {label: assume minus plain}
    judge_accuracy: float = 0.95  # P(judge label == true disclosure), both classes
    audit_fraction: float = 0.2
    mix: tuple = DRAFT_MIX
    task_sd: float = 0.8
    decision_sd: float = 0.5
    trial_sd: float = 0.5
    arm_sd: float = 0.25  # logit SD of a task-by-arm interaction in satisfaction (tasks share the rest)
    expect: str = ""


E2_SCENARIOS = (
    E2Scenario(
        "effective",
        "plain discloses 10%, assume 75%; satisfaction unchanged (arbitrary 0.30, inferable 0.80); judge 95% accurate",
        expect="P5 supported",
    ),
    E2Scenario(
        "effective-noisy-judge",
        "as effective, judge 85% accurate (judged plain rate about 0.22, audit kappa about 0.7)",
        judge_accuracy=0.85,
        expect="P5 supported in truth; judge error biases P5(a) and lowers kappa",
    ),
    E2Scenario(
        "weak-instruction",
        "plain 10%, assume 45%: the instruction helps, but less than the prediction",
        assume=0.45,
        expect="P5 inconclusive",
    ),
    E2Scenario(
        "no-effect",
        "plain and assume both disclose 10%",
        assume=0.10,
        expect="P5 falsified (P5(b))",
    ),
    E2Scenario(
        "satisfaction-shift",
        "as effective, plus assume raises arbitrary satisfaction by 20 pp",
        satisfaction_gap={"arbitrary": 0.20},
        expect="P5 falsified (P5(c))",
    ),
    E2Scenario(
        "satisfaction-shift-inferable",
        "as effective, plus assume lowers inferable satisfaction by 20 pp (inferable only)",
        satisfaction_gap={"inferable": -0.20},
        expect="P5 falsified (P5(c))",
    ),
)


def e2_parameters(scenario):
    sd = math.sqrt(scenario.task_sd**2 + scenario.decision_sd**2 + scenario.trial_sd**2)
    disclose = {"plain": calibrate(scenario.plain, sd), "assume": calibrate(scenario.assume, sd)}
    sd = math.sqrt(sd**2 + scenario.arm_sd**2)
    satisfy = {}
    for label in STRATA:
        p = scenario.satisfaction[label]
        h = symmetric_shift(p, scenario.satisfaction_gap.get(label, 0.0), sd)
        x = calibrate(p, sd)
        satisfy[label] = {"plain": x - h / 2, "assume": x + h / 2}
    return disclose, satisfy


def cohen_kappa(pairs):
    """Cohen's kappa for [(rater a, rater b)] booleans; NaN when chance agreement is 1."""
    n = len(pairs)
    if not n:
        return math.nan
    agree = sum(1 for a, b in pairs if a == b) / n
    pa = sum(1 for a, _ in pairs if a) / n
    pb = sum(1 for _, b in pairs if b) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    return math.nan if chance == 1 else (agree - chance) / (1 - chance)


def simulate_e2(scenario, n_tasks, per_arm, rng, params=None):
    """Return (obs, kappa): E2 observations with judged disclosure, and the simulated audit's kappa."""
    disclose, satisfy = params or e2_parameters(scenario)
    gauss = rng.gauss
    sd_t, sd_d, sd_r = scenario.task_sd, scenario.decision_sd, scenario.trial_sd
    obs = []
    audit_pool = []
    trial_index = 0
    for t in range(n_tasks):
        task = f"t{t:02d}"
        n_arb, n_inf, _ = scenario.mix[t % len(scenario.mix)]
        labels = ["arbitrary"] * n_arb + ["inferable"] * n_inf
        # Disclosure: arm-specific task effects, so the instruction's effect varies freely across tasks.
        # Satisfaction: one task effect per stratum shared by both arms, plus a small task-by-arm interaction.
        task_disc = {arm: gauss(0, sd_t) for arm in ("plain", "assume")}
        shared_task = {label: gauss(0, sd_t) for label in STRATA}
        task_sat = {
            (label, arm): shared_task[label] + gauss(0, scenario.arm_sd)
            for label in STRATA
            for arm in ("plain", "assume")
        }
        dec_disc = [gauss(0, sd_d) for _ in labels]
        dec_sat = [gauss(0, sd_d) for _ in labels]
        arms = ["plain"] * per_arm + ["assume"] * per_arm
        rng.shuffle(arms)
        for arm in arms:
            shared_disc = gauss(0, sd_r)
            shared_sat = gauss(0, sd_r)
            for d, label in enumerate(labels):
                x = satisfy[label][arm] + task_sat[label, arm] + dec_sat[d] + shared_sat
                result = rng.random() < expit(x)
                judged = None
                if label == "arbitrary":
                    truth = rng.random() < expit(disclose[arm] + task_disc[arm] + dec_disc[d] + shared_disc)
                    judged = truth if rng.random() < scenario.judge_accuracy else not truth
                    audit_pool.append((judged, truth))
                obs.append(Obs(trial_index, task, f"d{d}", label, "none", False, 0.0, result, arm, judged))
            trial_index += 1
    sample = rng.sample(audit_pool, max(1, round(scenario.audit_fraction * len(audit_pool))))
    return obs, cohen_kappa(sample)


E2_KEYS = ("P5", "P5a", "P5b", "P5c", "P5c[arbitrary]", "P5c[inferable]", "kappa_ok")


def e2_chunk(scenario, design, start, stop, n_boot, seed):
    n_tasks, per_arm = design
    params = e2_parameters(scenario)
    for sim in range(start, stop):
        tag = f"{seed}:{scenario.name}:{n_tasks}:{per_arm}:{sim}"
        obs, kappa = simulate_e2(scenario, n_tasks, per_arm, random.Random(f"e2:{tag}"), params)
        p5 = compute_p5(obs, tag, kappa, n_boot)
        a, b, c = p5["components"]
        out = {"P5": p5["verdict"], "P5a": a["verdict"], "P5b": b["verdict"], "P5c": c["verdict"]}
        out.update({f"P5c[{label}]": sub["verdict"] for label, sub in c["strata"].items()})
        out["kappa_ok"] = "supported" if math.isfinite(kappa) and kappa >= 0.6 else "falsified"
        yield out


# ---------------------------------------------------------------- E3 scenarios and simulation


@dataclass(frozen=True)
class E3Scenario:
    name: str
    description: str
    mean_rate: float = 0.6  # marginal single-task success: one unsettled decision near 0.75, the rest contracted
    task_sd: float = 0.6  # logit SD of single-task success across tasks
    multi_rate: object = None  # marginal per-task success inside a B>1 trial (None: same as mean_rate)
    trial_sd: float = 0.0  # logit SD of a trial effect shared by its tasks (positive dependence)
    expect: str = ""  # what a well-powered test should conclude, given the true gaps


E3_SCENARIOS = (
    E3Scenario(
        "independent",
        "tasks fail independently: observed equals the product in expectation",
        expect="P6 supported at B=2 and B=4 (H4 supported)",
    ),
    E3Scenario(
        "interference",
        "per-task success drops from 0.60 to 0.40 inside multi-task sessions",
        multi_rate=0.4,
        expect="P6 falsified at B=2; the B=4 gap is inside the margin, because products near 0.13 cannot fall 15 pp",
    ),
    E3Scenario(
        "shared-trial",
        "a shared per-trial logit effect (SD 2.5) makes successes positively dependent",
        trial_sd=2.5,
        expect="P6 falsified at B=4; the B=2 gap is inside the margin",
    ),
)


def e3_subsets(n_tasks, n_trials, breadth, rng):
    """Task subsets for one breadth, each task used equally often (±1 when not divisible); orders shuffled."""
    if breadth > n_tasks:
        raise ValueError("breadth exceeds the number of tasks")
    used = [0] * n_tasks
    groups = []
    for _ in range(n_trials):
        # The breadth least-used tasks, ties broken at random, keep usage counts within one of each other.
        order = sorted(range(n_tasks), key=lambda t: (used[t], rng.random()))
        group = order[:breadth]
        rng.shuffle(group)
        for t in group:
            used[t] += 1
        groups.append(tuple(group))
    return groups


def simulate_e3(scenario, n_tasks, n_single, n_multi, rng):
    """Return [(task_ids, {task: success})] for B=1, 2 and 4, as analysis.e3_trials would."""
    sd = math.sqrt(scenario.task_sd**2 + scenario.trial_sd**2)
    location = calibrate(scenario.mean_rate, sd)
    multi_shift = 0.0 if scenario.multi_rate is None else calibrate(scenario.multi_rate, sd) - location
    task_logit = [location + rng.gauss(0, scenario.task_sd) for _ in range(n_tasks)]
    designs = [(t,) for t in range(n_tasks) for _ in range(n_single)]
    for breadth in (2, 4):
        designs += e3_subsets(n_tasks, n_multi, breadth, rng)
    trials = []
    for tasks in designs:
        shared = rng.gauss(0, scenario.trial_sd) if scenario.trial_sd else 0.0
        shift = multi_shift if len(tasks) > 1 else 0.0
        names = tuple(f"t{t:02d}" for t in tasks)
        trials.append((names, {f"t{t:02d}": rng.random() < expit(task_logit[t] + shared + shift) for t in tasks}))
    return trials


E3_KEYS = ("P6-B2", "P6-B4", "H4")


def e3_chunk(scenario, design, start, stop, n_boot, seed):
    n_tasks, n_single, n_multi = design
    for sim in range(start, stop):
        tag = f"{seed}:{scenario.name}:{n_tasks}:{n_single}:{n_multi}:{sim}"
        trials = simulate_e3(scenario, n_tasks, n_single, n_multi, random.Random(f"e3:{tag}"))
        single, multi = p6_inputs(trials)
        parts = [p6_breadth(single, multi.get(b, []), tag, n_boot, f"P6-B{b}")["verdict"] for b in (2, 4)]
        h4 = "falsified" if "falsified" in parts else "supported" if all(v == "supported" for v in parts) else ""
        yield dict(zip(E3_KEYS, (*parts, h4)))


# ---------------------------------------------------------------- cells, chunks and refinement

KINDS = {
    "e1": (e1_chunk, E1_KEYS, ("tasks", "trials_per_task")),
    "e2": (e2_chunk, E2_KEYS, ("tasks", "trials_per_arm")),
    "e3": (e3_chunk, E3_KEYS, ("tasks", "single_per_task", "trials_per_breadth")),
}


def run_chunk(job):
    """Count verdicts for simulations [start, stop) of one cell. Seeds depend on the simulation index only."""
    kind, scenario, design, start, stop, n_boot, seed = job
    chunk, keys, _ = KINDS[kind]
    counts = {k: {"supported": 0, "falsified": 0} for k in keys}
    for verdicts in chunk(scenario, design, start, stop, n_boot, seed):
        for key, verdict in verdicts.items():
            if verdict in counts[key]:
                counts[key][verdict] += 1
    return counts


def cell_result(kind, scenario, design, counts, sims, n_boot):
    _, _, names = KINDS[kind]
    out = {"scenario": scenario.name, **dict(zip(names, design)), "sims": sims, "n_boot": n_boot}
    if kind == "e1":
        out["total_trials"] = design[0] * design[1]
    elif kind == "e2":
        out["total_trials"] = design[0] * design[1] * 2
    else:
        out["total_trials"] = design[0] * design[1] + 2 * design[2]
    out["counts"] = counts
    out["rates"] = {k: {v: c / sims for v, c in cs.items()} for k, cs in counts.items()}
    out["se"] = {k: {v: mc_se(c / sims, sims) for v, c in cs.items()} for k, cs in counts.items()}
    out["refined"] = False
    return out


def run_cells(cells, sims, n_boot, seed, workers):
    """cells: [(kind, scenario, design)]. Returns one result per cell, in order."""
    jobs = []
    for index, (kind, scenario, design) in enumerate(cells):
        for start in range(0, sims, CHUNK):
            jobs.append((index, (kind, scenario, design, start, min(start + CHUNK, sims), n_boot, seed)))
    if workers == 1:
        outputs = [run_chunk(job) for _, job in jobs]
    else:

        def cost(job):
            kind, _, design, start, stop, *_ = job
            return (stop - start) * math.prod(design) * (4 if kind == "e3" else 1)

        order = sorted(range(len(jobs)), key=lambda i: -cost(jobs[i][1]))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {i: pool.submit(run_chunk, jobs[i][1]) for i in order}
            outputs = [futures[i].result() for i in range(len(jobs))]
    merged = [None] * len(cells)
    for (index, _), counts in zip(jobs, outputs):
        if merged[index] is None:
            merged[index] = counts
        else:
            for key, cs in counts.items():
                for verdict, c in cs.items():
                    merged[index][key][verdict] += c
    out = []
    for (kind, scenario, design), counts in zip(cells, merged):
        out.append(cell_result(kind, scenario, design, counts, sims, n_boot))
    return out


def needs_refinement(kind, cell, targets=REFINE_TARGETS):
    """True when a target power of the cell is within 2 standard errors of 0.80."""
    keys = targets.get(kind, {}).get(cell["scenario"], ())
    return any(near_target(cell["counts"][k]["supported"], cell["sims"]) for k in keys)


# ---------------------------------------------------------------- driver and recommendation


def grid(kind, scenarios, axes):
    designs = [()]
    for axis in axes:
        designs = [(*d, a) for d in designs for a in axis]
    return [(kind, s, d) for s in scenarios for d in designs]


def run(
    sims=400,
    n_boot=1000,
    jobs=None,
    seed=0,
    only=("e1", "e2", "e3"),
    e1_scenarios=E1_SCENARIOS,
    e2_scenarios=E2_SCENARIOS,
    e3_scenarios=E3_SCENARIOS,
    e1_tasks=E1_TASKS,
    e1_trials=E1_TRIALS,
    e2_tasks=E2_TASKS,
    e2_per_arm=E2_PER_ARM,
    e3_tasks=E3_TASKS,
    e3_single=E3_SINGLE,
    e3_multi=E3_MULTI,
    refine_sims=REFINE_SIMS,
    refine_boot=REFINE_BOOT,
):
    workers = jobs or os.cpu_count() or 1
    cells = []
    if "e1" in only:
        cells += grid("e1", e1_scenarios, (e1_tasks, e1_trials))
    if "e2" in only:
        cells += grid("e2", e2_scenarios, (e2_tasks, e2_per_arm))
    if "e3" in only:
        cells += grid("e3", e3_scenarios, (e3_tasks, e3_single, e3_multi))
    results = run_cells(cells, sims, n_boot, seed, workers)
    # Refine cells near the target before any recommendation uses them (§3.2 clarifications, power targets).
    near = [i for i, (kind, _, _) in enumerate(cells) if needs_refinement(kind, results[i])]
    refine_sims = max(refine_sims, sims)
    if sims >= refine_sims and n_boot >= refine_boot:
        for i in near:  # the first pass already ran at refinement strength
            results[i]["refined"] = True
    elif near and refine_sims > 0:
        refined = run_cells([cells[i] for i in near], refine_sims, refine_boot, seed, workers)
        for i, cell in zip(near, refined):
            cell["refined"] = True
            cell["first_pass"] = {"sims": results[i]["sims"], "n_boot": n_boot, "rates": results[i]["rates"]}
            results[i] = cell
    by_kind = {kind: [r for (k, _, _), r in zip(cells, results) if k == kind] for kind in ("e1", "e2", "e3")}
    for kind in ("e2", "e3"):
        if kind == "e3":
            scenarios = [
                {**asdict(s), "true_gap": [e3_true_gap(s, b, seed=seed) for b in (2, 4)]} for s in e3_scenarios
            ]
        else:
            scenarios = [asdict(s) for s in e2_scenarios]
        by_kind[f"{kind}_scenarios"] = scenarios
    return {
        "seed": seed,
        "sims": sims,
        "n_boot": n_boot,
        "refine_sims": refine_sims,
        "refine_boot": refine_boot,
        "target_power": TARGET,
        "e1_scenarios": [asdict(s) for s in e1_scenarios],
        "e2_scenarios": by_kind["e2_scenarios"],
        "e3_scenarios": by_kind["e3_scenarios"],
        "e1": by_kind["e1"],
        "e2": by_kind["e2"],
        "e3": by_kind["e3"],
        "recommendation": recommend(by_kind["e1"], by_kind["e2"], by_kind["e3"], refine_sims, refine_boot),
    }


def smallest(cells, key, target, refine_sims, refine_boot):
    """The cell with the fewest total trials whose `key` power reaches the target, ties to more tasks. A cell
    within 2 SE of the target qualifies only after refinement at full strength."""
    ok = []
    for c in cells:
        k, n = c["counts"][key]["supported"], c["sims"]
        refined = n >= refine_sims and c["n_boot"] >= refine_boot
        if k / n >= target and (refined or not near_target(k, n, target)):
            ok.append(c)
    pick = min(ok, key=lambda c: (c["total_trials"], -c["tasks"]), default=None)
    if pick is None:
        return None
    _, _, names = KINDS["e3" if "trials_per_breadth" in pick else "e2" if "trials_per_arm" in pick else "e1"]
    return {
        **{n: pick[n] for n in names},
        "total_trials": pick["total_trials"],
        "power": pick["rates"][key]["supported"],
        "se": pick["se"][key]["supported"],
        "sims": pick["sims"],
        "n_boot": pick["n_boot"],
    }


def recommend(e1, e2, e3, refine_sims=REFINE_SIMS, refine_boot=REFINE_BOOT, target=TARGET):
    """D7: the smallest E1 total with P(P1 supported | strong) >= target; E2: the smallest with
    P(P5 supported | effective) >= target; E3: the smallest total with joint H4 power (P6 supported at both
    breadths in one replicate) >= target under independence, within the E3 budget and without it."""

    def of(cells, scenario):
        return [c for c in cells if c["scenario"] == scenario]

    def at(cells, **design):
        return {
            c["scenario"]: {"rates": c["rates"], "se": c["se"], "sims": c["sims"], "n_boot": c["n_boot"]}
            for c in cells
            if all(c[k] == v for k, v in design.items())
        } or None

    indep = of(e3, "independent")
    return {
        "target_power": target,
        "e1": smallest(of(e1, "strong"), "P1", target, refine_sims, refine_boot),
        "e1_planned_power": at(e1, tasks=12, trials_per_task=16),
        "e1_p2_p3_p4": {
            key: smallest(of(e1, "strong"), key, target, refine_sims, refine_boot) for key in ("P2", "P3", "P4")
        },
        "e2": smallest(of(e2, "effective"), "P5", target, refine_sims, refine_boot),
        "e2_planned_power": at(e2, tasks=12, trials_per_arm=4),
        "e3_within_budget": smallest(
            [c for c in indep if c["total_trials"] <= E3_BUDGET], "H4", target, refine_sims, refine_boot
        ),
        "e3_any": smallest(indep, "H4", target, refine_sims, refine_boot),
        "e3_best_within_budget": max(
            (
                {
                    "tasks": c["tasks"],
                    "single_per_task": c["single_per_task"],
                    "trials_per_breadth": c["trials_per_breadth"],
                    "total_trials": c["total_trials"],
                    "power": c["rates"]["H4"]["supported"],
                    "se": c["se"]["H4"]["supported"],
                    "sims": c["sims"],
                }
                for c in indep
                if c["total_trials"] <= E3_BUDGET
            ),
            key=lambda c: (c["power"], -c["total_trials"]),
            default=None,
        ),
    }


def e3_true_gap(scenario, breadth, n=200_000, seed=0):
    """Population mean of (observed - product of single-task rates) at a breadth, by Monte Carlo."""
    rng = random.Random(f"e3-gap:{seed}:{scenario.name}:{breadth}")
    sd = math.sqrt(scenario.task_sd**2 + scenario.trial_sd**2)
    location = calibrate(scenario.mean_rate, sd)
    shift = 0.0 if scenario.multi_rate is None else calibrate(scenario.multi_rate, sd) - location
    gauss = rng.gauss
    total = 0.0
    for _ in range(n):
        tasks = [location + gauss(0, scenario.task_sd) for _ in range(breadth)]
        shared = gauss(0, scenario.trial_sd)
        joint = single = 1.0
        for a in tasks:
            joint *= expit(a + shift + shared)
            single *= expit(a + gauss(0, scenario.trial_sd))
        total += joint - single
    return total / n


# ---------------------------------------------------------------- report


def _ps(rate, se):
    return f"{100 * rate:.0f}±{100 * se:.1f}"


def _cell(c, key):
    return f"{_ps(c['rates'][key]['supported'], c['se'][key]['supported'])} / " + _ps(
        c["rates"][key]["falsified"], c["se"][key]["falsified"]
    )


def _strength(c):
    return f"{c['sims']}{'*' if c['refined'] else ''}"


def _mix_text(mix):
    return ", ".join("/".join(map(str, m)) for m in mix)


def to_markdown(result):
    lines = [
        f"Power simulation, seed {result['seed']}: {result['sims']} simulations per cell with {result['n_boot']} "
        f"bootstrap resamples; cells within 2 SE of {100 * result['target_power']:.0f}% on their target were "
        f"re-simulated with {result['refine_sims']} simulations and {result['refine_boot']} resamples (marked * "
        "in the Sims column). Cells show % supported ± 1 Monte Carlo SE / % falsified ± 1 SE.",
    ]
    if result["e1"]:
        lines += [
            "",
            "E1 scenarios (logit SDs task 0.8, decision 0.5, trial 0.5). Decision mix per task, arbitrary/"
            f"inferable/preserve, cycled over tasks: draft bank {_mix_text(DRAFT_MIX)} unless stated:",
            "",
        ]
        lines += [
            f"- **{s['name']}**: {s['description']}. Correct outcome: "
            + ", ".join(f"{p} {v}" for p, v in s["expect"].items())
            + "."
            for s in result["e1_scenarios"]
        ]
        lines += [
            "",
            "| Scenario | Tasks | Trials/task | Total | Sims | P1 | P1(a) | P2 | P3 | P4 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in result["e1"]:
            cells = " | ".join(_cell(c, p) for p in ("P1", "P1a", "P2", "P3", "P4"))
            lines.append(
                f"| {c['scenario']} | {c['tasks']} | {c['trials_per_task']} | {c['total_trials']} | {_strength(c)} "
                f"| {cells} |"
            )
        strata_keys = [f"{p}[{s}]" for p in ("P2", "P3", "P4") for s in STRATA]
        lines += [
            "",
            "Per-stratum support (% supported ± SE) for the strong scenario; a component needs both strata:",
            "",
            "| Tasks | Trials/task | " + " | ".join(strata_keys) + " |",
            "|---|---|" + "---|" * len(strata_keys),
        ]
        for c in result["e1"]:
            if c["scenario"] == "strong":
                vals = " | ".join(_ps(c["rates"][k]["supported"], c["se"][k]["supported"]) for k in strata_keys)
                lines.append(f"| {c['tasks']} | {c['trials_per_task']} | {vals} |")
    if result["e2"]:
        lines += [
            "",
            "E2 scenarios (P5; every decision unsettled, arms balanced within task, draft-bank mix; logit SDs "
            "task 0.8, decision 0.5, trial 0.5, satisfaction task-by-arm 0.25; 20% audit, auditor assumed "
            "error-free):",
            "",
        ]
        lines += [
            f"- **{s['name']}**: {s['description']}. Correct outcome: {s['expect']}." for s in result["e2_scenarios"]
        ]
        lines += [
            "",
            "| Scenario | Tasks | Trials/arm | Total | Sims | P5 | P5(a) | P5(b) | P5(c) | P5(c) arb | P5(c) inf "
            "| kappa >= 0.6 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in result["e2"]:
            cells = " | ".join(_cell(c, p) for p in ("P5", "P5a", "P5b", "P5c"))
            strata = " | ".join(
                _ps(c["rates"][k]["supported"], c["se"][k]["supported"]) for k in ("P5c[arbitrary]", "P5c[inferable]")
            )
            kappa = _ps(c["rates"]["kappa_ok"]["supported"], c["se"]["kappa_ok"]["supported"])
            lines.append(
                f"| {c['scenario']} | {c['tasks']} | {c['trials_per_arm']} | {c['total_trials']} | {_strength(c)} "
                f"| {cells} | {strata} | {kappa} |"
            )
    if result["e3"]:
        lines += ["", "E3 scenarios (single-task success mean 0.60, logit SD 0.6; true mean observed - predicted):", ""]
        lines += [
            f"- **{s['name']}**: {s['description']} (B=2 {100 * s['true_gap'][0]:+.1f} pp, "
            f"B=4 {100 * s['true_gap'][1]:+.1f} pp). Correct outcome: {s['expect']}."
            for s in result["e3_scenarios"]
        ]
        lines += [
            "",
            "| Scenario | Tasks | B=1 per task | Trials per breadth | Total | Sims | P6 B=2 | P6 B=4 | H4 (joint) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for c in result["e3"]:
            cells = " | ".join(_cell(c, k) for k in E3_KEYS)
            lines.append(
                f"| {c['scenario']} | {c['tasks']} | {c['single_per_task']} | {c['trials_per_breadth']} | "
                f"{c['total_trials']} | {_strength(c)} | {cells} |"
            )
    lines += recommendation_lines(result["recommendation"])
    return "\n".join(lines) + "\n"


def _pick(p, axes):
    return (
        " x ".join(f"{p[a]} {label}" for a, label in axes)
        + f" ({p['total_trials']} trials): {_ps(p['power'], p['se'])}%"
    )


def recommendation_lines(rec):
    lines = ["", f"Recommendation (target power {100 * rec['target_power']:.0f}%, power ± 1 MC SE):", ""]
    e1_axes = (("tasks", "tasks"), ("trials_per_task", "trials per task"))
    if rec["e1"]:
        lines.append(f"- E1, P1 under strong: smallest qualifying cell {_pick(rec['e1'], e1_axes)}.")
    else:
        lines.append("- E1, P1 under strong: no grid cell reaches the target; add tasks.")
    for key, pick in rec["e1_p2_p3_p4"].items():
        lines.append(
            f"- E1, {key} under strong: "
            + (f"smallest qualifying cell {_pick(pick, e1_axes)}." if pick else "no grid cell reaches the target.")
        )
    planned = rec.get("e1_planned_power") or {}
    if "strong" in planned:
        r, se = planned["strong"]["rates"], planned["strong"]["se"]
        parts = ", ".join(f"{p} {_ps(r[p]['supported'], se[p]['supported'])}%" for p in ("P1", "P2", "P3", "P4"))
        lines.append(f"  - At the planned 12 x 16, supported under strong: {parts}.")
    e2_axes = (("tasks", "tasks"), ("trials_per_arm", "trials per arm"))
    if rec.get("e2"):
        lines.append(f"- E2, P5 under effective: smallest qualifying cell {_pick(rec['e2'], e2_axes)}.")
    elif rec.get("e2_planned_power") is not None or rec.get("e2") is None:
        lines.append("- E2, P5 under effective: no grid cell reaches the target.")
    planned = rec.get("e2_planned_power") or {}
    if "effective" in planned:
        r, se = planned["effective"]["rates"], planned["effective"]["se"]
        parts = ", ".join(
            f"{p} {_ps(r[p]['supported'], se[p]['supported'])}%"
            for p in ("P5", "P5a", "P5b", "P5c", "P5c[arbitrary]", "P5c[inferable]")
        )
        lines.append(f"  - At the planned 12 x 4 x 2, supported under effective: {parts}.")
    e3_axes = (("tasks", "tasks"), ("single_per_task", "B=1 per task"), ("trials_per_breadth", "per breadth"))
    for key, label in (("e3_within_budget", f"within the {E3_BUDGET}-trial E3 budget"), ("e3_any", "in the grid")):
        pick = rec.get(key)
        lines.append(
            f"- E3, joint H4 under independence, {label}: "
            + (f"smallest qualifying cell {_pick(pick, e3_axes)}." if pick else "no cell reaches the target.")
        )
    best = rec.get("e3_best_within_budget")
    if best:
        lines.append(f"  - Best joint H4 power within the budget: {_pick(best, e3_axes)}.")
    return lines


def _int_list(text):
    return tuple(int(x) for x in text.split(",") if x.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monte-Carlo power simulation for the Autonomy Lab design.")
    parser.add_argument("--sims", type=int, default=400, help="simulations per grid cell (default 400)")
    parser.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples per CI (default 1000)")
    parser.add_argument("--jobs", type=int, default=None, help="worker processes (default: CPU count)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", default=None, help="also write the full result as JSON to this path")
    parser.add_argument("--only", default="e1,e2,e3", help="experiments to simulate (default e1,e2,e3)")
    parser.add_argument(
        "--refine-sims", type=int, default=REFINE_SIMS, help=f"simulations for near-target cells ({REFINE_SIMS})"
    )
    parser.add_argument(
        "--refine-boot", type=int, default=REFINE_BOOT, help=f"resamples for near-target cells ({REFINE_BOOT})"
    )
    for name, default in (
        ("e1-tasks", E1_TASKS),
        ("e1-trials", E1_TRIALS),
        ("e2-tasks", E2_TASKS),
        ("e2-per-arm", E2_PER_ARM),
        ("e3-tasks", E3_TASKS),
        ("e3-single", E3_SINGLE),
        ("e3-multi", E3_MULTI),
    ):
        parser.add_argument(
            f"--{name}",
            type=_int_list,
            default=default,
            help=f"comma-separated grid (default {','.join(map(str, default))})",
        )
    args = parser.parse_args(argv)
    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    if not only or not set(only) <= set(KINDS):
        parser.error(f"--only takes a comma-separated subset of {','.join(KINDS)}")
    start = time.monotonic()
    result = run(
        sims=args.sims,
        n_boot=args.n_boot,
        jobs=args.jobs,
        seed=args.seed,
        only=only,
        e1_tasks=args.e1_tasks,
        e1_trials=args.e1_trials,
        e2_tasks=args.e2_tasks,
        e2_per_arm=args.e2_per_arm,
        e3_tasks=args.e3_tasks,
        e3_single=args.e3_single,
        e3_multi=args.e3_multi,
        refine_sims=args.refine_sims,
        refine_boot=args.refine_boot,
    )
    result["elapsed_seconds"] = round(time.monotonic() - start, 1)
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot write {args.json}: {exc}")
    sys.stdout.write(to_markdown(result))
    sys.stdout.write(f"\nElapsed: {result['elapsed_seconds']} s\n")


if __name__ == "__main__":
    main()
