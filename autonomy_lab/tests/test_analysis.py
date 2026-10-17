"""Offline tests for the pre-registered analysis. Synthetic trial records only; no model or Pi calls."""

import contextlib
import io
import itertools
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

import autonomy_lab.analysis as analysis
from autonomy_lab.power import generate_design

LABELS = {
    "d0": "arbitrary",
    "d1": "arbitrary",
    "d2": "inferable",
    "d3": "inferable",
    "d4": "inferable",
    "d5": "preserve",
}


def record(
    experiment,
    seed,
    task_channels,
    results,
    *,
    filler=False,
    arm=None,
    disclosed=None,
    attempt=1,
    infrastructure_failure=None,
    agent_failure=None,
    design_id="test-design",
):
    tasks = list(task_channels)
    out = {
        "experiment": experiment,
        "design_id": design_id,
        "seed": seed,
        "task_ids": tasks,
        "breadth": len(tasks),
        "channels": task_channels,
        "filler": filler,
        "arm": arm,
        "decision_results": results,
        "labels": {t: dict(LABELS) for t in tasks},
        "task_bank_sha256": "0" * 64,
        "infrastructure_failure": infrastructure_failure,
        "agent_failure": agent_failure,
        "confinement_signals": [],
        "attempt": attempt,
        "runtime_matches_request": True,
        "spec_files_intact": True,
    }
    if disclosed is not None:
        out["disclosed"] = disclosed
    return out


def e1_records(outcome, n_tasks=12, n_trials=16, seed=0, labels=None):
    """Balanced E1 records. outcome(rng, task, decision, label, channel, filler, sib) -> bool.
    labels(task) -> {decision: label} overrides the default LABELS per task."""
    rng = random.Random(f"e1-records:{seed}")
    records = []
    trial_seed = 0
    for t in range(n_tasks):
        task = f"task{t:02d}"
        task_labels = labels(task) if labels else LABELS
        design, _ = generate_design(len(task_labels), n_trials, rng)
        for filler, channels in design:
            chans = dict(zip(task_labels, channels))
            res = {}
            for i, (d, label) in enumerate(task_labels.items()):
                checks = sum(1 for j, c in enumerate(channels) if j != i and c in ("check", "both"))
                res[d] = bool(outcome(rng, task, d, label, chans[d], filler, checks / (len(channels) - 1)))
            rec = record("E1", trial_seed, {task: chans}, {task: res}, filler=filler)
            rec["labels"] = {task: dict(task_labels)}
            records.append(rec)
            trial_seed += 1
    return records


def design_for(records):
    """The pre-run design the records were drawn from: one design per experiment, every distinct trial planned."""
    designs = {}
    for r in records:
        design = designs.setdefault(
            r["experiment"],
            {
                "experiment": r["experiment"],
                "design_id": r["design_id"],
                "task_bank_sha256": r["task_bank_sha256"],
                "trials": {},
            },
        )
        design["trials"].setdefault(
            r["seed"],
            {"seed": r["seed"], **{f: json.loads(json.dumps(r[f])) for f in ("task_ids", "channels", "filler", "arm")}},
        )
    return [{**d, "trials": list(d["trials"].values())} for d in designs.values()]


def analyze(records, **kwargs):
    """analysis.analyze with the design the records came from, unless a test passes its own."""
    kwargs.setdefault("design", design_for(records))
    return analysis.analyze(records, **kwargs)


