"""Pre-registered analysis of one Autonomy Lab run (LOOP_SPEC.md §2.1, §2.2, §2.4, §3.2, §4.3).

Usage: python3 -m autonomy_lab.analysis <run_dir> [--seed N] [--tasks-dir DIR]
       [--confinement-review PATH]

Reads <run_dir>/trials.jsonl and the pre-run design <run_dir>/design.json, writes <run_dir>/analysis.json
and <run_dir>/analysis.md, and prints the Markdown. Standard library only. Every bootstrap is deterministic
given --seed.

Interpretation choices (the spec, including its "Pre-registration clarifications", is authoritative; these
resolve what it leaves open, conservatively):

- Populations come from the pre-run design (§2.3 "Recording"): design.json is one design object, as the runner
  writes it, or a list of them, one per experiment. The tasks, decisions and E2 arms an analysis must account
  for are those the design plans, not those that happen to have counted observations, so a task or decision
  whose trials are all excluded still counts (against support) as described below. Decision labels come from
  any record of the run, excluded attempts included, then from the bank's decisions.json (--tasks-dir); a
  decision whose label is still unknown counts wherever its stratum could matter.
- Counted trials: records are grouped by (experiment, design_id, seed) and only the highest `attempt` is
  used. A final attempt that is still an infrastructure failure is excluded and reported as unresolved.
- Validity gates (§2.4). If any gate fails, the run is `invalid`: every prediction and component verdict
  is `invalid`, and the estimates that remain are labelled descriptive only. The gates are:
  - runtime: every counted trial has `runtime_matches_request` exactly true; a missing field counts as a
    mismatch;
  - infrastructure (frozen denominator): failed attempts / distinct trials <= 0.05, where a failed
    attempt is any record, first attempt or rerun, with a non-null `infrastructure_failure`, and distinct
    trials are the distinct (experiment, design_id, seed) keys over all records in trials.jsonl. Reruns
    add to the numerator only; the denominator is never the attempt count;
  - confinement: no confirmed violation. A signal entry with `"confirmed": true`, or a positive
    `confirmed_violations` in the hand review (<run_dir>/confinement_review.json or
    --confinement-review: `{"signals_reviewed": int, "confirmed_violations": int}`), fails the gate.
    Signals that the review does not cover yet leave the run `pending_review`: verdicts are withheld
    (reported as `pending_review`) until the review covers every signal;
  - tampering: every record has `spec_files_intact` exactly true (prompt.txt and visible checks
    unchanged); any other value (missing, null, 0, "", "false", ...) counts as tampering not ruled out;
  - design: design.json is present, and every record is a planned trial of it (same experiment, design_id
    and seed, and, where recorded, the same trial_id and bank hash) with the planned tasks, channels,
    filler and arm. Without the design the populations below cannot be established.
- A decision missing from `decision_results` in a counted trial is scored as not satisfied (agent
  failures count against the condition) and counted under `missing_results`.
- "Settled" means channel `contract`, `check` or `both`; "unsettled" means `none`.
- Decision populations: P1 and P5(a)/(b) use `arbitrary` decisions. P2, P3, P4 and P5(c) are evaluated
  separately in the `arbitrary` and `inferable` strata; such a component is supported only if it is
  supported in both strata, falsified if it is falsified in either, and a stratum with fewer than 2
  contributing tasks is inconclusive. `preserve` decisions are reported descriptively only.
- P1(a): the per-task effect is the task's pooled settled rate minus its pooled unsettled rate over
  arbitrary decision observations. The sign test is an exact binomial (p = 1/2) over all E1 tasks; a task
  whose effect is exactly 0, or cannot be computed, is a tie and counts against whichever direction is
  tested. "All E1 tasks" are the tasks the design plans, so a task whose trials were all excluded is a tie.
  Supported needs median >= +0.30 and P(X >= positives) < 0.025; falsified needs P(X >= negatives) < 0.025,
  X ~ Binomial(all tasks, 1/2).
- P2 and P3 are the mean across tasks of per-task differences (contract-only minus check-only; filler on
  minus off among unsettled decisions). P4 is computed per decision (rate at sibling-check fraction
  >= 0.6 minus rate at <= 0.4 among its `none` cells), averaged per task, then averaged across tasks.
  The sibling-check fraction uses every other decision in the trial. A planned decision of either stratum
  (or of unknown label) whose counted `none` cells lack either exposure side, including one with no counted
  cell at all, is unidentified; it is counted and reported, and if any exists P4 cannot be supported (only
  falsified or inconclusive).
- P5 uses E2 records. Disclosure is read from an optional per-decision map in each record,
  `disclosed: {task_id: {decision_id: true|false}}`, written by the T6 judge; any value other than a JSON
  boolean counts as unjudged. P5(a)/(b) are means across
  tasks of per-task disclosure rates over unsettled arbitrary decisions; P5(c) is the mean across tasks
  of per-task satisfaction differences (assume minus plain), per stratum. The audit's Cohen's kappa
  comes from <run_dir>/disclosure_audit.json (written by `python3 -m autonomy_lab.judge audit`) and is
  used only after judge.verify_audit confirms, against the current disclosures.jsonl, that the audit is
  complete, bound to exactly the judged decisions, prompts, verdicts and judge setup in that file (its
  binding digest), drawn by the seeded sampler, and that its population covers every counted eligible E2
  decision; kappa is recomputed from the audit's labels. Otherwise kappa is unavailable and the reason is
  stated. There is no command-line kappa. P5 is inconclusive, whatever its components say, if any eligible
  (unsettled arbitrary) decision is unjudged, or kappa is unavailable, not a finite number in [-1, 1], or
  below 0.6. A planned (task, unsettled arbitrary or unknown-label decision, arm) cell of the E2 design with
  no counted observation is unjudged too.
- Judge output: when <run_dir>/disclosures.jsonl exists (written by `python3 -m autonomy_lab.judge judge`; one
  line per (trial_id, attempt)), its `disclosed` maps are merged into the records with the same trial_id and
  attempt before anything else. Every judged value must agree with the record's own `disclosed` map, if it
  has one, and with any other line for the same attempt; any disagreement (or a record `disclosed` that is not
  a map while the file judges that attempt) fails the `disclosures_agree` validity gate, because the run then
  has two conflicting sources for P5's outcome and neither can be preferred. Lines that match no record are
  counted and reported.
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
from fractions import Fraction
from pathlib import Path

from autonomy_lab.judge import verify_audit

ANALYSIS_VERSION = 4
N_BOOT = 10_000
SETTLED = ("contract", "check", "both")
CHANNELS = ("none", "contract", "check", "both")
KAPPA_MIN = 0.6
MIN_TASKS = 2
BANK_MIN_TASKS = 10
INFRA_GATE = 0.05
SIGN_ALPHA = 0.025  # one-sided; two-sided 0.05
STRATA = ("arbitrary", "inferable")


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


def normalize_designs(design):
    """{experiment: design} from one design object or a list of them, one per experiment; None if absent."""
    if design is None:
        return None
    out = {}
    for item in design if isinstance(design, list) else [design]:
        if not isinstance(item, dict) or not isinstance(item.get("trials"), list):
            raise ValueError("design: expected an object with a `trials` list, or a list of such objects")
        if "experiment" not in item or "design_id" not in item:
            raise ValueError("design: every design needs `experiment` and `design_id`")
        if item["experiment"] in out:
            raise ValueError(f"design: more than one design for experiment {item['experiment']!r}")
        for trial in item["trials"]:
            if (
                not isinstance(trial, dict)
                or "seed" not in trial
                or not isinstance(trial.get("task_ids"), list)
                or not isinstance(trial.get("channels"), dict)
            ):
                raise ValueError(f"design {item['design_id']}: every trial needs seed, task_ids and channels")
        out[item["experiment"]] = item
    return out


ASSIGNMENT_FIELDS = ("task_ids", "channels", "filler", "arm")


def off_design(records, designs):
    """Number of records that are not a planned trial of the design, or whose assignment differs from it."""
    planned = {(exp, d["design_id"], t["seed"]): (d, t) for exp, d in designs.items() for t in d["trials"]}
    count = 0
    for record in records:
        hit = planned.get(trial_key(record))
        if hit is None:
            count += 1
            continue
        design, trial = hit
        expected = {"trial_id": trial.get("trial_id"), "task_bank_sha256": design.get("task_bank_sha256")}
        if any(record.get(f) != trial.get(f) for f in ASSIGNMENT_FIELDS) or any(
            record.get(f) is not None and v is not None and record[f] != v for f, v in expected.items()
        ):
            count += 1
    return count


def _same_judgement(a, b):
    return json.dumps(a) == json.dumps(b)


def merge_disclosures(records, lines):
    """Merge the judge's disclosures.jsonl lines into the records by (trial_id, attempt).

    Returns (records, summary): records are copies where a judgement was merged, and the summary counts the
    merged records, the lines matching no record and the conflicts (see the module docstring)."""
    judged = {}
    conflicts = []
    for number, line in enumerate(lines, 1):
        maps = line.get("disclosed") if isinstance(line, dict) else None
        if not isinstance(maps, dict) or not all(isinstance(m, dict) for m in maps.values()):
            raise ValueError(f"disclosures.jsonl:{number}: expected `disclosed: {{task_id: {{decision_id: ...}}}}`")
        key = (line.get("trial_id"), int(line.get("attempt", 1)))
        merged = judged.setdefault(key, {})
        for task, decisions in maps.items():
            for decision, value in decisions.items():
                if decision in merged.get(task, {}) and not _same_judgement(merged[task][decision], value):
                    conflicts.append(f"{key[0]}-a{key[1]} {task}/{decision}: two disclosures.jsonl lines differ")
                merged.setdefault(task, {})[decision] = value
    out = []
    matched = set()
    for record in records:
        key = (record.get("trial_id"), int(record.get("attempt", 1)))
        if record.get("trial_id") is None or key not in judged:
            out.append(record)
            continue
        matched.add(key)
        own = record.get("disclosed", {})
        if not isinstance(own, dict):
            conflicts.append(f"{key[0]}-a{key[1]}: the record's own `disclosed` is not a map")
            own = {}
        merged = {
            task: dict(decisions) if isinstance(decisions, dict) else decisions for task, decisions in own.items()
        }
        for task, decisions in judged[key].items():
            if task in merged and not isinstance(merged[task], dict):
                conflicts.append(f"{key[0]}-a{key[1]} {task}: the record's own `disclosed` entry is not a map")
                merged[task] = {}
            for decision, value in decisions.items():
                target = merged.setdefault(task, {})
                if decision in target and not _same_judgement(target[decision], value):
                    conflicts.append(
                        f"{key[0]}-a{key[1]} {task}/{decision}: record says {target[decision]!r}, "
                        f"disclosures.jsonl says {value!r}"
                    )
                target[decision] = value
        out.append({**record, "disclosed": merged})
    summary = {
        "lines": len(lines),
        "merged_records": sum(1 for r in records if (r.get("trial_id"), int(r.get("attempt", 1))) in matched),
        "unmatched_lines": len(set(judged) - matched),
        "conflicts": len(conflicts),
        "conflict_examples": conflicts[:10],
        "judge_models": sorted({str(line.get("judge_model")) for line in lines}),
        "rubric_sha256": sorted({str(line.get("rubric_sha256")) for line in lines}),
    }
    return out, summary


def select_counted(records, confinement_review=None, designs=None, disclosure_conflicts=0):
    """Return (counted records, infrastructure summary). Uses the final attempt per trial seed.

    designs is normalize_designs(design.json), None when no design was recorded. disclosure_conflicts counts
    judged decisions on which the records and disclosures.jsonl disagree (merge_disclosures). The summary's
    `validity` holds the §2.4 gates; see the module docstring for the frozen definitions."""
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
    runtime_not_true = sum(1 for r in counted if r.get("runtime_matches_request") is not True)
    tampered = sum(1 for r in records if r.get("spec_files_intact") is False)
    # Only literal true clears a record: null, 0, "", "false" and a missing field all leave tampering open.
    integrity_unknown = sum(1 for r in records if r.get("spec_files_intact") is not True) - tampered
    off = off_design(records, designs) if designs is not None else None
    signal_list = [s for r in records for s in (r.get("confinement_signals") or [])]
    signals = len(signal_list)
    recorded_violations = sum(1 for s in signal_list if isinstance(s, dict) and s.get("confirmed") is True)
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
        "spec_files_tampered": tampered,
        "spec_files_integrity_unknown": integrity_unknown,
        "confinement_signals": signals,
        "confinement_violations_recorded": recorded_violations,
        "design_recorded": designs is not None,
        "records_off_design": off,
    }
    summary["validity"] = assess_validity(
        rate,
        runtime_not_true,
        tampered + integrity_unknown,
        signals,
        recorded_violations,
        confinement_review,
        off,
        disclosure_conflicts,
    )
    return counted, summary


def assess_validity(
    rate,
    runtime_not_true,
    tamper_failures,
    signals,
    recorded_violations,
    review,
    off_design: int | None = 0,
    disclosure_conflicts=0,
):
    """§2.4 gates and the run status: `invalid` if any gate fails, else `pending_review` while confinement
    signals remain unreviewed, else `valid`. off_design is None when no design was recorded."""
    review = review or {}
    reviewed = review.get("signals_reviewed", 0)
    confirmed = review.get("confirmed_violations", 0)
    for name, value in (("signals_reviewed", reviewed), ("confirmed_violations", confirmed)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"confinement review: {name} must be a non-negative integer, got {value!r}")
    gates = {
        "runtime_matches_request": runtime_not_true == 0,
        "infrastructure_rate_at_most_5pct": _r(rate) <= INFRA_GATE,
        "no_confirmed_confinement_violation": recorded_violations == 0 and confirmed == 0,
        "spec_files_intact": tamper_failures == 0,
        "design_recorded": off_design is not None,
        "records_match_design": off_design == 0,
        "disclosures_agree": disclosure_conflicts == 0,
    }
    failed = []
    if not gates["runtime_matches_request"]:
        failed.append(f"{runtime_not_true} counted trial(s) without runtime_matches_request = true")
    if not gates["infrastructure_rate_at_most_5pct"]:
        failed.append(f"infrastructure-failure rate {100 * rate:.1f}% > 5% (failed attempts / distinct trials)")
    if not gates["no_confirmed_confinement_violation"]:
        failed.append(f"confirmed confinement violation(s): {recorded_violations} recorded, {confirmed} from review")
    if not gates["spec_files_intact"]:
        failed.append(
            f"{tamper_failures} record(s) with prompt.txt or visible checks changed, or integrity unknown "
            "(spec_files_intact not exactly true)"
        )
    if off_design is None:
        failed.append(
            "design.json missing: without the pre-run design the task and decision populations cannot be "
            "established, so a task or decision whose trials were all excluded would drop out of P1, P4 and P5 "
            "unseen (§2.3, §3.2)"
        )
    elif off_design:
        failed.append(
            f"{off_design} record(s) not a planned trial of design.json (experiment, design_id, seed, trial_id, "
            "bank hash, tasks, channels, filler or arm differ)"
        )
    if disclosure_conflicts:
        failed.append(
            f"{disclosure_conflicts} disclosure judgement(s) where disclosures.jsonl disagrees with a record's own "
            "`disclosed` map or with another line for the same attempt: P5's outcome has two conflicting sources"
        )
    unreviewed = max(0, signals - reviewed)
    if failed:
        status = "invalid"
    elif unreviewed:
        status = "pending_review"
    else:
        status = "valid"
    return {
        "status": status,
        "gates": gates,
        "failed_gates": failed,
        "unreviewed_confinement_signals": unreviewed,
    }


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
        disclosed = record.get("disclosed")
        disclosed = disclosed if isinstance(disclosed, dict) else {}
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
            judged = disclosed.get(task)
            judged = judged.get(decision) if isinstance(judged, dict) else None
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
                    # Only a JSON boolean is a judgement; anything else ("true", 1, null) is unjudged.
                    disclosed=judged if isinstance(judged, bool) else None,
                )
            )
    return out, missing


# ---------------------------------------------------------------- planned populations (design.json)


@dataclass(frozen=True, slots=True)
class Planned:
    """The population an experiment's design plans: tasks, decisions with labels, and E2 cells."""

    tasks: tuple  # task ids
    decisions: dict  # {(task, decision): label, or None when unknown}
    unsettled_arms: frozenset  # {(task, decision, arm)} for planned `none` cells


