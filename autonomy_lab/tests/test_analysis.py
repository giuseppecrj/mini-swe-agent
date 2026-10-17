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
    }
    if disclosed is not None:
        out["disclosed"] = disclosed
    return out


def e1_records(outcome, n_tasks=12, n_trials=16, seed=0):
    """Balanced E1 records. outcome(rng, task, decision, label, channel, filler, sib) -> bool."""
    rng = random.Random(f"e1-records:{seed}")
    records = []
    trial_seed = 0
    for t in range(n_tasks):
        task = f"task{t:02d}"
        design, _ = generate_design(len(LABELS), n_trials, rng)
        for filler, channels in design:
            chans = dict(zip(LABELS, channels))
            res = {}
            for i, (d, label) in enumerate(LABELS.items()):
                checks = sum(1 for j, c in enumerate(channels) if j != i and c in ("check", "both"))
                res[d] = bool(outcome(rng, task, d, label, chans[d], filler, checks / (len(channels) - 1)))
            records.append(record("E1", trial_seed, {task: chans}, {task: res}, filler=filler))
            trial_seed += 1
    return records


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
    return out


class SignTestTests(unittest.TestCase):
    def brute(self, effects):
        signs = [e for e in effects if e != 0]
        n = len(signs)
        observed = abs(sum(1 for e in signs if e > 0) - n / 2)
        extreme = sum(1 for bits in itertools.product((0, 1), repeat=n) if abs(sum(bits) - n / 2) >= observed)
        return extreme / 2**n if n else 1.0

    def test_exact_small_examples(self):
        self.assertAlmostEqual(analysis.sign_test([1] * 5)[2], 2 / 32)
        self.assertAlmostEqual(analysis.sign_test([1] * 6)[2], 2 / 64)
        self.assertAlmostEqual(analysis.sign_test([1] * 5 + [-1])[2], 2 * 7 / 64)
        self.assertEqual(analysis.sign_test([0, 0])[2], 1.0)
        self.assertEqual(analysis.sign_test([1, -1])[2], 1.0)
        self.assertEqual(analysis.sign_test([0.5, 0, -0.2, 0.1]), (2, 1, 1.0))

    def test_matches_enumeration(self):
        rng = random.Random(3)
        for n in range(1, 13):
            effects = [rng.choice((-0.3, 0.0, 0.2, 0.4)) for _ in range(n)]
            self.assertAlmostEqual(analysis.sign_test(effects)[2], self.brute(effects), places=12)

    def test_twelve_tasks_need_ten_positive(self):
        self.assertLess(analysis.sign_test([1] * 10 + [-1] * 2)[2], 0.05)
        self.assertGreater(analysis.sign_test([1] * 9 + [-1] * 3)[2], 0.05)


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
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or label != "arbitrary"

        result = analysis.analyze(e1_records(outcome), seed=1)
        v = verdicts(result)
        for key in ("P1", "P1a", "P1b", "P1c", "P2", "P3", "P4"):
            self.assertEqual(v[key], "supported", key)
        p1a = result["predictions"][0]["components"][0]
        self.assertEqual(p1a["estimate"], 1.0)
        self.assertEqual((p1a["positive_tasks"], p1a["negative_tasks"]), (12, 0))

    def test_noisy_strong_effect_is_recovered(self):
        result = analysis.analyze(e1_records(bernoulli(0.97, 0.2), seed=5), seed=2, n_boot=2000)
        v = verdicts(result)
        self.assertEqual(v["P1"], "supported")
        p1 = result["predictions"][0]["components"]
        self.assertGreater(p1[0]["estimate"], 0.6)
        lo, hi = p1[1]["ci95"]
        self.assertLessEqual(lo, p1[1]["estimate"])
        self.assertLessEqual(p1[1]["estimate"], hi)

    def test_null_effect_does_not_support_p1(self):
        for rate in (0.5, 0.85):
            result = analysis.analyze(e1_records(bernoulli(rate, rate), seed=7), seed=3, n_boot=2000)
            v = verdicts(result)
            self.assertNotEqual(v["P1"], "supported", rate)
            self.assertNotEqual(v["P1a"], "supported", rate)
        self.assertEqual(v["P1c"], "falsified")  # unsettled 0.85 is not arbitrary for this agent

    def test_execution_failure_falsifies_p1_through_settled_component(self):
        result = analysis.analyze(e1_records(bernoulli(0.6, 0.05), seed=11), seed=4, n_boot=2000)
        v = verdicts(result)
        self.assertEqual(v["P1a"], "supported")
        self.assertEqual(v["P1b"], "falsified")
        self.assertEqual(v["P1"], "falsified")

    def test_negative_effect_falsifies_sign_component(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel == "none"

        v = verdicts(analysis.analyze(e1_records(outcome), seed=1, n_boot=500))
        self.assertEqual(v["P1a"], "falsified")

    def test_channel_gap_falsifies_p2(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel in ("contract", "both")

        result = analysis.analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P2"], "falsified")
        self.assertEqual(result["predictions"][1]["components"][0]["estimate"], 1.0)

    def test_filler_effect_falsifies_p3(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or filler

        result = analysis.analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P3"], "falsified")
        self.assertEqual(verdicts(result)["P2"], "supported")

    def test_spillover_falsifies_p4(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none" or sib >= 0.5

        result = analysis.analyze(e1_records(outcome), seed=1, n_boot=500)
        self.assertEqual(verdicts(result)["P4"], "falsified")
        self.assertEqual(result["predictions"][3]["components"][0]["estimate"], 1.0)

    def test_moderate_gaps_are_inconclusive(self):
        # A 15 pp check-only shortfall in half the tasks: neither within 10 pp nor clearly beyond 20 pp.
        def outcome(rng, task, decision, label, channel, filler, sib):
            if channel == "check" and int(task[-2:]) % 2:
                return rng.random() < 0.7
            return channel != "none"

        v = verdicts(analysis.analyze(e1_records(outcome, seed=2), seed=1, n_boot=2000))
        self.assertEqual(v["P2"], "inconclusive")

    def test_deterministic_given_seed(self):
        records = e1_records(bernoulli(0.9, 0.4), seed=13)
        first = json.dumps(analysis.analyze(records, seed=9, n_boot=1000), sort_keys=True)
        second = json.dumps(analysis.analyze(records, seed=9, n_boot=1000), sort_keys=True)
        other = json.dumps(analysis.analyze(records, seed=10, n_boot=1000), sort_keys=True)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_single_task_is_inconclusive(self):
        def outcome(rng, task, decision, label, channel, filler, sib):
            return channel != "none"

        result = analysis.analyze(e1_records(outcome, n_tasks=1), seed=1, n_boot=200)
        self.assertTrue(all(verdicts(result)[p] == "inconclusive" for p in ("P1", "P2", "P3", "P4")))
        self.assertTrue(any("minimum bank" in w for w in result["warnings"]))


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
        result = analysis.analyze(records, seed=1, n_boot=500)
        infra = result["infrastructure"]
        self.assertEqual(infra["distinct_trials"], len(clean))
        self.assertEqual(infra["counted_trials"], len(clean) - 1)
        self.assertEqual(infra["infrastructure_failed_attempts"], 3)
        self.assertEqual(infra["infrastructure_failures_by_type"], {"no_settle": 2, "provider_error": 1})
        self.assertEqual(infra["reruns"], 2)
        self.assertEqual(infra["unresolved_after_rerun"], 1)
        self.assertAlmostEqual(infra["infrastructure_rate"], 3 / len(clean))
        self.assertTrue(infra["gates"]["infrastructure_rate_at_most_5pct"])
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
        self.assertFalse(infra["gates"]["infrastructure_rate_at_most_5pct"])
        self.assertFalse(infra["gates"]["runtime_matches_request"])

    def test_agent_failures_are_counted_not_excluded(self):
        recs = [
            record("E1", s, {"t": dict.fromkeys(LABELS, "none")}, {"t": {}}, agent_failure="timeout") for s in range(3)
        ]
        _, infra = analysis.select_counted(recs)
        self.assertEqual(infra["counted_trials"], 3)
        self.assertEqual(infra["agent_failures_by_type"], {"timeout": 3})


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
        v = verdicts(analysis.analyze(records, seed=1, kappa=0.8, n_boot=1000))
        self.assertEqual((v["P5a"], v["P5b"], v["P5c"], v["P5"]), ("supported",) * 4)

    def test_kappa_gate(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        self.assertEqual(verdicts(analysis.analyze(records, seed=1, kappa=0.5, n_boot=200))["P5"], "inconclusive")
        result = analysis.analyze(records, seed=1, kappa=None, n_boot=200)
        self.assertEqual(verdicts(result)["P5"], "inconclusive")
        self.assertIn("kappa", result["predictions"][4]["note"])

    def test_no_disclosure_change_falsifies_p5(self):
        v = verdicts(analysis.analyze(e2_records(0.1, 0.1, seed=3), seed=1, kappa=0.9, n_boot=1000))
        self.assertEqual(v["P5b"], "falsified")
        self.assertEqual(v["P5"], "falsified")

    def test_correctness_change_falsifies_p5c(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: arm == "assume")
        v = verdicts(analysis.analyze(records, seed=1, kappa=0.9, n_boot=500))
        self.assertEqual(v["P5c"], "falsified")

    def test_unjudged_decisions_are_excluded(self):
        records = e2_records(0.0, 1.0, satisfied=lambda rng, arm: True)
        for r in records:
            r.pop("disclosed")
        result = analysis.analyze(records, seed=1, kappa=0.9, n_boot=200)
        p5 = result["predictions"][4]
        self.assertEqual(p5["unjudged_decisions"], 12 * 8 * 2)
        self.assertEqual(p5["components"][0]["verdict"], "inconclusive")


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


class E3PredictionTests(unittest.TestCase):
    rates = {f"task{t:02d}": r for t, r in enumerate((0.5, 0.6, 0.7, 0.8, 0.9, 0.65, 0.75, 0.85))}

    def test_independence_supports_p6(self):
        records = e3_records(self.rates, 150, {2: (400, independent), 4: (400, independent)}, seed=1)
        result = analysis.analyze(records, seed=1, n_boot=1000)
        v = verdicts(result)
        self.assertEqual((v["P6-B2"], v["P6-B4"], v["P6"]), ("supported", "supported", "supported"))

    def test_dependence_falsifies_p6(self):
        def together(rng, tasks, rates):
            ok = rng.random() < 0.9
            return dict.fromkeys(tasks, ok)

        records = e3_records(self.rates, 30, {2: (60, together), 4: (60, independent)}, seed=2)
        result = analysis.analyze(records, seed=1, n_boot=1000)
        v = verdicts(result)
        self.assertEqual(v["P6-B2"], "falsified")
        self.assertEqual(v["P6"], "falsified")
        b2 = result["predictions"][5]["components"][0]
        self.assertGreater(b2["estimate"], 0.15)
        self.assertGreater(b2["ci95"][0], 0)

    def test_small_samples_are_inconclusive(self):
        records = e3_records(self.rates, 3, {2: (8, independent), 4: (8, independent)}, seed=3)
        v = verdicts(analysis.analyze(records, seed=1, n_boot=500))
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
            (run / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            (run / "disclosure_audit.json").write_text(json.dumps({"kappa": 0.75}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                analysis.main([str(run), "--seed", "4", "--n-boot", "500"])
            result = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
            markdown = (run / "analysis.md").read_text(encoding="utf-8")
        self.assertEqual(result["seed"], 4)
        self.assertEqual(result["predictions"][4]["kappa"], 0.75)
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertIn("| P1 | supported |", markdown)
        self.assertIn("| P1a |", markdown)
        self.assertEqual(out.getvalue(), markdown)
        self.assertTrue(math.isclose(result["infrastructure"]["infrastructure_rate"], 0.0))

    def test_cli_reports_malformed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "trials.jsonl").write_text("{not json\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                analysis.main([tmp])


if __name__ == "__main__":
    unittest.main()
