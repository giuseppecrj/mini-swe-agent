"""Offline tests for the seeded design generators, prompts and visible checks (T2)."""

import time
import unittest
from collections import Counter
from pathlib import Path

from autonomy_lab.design import (
    ASSUME_SENTENCE,
    CHANNELS,
    PREAMBLE,
    DesignError,
    _Rng,
    all_contract_length,
    balanced_groups,
    build_prompt,
    build_visible_checks,
    canonical_json,
    filler_count,
    generate_design,
    p4_exposed,
    sibling_check_fraction,
    trial_files,
)
from autonomy_lab.runner import TASKS
from autonomy_lab.runner import build_prompt as runner_build_prompt
from autonomy_lab.taskbank import load_bank, task_bank_sha256

VALID_BANK = Path(__file__).resolve().parent / "fixtures" / "bank_valid"
FAKE_HASH = "0" * 64


def synthetic_task(task_id, decisions=4):
    """An in-memory task (design code never runs checks) with distinctive contract and check text."""
    module = task_id.replace("-", "_")
    return {
        "id": task_id,
        "module": module + ".py",
        "module_name": module,
        "brief": f"Fix the {task_id} helper so it behaves as described.",
        "version": 1,
        "author": "coordinator",
        "starter": "",
        "reference": "",
        "mutants": {},
        "decisions": [
            {
                "id": f"d{i}",
                "label": "arbitrary" if i < 2 else "inferable",
                "contract": f"CONTRACT-{task_id}-d{i}: the helper handles case {i} in the documented way.",
                "visible_check": f"import {module}\nassert {module}.case({i}) == 'VISIBLE-{task_id}-d{i}'\n",
                "heldout_check": f"import {module}\nassert {module}.case({i})\n",
            }
            for i in range(decisions)
        ],
        "neutral": [f"Neutral line {i} about the {task_id} helper, settling nothing." for i in range(60)],
    }


def single(task, channels, filler=False, arm=None):
    return build_prompt([task], {task["id"]: channels}, filler, arm)[0]


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.task = synthetic_task("alpha", 6)
        self.ids = [d["id"] for d in self.task["decisions"]]

    def test_preamble_is_the_runner_preamble_without_the_strength_sentence(self):
        for depth in ("shallow", "deep"):
            for verification in ("basic", "rich"):
                lines = runner_build_prompt(TASKS[:1], depth, verification).split("\n")
                self.assertEqual(tuple(lines[: len(PREAMBLE)]), PREAMBLE)
        prompt = single(self.task, {d: "both" for d in self.ids})
        self.assertNotIn("The included checks", prompt)

    def test_contracts_and_checks_follow_their_channel_in_decision_order(self):
        channels = {"d0": "both", "d1": "check", "d2": "contract", "d3": "none", "d4": "contract", "d5": "both"}
        prompt = single(self.task, channels)
        checks = build_visible_checks([self.task], {"alpha": channels})
        for did, channel in channels.items():
            self.assertEqual(f"CONTRACT-alpha-{did}" in prompt, channel in ("contract", "both"), did)
            self.assertEqual(f"VISIBLE-alpha-{did}" in checks, channel in ("check", "both"), did)
        shown = [line for line in prompt.split("\n") if line.startswith("CONTRACT-")]
        expected = [f"CONTRACT-alpha-{d}" for d in ("d0", "d2", "d4", "d5")]
        self.assertEqual([line.split(":")[0] for line in shown], expected)
        self.assertTrue(checks.startswith("import alpha\n"))
        self.assertEqual(checks.count("import alpha\n"), 1 + 3)  # smoke line + one per shown check

    def test_check_channel_adds_nothing_to_the_prompt_and_contract_nothing_to_the_checks(self):
        base = {d: "none" for d in self.ids}
        for filler in (False, True):
            for did in self.ids:
                with self.subTest(decision=did, filler=filler):
                    as_check = dict(base, **{did: "check"})
                    as_contract = dict(base, **{did: "contract"})
                    as_both = dict(base, **{did: "both"})
                    self.assertEqual(single(self.task, as_check, filler), single(self.task, base, filler))
                    self.assertEqual(single(self.task, as_both, filler), single(self.task, as_contract, filler))
                    none_checks = build_visible_checks([self.task], {"alpha": base})
                    self.assertEqual(build_visible_checks([self.task], {"alpha": as_contract}), none_checks)
                    self.assertEqual(
                        build_visible_checks([self.task], {"alpha": as_both}),
                        build_visible_checks([self.task], {"alpha": as_check}),
                    )

    def test_filler_brings_every_coverage_level_within_five_percent(self):
        target = all_contract_length(self.task)
        for mask in range(1 << len(self.ids)):
            channels = {d: ("contract" if mask >> i & 1 else "none") for i, d in enumerate(self.ids)}
            prompt = single(self.task, channels, filler=True)
            self.assertTrue(0.95 * target <= len(prompt) <= 1.05 * target, (mask, len(prompt), target))
            self.assertNotIn("Neutral line", single(self.task, channels, filler=False))
        # All-contract needs no filler at all.
        self.assertEqual(filler_count(self.task, {d: "contract" for d in self.ids}), 0)
        # Whole sentences, in file order.
        prompt = single(self.task, {d: "none" for d in self.ids}, filler=True)
        used = [line for line in prompt.split("\n") if line.startswith("Neutral line")]
        self.assertEqual(used, self.task["neutral"][: len(used)])

    def test_filler_is_applied_per_task_in_multi_task_prompts(self):
        beta = synthetic_task("beta", 4)
        channels = {
            "alpha": {d: "none" for d in self.ids},
            "beta": {"d0": "contract", "d1": "none", "d2": "none", "d3": "none"},
        }
        prompt, counts = build_prompt([beta, self.task], channels, filler=True)
        self.assertEqual(counts["alpha"], filler_count(self.task, channels["alpha"]))
        self.assertEqual(counts["beta"], filler_count(beta, channels["beta"]))
        self.assertLess(prompt.index("Task: beta"), prompt.index("Task: alpha"))
        self.assertEqual(prompt.count(PREAMBLE[0]), 1)
        alpha_section = prompt[prompt.index("Task: alpha") :]
        self.assertEqual(alpha_section.count("Neutral line"), counts["alpha"])

    def test_assume_arm_appends_one_sentence(self):
        channels = {d: "none" for d in self.ids}
        plain = single(self.task, channels, arm="plain")
        assume = single(self.task, channels, arm="assume")
        self.assertEqual(plain, single(self.task, channels))
        self.assertEqual(assume, plain + "\n\n" + ASSUME_SENTENCE)

    def test_impossible_filler_is_an_error(self):
        short = dict(self.task, neutral=["Too short."])
        with self.assertRaises(DesignError):
            filler_count(short, {d: "none" for d in self.ids})
        long = dict(self.task, neutral=["x" * 2000])
        with self.assertRaisesRegex(DesignError, "overshoots"):
            filler_count(long, {d: "none" for d in self.ids})


