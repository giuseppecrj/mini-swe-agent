"""Offline tests for the power simulation. No model or Pi calls."""

import contextlib
import io
import json
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import autonomy_lab.power as power
from autonomy_lab.analysis import CHANNELS


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
        settled = [o.result for o in obs if o.channel != "none"]
        unsettled = [o.result for o in obs if o.channel == "none"]
        self.assertAlmostEqual(sum(settled) / len(settled), 0.95, delta=0.02)
        self.assertAlmostEqual(sum(unsettled) / len(unsettled), 0.30, delta=0.04)

    def test_e3_independent_gap_is_zero_and_interference_negative(self):
        by_name = {s.name: s for s in power.E3_SCENARIOS}
        self.assertAlmostEqual(power.e3_true_gap(by_name["independent"], 2, n=50_000), 0.0, delta=0.01)
        self.assertLess(power.e3_true_gap(by_name["interference"], 2, n=50_000), -0.15)
        self.assertGreater(power.e3_true_gap(by_name["shared-trial"], 4, n=50_000), 0.15)


class SimulationTests(unittest.TestCase):
    def test_strong_effect_is_powered_and_null_is_not_supported(self):
        by_name = {s.name: s for s in power.E1_SCENARIOS}
        strong = power.run_e1_cell((by_name["strong"], 12, 16, 20, 300, 0))
        null = power.run_e1_cell((by_name["null"], 12, 16, 20, 300, 0))
        execution = power.run_e1_cell((by_name["execution"], 12, 16, 20, 300, 0))
        self.assertGreaterEqual(strong["rates"]["P1"]["supported"], 0.8)
        self.assertEqual(strong["rates"]["P1"]["falsified"], 0.0)
        self.assertEqual(null["rates"]["P1"]["supported"], 0.0)
        self.assertGreater(null["rates"]["P1"]["falsified"], 0.5)
        self.assertEqual(execution["rates"]["P1"]["supported"], 0.0)

    def test_e3_cell_reports_both_breadths(self):
        cell = power.run_e3_cell((power.E3_SCENARIOS[1], 6, 8, 48, 10, 300, 0))
        self.assertEqual(cell["total_trials"], 6 * 8 + 2 * 48)
        self.assertEqual(set(cell["rates"]), {"P6-B2", "P6-B4", "H4"})
        self.assertGreater(cell["rates"]["P6-B2"]["falsified"], 0.0)

    def test_run_is_deterministic_and_recommends(self):
        def small_run():
            return power.run(
                sims=3,
                n_boot=100,
                jobs=1,
                seed=5,
                e1_scenarios=power.E1_SCENARIOS[:1],
                e3_scenarios=power.E3_SCENARIOS[:1],
                e1_tasks=(10,),
                e1_trials=(8,),
                e3_tasks=(6,),
                e3_single=(4,),
                e3_multi=(12,),
            )

        first = small_run()
        second = small_run()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertIn("e1", first["recommendation"])
        self.assertIn("| strong | 10 | 8 | 80 |", power.to_markdown(first))

    def test_cli_small_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "power.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                power.main(
                    [
                        "--sims",
                        "2",
                        "--n-boot",
                        "50",
                        "--jobs",
                        "1",
                        "--json",
                        str(path),
                        "--e1-tasks",
                        "10",
                        "--e1-trials",
                        "8",
                        "--e3-tasks",
                        "6",
                        "--e3-single",
                        "4",
                        "--e3-multi",
                        "12",
                    ]
                )
            result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["sims"], 2)
        self.assertEqual(len(result["e1"]), len(power.E1_SCENARIOS))
        self.assertEqual(len(result["e3"]), len(power.E3_SCENARIOS))
        self.assertIn("Recommendation", out.getvalue())


if __name__ == "__main__":
    unittest.main()