def decision_labels(records, tasks_dir=None):
    """{(task, decision): label} from every record, excluded attempts included, then the bank's decisions.json."""
    labels = {}
    for record in records:
        for task, decisions in (record.get("labels") or {}).items():
            for decision, label in (decisions or {}).items():
                labels.setdefault((task, decision), label)
    if tasks_dir:
        for path in sorted(Path(tasks_dir).glob("*/decisions.json")):
            entries = read_json(path)
            if not isinstance(entries, list) or not all(isinstance(e, dict) and "id" in e for e in entries):
                raise ValueError(f"{path}: expected a list of decisions with `id` and `label`")
            for entry in entries:
                labels.setdefault((path.parent.name, entry["id"]), entry.get("label"))
    return labels


def planned_population(designs, experiment, labels):
    """The Planned population of one experiment, or None when no design was recorded."""
    if designs is None:
        return None
    tasks = []
    decisions = {}
    arms = set()
    design = designs.get(experiment)
    for trial in design["trials"] if design else []:
        for task in trial["task_ids"]:
            if task not in tasks:
                tasks.append(task)
            for decision, channel in (trial["channels"].get(task) or {}).items():
                decisions[(task, decision)] = labels.get((task, decision))
                if channel == "none":
                    arms.add((task, decision, trial.get("arm")))
    return Planned(tuple(tasks), decisions, frozenset(arms))


