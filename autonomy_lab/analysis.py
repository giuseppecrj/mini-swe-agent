"""Pre-registered analysis of one Autonomy Lab run (LOOP_SPEC.md §2.1, §2.2, §3.2, §4.3).

Usage: python3 -m autonomy_lab.analysis <run_dir> [--seed N] [--kappa K] [--tasks-dir DIR]

Reads <run_dir>/trials.jsonl, writes <run_dir>/analysis.json and <run_dir>/analysis.md, and prints the
Markdown. Standard library only. Every bootstrap is deterministic given --seed.

Interpretation choices (the spec is authoritative; these resolve what it leaves open, conservatively):

- Counted trials: records are grouped by (experiment, design_id, seed) and only the highest `attempt` is
  used. A final attempt that is still an infrastructure failure is excluded and reported as unresolved.
  The infrastructure-failure rate for the §2.4 gate is failed attempts / distinct trials.
- A decision missing from `decision_results` in a counted trial is scored as not satisfied (agent
  failures count against the condition) and counted under `missing_results`.
- "Settled" means channel `contract`, `check` or `both`; "unsettled" means `none`.
- Primary estimands for P1–P5 use `arbitrary` decisions only. Including inferable or preserve decisions
  would dilute differences towards zero and make the equivalence predictions easier to support.
  All-label versions are reported as descriptive extras.
- P1(a): the per-task effect is the task's pooled settled rate minus its pooled unsettled rate over
  arbitrary decision observations. The sign test is exact and two-sided; ties (effect exactly 0) are
  dropped. Supported needs median >= +0.30, p < 0.05 and more positive than negative tasks; falsified
  needs p < 0.05 and more negative tasks. P1(b) and P1(c) are rates pooled over observations.
- P2 and P3 are the mean across tasks of per-task differences (contract-only minus check-only; filler on
  minus off among unsettled decisions). P4 is computed per decision (rate at sibling-check fraction
  >= 0.6 minus rate at <= 0.4 among its `none` cells), averaged per task, then averaged across tasks.
  The sibling-check fraction uses every other decision in the trial.
- P5 uses E2 records. Disclosure is read from an optional per-decision map in each record,
  `disclosed: {task_id: {decision_id: true|false}}`, written by the T6 judge; decisions without a
  judgement are excluded from disclosure rates and counted. P5(a)/(b) are means across tasks of per-task
  disclosure rates; P5(c) is the mean across tasks of per-task satisfaction differences (assume minus
  plain). The audit's Cohen's kappa comes from --kappa or <run_dir>/disclosure_audit.json
  (`{"kappa": float}`). If kappa is missing or < 0.6, P5 is reported as inconclusive whatever its
  components say.
- P6 uses E3 records. A task succeeds in a trial when all its decisions are satisfied; a trial succeeds
  when all its tasks do. B>1 trials containing a task with no B=1 data are excluded and counted.
- Every CI is a percentile bootstrap. Task-clustered components resample tasks with replacement; P6
  follows the three steps in §3.2. A component needs at least 2 contributing tasks, otherwise it is
  inconclusive. Region boundaries follow the spec's strict/inclusive wording; values are rounded to
  12 decimals before comparison to avoid float artefacts.
"""

import argparse
import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path

ANALYSIS_VERSION = 1
N_BOOT = 10_000
SETTLED = ("contract", "check", "both")
CHANNELS = ("none", "contract", "check", "both")
KAPPA_MIN = 0.6
MIN_TASKS = 2
BANK_MIN_TASKS = 10
INFRA_GATE = 0.05


# ---------------------------------------------------------------- loading and counted trials


def load_records(path):
    records = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not valid JSON ({exc})") from exc
    return records