def write_run(run, records, design=True):
    (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    if design:
        (run / "design.json").write_text(json.dumps(design_for(records)), encoding="utf-8")


def bernoulli(settled, unsettled, other=0.95):
    def outcome(rng, task, decision, label, channel, filler, sib):
        if label != "arbitrary":
            return rng.random() < other
        return rng.random() < (settled if channel != "none" else unsettled)

    return outcome


def verdicts(result):
    out = {}
    for p in result["predictions"]:
        out[p["id"]] = p["verdict"]
        for c in p["components"]:
            out[c["name"]] = c["verdict"]
            for sub in (c.get("strata") or {}).values():
                out[sub["name"]] = sub["verdict"]
    return out


def stratum(result, index, label):
    return result["predictions"][index]["components"][-1]["strata"][label]


def perfect(rng, task, decision, label, channel, filler, sib):
    """Settled decisions always satisfied; unsettled arbitrary never; unsettled inferable/preserve always."""
    return channel != "none" or label != "arbitrary"


class SignTestTests(unittest.TestCase):
    def brute_tail(self, k, n):
        return sum(1 for bits in itertools.product((0, 1), repeat=n) if sum(bits) >= k) / 2**n

    def test_exact_one_sided_tails(self):
        self.assertAlmostEqual(analysis.sign_test([1] * 6)["p_positive"], 1 / 64)
        self.assertAlmostEqual(analysis.sign_test([1] * 5 + [-1])["p_positive"], 7 / 64)
        self.assertAlmostEqual(analysis.sign_test([1] * 5 + [-1])["p_negative"], 63 / 64)
        self.assertEqual(analysis.sign_test([0.5, 0, -0.2, 0.1])["ties"], 1)
        rng = random.Random(3)
        for n in range(1, 13):
            effects = [rng.choice((-0.3, 0.0, 0.2, 0.4)) for _ in range(n)]
            test = analysis.sign_test(effects)
            self.assertAlmostEqual(test["p_positive"], self.brute_tail(test["positive"], n), places=12)
            self.assertAlmostEqual(test["p_negative"], self.brute_tail(test["negative"], n), places=12)

    def test_critical_counts(self):
        # One-sided p < 0.025 over all tasks: 10 of 12, 9 of 10, 6 of 6; none reachable with 5 tasks.
        self.assertEqual(analysis.sign_test([1] * 12)["critical"], 10)
        self.assertEqual(analysis.sign_test([1] * 10)["critical"], 9)
        self.assertEqual(analysis.sign_test([1] * 6)["critical"], 6)
        self.assertIsNone(analysis.sign_test([1] * 5)["critical"])

    def test_ties_count_against_the_tested_direction(self):
        # Regression (T5 review, finding 4): dropping ties made 9 positive + 3 tied look like p = 0.004.
        test = analysis.sign_test([0.5] * 9 + [0.0] * 3)
        self.assertEqual((test["positive"], test["ties"], test["n"]), (9, 3, 12))
        self.assertGreater(test["p_positive"], 0.025)
        self.assertLess(analysis.sign_test([0.5] * 10 + [0.0] * 2)["p_positive"], 0.025)
        # Tasks without a computable effect are ties too.
        self.assertEqual(analysis.sign_test([0.5] * 9, n_total=12)["ties"], 3)

    def test_p1a_ties_block_support_and_falsification(self):
        def outcome(direction):
            def fn(rng, task, decision, label, channel, filler, sib):
                if label != "arbitrary" or int(task[-2:]) >= 9:  # tasks 9-11: no effect, a tie
                    return True
                return (channel != "none") == direction

            return fn

        up = analyze(e1_records(outcome(True)), seed=1, n_boot=200)
        p1a = up["predictions"][0]["components"][0]
        self.assertEqual((p1a["positive_tasks"], p1a["tied_tasks"], p1a["critical_count"]), (9, 3, 10))
        self.assertEqual(p1a["verdict"], "inconclusive")
        down = analyze(e1_records(outcome(False)), seed=1, n_boot=200)
        self.assertEqual(verdicts(down)["P1a"], "inconclusive")


class HelperTests(unittest.TestCase):
    def test_quantile_type7(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(analysis.quantile(values, 0.0), 1.0)
        self.assertEqual(analysis.quantile(values, 1.0), 4.0)
        self.assertAlmostEqual(analysis.quantile(values, 0.5), 2.5)
        self.assertAlmostEqual(analysis.quantile(values, 0.25), 1.75)

    def test_region_boundaries(self):
        # P1(b): falsification is strictly below 0.80, so a CI touching 0.80 does not intersect it.
        self.assertFalse(analysis.ci_intersects(("below", 0.80, False), [0.80, 0.95]))
        self.assertTrue(analysis.ci_within(("below", 0.80, False), [0.6, 0.79]))
        # P1(c): falsification is >= 0.70, inclusive.
        self.assertTrue(analysis.ci_intersects(("above", 0.70, True), [0.2, 0.70]))
        self.assertTrue(analysis.ci_within(("above", 0.70, True), [0.70, 0.9]))
        self.assertTrue(analysis.ci_within(("abs_above", 0.20, False), [-0.5, -0.21]))
        self.assertFalse(analysis.ci_within(("abs_above", 0.20, False), [-0.5, 0.3]))
        self.assertTrue(analysis.in_region(("abs_below", 0.10), 0.0999))
        self.assertTrue(analysis.in_region(("at_least", 0.30), 0.1 + 0.2))  # float artefact 0.30000000000000004
        self.assertFalse(analysis.in_region(("abs_below", 0.10), 0.1))

    def test_sibling_fraction_uses_other_decisions(self):
        chans = {"d0": "none", "d1": "check", "d2": "both", "d3": "contract", "d4": "none", "d5": "check"}
        rec = record("E1", 1, {"t": chans}, {"t": dict.fromkeys(LABELS, True)})
        obs = {o.decision: o for o in analysis.observations([rec], "E1")[0]}
        self.assertAlmostEqual(obs["d0"].sib, 3 / 5)
        self.assertAlmostEqual(obs["d1"].sib, 2 / 5)
        self.assertAlmostEqual(obs["d3"].sib, 3 / 5)

    def test_missing_results_count_as_failures(self):
        chans = dict.fromkeys(LABELS, "contract")
        rec = record("E1", 1, {"t": chans}, {"t": {"d0": True}}, agent_failure="timeout")
        obs, missing = analysis.observations([rec], "E1")
        self.assertEqual(missing, 5)
        self.assertEqual(sum(o.result for o in obs), 1)

    def test_bad_channel_is_rejected(self):
        rec = record("E1", 1, {"t": dict.fromkeys(LABELS, "maybe")}, {"t": {}})
        with self.assertRaises(ValueError):
            analysis.observations([rec], "E1")


class E1PredictionTests(unittest.TestCase):
    def test_clean_strong_effect_supports_p1_to_p4(self):
        result = analyze(e1_records(perfect), seed=1)
        v = verdicts(result)
        self.assertEqual(result["status"], "valid")
        for key in ("P1", "P1a", "P1b", "P1c", "P2", "P3", "P4"):
            self.assertEqual(v[key], "supported", key)
        for key in ("P2", "P3", "P4"):
            for label in ("arbitrary", "inferable"):
                self.assertEqual(v[f"{key}[{label}]"], "supported", (key, label))
        self.assertEqual(result["predictions"][3]["unidentified_count"], 0)
        p1a = result["predictions"][0]["components"][0]
        self.assertEqual(p1a["estimate"], 1.0)
        self.assertEqual((p1a["positive_tasks"], p1a["negative_tasks"]), (12, 0))

    def test_noisy_strong_effect_is_recovered(self):
        result = analyze(e1_records(bernoulli(0.97, 0.2), seed=5), seed=2, n_boot=2000)
        v = verdicts(result)
        self.assertEqual(v["P1"], "supported")
        p1 = result["predictions"][0]["components"]
        self.assertGreater(p1[0]["estimate"], 0.6)
        lo, hi = p1[1]["ci95"]
        self.assertLessEqual(lo, p1[1]["estimate"])
        self.assertLessEqual(p1[1]["estimate"], hi)

    def test_null_effect_does_not_support_p1(self):
        for rate in (0.5, 0.85):
            result = analyze(e1_records(bernoulli(rate, rate), seed=7), seed=3, n_boot=2000)
            v = verdicts(result)
            self.assertNotEqual(v["P1"], "supported", rate)
            self.assertNotEqual(v["P1a"], "supported", rate)
        self.assertEqual(v["P1c"], "falsified")  # unsettled 0.85 is not arbitrary for this agent

    def test_execution_failure_falsifies_p1_through_settled_component(self):
        result = analyze(e1_records(bernoulli(0.6, 0.05), seed=11), seed=4, n_boot=2000)
        v = verdicts(result)
        self.assertEqual(v["P1a"], "supported")
        self.assertEqual(v["P1b"], "falsified")
        self.assertEqual(v["P1"], "falsified")

    def test_negative_effect_falsifies_sign_component(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel == "none"

        v = verdicts(analyze(e1_records(outcome), seed=1, n_boot=500))
        self.assertEqual(v["P1a"], "falsified")

    def test_channel_gap_falsifies_p2(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel in ("contract", "both")

        result = analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P2"], "falsified")
        self.assertEqual(stratum(result, 1, "arbitrary")["estimate"], 1.0)

    def test_filler_effect_falsifies_p3(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or filler

        result = analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P3"], "falsified")
        self.assertEqual(verdicts(result)["P2"], "supported")

    def test_spillover_falsifies_p4(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or sib >= 0.5

        result = analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P4"], "falsified")
        self.assertEqual(stratum(result, 3, "arbitrary")["estimate"], 1.0)

    def test_moderate_gaps_are_inconclusive(self):
        # A 15 pp check-only shortfall in half the tasks: neither within 10 pp nor clearly beyond 20 pp.
        def outcome(rng, task, decision, label, channel, filler, sib):
            if channel == "check" and int(task[-2:]) % 2:
                return rng.random() < 0.7
            return channel != "none"

        v = verdicts(analyze(e1_records(outcome, seed=2), seed=1, n_boot=2000))
        self.assertEqual(v["P2"], "inconclusive")

    def test_deterministic_given_seed(self):
        records = e1_records(bernoulli(0.9, 0.4), seed=13)
        first = json.dumps(analyze(records, seed=9, n_boot=1000), sort_keys=True)
        second = json.dumps(analyze(records, seed=9, n_boot=1000), sort_keys=True)
        other = json.dumps(analyze(records, seed=10, n_boot=1000), sort_keys=True)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_single_task_is_inconclusive(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none"

        result = analyze(e1_records(outcome, n_tasks=1), seed=1, n_boot=200)
        self.assertTrue(all(verdicts(result)[p] == "inconclusive" for p in ("P1", "P2", "P3", "P4")))
        self.assertTrue(any("minimum bank" in w for w in result["warnings"]))


class StrataTests(unittest.TestCase):
    """Regression (T5 review, finding 1): P2-P4 and P5(c) must hold in both the arbitrary and inferable strata."""

    def test_inferable_only_effects_block_p2_p3_p4(self):
        def channel_gap(rng, task, decision, label, channel, filler, sib):
            if label == "inferable":
                return channel in ("contract", "both", "none")
            return perfect(rng, task, decision, label, channel, filler, sib)

        def filler_gap(rng, task, decision, label, channel, filler, sib):
            if label == "inferable" and channel == "none":
                return filler
            return perfect(rng, task, decision, label, channel, filler, sib)

        def spillover(rng, task, decision, label, channel, filler, sib):
            if label == "inferable" and channel == "none":
                return sib >= 0.5
            return perfect(rng, task, decision, label, channel, filler, sib)

        for outcome, key in ((channel_gap, "P2"), (filler_gap, "P3"), (spillover, "P4")):
            v = verdicts(analyze(e1_records(outcome), seed=1, n_boot=500))
            self.assertEqual(v[f"{key}[arbitrary]"], "supported", key)
            self.assertEqual(v[f"{key}[inferable]"], "falsified", key)
            self.assertEqual(v[key], "falsified", key)
            self.assertEqual(v["P1"], "supported", key)

    def test_preserve_effects_are_descriptive_only(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            if label == "preserve":
                return channel in ("contract", "both")
            return perfect(rng, task, decision, label, channel, filler, sib)

        result = analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P2"], "supported")
        preserve = result["descriptive"]["E1"]["rates_by_label_and_channel"]["preserve"]
        self.assertEqual(preserve["check"]["rate"], 0.0)

    def test_stratum_with_one_task_is_inconclusive(self):
        def labels(task):
            if task == "task00":
                return LABELS
            return {d: ("arbitrary" if label == "inferable" else label) for d, label in LABELS.items()}

        result = analyze(e1_records(perfect, labels=labels), seed=1, n_boot=500)
        v = verdicts(result)
        for key in ("P2", "P3", "P4"):
            self.assertEqual(v[f"{key}[arbitrary]"], "supported", key)
            self.assertEqual(v[f"{key}[inferable]"], "inconclusive", key)
            self.assertEqual(v[key], "inconclusive", key)
            self.assertEqual(stratum(result, int(key[1]) - 1, "inferable")["n_tasks"], 1)

    def test_p4_unidentified_decision_blocks_support(self):
        # Regression (T5 review, finding 5): a decision missing an exposure side must stop P4 being supported.
        records = e1_records(perfect)
        changed = 0
        for rec in records:
            chans = rec["channels"].get("task00")
            if chans and chans["d0"] == "none":
                others = [c for d, c in chans.items() if d != "d0"]
                if sum(c in ("check", "both") for c in others) / len(others) >= 0.6:
                    chans["d0"] = "contract"
                    changed += 1
        self.assertGreater(changed, 0)
        result = analyze(records, seed=1, n_boot=500)
        p4 = result["predictions"][3]
        self.assertEqual(p4["unidentified_count"], 1)
        self.assertEqual(p4["components"][0]["unidentified_decisions"], {"arbitrary": ["task00/d0"], "inferable": []})
        self.assertEqual(verdicts(result)["P4[arbitrary]"], "supported")
        self.assertEqual(p4["verdict"], "inconclusive")
        self.assertIn("cannot be supported", p4["components"][0]["note"])

    def test_p4_unidentified_decision_still_allows_falsification(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or sib >= 0.5

        records = e1_records(outcome)
        for rec in records:
            chans = rec["channels"].get("task00")
            if chans and chans["d0"] == "none":
                chans["d0"] = "contract"
        result = analyze(records, seed=1, n_boot=500)
        self.assertEqual(result["predictions"][3]["unidentified_count"], 1)
        self.assertEqual(verdicts(result)["P4"], "falsified")


class InfrastructureTests(unittest.TestCase):
    def test_final_attempt_per_seed_is_counted(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or label != "arbitrary"

        clean = e1_records(outcome)
        # Seed 0 failed on infrastructure first, with junk results that must be ignored.
        junk = dict(clean[0])
        junk.update(
            attempt=1,
            infrastructure_failure="provider_error",
            decision_results={t: dict.fromkeys(LABELS, False) for t in clean[0]["task_ids"]},
        )
        rerun = dict(clean[0], attempt=2)
        # Seed 1 failed twice: excluded and reported as unresolved.
        lost = [dict(clean[1], attempt=a, infrastructure_failure="no_settle") for a in (1, 2)]
        records = [junk, *lost, rerun, *clean[2:]]
        result = analyze(records, seed=1, n_boot=500)
        infra = result["infrastructure"]
        self.assertEqual(infra["distinct_trials"], len(clean))
        self.assertEqual(infra["counted_trials"], len(clean) - 1)
        self.assertEqual(infra["infrastructure_failed_attempts"], 3)
        self.assertEqual(infra["infrastructure_failures_by_type"], {"no_settle": 2, "provider_error": 1})
        self.assertEqual(infra["reruns"], 2)
        self.assertEqual(infra["unresolved_after_rerun"], 1)
        self.assertAlmostEqual(infra["infrastructure_rate"], 3 / len(clean))
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["validity"]["gates"]["infrastructure_rate_at_most_5pct"])
        # The junk attempt was dropped, so the clean effect survives intact.
        self.assertEqual(result["predictions"][0]["components"][1]["estimate"], 1.0)

    def test_infrastructure_gate_and_runtime_mismatch(self):
        recs = [
            record(
                "E1",
                s,
                {"t": dict.fromkeys(LABELS, "none")},
                {"t": dict.fromkeys(LABELS, True)},
                infrastructure_failure="spawn" if s < 2 else None,
            )
            for s in range(10)
        ]
        recs[5]["runtime_matches_request"] = False
        _, infra = analysis.select_counted(recs)
        self.assertEqual(infra["counted_trials"], 8)
        self.assertFalse(infra["validity"]["gates"]["infrastructure_rate_at_most_5pct"])
        self.assertFalse(infra["validity"]["gates"]["runtime_matches_request"])
        self.assertEqual(infra["validity"]["status"], "invalid")

    def test_agent_failures_are_counted_not_excluded(self):
        recs = [
            record("E1", s, {"t": dict.fromkeys(LABELS, "none")}, {"t": {}}, agent_failure="timeout") for s in range(3)
        ]
        _, infra = analysis.select_counted(recs)
        self.assertEqual(infra["counted_trials"], 3)
        self.assertEqual(infra["agent_failures_by_type"], {"timeout": 3})


class InvalidRunTests(unittest.TestCase):
    """Regression (T5 review, finding 3): a failed §2.4 gate makes every verdict `invalid`."""

    def assert_invalid(self, result, reason):
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any(reason in f for f in result["validity"]["failed_gates"]), result["validity"])
        v = verdicts(result)
        self.assertEqual(set(v.values()), {"invalid"}, v)
        self.assertEqual(result["predictions"][0]["label"], "descriptive only: run invalid (§2.4)")
        self.assertEqual(result["predictions"][0]["components"][1]["estimate"], 1.0)  # numbers remain
        markdown = analysis.to_markdown(result)
        self.assertIn("**Run status: invalid.**", markdown)
        self.assertNotIn("| supported", markdown)

    def test_missing_runtime_field_invalidates(self):
        records = e1_records(perfect)
        del records[3]["runtime_matches_request"]
        self.assert_invalid(analyze(records, seed=1, n_boot=200), "runtime_matches_request")

    def test_runtime_mismatch_invalidates(self):
        records = e1_records(perfect)
        records[3]["runtime_matches_request"] = False
        self.assert_invalid(analyze(records, seed=1, n_boot=200), "runtime_matches_request")

    def test_infrastructure_denominator_is_distinct_trials(self):
        # 19 distinct trials, one failed first attempt rerun successfully: 1/19 = 5.3% > 5% fails the gate,
        # although failed attempts / all attempts would be 1/20 = 5%.
        records = e1_records(perfect, n_tasks=12, n_trials=8)
        base = [r for r in records if r["seed"] < 19]
        failed = dict(base[0], infrastructure_failure="provider_error")
        rerun = dict(base[0], attempt=2)
        result = analyze([failed, rerun, *base[1:]], seed=1, n_boot=200)
        self.assertEqual(result["infrastructure"]["distinct_trials"], 19)
        self.assert_invalid(result, "infrastructure-failure rate")
        # 20 distinct trials with one failure: exactly 5% passes.
        base = [r for r in records if r["seed"] < 20]
        failed = dict(base[0], infrastructure_failure="provider_error")
        rerun = dict(base[0], attempt=2)
        self.assertEqual(analyze([failed, rerun, *base[1:]], seed=1, n_boot=200)["status"], "valid")

    def test_tampering_invalidates(self):
        records = e1_records(perfect)
        records[5]["spec_files_intact"] = False
        self.assert_invalid(analyze(records, seed=1, n_boot=200), "prompt.txt or visible checks")
        records[5]["spec_files_intact"] = True
        del records[6]["spec_files_intact"]
        self.assert_invalid(analyze(records, seed=1, n_boot=200), "integrity unknown")

    def test_only_literal_true_clears_integrity(self):
        # Regression (T5 round-2 review, finding 2): falsy or string values passed the tampering gate.
        for value in (None, 0, "", "false", "true", 1, [], {}):
            records = e1_records(perfect, n_tasks=10, n_trials=8)
            records[5]["spec_files_intact"] = value
            result = analyze(records, seed=1, n_boot=100)
            self.assert_invalid(result, "integrity unknown")
            self.assertFalse(result["validity"]["gates"]["spec_files_intact"], value)
            self.assertEqual(result["infrastructure"]["spec_files_integrity_unknown"], 1, value)

    def test_tampering_on_an_infrastructure_attempt_invalidates(self):
        records = e1_records(perfect)
        failed = dict(records[0], infrastructure_failure="no_settle", spec_files_intact=False)
        rerun = dict(records[0], attempt=2)
        result = analyze([failed, rerun, *records[1:]], seed=1, n_boot=200)
        self.assert_invalid(result, "prompt.txt or visible checks")

    def test_confirmed_confinement_violation_invalidates(self):
        records = e1_records(perfect)
        records[2]["confinement_signals"] = [{"kind": "denied_path", "detail": "read: ../runner.py", "confirmed": True}]
        self.assert_invalid(analyze(records, seed=1, n_boot=200), "confinement violation")
        records[2]["confinement_signals"] = [{"kind": "denied_path", "detail": "read: ../runner.py"}]
        review = {"signals_reviewed": 1, "confirmed_violations": 1}
        self.assert_invalid(analyze(records, seed=1, n_boot=200, confinement_review=review), "confinement violation")

    def test_unreviewed_signals_withhold_verdicts(self):
        records = e1_records(perfect)
        records[2]["confinement_signals"] = [{"kind": "absolute_path", "detail": "read: /etc/hostname"}]
        result = analyze(records, seed=1, n_boot=200)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(set(verdicts(result).values()), {"pending_review"})
        cleared = analyze(
            records, seed=1, n_boot=200, confinement_review={"signals_reviewed": 1, "confirmed_violations": 0}
        )
        self.assertEqual(cleared["status"], "valid")
        self.assertEqual(verdicts(cleared)["P1"], "supported")

    def test_bad_review_is_rejected(self):
        with self.assertRaises(ValueError):
            analyze(e1_records(perfect, n_tasks=2, n_trials=8), confinement_review={"signals_reviewed": "x"})


def e2_records(plain_rate, assume_rate, satisfied=lambda rng, arm: rng.random() < 0.3, n_tasks=12, per_arm=4, seed=0):
    rng = random.Random(f"e2:{seed}")
    out = []
    s = 0
    for t in range(n_tasks):
        task = f"task{t:02d}"
        for arm, disclose in (("plain", plain_rate), ("assume", assume_rate)):
            for _ in range(per_arm):
                res = {d: satisfied(rng, arm) for d in LABELS}
                disc = {d: rng.random() < disclose for d in LABELS}
                out.append(
                    record("E2", s, {task: dict.fromkeys(LABELS, "none")}, {task: res}, arm=arm, disclosed={task: disc})
                )
                s += 1
    return out


class E2PredictionTests(unittest.TestCase):
    def test_disclosure_effect_supports_p5_with_good_kappa(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        v = verdicts(analyze(records, seed=1, kappa=0.8, n_boot=1000))
        self.assertEqual((v["P5a"], v["P5b"], v["P5c"], v["P5"]), ("supported",) * 4)

    def test_kappa_gate(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        self.assertEqual(verdicts(analyze(records, seed=1, kappa=0.5, n_boot=200))["P5"], "inconclusive")
        result = analyze(records, seed=1, kappa=None, n_boot=200)
        self.assertEqual(verdicts(result)["P5"], "inconclusive")
        self.assertIn("kappa", result["predictions"][4]["note"])

    def test_no_disclosure_change_falsifies_p5(self):
        v = verdicts(analyze(e2_records(0.1, 0.1, seed=3), seed=1, kappa=0.9, n_boot=1000))
        self.assertEqual(v["P5b"], "falsified")
        self.assertEqual(v["P5"], "falsified")

    def test_correctness_change_falsifies_p5c(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: arm == "assume")
        v = verdicts(analyze(records, seed=1, kappa=0.9, n_boot=500))
        self.assertEqual(v["P5c"], "falsified")

    def test_unjudged_decisions_are_counted(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        for r in records:
            r.pop("disclosed")
        result = analyze(records, seed=1, kappa=0.9, n_boot=200)
        p5 = result["predictions"][4]
        self.assertEqual(p5["unjudged_decisions"], 12 * 8 * 2)
        self.assertEqual(p5["eligible_decisions"], 12 * 8 * 2)
        self.assertEqual(p5["components"][0]["verdict"], "inconclusive")
        self.assertEqual(p5["verdict"], "inconclusive")

    def test_one_unjudged_decision_makes_p5_inconclusive(self):
        # Regression (T5 review, finding 2): unjudged decisions were silently excluded and P5 stayed supported.
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        task = records[0]["task_ids"][0]
        del records[0]["disclosed"][task]["d0"]
        result = analyze(records, seed=1, kappa=0.9, n_boot=500)
        p5 = result["predictions"][4]
        self.assertEqual(p5["unjudged_decisions"], 1)
        self.assertEqual([c["verdict"] for c in p5["components"]], ["supported"] * 3)
        self.assertEqual(p5["verdict"], "inconclusive")
        self.assertIn("unjudged", p5["note"])

    def test_non_boolean_judgements_are_unjudged(self):
        # Regression (T5 round-2 review, finding 3): "false" or 0 was read as a judgement (and "true" as false).
        for value in ("true", "false", 1, 0, None, "yes", [True]):
            records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
            task = records[0]["task_ids"][0]
            records[0]["disclosed"][task]["d0"] = value
            p5 = analyze(records, seed=1, kappa=0.9, n_boot=200)["predictions"][4]
            self.assertEqual(p5["unjudged_decisions"], 1, value)
            self.assertEqual(p5["verdict"], "inconclusive", value)
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        for r in records:
            r["disclosed"] = {t: {d: int(v) for d, v in m.items()} for t, m in r["disclosed"].items()}
        p5 = analyze(records, seed=1, kappa=0.9, n_boot=200)["predictions"][4]
        self.assertEqual(p5["unjudged_decisions"], p5["eligible_decisions"])
        self.assertEqual(p5["components"][0]["verdict"], "inconclusive")
        records[0]["disclosed"] = "malformed"
        self.assertEqual(analyze(records, seed=1, kappa=0.9, n_boot=50)["predictions"][4]["verdict"], "inconclusive")

    def test_non_finite_or_invalid_kappa_makes_p5_inconclusive(self):
        # Regression (T5 review, finding 2): NaN < 0.6 is False, so NaN used to pass the kappa gate.
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        for kappa in (math.nan, math.inf, -math.inf, "0.9", True, 1.5):
            result = analyze(records, seed=1, kappa=kappa, n_boot=200)
            self.assertEqual(result["predictions"][4]["verdict"], "inconclusive", kappa)
        self.assertEqual(analyze(records, seed=1, kappa=0.6, n_boot=200)["predictions"][4]["verdict"], "supported")

    def test_inferable_only_satisfaction_change_blocks_p5c(self):
        # Regression (T5 review, finding 1): P5(c) is evaluated in both strata.
        rng = random.Random(1)
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        for r in records:
            if r["arm"] == "plain":
                task = r["task_ids"][0]
                for d, label in LABELS.items():
                    if label == "inferable":
                        r["decision_results"][task][d] = rng.random() < 0.5
        v = verdicts(analyze(records, seed=1, kappa=0.9, n_boot=500))
        self.assertEqual(v["P5c[arbitrary]"], "supported")
        self.assertEqual(v["P5c[inferable]"], "falsified")
        self.assertEqual(v["P5c"], "falsified")
        self.assertEqual(v["P5"], "falsified")


def e3_records(single_rates, n_single, multi, seed=0):
    """multi: {breadth: (n_trials, success_fn(rng, tasks, rates) -> {task: bool})}."""
    rng = random.Random(f"e3:{seed}")
    tasks = sorted(single_rates)
    out = []
    s = 0

    def emit(task_ids, success):
        nonlocal s
        chans = {t: dict.fromkeys(LABELS, "contract") for t in task_ids}
        res = {t: dict.fromkeys(LABELS, bool(success[t])) for t in task_ids}
        out.append(record("E3", s, chans, res))
        s += 1

    for t in tasks:
        for _ in range(n_single):
            emit([t], {t: rng.random() < single_rates[t]})
    for breadth, (n_trials, fn) in multi.items():
        for _ in range(n_trials):
            chosen = rng.sample(tasks, breadth)
            emit(chosen, fn(rng, chosen, single_rates))
    return out


def independent(rng, tasks, rates):
    return {t: rng.random() < rates[t] for t in tasks}


class DesignPopulationTests(unittest.TestCase):
    """Regression (T5 round-2 review, finding 1): populations come from design.json, not counted observations."""

    def reviewer_run(self):
        # E1 10 tasks x 8 trials, E2 10 x 12 per arm; every E1 trial of task08 fails twice on infrastructure
        # (16 failed attempts / 320 distinct trials = 5%, a valid run). task09 has no effect (a tie); the other
        # 8 tasks have a perfect positive effect.
        def outcome(rng, task, decision, label, channel, filler, sib):
            return True if task == "task09" else perfect(rng, task, decision, label, channel, filler, sib)

        e1 = []
        for r in e1_records(outcome, n_tasks=10, n_trials=8):
            if r["task_ids"] == ["task08"]:
                e1 += [dict(r, attempt=a, infrastructure_failure="no_settle") for a in (1, 2)]
            else:
                e1.append(r)
        e2 = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True, n_tasks=10, per_arm=12)
        return e1 + e2

    def test_entirely_excluded_task_counts_in_p1_and_p4(self):
        records = self.reviewer_run()
        result = analyze(records, seed=1, kappa=0.9, n_boot=500)
        infra = result["infrastructure"]
        self.assertEqual((infra["distinct_trials"], infra["infrastructure_failed_attempts"]), (320, 16))
        self.assertEqual(result["status"], "valid")
        p1a = result["predictions"][0]["components"][0]
        self.assertEqual((p1a["positive_tasks"], p1a["tied_tasks"], p1a["sign_test_tasks"]), (8, 2, 10))
        self.assertEqual(p1a["tasks_without_effect"], ["task08"])
        self.assertAlmostEqual(p1a["sign_test_p_positive"], 56 / 1024)  # 0.0547
        self.assertEqual(p1a["verdict"], "inconclusive")
        self.assertEqual(result["predictions"][0]["verdict"], "inconclusive")
        p4 = result["predictions"][3]
        self.assertEqual(
            p4["components"][0]["unidentified_decisions"],
            {"arbitrary": ["task08/d0", "task08/d1"], "inferable": ["task08/d2", "task08/d3", "task08/d4"]},
        )
        self.assertNotEqual(p4["verdict"], "supported")
        # The counted observations alone (the old population) gave a 9-task test, p = 0.0195, and support.
        e1 = analysis.observations([r for r in records if not r["infrastructure_failure"]], "E1")[0]
        old = analysis.compute_p1(e1, 1, 200)["components"][0]
        self.assertAlmostEqual(old["sign_test_p_positive"], 10 / 512)
        self.assertEqual(old["verdict"], "supported")
        self.assertEqual(analysis.compute_p4(e1, 1, 200)["verdict"], "supported")

    def test_unknown_labels_of_an_excluded_task_still_block_p4(self):
        records = [r for r in self.reviewer_run() if r["experiment"] == "E1"]
        labels = {k: v for k, v in analysis.decision_labels(records).items() if k[0] != "task08"}
        planned = analysis.planned_population(analysis.normalize_designs(design_for(records)), "E1", labels)
        e1 = analysis.observations([r for r in records if not r["infrastructure_failure"]], "E1")[0]
        p4 = analysis.compute_p4(e1, 1, 200, planned)
        unidentified = p4["components"][0]["unidentified_decisions"]
        self.assertEqual(len(unidentified["unknown"]), 6)  # preserve may be any label once unknown
        self.assertEqual(p4["verdict"], "inconclusive")

    def test_labels_fall_back_to_the_bank(self):
        records = self.reviewer_run()
        for r in records:
            if r["experiment"] == "E1" and r["task_ids"] == ["task08"]:
                del r["labels"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "task08").mkdir()
            bank = [{"id": d, "label": label} for d, label in LABELS.items()]
            (Path(tmp) / "task08" / "decisions.json").write_text(json.dumps(bank), encoding="utf-8")
            result = analyze(records, seed=1, kappa=0.9, n_boot=200, tasks_dir=tmp)
        unidentified = result["predictions"][3]["components"][0]["unidentified_decisions"]
        self.assertEqual(unidentified["arbitrary"], ["task08/d0", "task08/d1"])
        self.assertNotIn("unknown", unidentified)

    def test_excluded_e2_task_leaves_p5_inconclusive(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        result = analyze(records, seed=1, kappa=0.9, n_boot=200)
        self.assertEqual(result["predictions"][4]["verdict"], "supported")
        lost = [
            dict(r, attempt=a, infrastructure_failure="spawn")
            for r in records
            if r["task_ids"] == ["task03"] and r["arm"] == "plain"
            for a in (1, 2)
        ]
        kept = [r for r in records if not (r["task_ids"] == ["task03"] and r["arm"] == "plain")]
        rest = analyze(kept + lost, seed=1, kappa=0.9, n_boot=200)
        p5 = rest["predictions"][4]
        self.assertEqual(rest["status"], "invalid")  # 8 failed attempts / 96 trials also fails the 5% gate
        self.assertEqual(p5["unobserved_planned_cells"], ["task03/d0/plain", "task03/d1/plain"])
        self.assertIn("no counted trial", p5["note"])
        # Same population rule, without the infrastructure gate in the way.
        planned = analysis.planned_population(
            analysis.normalize_designs(design_for(records)), "E2", analysis.decision_labels(records)
        )
        e2 = analysis.observations(kept, "E2")[0]
        self.assertEqual(analysis.compute_p5(e2, 1, 0.9, 200, planned)["verdict"], "inconclusive")
        self.assertEqual(analysis.compute_p5(e2, 1, 0.9, 200)["verdict"], "supported")

    def test_missing_design_invalidates(self):
        result = analysis.analyze(e1_records(perfect), seed=1, n_boot=200)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["validity"]["gates"]["design_recorded"])
        self.assertTrue(any("design.json missing" in f for f in result["validity"]["failed_gates"]))
        self.assertEqual(set(verdicts(result).values()), {"invalid"})

    def test_records_off_design_invalidate(self):
        records = e1_records(perfect)
        design = design_for(records)
        changed = json.loads(json.dumps(records))
        task = changed[4]["task_ids"][0]
        changed[4]["channels"][task]["d0"] = "both" if changed[4]["channels"][task]["d0"] != "both" else "none"
        result = analysis.analyze(changed, design=design, seed=1, n_boot=200)
        self.assertEqual(result["infrastructure"]["records_off_design"], 1)
        self.assertTrue(any("not a planned trial" in f for f in result["validity"]["failed_gates"]))
        unplanned = analysis.analyze(records + [dict(records[0], seed=999)], design=design, seed=1, n_boot=200)
        self.assertEqual(unplanned["status"], "invalid")
        wrong_id = analysis.analyze(
            [dict(r, trial_id="x") for r in records],
            design=[{**design[0], "trials": [dict(t, trial_id=f"t{t['seed']}") for t in design[0]["trials"]]}],
            seed=1,
            n_boot=200,
        )
        self.assertEqual(wrong_id["status"], "invalid")
        self.assertEqual(analysis.analyze(records, design=design[0], seed=1, n_boot=200)["status"], "valid")

    def test_malformed_design_is_rejected(self):
        for bad in ({"experiment": "E1"}, [{"experiment": "E1", "design_id": "x", "trials": [{"seed": 1}]}], 3):
            with self.assertRaises(ValueError):
                analysis.analyze(e1_records(perfect, n_tasks=2, n_trials=8), design=bad, n_boot=50)


class E3PredictionTests(unittest.TestCase):
    rates = {f"task{t:02d}": r for t, r in enumerate((0.5, 0.6, 0.7, 0.8, 0.9, 0.65, 0.75, 0.85))}

    def test_independence_supports_p6(self):
        records = e3_records(self.rates, 150, {2: (400, independent), 4: (400, independent)}, seed=1)
        result = analyze(records, seed=1, n_boot=1000)
        v = verdicts(result)
        self.assertEqual((v["P6-B2"], v["P6-B4"], v["P6"]), ("supported", "supported", "supported"))

    def test_dependence_falsifies_p6(self):
        def together(rng, tasks, rates):
            ok = rng.random() < 0.9
            return dict.fromkeys(tasks, ok)

        records = e3_records(self.rates, 30, {2: (60, together), 4: (60, independent)}, seed=2)
        result = analyze(records, seed=1, n_boot=1000)
        v = verdicts(result)
        self.assertEqual(v["P6-B2"], "falsified")
        self.assertEqual(v["P6"], "falsified")
        b2 = result["predictions"][5]["components"][0]
        self.assertGreater(b2["estimate"], 0.15)
        self.assertGreater(b2["ci95"][0], 0)

    def test_small_samples_are_inconclusive(self):
        records = e3_records(self.rates, 3, {2: (8, independent), 4: (8, independent)}, seed=3)
        v = verdicts(analyze(records, seed=1, n_boot=500))
        self.assertEqual(v["P6"], "inconclusive")

    def test_prediction_is_product_of_single_task_rates(self):
        # Two tasks with exact B=1 rates 1/2 and 1/4; one B=2 trial that succeeds: 1 - 1/8.
        single = {"a": [1, 0], "b": [1, 0, 0, 0]}
        part = analysis.p6_breadth(single, [(("a", "b"), 1)], seed=0, n_boot=200, name="P6-B2")
        self.assertAlmostEqual(part["estimate"], 1 - 0.125)
        self.assertAlmostEqual(part["predicted_rate"], 0.125)

    def test_trials_with_unknown_tasks_are_excluded(self):
        single = {"a": [1, 1], "b": [1, 0]}
        part = analysis.p6_breadth(single, [(("a", "b"), 1), (("a", "z"), 0)], seed=0, n_boot=100, name="x")
        self.assertEqual((part["n_trials"], part["excluded_trials"]), (1, 1))


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_markdown(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or label != "arbitrary"

        records = e1_records(outcome, n_tasks=10, n_trials=8) + e2_records(0.0, 1.0, n_tasks=10)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            write_run(run, records)
            (run / "disclosure_audit.json").write_text(json.dumps({"kappa": 0.75}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                analysis.main([str(run), "--seed", "4", "--n-boot", "500"])
            result = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
            markdown = (run / "analysis.md").read_text(encoding="utf-8")
        self.assertEqual(result["seed"], 4)
        self.assertEqual(result["predictions"][4]["kappa"], 0.75)
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(len(result["design_sha256"]), 64)
        self.assertIn("| P1 | supported |", markdown)
        self.assertIn("| P1a |", markdown)
        self.assertEqual(out.getvalue(), markdown)
        self.assertTrue(math.isclose(result["infrastructure"]["infrastructure_rate"], 0.0))

    def test_cli_reads_confinement_review(self):
        records = e1_records(perfect, n_tasks=10, n_trials=8)
        records[0]["confinement_signals"] = [{"kind": "home_path", "detail": "bash: ~"}]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            write_run(run, records)
            with contextlib.redirect_stdout(io.StringIO()):
                analysis.main([str(run), "--n-boot", "100"])
            self.assertEqual(
                json.loads((run / "analysis.json").read_text(encoding="utf-8"))["status"], "pending_review"
            )
            review = {"signals_reviewed": 1, "confirmed_violations": 1}
            (run / "confinement_review.json").write_text(json.dumps(review), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                analysis.main([str(run), "--n-boot", "100"])
            result = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["predictions"][0]["verdict"], "invalid")

    def test_cli_without_design_json_is_invalid(self):
        records = e1_records(perfect, n_tasks=10, n_trials=8)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            write_run(run, records, design=False)
            with contextlib.redirect_stdout(io.StringIO()):
                analysis.main([str(run), "--n-boot", "100"])
            result = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
            markdown = (run / "analysis.md").read_text(encoding="utf-8")
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("design_sha256", result)
        self.assertIn("design.json missing", markdown)
        self.assertIn("| Records off design.json | no design.json |", markdown)

    def test_cli_reports_malformed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "trials.jsonl").write_text("{not json\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                analysis.main([tmp])


if __name__ == "__main__":
    unittest.main()
