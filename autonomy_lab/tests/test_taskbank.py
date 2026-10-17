"""Offline tests for task-bank loading, hashing, validation and per-decision grading (T1)."""

import hashlib
import itertools
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autonomy_lab.taskbank import (
    grade_workspace,
    load_bank,
    run_check,
    task_bank_sha256,
    validate_bank,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_BANK = FIXTURES / "bank_valid"
INVALID_BANK = FIXTURES / "bank_invalid"

solution = runpy.run_path(str(FIXTURES / "fake_solutions.py"))["solution"]


def task_errors(report, task_id):
    for task in report["tasks"]:
        if task["id"] == task_id:
            return task["errors"]
    raise AssertionError(f"no report for {task_id}")


class TaskBankHashTests(unittest.TestCase):
    def test_hash_covers_sorted_paths_and_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bank"
            (root / "b").mkdir(parents=True)
            (root / "a.txt").write_bytes(b"x")
            (root / "b" / "c.txt").write_bytes(b"y")
            manifest = b"".join(
                rel + b"\0" + hashlib.sha256(content).hexdigest().encode() + b"\n"
                for rel, content in ((b"a.txt", b"x"), (b"b/c.txt", b"y"))
            )
            self.assertEqual(task_bank_sha256(root), hashlib.sha256(manifest).hexdigest())
            before = task_bank_sha256(root)
            (root / "b" / "c.txt").write_bytes(b"z")
            self.assertNotEqual(task_bank_sha256(root), before)
            (root / "b" / "c.txt").write_bytes(b"y")
            self.assertEqual(task_bank_sha256(root), before)
            (root / "b" / "c.txt").rename(root / "b" / "d.txt")
            self.assertNotEqual(task_bank_sha256(root), before)

    def test_hash_is_location_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "elsewhere"
            shutil.copytree(VALID_BANK, copy)
            self.assertEqual(task_bank_sha256(copy), task_bank_sha256(VALID_BANK))


class TaskBankValidationTests(unittest.TestCase):
    def test_fixture_bank_is_valid(self):
        report = validate_bank(VALID_BANK)
        self.assertTrue(report["valid"], json.dumps(report, indent=2))
        self.assertEqual([t["id"] for t in report["tasks"]], ["clamp-range", "slugify"])
        self.assertEqual(report["task_bank_sha256"], task_bank_sha256(VALID_BANK))
        tasks = load_bank(VALID_BANK)
        self.assertEqual([t["id"] for t in tasks], ["clamp-range", "slugify"])
        self.assertEqual(tasks[1]["module_name"], "lab_slugger")

    def test_on_disk_invalid_fixture_is_rejected_with_clear_messages(self):
        report = validate_bank(INVALID_BANK)
        self.assertFalse(report["valid"])
        self.assertEqual(
            task_errors(report, "leaky-mutant"), ["mutant 'separator' also fails the held-out checks of 'lowercase'"]
        )
        self.assertEqual(
            task_errors(report, "weak-starter"),
            [
                "starter fails 1 of 4 held-out checks; it must fail at least half",
                "starter fails 1 arbitrary held-out checks; it must fail at least 2",
            ],
        )

    def defect(self, edit, expected, task_id: str | None = "slugify"):
        """Copy the valid bank, apply one defect, and assert the validator names it."""
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / "bank"
            shutil.copytree(VALID_BANK, bank)
            edit(bank / "slugify")
            report = validate_bank(bank)
        self.assertFalse(report["valid"])
        messages = report["errors"] + (task_errors(report, task_id) if task_id else [])
        self.assertTrue(any(expected in m for m in messages), f"{expected!r} not in {messages}")
        # The untouched task stays valid, so each message is attributable.
        if task_id == "slugify":
            self.assertEqual(task_errors(report, "clamp-range"), [])
        return messages

    @staticmethod
    def edit_decisions(change):
        def edit(task):
            decisions = json.loads((task / "decisions.json").read_text())
            change(decisions)
            (task / "decisions.json").write_text(json.dumps(decisions))

        return edit

    @staticmethod
    def edit_meta(change):
        def edit(task):
            meta = json.loads((task / "task.json").read_text())
            change(meta)
            (task / "task.json").write_text(json.dumps(meta))

        return edit

    def test_reference_failing_a_check(self):
        def edit(task):
            shutil.copy(task / "starter.py", task / "reference.py")

        self.defect(edit, "reference fails the held-out check of 'lowercase'")

    def test_mutant_passing_its_own_heldout_check(self):
        def edit(task):
            shutil.copy(task / "reference.py", task / "mutants" / "empty.py")

        self.defect(edit, "mutant 'empty' passes its own held-out check")

    def test_mutant_passing_its_own_visible_check(self):
        def change(decisions):
            decisions[1]["visible_check"] = "from lab_slugger import slug\nassert slug('xy') == 'xy'\n"

        self.defect(self.edit_decisions(change), "mutant 'separator' passes its own visible check")

    def test_visible_check_that_exits_early_hides_later_checks(self):
        def change(decisions):
            decisions[0]["visible_check"] = "import sys\nsys.exit(0)\n"

        messages = self.defect(self.edit_decisions(change), "passes the assembled visible_checks.py")
        self.assertIn("mutant 'lowercase' passes its own visible check", messages)

    def test_starter_failing_too_few_arbitrary_decisions(self):
        def change(decisions):
            decisions[1]["label"] = "inferable"  # separator
            decisions[3]["label"] = "arbitrary"  # strip, which the starter passes

        self.defect(self.edit_decisions(change), "starter fails 1 arbitrary held-out checks; it must fail at least 2")

    def test_starter_breaking_a_preserve_decision(self):
        def edit(task):
            (task / "starter.py").write_text('def slug(text):\n    return text.replace(" ", "_")\n')

        self.defect(edit, "starter fails the held-out check of preserve decision 'strip'")

    def test_too_few_decisions(self):
        def edit(task):
            self.edit_decisions(lambda d: d.pop())(task)
            (task / "mutants" / "strip.py").unlink()

        self.defect(edit, "task has 3 decisions; it needs 4-8")

    def test_too_many_decisions(self):
        def edit(task):
            def change(decisions):
                for i in range(5):
                    decisions.append(dict(decisions[0], id=f"extra{i}"))

            self.edit_decisions(change)(task)
            for i in range(5):
                shutil.copy(task / "mutants" / "lowercase.py", task / "mutants" / f"extra{i}.py")

        self.defect(edit, "task has 9 decisions; it needs 4-8")

    def test_fewer_than_two_arbitrary_labels(self):
        def change(decisions):
            decisions[1]["label"] = "inferable"

        self.defect(self.edit_decisions(change), "task has 1 arbitrary decisions; it needs at least 2")

    def test_unknown_label(self):
        def change(decisions):
            decisions[0]["label"] = "optional"

        self.defect(self.edit_decisions(change), "label 'optional' must be one of inferable, arbitrary, preserve")

    def test_missing_and_stray_mutants(self):
        self.defect(lambda task: (task / "mutants" / "strip.py").unlink(), "missing mutants/strip.py")
        self.defect(
            lambda task: (task / "mutants" / "extra.py").write_text("x = 1\n"),
            "mutants/extra.py does not correspond to a decision",
        )

    def test_malformed_task_json(self):
        self.defect(lambda task: (task / "task.json").write_text("{"), "task.json is not valid JSON")
        self.defect(self.edit_meta(lambda m: m.pop("brief")), "task.json is missing brief")
        self.defect(self.edit_meta(lambda m: m.update(id="other")), "does not match directory name 'slugify'")
        self.defect(self.edit_meta(lambda m: m.update(author="someone")), "author 'someone' must be one of")
        self.defect(self.edit_meta(lambda m: m.update(version="1")), "version must be an integer")

    def test_module_that_shadows_the_standard_library(self):
        self.defect(self.edit_meta(lambda m: m.update(module="json.py")), "shadows a standard-library module")

    def test_duplicate_module_names_across_tasks(self):
        def edit(task):
            shutil.copytree(task, task.parent / "slugify-two")
            self.edit_meta(lambda m: m.update(id="slugify-two"))(task.parent / "slugify-two")

        self.defect(edit, "module lab_slugger.py is used by several tasks (slugify, slugify-two)", task_id=None)

    def test_filler_that_cannot_reach_the_length_band(self):
        self.defect(lambda task: (task / "neutral.md").write_text("Short.\n"), "neutral.md is too short")
        self.defect(lambda task: (task / "neutral.md").write_text("Long " * 120 + "\n"), "overshoots ±5%")
        self.defect(lambda task: (task / "neutral.md").write_text("\n\n"), "neutral.md has no sentences")

    def test_cli_reports_and_exit_codes(self):
        ok = subprocess.run(
            [sys.executable, "-m", "autonomy_lab.taskbank", "validate", str(VALID_BANK)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertTrue(ok.stdout.rstrip().endswith("VALID"))
        bad = subprocess.run(
            [sys.executable, "-m", "autonomy_lab.taskbank", "validate", str(INVALID_BANK)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ERROR mutant 'separator' also fails the held-out checks of 'lowercase'", bad.stdout)
        digest = subprocess.run(
            [sys.executable, "-m", "autonomy_lab.taskbank", "hash", str(VALID_BANK)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(digest.stdout.strip(), task_bank_sha256(VALID_BANK))


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {task["module_name"]: task for task in load_bank(VALID_BANK)}

    def test_exactly_the_satisfied_decisions_pass(self):
        for module, task in self.tasks.items():
            ids = [d["id"] for d in task["decisions"]]
            for size in range(len(ids) + 1):
                for satisfied in itertools.combinations(ids, size):
                    with tempfile.TemporaryDirectory() as tmp, self.subTest(module=module, satisfied=satisfied):
                        (Path(tmp) / task["module"]).write_text(solution(module, satisfied))
                        results, details = grade_workspace(Path(tmp), [task])
                        self.assertEqual(results, {task["id"]: {d: d in satisfied for d in ids}})
                        self.assertEqual(set(details[task["id"]]), set(ids))

    def test_multi_task_workspace_grades_each_task(self):
        tasks = list(self.tasks.values())
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lab_slugger.py").write_text(solution("lab_slugger", ["empty"]))
            (Path(tmp) / "lab_ranger.py").write_text(solution("lab_ranger", ["order", "bool", "inclusive"]))
            results, _ = grade_workspace(Path(tmp), tasks)
        self.assertEqual(
            results,
            {
                "clamp-range": {"order": True, "bool": True, "open-bound": False, "inclusive": True},
                "slugify": {"lowercase": False, "separator": False, "empty": True, "strip": False},
            },
        )

    def test_check_runs_isolated_in_the_workspace(self):
        task = self.tasks["lab_slugger"]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            shadow = Path(tmp) / "shadow"
            workspace.mkdir()
            shadow.mkdir()
            (workspace / "lab_slugger.py").write_text(task["starter"])
            # PYTHONPATH is ignored under -I, so this reference copy is never imported.
            (shadow / "lab_slugger.py").write_text(task["reference"])
            # A workspace file cannot shadow the standard library.
            (workspace / "json.py").write_text("raise RuntimeError('shadowed')\n")
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(shadow)}):
                results, _ = grade_workspace(workspace, [task])
                cwd = run_check(workspace, f"import json, os\nassert os.getcwd() == {str(workspace.resolve())!r}\n")
                where = run_check(workspace, "import pathlib\nassert pathlib.Path('lab_slugger.py').is_file()\n")
                isolated = run_check(workspace, "import sys\nassert sys.flags.isolated\n")
        self.assertEqual(results["slugify"], {"lowercase": False, "separator": False, "empty": False, "strip": True})
        self.assertTrue(cwd["passed"], cwd["output_tail"])
        self.assertTrue(where["passed"], where["output_tail"])
        self.assertTrue(isolated["passed"], isolated["output_tail"])

    def test_check_timeout_fails_the_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_check(Path(tmp), "while True:\n    pass\n", timeout=1)
        self.assertFalse(outcome["passed"])
        self.assertTrue(outcome["timed_out"])
        self.assertIsNone(outcome["exit_code"])


if __name__ == "__main__":
    unittest.main()