def read_json(path):
    """Parse a JSON file, naming the file in any error."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from exc


def trial_key(record):
    return (record.get("experiment"), record.get("design_id"), record.get("seed"))


def select_counted(records):
    """Return (counted records, infrastructure summary). Uses the final attempt per trial seed."""
    groups = {}
    for record in records:
        groups.setdefault(trial_key(record), []).append(record)
    counted = []
    duplicate_attempts = 0
    unresolved = 0
    reruns = 0
    failed_attempts = 0
    by_type = {}
    for key in groups:
        attempts = groups[key]
        numbers = [int(r.get("attempt", 1)) for r in attempts]
        duplicate_attempts += len(numbers) - len(set(numbers))
        reruns += sum(1 for n in set(numbers) if n > 1)
        for r in attempts:
            kind = r.get("infrastructure_failure")
            if kind:
                failed_attempts += 1
                by_type[kind] = by_type.get(kind, 0) + 1
        # On duplicate attempt numbers the later line wins (max keeps the first, so reverse).
        final = max(reversed(attempts), key=lambda r: int(r.get("attempt", 1)))
        if final.get("infrastructure_failure"):
            unresolved += 1
        else:
            counted.append(final)
    distinct = len(groups)
    rate = failed_attempts / distinct if distinct else 0.0
    runtime_mismatch = sum(1 for r in counted if r.get("runtime_matches_request") is False)
    runtime_unknown = sum(1 for r in counted if "runtime_matches_request" not in r)
    signals = sum(len(r.get("confinement_signals") or []) for r in records)
    agent = {}
    for r in counted:
        kind = r.get("agent_failure")
        if kind:
            agent[kind] = agent.get(kind, 0) + 1
    summary = {
        "records": len(records),
        "distinct_trials": distinct,
        "counted_trials": len(counted),
        "infrastructure_failed_attempts": failed_attempts,
        "infrastructure_failures_by_type": dict(sorted(by_type.items())),
        "reruns": reruns,
        "unresolved_after_rerun": unresolved,
        "duplicate_attempt_numbers": duplicate_attempts,
        "infrastructure_rate": rate,
        "agent_failures_by_type": dict(sorted(agent.items())),
        "runtime_mismatches": runtime_mismatch,
        "runtime_match_unknown": runtime_unknown,
        "confinement_signals": signals,
        "gates": {
            "infrastructure_rate_at_most_5pct": rate <= INFRA_GATE,
            "runtime_matches_request": runtime_mismatch == 0,
            "confinement_signals_need_review": signals > 0,
        },
    }
    return counted, summary


# ---------------------------------------------------------------- observations


@dataclass(frozen=True, slots=True)
class Obs:
    """One held-out decision outcome in one counted trial."""

    trial: int
    task: str
    decision: str
    label: str
    channel: str
    filler: bool
    sib: float  # share of the other decisions in the trial assigned check or both
    result: bool
    arm: object = None
    disclosed: object = None  # None when no judgement exists


def observations(records, experiment):
    """Flatten counted records of one experiment into Obs, plus a count of missing results."""
    out = []
    missing = 0
    for index, record in enumerate(records):
        if record.get("experiment") != experiment:
            continue
        labels = record.get("labels") or {}
        channels = record.get("channels") or {}
        results = record.get("decision_results") or {}
        disclosed = record.get("disclosed") or {}
        cells = []
        for task in record["task_ids"]:
            if task not in labels:
                raise ValueError(f"trial {trial_key(record)}: no labels for task {task!r}")
            for decision, label in labels[task].items():
                channel = (channels.get(task) or {}).get(decision)
                if channel not in CHANNELS:
                    raise ValueError(f"trial {trial_key(record)}: bad channel {channel!r} for {task}/{decision}")
                cells.append((task, decision, label, channel))
        checks = sum(1 for cell in cells if cell[3] in ("check", "both"))
        others = len(cells) - 1
        for task, decision, label, channel in cells:
            value = (results.get(task) or {}).get(decision)
            if value is None:
                missing += 1
            own = 1 if channel in ("check", "both") else 0
            sib = (checks - own) / others if others else 0.0
            out.append(
                Obs(
                    trial=index,
                    task=task,
                    decision=decision,
                    label=label,
                    channel=channel,
                    filler=bool(record.get("filler")),
                    sib=sib,
                    result=value is True,
                    arm=record.get("arm"),
                    disclosed=(disclosed.get(task) or {}).get(decision),
                )
            )
    return out, missing


# ---------------------------------------------------------------- statistics


def sign_test(effects):
    """Exact two-sided sign test. Zeros are dropped. Returns (positives, negatives, p)."""
    pos = sum(1 for e in effects if e > 0)
    neg = sum(1 for e in effects if e < 0)
    n = pos + neg
    if n == 0:
        return pos, neg, 1.0
    tail = sum(math.comb(n, i) for i in range(min(pos, neg) + 1)) / 2**n
    return pos, neg, min(1.0, 2 * tail)


def quantile(sorted_values, q):
    """Linear-interpolation quantile (Hyndman–Fan type 7) of an already sorted list."""
    h = (len(sorted_values) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


def percentile_ci(replicates, level=0.95):
    values = sorted(replicates)
    alpha = (1 - level) / 2
    return [quantile(values, alpha), quantile(values, 1 - alpha)]


def make_rng(seed, name):
    # String seeds are hashed with SHA-512 by random.seed, so this is stable across processes.
    return random.Random(f"autonomy-lab:{seed}:{name}")


def boot_mean(values, n_boot, rng):
    n = len(values)
    choices = rng.choices
    return [sum(choices(values, k=n)) / n for _ in range(n_boot)]


def boot_ratio(pairs, n_boot, rng):
    """Resample (successes, trials) clusters and return pooled-rate replicates."""
    n = len(pairs)
    choices = rng.choices
    out = []
    for _ in range(n_boot):
        sample = choices(pairs, k=n)
        out.append(sum(p[0] for p in sample) / sum(p[1] for p in sample))
    return out


def boot_median(values, n_boot, rng):
    n = len(values)
    return [statistics.median(rng.choices(values, k=n)) for _ in range(n_boot)]


def rate(values):
    """Mean of a non-empty sequence of booleans or 0/1 values."""
    return sum(values) / len(values)


# ---------------------------------------------------------------- verdict rules


def _r(x: float) -> float:
    return round(x, 12)


def in_region(region, x):
    kind, a = region
    x = _r(x)
    if kind == "at_least":
        return x >= a
    if kind == "at_most":
        return x <= a
    if kind == "abs_below":
        return abs(x) < a
    raise ValueError(kind)


def ci_intersects(region, ci):
    kind, a, inclusive = region
    lo, hi = _r(ci[0]), _r(ci[1])
    if kind == "below":
        return lo <= a if inclusive else lo < a
    if kind == "above":
        return hi >= a if inclusive else hi > a
    if kind == "abs_above":
        return (lo < -a or hi > a) if not inclusive else (lo <= -a or hi >= a)
    raise ValueError(kind)


def ci_within(region, ci):
    kind, a, inclusive = region
    lo, hi = _r(ci[0]), _r(ci[1])
    if kind == "below":
        return hi <= a if inclusive else hi < a
    if kind == "above":
        return lo >= a if inclusive else lo > a
    if kind == "abs_above":
        return (lo > a or hi < -a) if not inclusive else (lo >= a or hi <= -a)
    raise ValueError(kind)


def interval_component(name, estimate, ci, n_tasks, support, falsify, rule, **extra):
    """A §3.2 component: supported when the point is in the prediction region and the CI misses the
    falsification region; falsified when the CI lies entirely within it."""
    if estimate is None or ci is None or n_tasks < MIN_TASKS:
        verdict = "inconclusive"
        note = f"insufficient data ({n_tasks} task(s) contribute; need {MIN_TASKS})"
    elif ci_within(falsify, ci):
        verdict, note = "falsified", ""
    elif in_region(support, estimate) and not ci_intersects(falsify, ci):
        verdict, note = "supported", ""
    else:
        verdict, note = "inconclusive", ""
    out = {"name": name, "estimate": estimate, "ci95": ci, "n_tasks": n_tasks, "rule": rule, "verdict": verdict}
    if note:
        out["note"] = note
    out.update(extra)
    return out


def combine(components):
    verdicts = [c["verdict"] for c in components]
    if "falsified" in verdicts:
        return "falsified"
    if verdicts and all(v == "supported" for v in verdicts):
        return "supported"
    return "inconclusive"


# ---------------------------------------------------------------- per-task summaries


def by_task(obs):
    tasks = {}
    for o in obs:
        tasks.setdefault(o.task, []).append(o)
    return dict(sorted(tasks.items()))


def task_counts(obs, keep):
    """{task: (successes, n)} over observations passing keep; tasks with n == 0 omitted."""
    out = {}
    for task, rows in by_task(obs).items():
        hits = [o.result for o in rows if keep(o)]
        if hits:
            out[task] = (sum(hits), len(hits))
    return out


def task_differences(obs, keep_a, keep_b):
    """{task: rate(a) - rate(b)} for tasks with observations on both sides."""
    out = {}
    for task, rows in by_task(obs).items():
        a = [o.result for o in rows if keep_a(o)]
        b = [o.result for o in rows if keep_b(o)]
        if a and b:
            out[task] = rate(a) - rate(b)
    return out


def p4_task_values(obs):
    """Per-decision high-minus-low sibling-check difference among `none` cells, averaged per task."""
    out = {}
    for task, rows in by_task(obs).items():
        per_decision = {}
        for o in rows:
            if o.channel != "none":
                continue
            side = "high" if _r(o.sib) >= 0.6 else "low" if _r(o.sib) <= 0.4 else None
            if side:
                per_decision.setdefault(o.decision, {"high": [], "low": []})[side].append(o.result)
        diffs = [rate(d["high"]) - rate(d["low"]) for d in per_decision.values() if d["high"] and d["low"]]
        if diffs:
            out[task] = sum(diffs) / len(diffs)
    return out


def mean_component(name, values, n_boot, seed, support, falsify, rule):
    if len(values) < 1:
        return interval_component(name, None, None, 0, support, falsify, rule)
    vals = list(values.values())
    estimate = sum(vals) / len(vals)
    ci = percentile_ci(boot_mean(vals, n_boot, make_rng(seed, name))) if len(vals) >= MIN_TASKS else None
    return interval_component(name, estimate, ci, len(vals), support, falsify, rule, per_task=values)


def pooled_component(name, counts, n_boot, seed, support, falsify, rule):
    if not counts:
        return interval_component(name, None, None, 0, support, falsify, rule)
    pairs = list(counts.values())
    estimate = sum(p[0] for p in pairs) / sum(p[1] for p in pairs)
    ci = percentile_ci(boot_ratio(pairs, n_boot, make_rng(seed, name))) if len(pairs) >= MIN_TASKS else None
    return interval_component(
        name,
        estimate,
        ci,
        len(pairs),
        support,
        falsify,
        rule,
        successes=sum(p[0] for p in pairs),
        n=sum(p[1] for p in pairs),
    )


# ---------------------------------------------------------------- predictions


def arbitrary(o):
    return o.label == "arbitrary"


def compute_p1(obs, seed, n_boot=N_BOOT):
    arb = [o for o in obs if arbitrary(o)]
    effects = task_differences(arb, lambda o: o.channel in SETTLED, lambda o: o.channel == "none")
    pos, neg, p = sign_test(list(effects.values()))
    median = statistics.median(effects.values()) if effects else None
    rule_a = (
        "supported: median >= +0.30 and two-sided exact sign test p < 0.05, positive; falsified: p < 0.05, negative"
    )
    if len(effects) < MIN_TASKS:
        verdict_a = "inconclusive"
    elif p < 0.05 and neg > pos:
        verdict_a = "falsified"
    elif p < 0.05 and pos > neg and median is not None and _r(median) >= 0.30:
        verdict_a = "supported"
    else:
        verdict_a = "inconclusive"
    median_ci = None
    if len(effects) >= MIN_TASKS:
        median_ci = percentile_ci(boot_median(list(effects.values()), n_boot, make_rng(seed, "P1a-median")))
    a = {
        "name": "P1a",
        "estimate": median,
        "ci95": median_ci,
        "n_tasks": len(effects),
        "positive_tasks": pos,
        "negative_tasks": neg,
        "sign_test_p": p,
        "rule": rule_a,
        "verdict": verdict_a,
        "per_task": effects,
    }
    b = pooled_component(
        "P1b",
        task_counts(arb, lambda o: o.channel in SETTLED),
        n_boot,
        seed,
        ("at_least", 0.90),
        ("below", 0.80, False),
        "supported: pooled settled >= 0.90, CI clear of < 0.80; falsified: CI < 0.80",
    )
    c = pooled_component(
        "P1c",
        task_counts(arb, lambda o: o.channel == "none"),
        n_boot,
        seed,
        ("at_most", 0.50),
        ("above", 0.70, True),
        "supported: pooled unsettled <= 0.50, CI clear of >= 0.70; falsified: CI >= 0.70",
    )
    return {"id": "P1", "components": [a, b, c], "verdict": combine([a, b, c])}


def compute_p2(obs, seed, n_boot=N_BOOT):
    arb = [o for o in obs if arbitrary(o)]
    values = task_differences(arb, lambda o: o.channel == "contract", lambda o: o.channel == "check")
    comp = mean_component(
        "P2",
        values,
        n_boot,
        seed,
        ("abs_below", 0.10),
        ("abs_above", 0.20, False),
        "contract-only minus check-only; supported: |d| < 0.10, CI within ±0.20; falsified: CI beyond ±0.20",
    )
    return {"id": "P2", "components": [comp], "verdict": combine([comp])}


def compute_p3(obs, seed, n_boot=N_BOOT):
    arb = [o for o in obs if arbitrary(o) and o.channel == "none"]
    values = task_differences(arb, lambda o: o.filler, lambda o: not o.filler)
    comp = mean_component(
        "P3",
        values,
        n_boot,
        seed,
        ("abs_below", 0.10),
        ("abs_above", 0.15, False),
        "unsettled, filler on minus off; supported: |d| < 0.10, CI within ±0.15; falsified: CI beyond ±0.15",
    )
    return {"id": "P3", "components": [comp], "verdict": combine([comp])}


def compute_p4(obs, seed, n_boot=N_BOOT):
    values = p4_task_values([o for o in obs if arbitrary(o)])
    comp = mean_component(
        "P4",
        values,
        n_boot,
        seed,
        ("abs_below", 0.10),
        ("abs_above", 0.15, False),
        "`none` cells, sibling-check >= 0.6 minus <= 0.4 per decision, averaged per task; supported: |d| < 0.10, "
        "CI within ±0.15; falsified: CI beyond ±0.15",
    )
    return {"id": "P4", "components": [comp], "verdict": combine([comp])}


def task_disclosure(obs, arm):
    out = {}
    for task, rows in by_task(obs).items():
        judged = [o.disclosed is True for o in rows if o.arm == arm and o.disclosed is not None]
        if judged:
            out[task] = rate(judged)
    return out


def compute_p5(obs, seed, kappa, n_boot=N_BOOT):
    arb = [o for o in obs if arbitrary(o) and o.channel == "none"]
    unjudged = sum(1 for o in arb if o.disclosed is None)
    a = mean_component(
        "P5a",
        task_disclosure(arb, "plain"),
        n_boot,
        seed,
        ("at_most", 0.20),
        ("above", 0.40, False),
        "`plain` disclosure; supported: <= 0.20, CI clear of > 0.40; falsified: CI > 0.40",
    )
    b = mean_component(
        "P5b",
        task_disclosure(arb, "assume"),
        n_boot,
        seed,
        ("at_least", 0.60),
        ("below", 0.30, False),
        "`assume` disclosure; supported: >= 0.60, CI clear of < 0.30; falsified: CI < 0.30",
    )
    c = mean_component(
        "P5c",
        task_differences(arb, lambda o: o.arm == "assume", lambda o: o.arm == "plain"),
        n_boot,
        seed,
        ("abs_below", 0.10),
        ("abs_above", 0.15, False),
        "satisfaction assume minus plain; supported: |d| < 0.10, CI within ±0.15; falsified: CI beyond ±0.15",
    )
    components = [a, b, c]
    verdict = combine(components)
    note = ""
    if kappa is None:
        verdict, note = "inconclusive", "no disclosure-audit kappa provided; P5 cannot resolve (§3.1)"
    elif kappa < KAPPA_MIN:
        verdict, note = "inconclusive", f"kappa {kappa:.3f} < {KAPPA_MIN}; P5 reported inconclusive (§3.1)"
    out = {"id": "P5", "components": components, "verdict": verdict, "kappa": kappa, "unjudged_decisions": unjudged}
    if note:
        out["note"] = note
    return out


def e3_trials(records):
    """[(task_ids tuple, {task: success})] for counted E3 records."""
    out = []
    for record in records:
        if record.get("experiment") != "E3":
            continue
        results = record.get("decision_results") or {}
        labels = record.get("labels") or {}
        success = {}
        for task in record["task_ids"]:
            decisions = labels.get(task) or results.get(task) or {}
            success[task] = bool(decisions) and all((results.get(task) or {}).get(d) is True for d in decisions)
        out.append((tuple(record["task_ids"]), success))
    return out


def p6_breadth(single, multi, seed, n_boot, name):
    """§3.2 P6 for one breadth. single: {task: [0/1, ...]} from B=1; multi: [(tasks, observed 0/1)]."""
    rule = (
        "mean(observed - predicted); supported: 90% CI within ±0.15 (TOST); falsified: 95% CI excludes 0 and "
        "|estimate| > 0.15"
    )
    usable = [(tasks, y) for tasks, y in multi if all(t in single for t in tasks)]
    excluded = len(multi) - len(usable)
    base = {"name": name, "n_trials": len(usable), "excluded_trials": excluded, "rule": rule}
    if not usable or len(single) < MIN_TASKS:
        return {
            **base,
            "estimate": None,
            "ci90": None,
            "ci95": None,
            "verdict": "inconclusive",
            "note": "insufficient data",
        }
    rates = {t: sum(v) / len(v) for t, v in single.items()}

    def diff(rate_map, trials):
        total = 0.0
        for tasks, y in trials:
            pred = 1.0
            for t in tasks:
                pred *= rate_map[t]
            total += y - pred
        return total / len(trials)

    estimate = diff(rates, usable)
    observed = sum(y for _, y in usable) / len(usable)
    rng = make_rng(seed, name)
    tasks_sorted = sorted(single)
    replicates = []
    for _ in range(n_boot):
        resampled = {}
        for t in tasks_sorted:
            values = single[t]
            resampled[t] = sum(rng.choices(values, k=len(values))) / len(values)
        replicates.append(diff(resampled, rng.choices(usable, k=len(usable))))
    ci90 = percentile_ci(replicates, 0.90)
    ci95 = percentile_ci(replicates, 0.95)
    lo90, hi90, lo95, hi95 = (_r(x) for x in (*ci90, *ci95))
    if lo90 > -0.15 and hi90 < 0.15:
        verdict = "supported"
    elif (lo95 > 0 or hi95 < 0) and abs(_r(estimate)) > 0.15:
        verdict = "falsified"
    else:
        verdict = "inconclusive"
    return {
        **base,
        "estimate": estimate,
        "observed_rate": observed,
        "predicted_rate": observed - estimate,
        "ci90": ci90,
        "ci95": ci95,
        "verdict": verdict,
    }


def p6_inputs(trials):
    single = {}
    multi = {}
    for tasks, success in trials:
        if len(tasks) == 1:
            single.setdefault(tasks[0], []).append(int(success[tasks[0]]))
        else:
            multi.setdefault(len(tasks), []).append((tasks, int(all(success.values()))))
    return single, multi


def compute_p6(trials, seed, n_boot=N_BOOT):
    single, multi = p6_inputs(trials)
    parts = [p6_breadth(single, multi.get(b, []), seed, n_boot, f"P6-B{b}") for b in (2, 4)]
    verdicts = [p["verdict"] for p in parts]
    if "falsified" in verdicts:
        h4 = "falsified"
    elif all(v == "supported" for v in verdicts):
        h4 = "supported"
    else:
        h4 = "inconclusive"
    return {
        "id": "P6",
        "components": parts,
        "verdict": h4,
        "single_task_rates": {t: sum(v) / len(v) for t, v in sorted(single.items())},
        "single_task_trials": {t: len(v) for t, v in sorted(single.items())},
    }


# ---------------------------------------------------------------- descriptive extras


def descriptive_e1(obs):
    table = {}
    for o in obs:
        cell = table.setdefault(o.label, {}).setdefault(o.channel, [0, 0])
        cell[0] += int(o.result)
        cell[1] += 1
    rates_by = {
        label: {ch: {"rate": v[0] / v[1], "n": v[1]} for ch, v in sorted(chs.items())}
        for label, chs in sorted(table.items())
    }
    all_labels = {
        "P2_all_labels": task_differences(obs, lambda o: o.channel == "contract", lambda o: o.channel == "check"),
        "P3_all_labels": task_differences(
            [o for o in obs if o.channel == "none"], lambda o: o.filler, lambda o: not o.filler
        ),
        "P4_all_labels": p4_task_values(obs),
    }
    summary = {k: (sum(v.values()) / len(v) if v else None) for k, v in all_labels.items()}
    judged = [o.disclosed for o in obs if arbitrary(o) and o.channel == "none" and o.disclosed is not None]
    return {
        "rates_by_label_and_channel": rates_by,
        "all_label_means": summary,
        "e1_none_arbitrary_disclosure": {
            "rate": rate([d is True for d in judged]) if judged else None,
            "n": len(judged),
        },
    }


def author_split(p1, tasks_dir):
    if not tasks_dir:
        return None
    groups = {}
    for task, effect in p1["components"][0]["per_task"].items():
        meta = Path(tasks_dir) / task / "task.json"
        author = read_json(meta).get("author", "unknown") if meta.exists() else "unknown"
        groups.setdefault(author, {})[task] = effect
    return {
        a: {"per_task": v, "median": statistics.median(v.values()), "n_tasks": len(v)}
        for a, v in sorted(groups.items())
    }


# ---------------------------------------------------------------- driver and output


def analyze(records, seed=0, kappa=None, n_boot=N_BOOT, tasks_dir=None):
    counted, infra = select_counted(records)
    e1, missing_e1 = observations(counted, "E1")
    e2, missing_e2 = observations(counted, "E2")
    e3, missing_e3 = (
        e3_trials(counted),
        sum(
            1
            for r in counted
            if r.get("experiment") == "E3"
            for t in r["task_ids"]
            for d in (r.get("labels") or {}).get(t, {})
            if (r.get("decision_results") or {}).get(t, {}).get(d) is None
        ),
    )
    infra["missing_results"] = {"E1": missing_e1, "E2": missing_e2, "E3": missing_e3}
    e1_tasks = len({o.task for o in e1})
    warnings = []
    if e1 and e1_tasks < BANK_MIN_TASKS:
        warnings.append(f"E1 has {e1_tasks} tasks; the spec's minimum bank is {BANK_MIN_TASKS}")
    if not infra["gates"]["infrastructure_rate_at_most_5pct"]:
        warnings.append("infrastructure-failure rate exceeds 5%: the run is invalid (§2.4)")
    if not infra["gates"]["runtime_matches_request"]:
        warnings.append("a counted trial's runtime does not match the request: the run is invalid (§2.4)")
    if infra["gates"]["confinement_signals_need_review"]:
        warnings.append("confinement signals recorded: review by hand before accepting the run (§2.4)")
    if infra["duplicate_attempt_numbers"]:
        warnings.append("duplicate attempt numbers for one seed; the last line was used")
    predictions = [
        compute_p1(e1, seed, n_boot),
        compute_p2(e1, seed, n_boot),
        compute_p3(e1, seed, n_boot),
        compute_p4(e1, seed, n_boot),
        compute_p5(e2, seed, kappa, n_boot),
        compute_p6(e3, seed, n_boot),
    ]
    result = {
        "analysis_version": ANALYSIS_VERSION,
        "seed": seed,
        "n_boot": n_boot,
        "experiments": sorted({r.get("experiment") for r in counted if r.get("experiment")}),
        "infrastructure": infra,
        "warnings": warnings,
        "predictions": predictions,
        "descriptive": {"E1": descriptive_e1(e1)},
    }
    split = author_split(predictions[0], tasks_dir)
    if split is not None:
        result["descriptive"]["P1_by_author"] = split
    return result


def _fmt(x, pct=False):
    if x is None:
        return "–"
    return f"{100 * x:+.1f} pp" if pct else f"{x:.3f}"


def _ci(ci, pct=False):
    if not ci:
        return "–"
    return f"[{_fmt(ci[0], pct)}, {_fmt(ci[1], pct)}]"


PCT_COMPONENTS = {"P1a", "P2", "P3", "P4", "P5c", "P6-B2", "P6-B4"}


def to_markdown(result):
    lines = [
        f"Analysis v{result['analysis_version']}, seed {result['seed']}, {result['n_boot']} bootstrap resamples.",
        "",
    ]
    lines += ["| Prediction | Verdict |", "|---|---|"]
    for p in result["predictions"]:
        note = f" ({p['note']})" if p.get("note") else ""
        lines.append(f"| {p['id']} | {p['verdict']}{note} |")
    lines += ["", "| Component | Estimate | 95% CI | Tasks | Detail | Verdict |", "|---|---|---|---|---|---|"]
    for p in result["predictions"]:
        for c in p["components"]:
            pct = c["name"] in PCT_COMPONENTS
            if c["name"] == "P1a":
                detail = f"{c['positive_tasks']}+ / {c['negative_tasks']}−, sign p = {c['sign_test_p']:.4f}"
            elif c["name"].startswith("P6"):
                detail = f"90% CI {_ci(c['ci90'], True)}, {c['n_trials']} trials"
            elif "n" in c:
                detail = f"{c['successes']}/{c['n']}"
            else:
                detail = ""
            tasks = c.get("n_tasks", "–")
            lines.append(
                f"| {c['name']} | {_fmt(c['estimate'], pct)} | {_ci(c['ci95'], pct)} | {tasks} | {detail} "
                f"| {c['verdict']} |"
            )
    p1a = result["predictions"][0]["components"][0]
    if p1a["per_task"]:
        lines += ["", "| Task | P1 effect (settled − unsettled, arbitrary) |", "|---|---|"]
        lines += [f"| {t} | {_fmt(e, True)} |" for t, e in p1a["per_task"].items()]
    infra = result["infrastructure"]
    lines += [
        "",
        "| Trials | Count |",
        "|---|---|",
        f"| Distinct trials | {infra['distinct_trials']} |",
        f"| Counted trials | {infra['counted_trials']} |",
        f"| Infrastructure-failed attempts | {infra['infrastructure_failed_attempts']} "
        f"({100 * infra['infrastructure_rate']:.1f}%) |",
        f"| Reruns | {infra['reruns']} |",
        f"| Unresolved after rerun | {infra['unresolved_after_rerun']} |",
        f"| Agent failures | {sum(infra['agent_failures_by_type'].values())} {infra['agent_failures_by_type'] or ''} |",
        f"| Missing decision results | {infra['missing_results']} |",
    ]
    if result["warnings"]:
        lines += ["", "Warnings:", ""] + [f"- {w}" for w in result["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pre-registered analysis of an Autonomy Lab run directory.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    parser.add_argument("--kappa", type=float, default=None, help="disclosure-audit Cohen's kappa for P5")
    parser.add_argument("--tasks-dir", type=Path, default=None, help="task bank, for the descriptive author split")
    parser.add_argument("--n-boot", type=int, default=N_BOOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    trials = args.run_dir / "trials.jsonl"
    kappa = args.kappa
    audit = args.run_dir / "disclosure_audit.json"
    try:
        if kappa is None and audit.exists():
            kappa = read_json(audit).get("kappa")
        result = analyze(
            load_records(trials), seed=args.seed, kappa=kappa, n_boot=args.n_boot, tasks_dir=args.tasks_dir
        )
        result["input_sha256"] = hashlib.sha256(trials.read_bytes()).hexdigest()
        markdown = to_markdown(result)
        (args.run_dir / "analysis.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.run_dir / "analysis.md").write_text(markdown, encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(markdown, end="")


if __name__ == "__main__":
    main()