# ---------------------------------------------------------------- statistics


def binomial_upper_tail(k, n):
    """P(X >= k) for X ~ Binomial(n, 1/2), exactly."""
    return Fraction(sum(math.comb(n, i) for i in range(max(k, 0), n + 1)), 2**n)


def sign_test(effects, n_total=None):
    """Exact one-sided sign tests over all tasks; ties count against the direction tested (§3.2).

    n_total (default len(effects)) is the number of tasks; tasks beyond len(effects) are ties. Returns a dict
    with the counts, p_positive = P(X >= positives), p_negative = P(X >= negatives) for X ~ Binomial(n, 1/2),
    and `critical`, the smallest count with tail probability < 0.025 (None when no count reaches it)."""
    n = len(effects) if n_total is None else n_total
    if n < len(effects):
        raise ValueError("n_total is smaller than the number of effects")
    pos = sum(1 for e in effects if _r(e) > 0)
    neg = sum(1 for e in effects if _r(e) < 0)
    alpha = Fraction(SIGN_ALPHA).limit_denominator(1000)
    critical = next((k for k in range(n + 1) if binomial_upper_tail(k, n) < alpha), None) if n else None
    return {
        "positive": pos,
        "negative": neg,
        "ties": n - pos - neg,
        "n": n,
        "p_positive": float(binomial_upper_tail(pos, n)) if n else 1.0,
        "p_negative": float(binomial_upper_tail(neg, n)) if n else 1.0,
        "critical": critical,
    }


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


