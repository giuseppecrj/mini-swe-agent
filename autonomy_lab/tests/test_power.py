"""Offline tests for the power simulation. No model or Pi calls."""

import contextlib
import inspect
import io
import json
import math
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import autonomy_lab.power as power
from autonomy_lab.analysis import CHANNELS, N_BOOT


class DesignTests(unittest.TestCase):
    def test_balanced_channels_and_filler(self):
        for n_decisions, n_trials in ((4, 8), (6, 16), (8, 24)):
            design, _ = power.generate_design(n_decisions, n_trials, random.Random(1))
            self.assertEqual(len(design), n_trials)
            self.assertEqual(sum(f for f, _ in design), n_trials // 2)
            for d in range(n_decisions):
                for filler in (True, False):
                    counts = Counter(ch[d] for f, ch in design if f == filler)
                    self.assertEqual(counts, dict.fromkeys(CHANNELS, n_trials // 8), (n_decisions, n_trials, d))

    def test_p4_exposure_holds(self):
        design, _ = power.generate_design(6, 8, random.Random(2))
        self.assertTrue(power.p4_exposed(design))
        for d in range(6):
            fractions = [power.sibling_fraction(ch, d) for _, ch in design if ch[d] == "none"]
            self.assertTrue(any(f <= 0.4 for f in fractions) and any(f >= 0.6 for f in fractions))

    def test_same_seed_same_design(self):
        a = power.generate_design(6, 16, random.Random("x"))
        b = power.generate_design(6, 16, random.Random("x"))
        self.assertEqual(a, b)

    def test_rejects_bad_trial_count(self):
        with self.assertRaises(ValueError):
            power.generate_design(6, 12, random.Random(0))

    def test_e3_subsets_are_balanced_and_distinct(self):
        for n_tasks, n_trials, breadth in ((6, 24, 4), (12, 24, 2), (6, 10, 4)):
            groups = power.e3_subsets(n_tasks, n_trials, breadth, random.Random(3))
            self.assertEqual(len(groups), n_trials)
            self.assertTrue(all(len(set(g)) == breadth for g in groups))
            usage = Counter(t for g in groups for t in g)
            self.assertEqual(len(usage), n_tasks)
            self.assertLessEqual(max(usage.values()) - min(usage.values()), 1)


class DecisionMixTests(unittest.TestCase):
    def test_e1_uses_the_draft_bank_mix(self):
        scenario = power.E1_SCENARIOS[0]
        self.assertEqual(scenario.mix, power.DRAFT_MIX)
        obs = power.simulate_e1(scenario, 6, 8, random.Random(1))
        for t, (n_arb, n_inf, n_pres) in enumerate(power.DRAFT_MIX):
            rows = [o for o in obs if o.task == f"t{t:02d}"]
            labels = Counter(o.label for o in rows if o.trial == rows[0].trial)
            self.assertEqual(labels, {"arbitrary": n_arb, "inferable": n_inf})  # preserve is not simulated
            n_decisions = n_arb + n_inf + n_pres
            # Sibling fractions are over every other decision, preserve included.
            self.assertTrue(all(round(o.sib * (n_decisions - 1), 9) % 1 == 0 for o in rows))
        means = [sum(m[i] for m in power.DRAFT_MIX) / len(power.DRAFT_MIX) for i in range(3)]
        self.assertTrue(3 <= means[0] <= 4 and 1 <= means[1] <= 2 and means[2] == 1)

    def test_e2_design_is_balanced_and_unsettled(self):
        scenario = power.E2_SCENARIOS[0]
        obs, _ = power.simulate_e2(scenario, 4, 4, random.Random(2))
        self.assertEqual({o.channel for o in obs}, {"none"})
        for t in range(4):
            arms = Counter(o.arm for o in obs if o.task == f"t{t:02d}" and o.decision == "d0")
            self.assertEqual(arms, {"plain": 4, "assume": 4})
        self.assertTrue(all((o.disclosed is None) == (o.label != "arbitrary") for o in obs))

    def test_cohen_kappa(self):
        # 2x2 table a=20 (yes/yes), b=5, c=10, d=15: po = 0.7, pe = 0.5, kappa = 0.4.
        pairs = [(True, True)] * 20 + [(True, False)] * 5 + [(False, True)] * 10 + [(False, False)] * 15
        self.assertAlmostEqual(power.cohen_kappa(pairs), 0.4)
        self.assertTrue(math.isnan(power.cohen_kappa([(True, True)] * 3)))


class CalibrationTests(unittest.TestCase):
    def test_marginal_rates_match_stated_rates(self):
        for p in (0.2, 0.5, 0.95):
            for sd in (0.0, 1.0, 2.5):
                self.assertAlmostEqual(power.marginal(power.calibrate(p, sd), sd), p, places=5)

    def test_symmetric_shift_gives_stated_gap(self):
        sd = 1.07
        x = power.calibrate(0.3, sd)
        h = power.symmetric_shift(0.3, 0.25, sd)
        self.assertAlmostEqual(power.marginal(x + h / 2, sd) - power.marginal(x - h / 2, sd), 0.25, places=5)
        self.assertEqual(power.symmetric_shift(0.3, 0.0, sd), 0.0)

    def test_simulated_rates_are_marginal(self):
        scenario = power.E1_SCENARIOS[0]
        obs = power.simulate_e1(scenario, 200, 16, random.Random(4))
        for label, settled_rate, unsettled_rate in (("arbitrary", 0.95, 0.30), ("inferable", 0.95, 0.80)):
            settled = [o.result for o in obs if o.label == label and o.channel != "none"]
            unsettled = [o.result for o in obs if o.label == label and o.channel == "none"]
            self.assertAlmostEqual(sum(settled) / len(settled), settled_rate, delta=0.02)
            self.assertAlmostEqual(sum(unsettled) / len(unsettled), unsettled_rate, delta=0.04)

    def test_e2_rates_are_marginal(self):
        scenario = power.E2Scenario("x", "", plain=0.1, assume=0.75, judge_accuracy=1.0)
        obs, kappa = power.simulate_e2(scenario, 300, 8, random.Random(5))
        for arm, expected in (("plain", 0.1), ("assume", 0.75)):
            judged = [o.disclosed for o in obs if o.arm == arm and o.disclosed is not None]
            self.assertAlmostEqual(sum(judged) / len(judged), expected, delta=0.03)
        sat = [o.result for o in obs if o.label == "inferable"]
        self.assertAlmostEqual(sum(sat) / len(sat), 0.80, delta=0.03)
        self.assertEqual(kappa, 1.0)  # a perfect judge agrees with the error-free auditor

    def test_e3_independent_gap_is_zero_and_interference_negative(self):
        by_name = {s.name: s for s in power.E3_SCENARIOS}
        self.assertAlmostEqual(power.e3_true_gap(by_name["independent"], 2, n=50_000), 0.0, delta=0.01)
        self.assertLess(power.e3_true_gap(by_name["interference"], 2, n=50_000), -0.15)
        self.assertGreater(power.e3_true_gap(by_name["shared-trial"], 4, n=50_000), 0.15)


def fake_cell(scenario, total, tasks, key_counts, sims=1000, n_boot=N_BOOT, **design):
    counts = {k: {"supported": v, "falsified": 0} for k, v in key_counts.items()}
    return {
        "scenario": scenario,
        "tasks": tasks,
        "total_trials": total,
        "sims": sims,
        "n_boot": n_boot,
        "counts": counts,
        "rates": {k: {v: c / sims for v, c in cs.items()} for k, cs in counts.items()},
        "se": {k: {v: power.mc_se(c / sims, sims) for v, c in cs.items()} for k, cs in counts.items()},
        "refined": False,
        **design,
    }


class SimulationTests(unittest.TestCase):
    def test_strong_effect_is_powered_and_null_is_not_supported(self):
        # Regression (T5 review, finding 8): 20 simulations could not support a >= 80% claim. With 200, the
        # claim is that 80% lies outside the 2-SE band (the module's refinement rule), not merely p >= 0.8.
        by_name = {s.name: s for s in power.E1_SCENARIOS}
        strong = power.run_chunk(("e1", by_name["strong"], (12, 16), 0, 200, 200, 0))
        k = strong["P1"]["supported"]
        self.assertGreaterEqual(k / 200, 0.8)
        self.assertFalse(power.near_target(k, 200))
        self.assertEqual(strong["P1"]["falsified"], 0)
        null = power.run_chunk(("e1", by_name["null"], (12, 16), 0, 40, 200, 0))
        execution = power.run_chunk(("e1", by_name["execution"], (12, 16), 0, 40, 200, 0))
        self.assertEqual(null["P1"]["supported"], 0)
        self.assertGreater(null["P1"]["falsified"], 20)
        self.assertEqual(execution["P1"]["supported"], 0)

    def test_inferable_only_effects_are_never_supported(self):
        by_name = {s.name: s for s in power.E1_SCENARIOS}
        for name, key in (("channel-gap-inferable", "P2"), ("spillover-inferable", "P4")):
            counts = power.run_chunk(("e1", by_name[name], (12, 16), 0, 30, 200, 0))
            self.assertEqual(counts[key]["supported"], 0, name)
            self.assertEqual(counts[f"{key}[inferable]"]["supported"], 0, name)

    def test_p5_simulation(self):
        # Regression (T5 review, finding 6): P5 had no power simulation.
        by_name = {s.name: s for s in power.E2_SCENARIOS}
        effective = power.run_chunk(("e2", by_name["effective"], (12, 8), 0, 40, 200, 0))
        none = power.run_chunk(("e2", by_name["no-effect"], (12, 4), 0, 40, 200, 0))
        self.assertGreater(effective["P5a"]["supported"], 30)
        self.assertGreater(effective["P5b"]["supported"], 30)
        self.assertGreater(effective["P5"]["supported"], 0)
        self.assertEqual(none["P5"]["supported"], 0)
        self.assertGreater(none["P5b"]["falsified"], 30)
        self.assertEqual(set(effective), set(power.E2_KEYS))

    def test_noisy_judge_lowers_kappa(self):
        by_name = {s.name: s for s in power.E2_SCENARIOS}

        def mean_kappa(name):
            values = [power.simulate_e2(by_name[name], 12, 4, random.Random(i))[1] for i in range(40)]
            return sum(values) / len(values)

        self.assertGreater(mean_kappa("effective"), mean_kappa("effective-noisy-judge") + 0.1)

    def test_e3_cell_reports_both_breadths(self):
        counts = power.run_chunk(("e3", power.E3_SCENARIOS[1], (6, 8, 48), 0, 10, 300, 0))
        cell = power.cell_result("e3", power.E3_SCENARIOS[1], (6, 8, 48), counts, 10, 300)
        self.assertEqual(cell["total_trials"], 6 * 8 + 2 * 48)
        self.assertEqual(set(cell["rates"]), {"P6-B2", "P6-B4", "H4"})
        self.assertGreater(cell["rates"]["P6-B2"]["falsified"], 0.0)

    def test_every_rate_has_a_monte_carlo_se(self):
        counts = {"P1": {"supported": 30, "falsified": 5}}
        cell = power.cell_result("e1", power.E1_SCENARIOS[0], (12, 16), counts, 40, 100)
        self.assertAlmostEqual(cell["se"]["P1"]["supported"], math.sqrt(0.75 * 0.25 / 40))
        self.assertAlmostEqual(cell["se"]["P1"]["falsified"], math.sqrt(0.125 * 0.875 / 40))
        self.assertIn("±", power._cell(cell, "P1"))

    def test_chunking_does_not_change_results(self):
        cells = [("e1", power.E1_SCENARIOS[0], (10, 8)), ("e3", power.E3_SCENARIOS[0], (6, 4, 12))]
        whole = power.run_cells(cells, 6, 100, 3, 1)
        with mock.patch.object(power, "CHUNK", 2):
            split = power.run_cells(cells, 6, 100, 3, 1)
        self.assertEqual([c["counts"] for c in whole], [c["counts"] for c in split])


class RefinementTests(unittest.TestCase):
    def test_near_target_rule(self):
        self.assertTrue(power.near_target(16, 20))  # 80% of 20: obviously near
        self.assertTrue(power.near_target(8, 8))  # 100% of 8 is not exact evidence
        self.assertFalse(power.near_target(20, 20))  # P(20/20 | power 0.8) is about 1%
        self.assertFalse(power.near_target(990, 1000))
        self.assertFalse(power.near_target(500, 1000))
        self.assertTrue(power.near_target(820, 1000))

    def test_refinement_defaults_are_production_strength(self):
        # Regression (T5 review, finding 8): near-threshold cells need >= 1,000 sims and 10,000 resamples.
        self.assertGreaterEqual(power.REFINE_SIMS, 1000)
        self.assertEqual(power.REFINE_BOOT, N_BOOT)
        self.assertEqual(N_BOOT, 10_000)
        self.assertEqual((power.REFINE_SIMS_FLOOR, power.REFINE_BOOT_FLOOR), (1000, 10_000))
        params = inspect.signature(power.run).parameters
        self.assertEqual(params["refine_sims"].default, power.REFINE_SIMS)
        self.assertEqual(params["refine_boot"].default, power.REFINE_BOOT)

    def refine_run(self):
        with mock.patch.object(power, "needs_refinement", lambda kind, cell: cell["scenario"] == "strong"):
            return power.run(
                sims=3,
                n_boot=100,
                jobs=1,
                seed=5,
                only=("e1",),
                e1_scenarios=power.E1_SCENARIOS[:2],
                e1_tasks=(10,),
                e1_trials=(8,),
                refine_sims=7,
                refine_boot=150,
            )

    def test_near_cells_are_refined_before_recommending(self):
        with mock.patch.multiple(power, REFINE_SIMS_FLOOR=7, REFINE_BOOT_FLOOR=150):
            result = self.refine_run()
        strong, legacy = result["e1"]
        self.assertTrue(strong["refined"])
        self.assertEqual((strong["sims"], strong["n_boot"]), (7, 150))
        self.assertEqual(strong["first_pass"]["sims"], 3)
        self.assertFalse(legacy["refined"])
        self.assertEqual(legacy["sims"], 3)
        self.assertFalse(result["exploratory"])
        self.assertIsNotNone(result["recommendation"])
        self.assertIn("7*", power.to_markdown(result))

    def test_refinement_below_the_floors_is_exploratory(self):
        # Regression (T5 round-2 review, finding 4): --refine-sims 7 --refine-boot 150 produced `refined` cells
        # and a recommendation. Below 1,000 sims or 10,000 resamples the cells are re-simulated but not refined,
        # and the run makes no recommendation.
        result = self.refine_run()
        strong, legacy = result["e1"]
        self.assertTrue(strong["resimulated"])
        self.assertEqual((strong["sims"], strong["n_boot"]), (7, 150))
        self.assertFalse(strong["refined"])
        self.assertFalse(legacy["refined"])
        self.assertTrue(result["exploratory"])
        self.assertIn("below the floors", result["exploratory_reason"])
        self.assertIsNone(result["recommendation"])
        markdown = power.to_markdown(result)
        self.assertIn("7†", markdown)
        self.assertNotIn("7*", markdown)
        self.assertIn("**Exploratory run**", markdown)
        self.assertIn("No recommendation: exploratory run", markdown)
        self.assertNotIn("smallest qualifying cell", markdown)
        # Each floor on its own makes a run exploratory.
        self.assertIsNotNone(power.exploratory_reason(999, N_BOOT))
        self.assertIsNotNone(power.exploratory_reason(1000, N_BOOT - 1))
        self.assertIsNone(power.exploratory_reason(1000, N_BOOT))

    def test_full_strength_first_pass_is_not_rerun(self):
        calls = []
        real = power.run_cells

        def counting(cells, sims, n_boot, seed, workers):
            calls.append((sims, n_boot))
            return real(cells, sims, n_boot, seed, workers)

        with (
            mock.patch.object(power, "needs_refinement", lambda kind, cell: True),
            mock.patch.object(power, "run_cells", counting),
            mock.patch.multiple(power, REFINE_SIMS_FLOOR=4, REFINE_BOOT_FLOOR=100),
        ):
            result = power.run(
                sims=4,
                n_boot=100,
                jobs=1,
                only=("e1",),
                e1_scenarios=power.E1_SCENARIOS[:1],
                e1_tasks=(10,),
                e1_trials=(8,),
                refine_sims=4,
                refine_boot=100,
            )
        self.assertEqual(calls, [(4, 100)])
        self.assertTrue(result["e1"][0]["refined"])

    def test_unrefined_near_target_cell_is_not_recommended(self):
        near = fake_cell("strong", 80, 10, {"P1": 170}, sims=200, n_boot=1000, trials_per_task=8)
        clear = fake_cell("strong", 192, 12, {"P1": 990}, trials_per_task=16)
        pick = power.smallest([near, clear], "P1", 0.8)
        assert pick is not None
        self.assertEqual(pick["total_trials"], 192)
        refined = fake_cell("strong", 80, 10, {"P1": 850}, trials_per_task=8)
        pick = power.smallest([refined, clear], "P1", 0.8)
        assert pick is not None
        self.assertEqual(pick["total_trials"], 80)
        # Near-target cells just below either floor do not qualify, whatever the run requested.
        for sims, n_boot in ((999, N_BOOT), (1000, N_BOOT - 1), (4000, 1000)):
            weak = fake_cell("strong", 80, 10, {"P1": round(0.81 * sims)}, sims=sims, n_boot=n_boot, trials_per_task=8)
            self.assertTrue(power.near_target(weak["counts"]["P1"]["supported"], sims))
            self.assertEqual(power.smallest([weak, clear], "P1", 0.8)["total_trials"], 192, (sims, n_boot))

    def test_recommendations_rest_only_on_the_stated_basis(self):
        # Regression (T5 round-2 review, finding 4): recommendations must rest only on figures that are refined.
        self.assertIs(power.REFINE_TARGETS, power.RECOMMENDATION_BASIS)
        self.assertEqual(
            power.RECOMMENDATION_BASIS,
            {"e1": {"strong": ("P1", "P2", "P3", "P4")}, "e2": {"effective": ("P5",)}, "e3": {"independent": ("H4",)}},
        )
        keys = {"P1": 990, "P2": 990, "P3": 990, "P4": 990}
        e1 = [
            fake_cell("moderate", 80, 10, keys, trials_per_task=8),  # high power, but not a basis scenario
            fake_cell("strong", 192, 12, keys, trials_per_task=16),
        ]
        e2 = [
            fake_cell("no-effect", 80, 10, {"P5": 990}, trials_per_arm=4),
            fake_cell("effective", 96, 12, {"P5": 990}, trials_per_arm=4),
        ]
        e3 = [
            fake_cell("shared-trial", 60, 6, {"H4": 990}, single_per_task=4, trials_per_breadth=18),
            fake_cell("independent", 72, 6, {"H4": 990}, single_per_task=4, trials_per_breadth=24),
        ]
        rec = power.recommend(e1, e2, e3)
        self.assertEqual(rec["e1"]["total_trials"], 192)
        self.assertEqual(
            {k: v["total_trials"] for k, v in rec["e1_p2_p3_p4"].items()}, dict.fromkeys(keys.keys() - {"P1"}, 192)
        )
        self.assertEqual(rec["e2"]["total_trials"], 96)
        self.assertEqual(rec["e3_any"]["total_trials"], 72)
        self.assertEqual(rec["e3_best_within_budget"]["total_trials"], 72)

    def test_refinement_covers_every_basis_figure(self):
        cell = fake_cell("strong", 80, 10, {"P1": 198, "P2": 198, "P3": 198, "P4": 162}, sims=200, trials_per_task=8)
        self.assertTrue(power.needs_refinement("e1", cell))
        other = fake_cell("moderate", 80, 10, {"P1": 160}, sims=200, trials_per_task=8)
        self.assertFalse(power.needs_refinement("e1", other))  # descriptive scenario: reported, never refined

    def test_e3_is_sized_on_joint_h4_power(self):
        # Regression (T5 review, finding 7): both breadths at 85% marginal power give only ~72% joint power.
        e3 = dict(single_per_task=4)
        marginal_only = fake_cell(
            "independent", 72, 6, {"P6-B2": 850, "P6-B4": 850, "H4": 720}, trials_per_breadth=24, **e3
        )
        joint = fake_cell("independent", 120, 6, {"P6-B2": 920, "P6-B4": 930, "H4": 860}, trials_per_breadth=48, **e3)
        rec = power.recommend([], [], [marginal_only, joint])
        self.assertEqual(rec["e3_any"]["total_trials"], 120)
        self.assertAlmostEqual(rec["e3_any"]["power"], 0.86)
        self.assertIsNone(rec["e3_within_budget"])
        self.assertEqual(rec["e3_best_within_budget"]["total_trials"], 72)


class RunTests(unittest.TestCase):
    def small_run(self):
        return power.run(
            sims=3,
            n_boot=100,
            jobs=1,
            seed=5,
            e1_scenarios=power.E1_SCENARIOS[:1],
            e2_scenarios=power.E2_SCENARIOS[:1],
            e3_scenarios=power.E3_SCENARIOS[:1],
            e1_tasks=(10,),
            e1_trials=(8,),
            e2_tasks=(10,),
            e2_per_arm=(4,),
            e3_tasks=(6,),
            e3_single=(4,),
            e3_multi=(12,),
            refine_sims=4,
            refine_boot=100,
        )

    def test_run_is_deterministic_and_recommends(self):
        first = self.small_run()
        second = self.small_run()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertTrue(first["exploratory"])
        self.assertIsNone(first["recommendation"])
        with mock.patch.multiple(power, REFINE_SIMS_FLOOR=4, REFINE_BOOT_FLOOR=100):
            floored = self.small_run()
        self.assertIn("e1", floored["recommendation"])
        self.assertIn("Recommendation (target power", power.to_markdown(floored))
        markdown = power.to_markdown(first)
        self.assertIn("Recommendations rest only on supported power for: E1 strong: P1, P2, P3, P4", markdown)
        self.assertIn("| strong | 10 | 8 | 80 |", markdown)
        self.assertIn("| effective | 10 | 4 | 80 |", markdown)
        self.assertIn("H4 (joint)", markdown)

    def test_cli_small_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "power.json"
            out = io.StringIO()
            args = ["--sims", "2", "--n-boot", "50", "--jobs", "1", "--json", str(path), "--refine-sims", "2"]
            args += ["--refine-boot", "50", "--e1-tasks", "10", "--e1-trials", "8", "--e2-tasks", "10"]
            args += ["--e2-per-arm", "4", "--e3-tasks", "6", "--e3-single", "4", "--e3-multi", "12"]
            with contextlib.redirect_stdout(out):
                power.main(args)
            result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["sims"], 2)
        self.assertEqual(len(result["e1"]), len(power.E1_SCENARIOS))
        self.assertEqual(len(result["e2"]), len(power.E2_SCENARIOS))
        self.assertEqual(len(result["e3"]), len(power.E3_SCENARIOS))
        self.assertTrue(result["exploratory"])
        self.assertIn("No recommendation: exploratory run", out.getvalue())

    def test_cli_rejects_unknown_experiment(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            power.main(["--only", "e4"])


if __name__ == "__main__":
    unittest.main()