class E1DesignTests(unittest.TestCase):
    def design(self, tasks, trials, seed=11, **kwargs):
        return generate_design(tasks, "E1", seed, FAKE_HASH, trials_per_task=trials, **kwargs)

    def test_balance_and_p4_exposure_hold_exactly(self):
        for decisions in (4, 5, 8):
            for trials in (8, 16, 24):
                task = synthetic_task(f"t{decisions}", decisions)
                design = self.design([task], trials)
                rows = [t for t in design["trials"] if t["task_ids"] == [task["id"]]]
                self.assertEqual(len(rows), trials)
                self.assertEqual(sum(t["filler"] for t in rows), trials // 2)
                channel_rows = [t["channels"][task["id"]] for t in rows]
                for decision in task["decisions"]:
                    did = decision["id"]
                    with self.subTest(decisions=decisions, trials=trials, decision=did):
                        cells = Counter(row[did] for row in channel_rows)
                        self.assertEqual(cells, Counter({c: trials // 4 for c in CHANNELS}))
                        on = Counter(t["channels"][task["id"]][did] for t in rows if t["filler"])
                        self.assertEqual(on, Counter({c: trials // 8 for c in CHANNELS}))
                        fractions = [
                            sibling_check_fraction(row, did) for row in channel_rows if row[did] == "none"
                        ]
                        self.assertTrue(any(k / n <= 0.4 for k, n in fractions), fractions)
                        self.assertTrue(any(k / n >= 0.6 for k, n in fractions), fractions)
                self.assertTrue(p4_exposed(channel_rows, [d["id"] for d in task["decisions"]]))
                self.assertLessEqual(design["generation"][task["id"]]["draws"], 100_000)

    def test_filler_on_prompts_are_within_five_percent_of_all_contract_length(self):
        tasks = load_bank(VALID_BANK)
        design = self.design(tasks, 16)
        by_id = {t["id"]: t for t in tasks}
        for trial in design["trials"]:
            task = by_id[trial["task_ids"][0]]
            target = all_contract_length(task)
            if trial["filler"]:
                self.assertTrue(0.95 * target <= trial["prompt_chars"] <= 1.05 * target, trial)
            else:
                self.assertEqual(trial["filler_sentences"], {task["id"]: 0})

    def test_same_seed_is_byte_identical_and_different_seeds_differ(self):
        tasks = load_bank(VALID_BANK)
        digest = task_bank_sha256(VALID_BANK)
        first = generate_design(tasks, "E1", 5, digest, trials_per_task=16)
        again = generate_design(list(reversed(tasks)), "E1", 5, digest, trials_per_task=16)
        self.assertEqual(canonical_json(first), canonical_json(again))
        by_id = {t["id"]: t for t in tasks}
        for a, b in zip(first["trials"], again["trials"]):
            self.assertEqual(trial_files(a, by_id), trial_files(b, by_id))
        other = generate_design(tasks, "E1", 6, digest, trials_per_task=16)
        self.assertNotEqual(canonical_json(first), canonical_json(other))
        self.assertNotEqual(first["design_id"], other["design_id"])
        self.assertNotEqual([t["channels"] for t in first["trials"]], [t["channels"] for t in other["trials"]])
        self.assertNotEqual(first["execution_order"], other["execution_order"])

    def test_design_is_pinned_across_python_versions(self):
        # Golden value: catches any change in seeding, shuffling or JSON layout,
        # including a Python upgrade that changed random's derived methods.
        design = self.design([synthetic_task("alpha", 4), synthetic_task("beta", 8)], 16, seed=2026)
        self.assertEqual(design["design_id"], GOLDEN_E1_DESIGN_ID)

    def test_execution_order_is_a_seeded_permutation(self):
        tasks = [synthetic_task("alpha"), synthetic_task("beta")]
        design = self.design(tasks, 8)
        ids = [t["trial_id"] for t in design["trials"]]
        self.assertEqual(sorted(design["execution_order"]), sorted(ids))
        self.assertNotEqual(design["execution_order"], ids)
        pinned = self.design(tasks, 8, order_seed=99)
        self.assertEqual(pinned["order_seed"], 99)
        self.assertEqual([t["channels"] for t in pinned["trials"]], [t["channels"] for t in design["trials"]])
        self.assertNotEqual(pinned["execution_order"], design["execution_order"])

    def test_trial_counts_must_be_multiples_of_eight(self):
        for trials in (0, 4, 12, 20):
            with self.assertRaises(DesignError):
                self.design([synthetic_task("alpha")], trials)

    def test_every_trial_has_a_distinct_recorded_seed(self):
        design = self.design([synthetic_task("alpha"), synthetic_task("beta")], 16)
        seeds = [t["seed"] for t in design["trials"]]
        self.assertEqual(len(set(seeds)), len(seeds))
        self.assertTrue(all(isinstance(s, int) and 0 <= s < 2**63 for s in seeds))


GOLDEN_E1_DESIGN_ID = "E1-232ebd62a590"


class OtherDesignTests(unittest.TestCase):
    def setUp(self):
        self.tasks = [synthetic_task(name, 4 + i) for i, name in enumerate(("alpha", "beta", "gamma", "delta"))]

    def test_e2_is_all_none_without_filler_and_balances_arms_within_task(self):
        design = generate_design(self.tasks, "E2", 3, FAKE_HASH, trials_per_task=8)
        self.assertEqual(len(design["trials"]), 32)
        for task in self.tasks:
            rows = [t for t in design["trials"] if t["task_ids"] == [task["id"]]]
            self.assertEqual(Counter(t["arm"] for t in rows), Counter({"plain": 4, "assume": 4}))
            for trial in rows:
                self.assertFalse(trial["filler"])
                self.assertEqual(set(trial["channels"][task["id"]].values()), {"none"})
        with self.assertRaises(DesignError):
            generate_design(self.tasks, "E2", 3, FAKE_HASH, trials_per_task=7)

    def test_e3_uses_each_task_equally_often_per_breadth(self):
        unsettled = {"alpha": "d0", "beta": "d1", "gamma": "d0", "delta": "d1"}
        counts = {1: 8, 2: 6, 4: 3}
        design = generate_design(self.tasks, "E3", 4, FAKE_HASH, e3_unsettled=unsettled, e3_breadth_trials=counts)
        by_id = {t["id"]: t for t in self.tasks}
        for breadth, trials in counts.items():
            rows = [t for t in design["trials"] if t["breadth"] == breadth]
            self.assertEqual(len(rows), trials)
            uses = Counter(task_id for t in rows for task_id in t["task_ids"])
            self.assertEqual(uses, Counter({task_id: trials * breadth // 4 for task_id in unsettled}))
            for trial in rows:
                self.assertEqual(len(set(trial["task_ids"])), breadth)
                self.assertFalse(trial["filler"])
                for task_id in trial["task_ids"]:
                    expected = {
                        d["id"]: ("none" if d["id"] == unsettled[task_id] else "contract")
                        for d in by_id[task_id]["decisions"]
                    }
                    self.assertEqual(trial["channels"][task_id], expected)
                prompt, checks, _ = trial_files(trial, by_id)
                positions = [prompt.index(f"Task: {task_id} (") for task_id in trial["task_ids"]]
                self.assertEqual(positions, sorted(positions))
                self.assertNotIn("VISIBLE-", checks)
        orders = {tuple(t["task_ids"]) for t in design["trials"] if t["breadth"] == 2}
        self.assertGreater(len(orders), 1)

    def test_e3_construction_is_balanced_fast_and_deterministic_where_rejection_sampling_failed(self):
        # Review finding 9: six tasks, seed 1 and breadth counts {1: 12, 2: 12, 4: 60} exhausted 100,000 draws.
        tasks = [synthetic_task(f"t{i}") for i in range(6)]
        unsettled = {task["id"]: "d0" for task in tasks}
        counts = {1: 12, 2: 12, 4: 60}
        started = time.monotonic()
        design = generate_design(tasks, "E3", 1, FAKE_HASH, e3_unsettled=unsettled, e3_breadth_trials=counts)
        self.assertLess(time.monotonic() - started, 5)
        for breadth, trials in counts.items():
            rows = [t["task_ids"] for t in design["trials"] if t["breadth"] == breadth]
            self.assertEqual(len(rows), trials)
            self.assertTrue(all(len(set(row)) == breadth for row in rows))
            self.assertEqual(
                Counter(t for row in rows for t in row), Counter({t: trials * breadth // 6 for t in unsettled})
            )
            self.assertEqual(design["generation"][f"b{breadth}"]["method"], "constructive")
        again = generate_design(tasks, "E3", 1, FAKE_HASH, e3_unsettled=unsettled, e3_breadth_trials=counts)
        self.assertEqual(canonical_json(design), canonical_json(again))
        other = generate_design(tasks, "E3", 2, FAKE_HASH, e3_unsettled=unsettled, e3_breadth_trials=counts)
        self.assertNotEqual([t["task_ids"] for t in design["trials"]], [t["task_ids"] for t in other["trials"]])

    def test_balanced_groups_never_fail_on_feasible_parameters(self):
        for n in range(1, 9):
            ids = [f"t{i}" for i in range(n)]
            for breadth in range(1, n + 1):
                for count in range(1, 25):
                    if (count * breadth) % n:
                        continue
                    for seed in range(3):
                        with self.subTest(n=n, breadth=breadth, count=count, seed=seed):
                            groups = balanced_groups(ids, breadth, count, _Rng(seed))
                            self.assertEqual(len(groups), count)
                            self.assertTrue(all(len(set(g)) == breadth for g in groups))
                            uses = Counter(t for g in groups for t in g)
                            self.assertEqual(set(uses.values()), {count * breadth // n})

    def test_e3_rejects_bad_parameters(self):
        good = {"alpha": "d0", "beta": "d0"}
        cases = [
            ({"alpha": "d3"}, {1: 1}, "must be labelled arbitrary"),
            ({"alpha": "nope"}, {1: 1}, "has no decision"),
            ({"omega": "d0"}, {1: 1}, "not in the bank"),
            (good, {1: 3}, "equally often"),
            (good, {4: 2}, "between 1 and 2"),
        ]
        for unsettled, counts, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(DesignError, message):
                generate_design(self.tasks, "E3", 1, FAKE_HASH, e3_unsettled=unsettled, e3_breadth_trials=counts)

    def test_t7_is_one_balanced_block_of_eight(self):
        design = generate_design(self.tasks, "T7", 8, FAKE_HASH)
        self.assertEqual(len(design["trials"]), 8)
        task_id = design["parameters"]["task_id"]
        self.assertEqual({tuple(t["task_ids"]) for t in design["trials"]}, {(task_id,)})
        rows = [t["channels"][task_id] for t in design["trials"]]
        for did in rows[0]:
            self.assertEqual(Counter(row[did] for row in rows), Counter({c: 2 for c in CHANNELS}))
        self.assertEqual(sum(t["filler"] for t in design["trials"]), 4)
        chosen = generate_design(self.tasks, "T7", 8, FAKE_HASH, t7_task="gamma")
        self.assertEqual(chosen["parameters"]["task_id"], "gamma")

    def test_t8_has_two_none_and_one_all_contract_trial_per_task(self):
        design = generate_design(self.tasks, "T8", 8, FAKE_HASH)
        self.assertEqual(len(design["trials"]), 12)
        for task in self.tasks:
            rows = [t for t in design["trials"] if t["task_ids"] == [task["id"]]]
            kinds = Counter(frozenset(t["channels"][task["id"]].values()) for t in rows)
            self.assertEqual(kinds, Counter({frozenset({"none"}): 2, frozenset({"contract"}): 1}))
            self.assertFalse(any(t["filler"] for t in rows))


if __name__ == "__main__":
    unittest.main()