def stratified(name, rule, make):
    """A component evaluated per stratum (§3.2 clarifications): supported only if supported in both strata,
    falsified if falsified in either. make(label) returns the stratum's component."""
    strata = {label: make(label) for label in STRATA}
    verdict = combine(list(strata.values()))
    return {"name": name, "rule": rule, "strata": strata, "verdict": verdict}


def compute_p1(obs, seed, n_boot=N_BOOT, planned=None):
    """planned: the design's E1 Planned population; its tasks are all E1 tasks, and a task without a computable
    effect (all its trials excluded, or one side unobserved) is a tie. Without it, all tasks are those with
    any arbitrary observation."""
    arb = [o for o in obs if arbitrary(o)]
    effects = task_differences(arb, lambda o: o.channel in SETTLED, lambda o: o.channel == "none")
    tasks = set(planned.tasks) if planned is not None else {o.task for o in arb}
    tasks |= set(effects)
    test = sign_test(list(effects.values()), len(tasks))
    median = statistics.median(effects.values()) if effects else None
    rule_a = (
        "exact binomial over all tasks, ties against the tested direction; supported: median >= +0.30 and "
        "P(X >= positives) < 0.025; falsified: P(X >= negatives) < 0.025"
    )
    if len(effects) < MIN_TASKS:
        verdict_a = "inconclusive"
    elif test["p_negative"] < SIGN_ALPHA:
        verdict_a = "falsified"
    elif test["p_positive"] < SIGN_ALPHA and median is not None and _r(median) >= 0.30:
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
        "sign_test_tasks": test["n"],
        "positive_tasks": test["positive"],
        "negative_tasks": test["negative"],
        "tied_tasks": test["ties"],
        "tasks_without_effect": sorted(tasks - set(effects)),
        "critical_count": test["critical"],
        "sign_test_p_positive": test["p_positive"],
        "sign_test_p_negative": test["p_negative"],
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


