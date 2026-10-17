"""Monte-Carlo power simulation for the Autonomy Lab design (LOOP_SPEC.md §2.2, §2.3, §3.2; T5, D7).

Usage: python3 -m autonomy_lab.power [--sims N] [--n-boot N] [--jobs N] [--seed S] [--json PATH]

Each simulated E1 replicate builds the exact nested design: tasks, decisions with labels, and the balanced
channel/filler assignment of §2.3 (filler halves, per-decision channel permutations, P4-exposure acceptance).
Outcomes come from a logistic model with per-task, per-decision and per-trial random effects. The simulated
observations then go through the same estimand and verdict functions as autonomy_lab.analysis, so the
power reported here is the power of the frozen analysis. To keep runtime bounded, the bootstrap uses
--n-boot resamples (default 1,000) rather than the 10,000 of the real analysis; percentile CIs from 1,000
resamples are slightly noisier, which is noted in the output.

E3 replicates draw per-task single-task success rates, a seeded balanced subset design per breadth, and run
the §3.2 P6 bootstrap at B=2 and B=4.

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
    Obs,
    compute_p1,
    compute_p2,
    compute_p3,
    compute_p4,
    p6_breadth,
    p6_inputs,
)

E1_TASKS = (10, 12, 16)
E1_TRIALS = (8, 16, 24)
E3_TASKS = (6, 12)
E3_SINGLE = (4, 8)
E3_MULTI = (12, 24, 48)
DESIGN_POOL = 32
MAX_DRAWS = 100_000
E3_BUDGET = 90


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


@dataclass(frozen=True)
class E1Scenario:
    name: str
    description: str
    rates: dict  # population satisfaction rate of arbitrary decisions per channel
    decisions: int = 6
    arbitrary: int = 2
    task_sd: float = 0.8  # logit SD of task effects (separate draws for settled and unsettled)
    decision_sd: float = 0.5  # logit SD of decision effects within a task
    trial_sd: float = 0.5  # logit SD of a trial effect shared by its decisions
    filler_gap: float = 0.0  # unsettled rate, filler on minus off
    spill_gap: float = 0.0  # unsettled rate, sibling-check >= 0.6 minus <= 0.4
    expect: dict = field(default_factory=dict)


def _rates(settled, unsettled, check=None):
    return {"none": unsettled, "contract": settled, "check": settled if check is None else check, "both": settled}


E1_SCENARIOS = (
    E1Scenario(
        "strong",
        "settled 0.95 on every channel, unsettled 0.30; no channel, filler or spillover effect",
        _rates(0.95, 0.30),
        expect={"P1": "supported", "P2": "supported", "P3": "supported", "P4": "supported"},
    ),
    E1Scenario(
        "strong-3arb",
        "as strong, with 3 of 6 decisions arbitrary instead of 2",
        _rates(0.95, 0.30),
        arbitrary=3,
        expect={"P1": "supported", "P2": "supported", "P3": "supported", "P4": "supported"},
    ),
    E1Scenario(
        "moderate",
        "settled 0.93, unsettled 0.45 (+48 pp, close to the P1(b)/(c) thresholds)",
        _rates(0.93, 0.45),
        expect={"P1": "supported"},
    ),
    E1Scenario(
        "null",
        "no coverage effect: every channel 0.85 (unsettled decisions are not arbitrary for this agent)",
        _rates(0.85, 0.85),
        expect={"P1": "falsified"},
    ),
    E1Scenario(
        "execution",
        "settled 0.70, unsettled 0.20: a large effect produced by poor execution",
        _rates(0.70, 0.20),
        expect={"P1": "falsified"},
    ),
    E1Scenario(
        "channel-gap",
        "contract/both 0.95, check-only 0.65, unsettled 0.30 (+30 pp contract minus check)",
        _rates(0.95, 0.30, check=0.65),
        expect={"P2": "falsified"},
    ),
    E1Scenario(
        "filler",
        "as strong, plus filler raises the unsettled rate by 25 pp (on minus off)",
        _rates(0.95, 0.30),
        filler_gap=0.25,
        expect={"P3": "falsified"},
    ),
    E1Scenario(
        "spillover",
        "as strong, plus unsettled rate 25 pp higher at sibling-check >= 0.6 than at <= 0.4",
        _rates(0.95, 0.30),
        spill_gap=0.25,
        expect={"P4": "falsified"},
    ),
)


def e1_parameters(scenario):
    """Base logits per channel and filler/spillover shifts, calibrated so the stated rates are marginal."""
    sd = math.sqrt(scenario.task_sd**2 + scenario.decision_sd**2 + scenario.trial_sd**2)
    base = {c: calibrate(p, sd) for c, p in scenario.rates.items()}
    filler_h = symmetric_shift(scenario.rates["none"], scenario.filler_gap, sd)
    spill_h = symmetric_shift(scenario.rates["none"], scenario.spill_gap, sd)
    return base, filler_h, spill_h


def simulate_e1(scenario, n_tasks, n_trials, rng, pool_seed=0, params=None):
    pool = design_pool(scenario.decisions, n_trials, pool_seed)
    base, filler_h, spill_h = params or e1_parameters(scenario)
    gauss = rng.gauss
    obs = []
    trial_index = 0
    for t in range(n_tasks):
        task = f"t{t:02d}"
        design = pool[rng.randrange(len(pool))]
        settled_t = gauss(0, scenario.task_sd)
        unsettled_t = gauss(0, scenario.task_sd)
        dec = [(gauss(0, scenario.decision_sd), gauss(0, scenario.decision_sd)) for _ in range(scenario.arbitrary)]
        for filler, channels in design:
            shared = gauss(0, scenario.trial_sd)
            for d in range(scenario.arbitrary):
                channel = channels[d]
                sib = sibling_fraction(channels, d)
                x = base[channel] + shared
                if channel == "none":
                    x += unsettled_t + dec[d][1]
                    x += filler_h / 2 if filler else -filler_h / 2
                    s = round(sib, 12)
                    x += spill_h / 2 if s >= 0.6 else -spill_h / 2 if s <= 0.4 else 0.0
                else:
                    x += settled_t + dec[d][0]
                result = rng.random() < expit(x)
                obs.append(Obs(trial_index, task, f"d{d}", "arbitrary", channel, filler, sib, result))
            trial_index += 1
    return obs


def run_e1_cell(args):
    scenario, n_tasks, n_trials, sims, n_boot, seed = args
    counts = {p: {"supported": 0, "falsified": 0} for p in ("P1", "P1a", "P2", "P3", "P4")}
    params = e1_parameters(scenario)
    for sim in range(sims):
        rng = random.Random(f"e1:{seed}:{scenario.name}:{n_tasks}:{n_trials}:{sim}")
        obs = simulate_e1(scenario, n_tasks, n_trials, rng, pool_seed=seed, params=params)
        bseed = f"{seed}:{scenario.name}:{n_tasks}:{n_trials}:{sim}"
        p1 = compute_p1(obs, bseed, n_boot)
        verdicts = {
            "P1": p1["verdict"],
            "P1a": p1["components"][0]["verdict"],
            "P2": compute_p2(obs, bseed, n_boot)["verdict"],
            "P3": compute_p3(obs, bseed, n_boot)["verdict"],
            "P4": compute_p4(obs, bseed, n_boot)["verdict"],
        }
        for p, v in verdicts.items():
            if v in counts[p]:
                counts[p][v] += 1
    return {
        "scenario": scenario.name,
        "tasks": n_tasks,
        "trials_per_task": n_trials,
        "sims": sims,
        "rates": {p: {k: v / sims for k, v in c.items()} for p, c in counts.items()},
    }


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
        expect="P6 supported at B=2 and B=4",
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


def run_e3_cell(args):
    scenario, n_tasks, n_single, n_multi, sims, n_boot, seed = args
    keys = ("P6-B2", "P6-B4", "H4")
    counts = {k: {"supported": 0, "falsified": 0} for k in keys}
    for sim in range(sims):
        tag = f"{seed}:{scenario.name}:{n_tasks}:{n_single}:{n_multi}:{sim}"
        trials = simulate_e3(scenario, n_tasks, n_single, n_multi, random.Random(f"e3:{tag}"))
        single, multi = p6_inputs(trials)
        parts = [p6_breadth(single, multi.get(b, []), tag, n_boot, f"P6-B{b}")["verdict"] for b in (2, 4)]
        h4 = "falsified" if "falsified" in parts else "supported" if all(v == "supported" for v in parts) else ""
        for key, verdict in zip(keys, (*parts, h4)):
            if verdict in counts[key]:
                counts[key][verdict] += 1
    return {
        "scenario": scenario.name,
        "tasks": n_tasks,
        "single_per_task": n_single,
        "trials_per_breadth": n_multi,
        "total_trials": n_tasks * n_single + 2 * n_multi,
        "sims": sims,
        "rates": {k: {v: c / sims for v, c in cs.items()} for k, cs in counts.items()},
    }


# ---------------------------------------------------------------- driver and report


def run(
    sims=200,
    n_boot=1000,
    jobs=None,
    seed=0,
    e1_scenarios=E1_SCENARIOS,
    e3_scenarios=E3_SCENARIOS,
    e1_tasks=E1_TASKS,
    e1_trials=E1_TRIALS,
    e3_tasks=E3_TASKS,
    e3_single=E3_SINGLE,
    e3_multi=E3_MULTI,
):
    e1_jobs = [(s, t, n, sims, n_boot, seed) for s in e1_scenarios for t in e1_tasks for n in e1_trials]
    e3_jobs = [
        (s, t, m, n, sims, n_boot, seed) for s in e3_scenarios for t in e3_tasks for m in e3_single for n in e3_multi
    ]
    workers = jobs or os.cpu_count() or 1
    if workers == 1:
        e1 = [run_e1_cell(j) for j in e1_jobs]
        e3 = [run_e3_cell(j) for j in e3_jobs]
    else:
        # One pool, most expensive cells first, so a few large cells do not leave workers idle at the end.
        tasks = [(run_e1_cell, j, j[1] * j[2]) for j in e1_jobs] + [
            (run_e3_cell, j, 3 * (j[1] * j[2] + 6 * j[3])) for j in e3_jobs
        ]
        order = sorted(range(len(tasks)), key=lambda i: -tasks[i][2])
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {i: pool.submit(tasks[i][0], tasks[i][1]) for i in order}
            results = [futures[i].result() for i in range(len(tasks))]
        e1, e3 = results[: len(e1_jobs)], results[len(e1_jobs) :]
    return {
        "seed": seed,
        "sims": sims,
        "n_boot": n_boot,
        "e1_scenarios": [asdict(s) for s in e1_scenarios],
        "e3_scenarios": [
            {**asdict(s), "true_gap": [e3_true_gap(s, b, seed=seed) for b in (2, 4)]} for s in e3_scenarios
        ],
        "e1": e1,
        "e3": e3,
        "recommendation": recommend(e1, e3),
    }


def recommend(e1, e3, target=0.8):
    """D7: the smallest E1 total (tasks x trials) with P(P1 supported | strong) >= target, ties to more tasks.
    E3: the smallest total with P(P6 supported | independent) >= target at both breadths, within the E3 budget
    and without it."""
    strong = [c for c in e1 if c["scenario"] == "strong" and c["rates"]["P1"]["supported"] >= target]
    e1_pick = min(strong, key=lambda c: (c["tasks"] * c["trials_per_task"], -c["tasks"]), default=None)
    indep = [
        c
        for c in e3
        if c["scenario"] == "independent"
        and min(c["rates"]["P6-B2"]["supported"], c["rates"]["P6-B4"]["supported"]) >= target
    ]

    def smallest(cells):
        pick = min(cells, key=lambda c: (c["total_trials"], -c["tasks"]), default=None)
        return (
            None
            if pick is None
            else {k: pick[k] for k in ("tasks", "single_per_task", "trials_per_breadth", "total_trials")}
        )

    def powers(n_tasks, n_trials):
        return {
            c["scenario"]: c["rates"] for c in e1 if c["tasks"] == n_tasks and c["trials_per_task"] == n_trials
        } or None

    return {
        "target_power": target,
        "e1": None if e1_pick is None else {"tasks": e1_pick["tasks"], "trials_per_task": e1_pick["trials_per_task"]},
        "e1_pick_power": None if e1_pick is None else powers(e1_pick["tasks"], e1_pick["trials_per_task"]),
        "e1_spec_start_power": powers(12, 16),
        "e3_within_budget": smallest([c for c in indep if c["total_trials"] <= E3_BUDGET]),
        "e3_any": smallest(indep),
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


def _pct(x):
    return f"{100 * x:.0f}"


def to_markdown(result):
    lines = [
        f"Power simulation: {result['sims']} simulations per cell, {result['n_boot']} bootstrap resamples per CI "
        f"(the real analysis uses 10,000), seed {result['seed']}. Cells show % supported / % falsified.",
        "",
        "E1 scenarios (arbitrary-decision rates; logit SDs task 0.8, decision 0.5, trial 0.5; 6 decisions per task):",
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
        "| Scenario | Tasks | Trials/task | Total | P1 | P1(a) | P2 | P3 | P4 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in result["e1"]:
        r = c["rates"]
        cells = " | ".join(
            f"{_pct(r[p]['supported'])} / {_pct(r[p]['falsified'])}" for p in ("P1", "P1a", "P2", "P3", "P4")
        )
        lines.append(
            f"| {c['scenario']} | {c['tasks']} | {c['trials_per_task']} | {c['tasks'] * c['trials_per_task']} | {cells} |"
        )
    lines += ["", "E3 scenarios (single-task success mean 0.60, logit SD 0.6; true mean observed - predicted):", ""]
    lines += [
        f"- **{s['name']}**: {s['description']} (B=2 {100 * s['true_gap'][0]:+.1f} pp, "
        f"B=4 {100 * s['true_gap'][1]:+.1f} pp). Correct outcome: {s['expect']}."
        for s in result["e3_scenarios"]
    ]
    lines += [
        "",
        "| Scenario | Tasks | B=1 per task | Trials per breadth | Total | P6 B=2 | P6 B=4 | H4 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in result["e3"]:
        r = c["rates"]
        cells = " | ".join(f"{_pct(r[k]['supported'])} / {_pct(r[k]['falsified'])}" for k in ("P6-B2", "P6-B4", "H4"))
        lines.append(
            f"| {c['scenario']} | {c['tasks']} | {c['single_per_task']} | {c['trials_per_breadth']} | "
            f"{c['total_trials']} | {cells} |"
        )
    rec = result["recommendation"]
    lines += ["", f"Recommendation (target power {rec['target_power']}):", ""]
    if rec["e1"]:
        lines.append(
            f"- E1: {rec['e1']['tasks']} tasks x {rec['e1']['trials_per_task']} trials per task is the "
            "smallest total with P1 supported at the target in the strong scenario."
        )
    else:
        lines.append("- E1: no grid cell reaches the target for P1 in the strong scenario; add tasks.")
    for key, label in (("e1_pick_power", "that cell"), ("e1_spec_start_power", "the spec's 12 x 16 start")):
        if rec.get(key):
            strong = rec[key].get("strong", {})
            moderate = rec[key].get("moderate", {})
            parts = ", ".join(f"{p} {_pct(strong[p]['supported'])}%" for p in ("P1", "P2", "P3", "P4") if p in strong)
            extra = f"; P1 under moderate {_pct(moderate['P1']['supported'])}%" if moderate else ""
            lines.append(f"  - Supported under strong at {label}: {parts}{extra}.")
    for key, label in (("e3_within_budget", f"within the {E3_BUDGET}-trial E3 budget"), ("e3_any", "in the grid")):
        e3 = rec.get(key)
        if e3:
            lines.append(
                f"- E3 {label}: {e3['tasks']} tasks, {e3['single_per_task']} B=1 trials per task, "
                f"{e3['trials_per_breadth']} trials at each of B=2 and B=4 ({e3['total_trials']} trials)."
            )
        else:
            lines.append(f"- E3 {label}: no cell reaches the target at both breadths under independence.")
    return "\n".join(lines) + "\n"


def _int_list(text):
    return tuple(int(x) for x in text.split(",") if x.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monte-Carlo power simulation for the Autonomy Lab design.")
    parser.add_argument("--sims", type=int, default=200, help="simulations per grid cell (default 200)")
    parser.add_argument("--n-boot", type=int, default=1000, help="bootstrap resamples per CI (default 1000)")
    parser.add_argument("--jobs", type=int, default=None, help="worker processes (default: CPU count)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", default=None, help="also write the full result as JSON to this path")
    for name, default in (
        ("e1-tasks", E1_TASKS),
        ("e1-trials", E1_TRIALS),
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
    start = time.monotonic()
    result = run(
        sims=args.sims,
        n_boot=args.n_boot,
        jobs=args.jobs,
        seed=args.seed,
        e1_tasks=args.e1_tasks,
        e1_trials=args.e1_trials,
        e3_tasks=args.e3_tasks,
        e3_single=args.e3_single,
        e3_multi=args.e3_multi,
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