STRATA_RULE = "; per stratum (arbitrary, inferable): supported only if both are, falsified if either is"


def compute_p2(obs, seed, n_boot=N_BOOT):
    rule = "contract-only minus check-only; supported: |d| < 0.10, CI within ±0.20; falsified: CI beyond ±0.20"

    def make(label):
        rows = [o for o in obs if o.label == label]
        values = task_differences(rows, lambda o: o.channel == "contract", lambda o: o.channel == "check")
        return mean_component(
            f"P2[{label}]", values, n_boot, seed, ("abs_below", 0.10), ("abs_above", 0.20, False), rule
        )

    comp = stratified("P2", rule + STRATA_RULE, make)
    return {"id": "P2", "components": [comp], "verdict": combine([comp])}


def compute_p3(obs, seed, n_boot=N_BOOT):
    rule = "unsettled, filler on minus off; supported: |d| < 0.10, CI within ±0.15; falsified: CI beyond ±0.15"

    def make(label):
        rows = [o for o in obs if o.label == label and o.channel == "none"]
        values = task_differences(rows, lambda o: o.filler, lambda o: not o.filler)
        return mean_component(
            f"P3[{label}]", values, n_boot, seed, ("abs_below", 0.10), ("abs_above", 0.15, False), rule
        )

    comp = stratified("P3", rule + STRATA_RULE, make)
    return {"id": "P3", "components": [comp], "verdict": combine([comp])}


def p4_unidentified(obs, planned=None):
    """{stratum: [task/decision]} for decisions whose counted `none` cells lack the <= 0.4 or the >= 0.6
    sibling-check side. planned (the design's population) adds decisions with no counted cell at all; one
    whose label is unknown is listed under `unknown`, since it may belong to either stratum."""
    labels = dict(planned.decisions) if planned is not None else {}
    sides = {}
    for o in obs:
        if labels.get((o.task, o.decision)) is None:
            labels[(o.task, o.decision)] = o.label
        seen = sides.setdefault((o.task, o.decision), set())
        if o.channel == "none":
            s = _r(o.sib)
            seen.add("high" if s >= 0.6 else "low" if s <= 0.4 else "mid")
    out = {label: [] for label in STRATA}
    for (task, decision), label in sorted(labels.items()):
        if label not in STRATA and label is not None:
            continue
        if not {"high", "low"} <= sides.get((task, decision), set()):
            out.setdefault(label or "unknown", []).append(f"{task}/{decision}")
    return out


def compute_p4(obs, seed, n_boot=N_BOOT, planned=None):
    rule = (
        "`none` cells, sibling-check >= 0.6 minus <= 0.4 per decision, averaged per task; supported: |d| < 0.10, "
        "CI within ±0.15; falsified: CI beyond ±0.15"
    )

    def make(label):
        values = p4_task_values([o for o in obs if o.label == label])
        return mean_component(
            f"P4[{label}]", values, n_boot, seed, ("abs_below", 0.10), ("abs_above", 0.15, False), rule
        )

    comp = stratified("P4", rule + STRATA_RULE + "; not supported if any decision is unidentified", make)
    unidentified = p4_unidentified(obs, planned)
    count = sum(len(v) for v in unidentified.values())
    comp["unidentified_decisions"] = unidentified
    comp["unidentified_count"] = count
    if count and comp["verdict"] == "supported":
        comp["verdict"] = "inconclusive"
        comp["note"] = f"{count} decision(s) lack an exposure stratum; P4 cannot be supported"
    return {"id": "P4", "components": [comp], "verdict": combine([comp]), "unidentified_count": count}


def task_disclosure(obs, arm):
    out = {}
    for task, rows in by_task(obs).items():
        judged = [o.disclosed is True for o in rows if o.arm == arm and o.disclosed is not None]
        if judged:
            out[task] = rate(judged)
    return out


def kappa_problem(kappa):
    """None when kappa is a finite number in [-1, 1] at or above 0.6, otherwise the reason it is not."""
    if kappa is None:
        return "no disclosure-audit kappa provided"
    if isinstance(kappa, bool) or not isinstance(kappa, (int, float)):
        return f"kappa {kappa!r} is not a number"
    if not math.isfinite(kappa) or not -1 <= kappa <= 1:
        return f"kappa {kappa!r} is not a finite value in [-1, 1]"
    if kappa < KAPPA_MIN:
        return f"kappa {kappa:.3f} < {KAPPA_MIN}"
    return None


def compute_p5(obs, seed, kappa, n_boot=N_BOOT, planned=None, kappa_note=None):
    """planned: the design's E2 Planned population. A planned unsettled (task, decision, arm) cell of an
    arbitrary or unknown-label decision with no counted observation is unjudged, so P5 is inconclusive.
    kappa_note: why no usable kappa exists (a failed audit verification), reported instead of kappa_problem."""
    arb = [o for o in obs if arbitrary(o) and o.channel == "none"]
    unjudged = sum(1 for o in arb if o.disclosed is None)
    unobserved = []
    if planned is not None:
        seen = {(o.task, o.decision, o.arm) for o in obs if o.channel == "none"}
        unobserved = sorted(
            (cell for cell in planned.unsettled_arms - seen if planned.decisions.get(cell[:2]) in ("arbitrary", None)),
            key=lambda cell: tuple(map(str, cell)),
        )
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
    rule_c = "satisfaction assume minus plain; supported: |d| < 0.10, CI within ±0.15; falsified: CI beyond ±0.15"

    def make(label):
        rows = [o for o in obs if o.label == label and o.channel == "none"]
        values = task_differences(rows, lambda o: o.arm == "assume", lambda o: o.arm == "plain")
        return mean_component(
            f"P5c[{label}]", values, n_boot, seed, ("abs_below", 0.10), ("abs_above", 0.15, False), rule_c
        )

    c = stratified("P5c", rule_c + STRATA_RULE, make)
    components = [a, b, c]
    verdict = combine(components)
    reasons = []
    if unjudged:
        reasons.append(f"{unjudged} of {len(arb)} eligible decision(s) unjudged")
    if unobserved:
        reasons.append(f"{len(unobserved)} planned eligible (task, decision, arm) cell(s) with no counted trial")
    problem = kappa_note or kappa_problem(kappa)
    if problem:
        reasons.append(problem)
    out = {
        "id": "P5",
        "components": components,
        "verdict": "inconclusive" if reasons else verdict,
        "kappa": kappa,
        "eligible_decisions": len(arb),
        "unjudged_decisions": unjudged,
        "unobserved_planned_cells": ["/".join(map(str, cell)) for cell in unobserved],
    }
    if reasons:
        out["note"] = "P5 cannot resolve: " + "; ".join(reasons) + " (§3.1, §3.2)"
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


def withhold(predictions, status):
    """Replace every prediction, component and stratum verdict by the run status (`invalid` or
    `pending_review`); estimates stay, labelled descriptive only."""
    label = f"descriptive only: run {status} (§2.4)"
    for p in predictions:
        p["verdict"] = status
        p["label"] = label
        for c in p["components"]:
            c["verdict"] = status
            for sub in (c.get("strata") or {}).values():
                sub["verdict"] = status


def analyze(
    records,
    design=None,
    seed=0,
    kappa=None,
    n_boot=N_BOOT,
    tasks_dir=None,
    confinement_review=None,
    disclosures=None,
    audit=None,
):
    """design: the parsed design.json (one design or a list of them); without it the run is invalid.
    disclosures: the parsed lines of disclosures.jsonl, merged into the records first (None: no judge file).
    audit: the parsed disclosure_audit.json. Whenever judge output (``disclosures``) or an audit is given, kappa
    comes only from the audit, verified against ``disclosures`` and the counted E2 population
    (judge.verify_audit). ``kappa`` is a caller-asserted value for simulations and tests without judge output
    and cannot be combined with either."""
    designs = normalize_designs(design)
    merge = None
    if disclosures is not None:
        records, merge = merge_disclosures(records, disclosures)
    counted, infra = select_counted(records, confinement_review, designs, merge["conflicts"] if merge else 0)
    labels = decision_labels(records, tasks_dir)
    planned = {exp: planned_population(designs, exp, labels) for exp in ("E1", "E2")}
    e1, missing_e1 = observations(counted, "E1")
    e2, missing_e2 = observations(counted, "E2")
    kappa_note = None
    audit_check = None
    if audit is not None or disclosures is not None:
        if kappa is not None:
            raise ValueError("with judge output or a disclosure audit, kappa comes only from the verified audit")
        eligible = [
            (counted[o.trial].get("trial_id"), int(counted[o.trial].get("attempt", 1)), o.task, o.decision)
            for o in e2
            if arbitrary(o) and o.channel == "none"
        ]
        kappa, kappa_note = verify_audit(audit, disclosures, eligible)
        audit_check = {"verified": kappa_note is None, "kappa": kappa, "problem": kappa_note}
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
    validity = infra.pop("validity")
    e1_tasks = len(planned["E1"].tasks) if planned["E1"] is not None else len({o.task for o in e1})
    warnings = []
    if e1 and e1_tasks < BANK_MIN_TASKS:
        warnings.append(f"E1 has {e1_tasks} tasks; the spec's minimum bank is {BANK_MIN_TASKS}")
    warnings += [f"validity gate failed: {f}" for f in validity["failed_gates"]]
    if validity["status"] == "invalid":
        warnings.append("the run is invalid (§2.4): every prediction is reported as invalid")
    if validity["unreviewed_confinement_signals"]:
        warnings.append(
            f"{validity['unreviewed_confinement_signals']} confinement signal(s) not covered by the hand review; "
            "verdicts are withheld until confinement_review.json covers them (§2.4)"
        )
    if infra["duplicate_attempt_numbers"]:
        warnings.append("duplicate attempt numbers for one seed; the last line was used")
    if merge and merge["unmatched_lines"]:
        warnings.append(f"{merge['unmatched_lines']} disclosures.jsonl line(s) match no trial record")
    if merge and (len(merge["rubric_sha256"]) > 1 or len(merge["judge_models"]) > 1):
        warnings.append("disclosures.jsonl mixes judge models or rubric versions")
    predictions = [
        compute_p1(e1, seed, n_boot, planned["E1"]),
        compute_p2(e1, seed, n_boot),
        compute_p3(e1, seed, n_boot),
        compute_p4(e1, seed, n_boot, planned["E1"]),
        compute_p5(e2, seed, kappa, n_boot, planned["E2"], kappa_note),
        compute_p6(e3, seed, n_boot),
    ]
    if validity["status"] != "valid":
        withhold(predictions, validity["status"])
    result = {
        "analysis_version": ANALYSIS_VERSION,
        "seed": seed,
        "n_boot": n_boot,
        "status": validity["status"],
        "validity": validity,
        "experiments": sorted({r.get("experiment") for r in counted if r.get("experiment")}),
        "infrastructure": infra,
        "warnings": warnings,
        "predictions": predictions,
        "descriptive": {"E1": descriptive_e1(e1)},
        "disclosures": merge,
        "disclosure_audit": audit_check,
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


def _component_rows(c):
    """Markdown rows for a component, one per stratum for stratified components."""
    if "strata" in c:
        rows = []
        for sub in c["strata"].values():
            rows += _component_rows(sub)
        extra = f"{c['unidentified_count']} unidentified decision(s)" if "unidentified_count" in c else ""
        if c.get("note"):
            extra = f"{extra}; {c['note']}" if extra else c["note"]
        rows.append(f"| {c['name']} (both strata) | – | – | – | {extra} | {c['verdict']} |")
        return rows
    pct = c["name"].split("[")[0] in PCT_COMPONENTS
    if c["name"] == "P1a":
        detail = (
            f"{c['positive_tasks']}+ / {c['negative_tasks']}− / {c['tied_tasks']} tied of {c['sign_test_tasks']}, "
            f"need {c['critical_count'] if c['critical_count'] is not None else 'unreachable'}; "
            f"p+ = {c['sign_test_p_positive']:.4f}, p− = {c['sign_test_p_negative']:.4f}"
        )
    elif c["name"].startswith("P6"):
        detail = f"90% CI {_ci(c['ci90'], True)}, {c['n_trials']} trials"
    elif "n" in c:
        detail = f"{c['successes']}/{c['n']}"
    else:
        detail = c.get("note", "")
    tasks = c.get("n_tasks", "–")
    return [
        f"| {c['name']} | {_fmt(c['estimate'], pct)} | {_ci(c['ci95'], pct)} | {tasks} | {detail} | {c['verdict']} |"
    ]


def to_markdown(result):
    lines = [
        f"Analysis v{result['analysis_version']}, seed {result['seed']}, {result['n_boot']} bootstrap resamples.",
        "",
        f"**Run status: {result['status']}.**",
    ]
    if result["status"] != "valid":
        lines.append(
            f"No prediction is supported or falsified; every verdict is `{result['status']}`, and the numbers "
            "below are descriptive only (§2.4)."
        )
        lines += [f"- {f}" for f in result["validity"]["failed_gates"]]
    lines += ["", "| Prediction | Verdict |", "|---|---|"]
    for p in result["predictions"]:
        note = f" ({p['note']})" if p.get("note") else ""
        lines.append(f"| {p['id']} | {p['verdict']}{note} |")
    lines += ["", "| Component | Estimate | 95% CI | Tasks | Detail | Verdict |", "|---|---|---|---|---|---|"]
    for p in result["predictions"]:
        for c in p["components"]:
            lines += _component_rows(c)
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
        f"({100 * infra['infrastructure_rate']:.1f}% of distinct trials) |",
        f"| Reruns | {infra['reruns']} |",
        f"| Unresolved after rerun | {infra['unresolved_after_rerun']} |",
        f"| Agent failures | {sum(infra['agent_failures_by_type'].values())} {infra['agent_failures_by_type'] or ''} |",
        f"| Missing decision results | {infra['missing_results']} |",
        f"| Runtime mismatches / unknown | {infra['runtime_mismatches']} / {infra['runtime_match_unknown']} |",
        f"| Spec files changed / unknown | {infra['spec_files_tampered']} / {infra['spec_files_integrity_unknown']} |",
        "| Records off design.json | "
        + (f"{infra['records_off_design']} |" if infra["design_recorded"] else "no design.json |"),
        f"| Confinement signals / recorded violations | {infra['confinement_signals']} / "
        f"{infra['confinement_violations_recorded']} |",
    ]
    if result["warnings"]:
        lines += ["", "Warnings:", ""] + [f"- {w}" for w in result["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pre-registered analysis of an Autonomy Lab run directory.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    parser.add_argument("--tasks-dir", type=Path, default=None, help="task bank, for the descriptive author split")
    parser.add_argument(
        "--confinement-review",
        type=Path,
        default=None,
        help="hand review of confinement signals (default: <run_dir>/confinement_review.json if present)",
    )
    parser.add_argument("--n-boot", type=int, default=N_BOOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    trials = args.run_dir / "trials.jsonl"
    design_path = args.run_dir / "design.json"
    audit = args.run_dir / "disclosure_audit.json"
    disclosures = args.run_dir / "disclosures.jsonl"
    try:
        review_path = args.confinement_review or args.run_dir / "confinement_review.json"
        review = read_json(review_path) if args.confinement_review or review_path.exists() else None
        if review is not None and not isinstance(review, dict):
            raise ValueError(f"{review_path}: expected a JSON object")
        result = analyze(
            load_records(trials),
            design=read_json(design_path) if design_path.exists() else None,
            seed=args.seed,
            n_boot=args.n_boot,
            tasks_dir=args.tasks_dir,
            confinement_review=review,
            disclosures=load_records(disclosures) if disclosures.exists() else None,
            audit=read_json(audit) if audit.exists() else None,
        )
        result["input_sha256"] = hashlib.sha256(trials.read_bytes()).hexdigest()
        for name, path in (("disclosures_sha256", disclosures), ("disclosure_audit_sha256", audit)):
            if path.exists():
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if design_path.exists():
            result["design_sha256"] = hashlib.sha256(design_path.read_bytes()).hexdigest()
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
