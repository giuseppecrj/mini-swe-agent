"""Offline tests for task-bank loading, hashing, validation and per-decision grading (T1, LOOP_SPEC §4.2a).

Checks run inside the bubblewrap grading sandbox when bwrap works on this host, and unconfined otherwise. Tests that
are only meaningful under the sandbox (escapes, network) skip without it.
"""

import contextlib
import hashlib
import io
import itertools
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from autonomy_lab import taskbank
from autonomy_lab.confine import bwrap_unavailable_reason, grading_sandbox_or_none
from autonomy_lab.taskbank import (
    GradingError,
    TaskBankError,
    default_grading_sandbox,
    grade_modules,
    grade_workspace,
    load_bank,
    load_bank_with_hash,
    read_module,
    run_check,
    task_bank_sha256,
    validate_bank,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_BANK = FIXTURES / "bank_valid"
INVALID_BANK = FIXTURES / "bank_invalid"
LAB = Path(__file__).resolve().parents[1]
BWRAP_REASON = bwrap_unavailable_reason()
SANDBOX = grading_sandbox_or_none()
UNCONFINED_FLAG = [] if SANDBOX is not None else ["--unconfined-grading-for-tests"]

solution = runpy.run_path(str(FIXTURES / "fake_solutions.py"))["solution"]
SLUG_STARTER = {"lowercase": False, "separator": False, "empty": False, "strip": True}


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

    def test_symlinked_task_directory_is_rejected_not_skipped(self):
        # Review finding 4: os.walk did not descend into a directory symlink, so a bank holding one symlinked task
        # validated with the hash of an empty manifest, and editing that task left the hash unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / "bank"
            bank.mkdir()
            shutil.copytree(VALID_BANK / "slugify", Path(tmp) / "outside-slugify")
            (bank / "slugify").symlink_to(Path(tmp) / "outside-slugify", target_is_directory=True)
            with self.assertRaisesRegex(TaskBankError, "symlink"):
                task_bank_sha256(bank)
            with self.assertRaisesRegex(TaskBankError, "symlink"):
                load_bank(bank)
            report = validate_bank(bank, sandbox=SANDBOX)
            self.assertFalse(report["valid"])
            self.assertTrue(any("symlink" in e for e in report["errors"]), report["errors"])

    def test_symlinks_anywhere_inside_the_bank_are_rejected(self):
        for relative in ("slugify/reference.py", "slugify/mutants"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(relative=relative):
                bank = Path(tmp) / "bank"
                shutil.copytree(VALID_BANK, bank)
                target = bank / relative
                moved = Path(tmp) / "moved"
                target.rename(moved)
                target.symlink_to(moved, target_is_directory=moved.is_dir())
                with self.assertRaisesRegex(TaskBankError, "symlink|not a regular file"):
                    task_bank_sha256(bank)
                with self.assertRaises(TaskBankError):
                    load_bank(bank)
                self.assertFalse(validate_bank(bank, sandbox=SANDBOX)["valid"])

    def test_load_bank_with_hash_returns_the_hash_of_what_was_loaded(self):
        tasks, digest = load_bank_with_hash(VALID_BANK)
        self.assertEqual(digest, task_bank_sha256(VALID_BANK))
        self.assertEqual([t["id"] for t in tasks], ["clamp-range", "slugify"])
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / "bank"
            shutil.copytree(VALID_BANK, bank)
            real = taskbank.load_bank

            def edit_while_loading(path):
                loaded = real(path)
                (bank / "slugify" / "reference.py").write_text("def slug(text):\n    return text\n")
                return loaded

            with mock.patch.object(taskbank, "load_bank", side_effect=edit_while_loading):
                with self.assertRaisesRegex(TaskBankError, "changed while it was being loaded"):
                    load_bank_with_hash(bank)


class TaskBankValidationTests(unittest.TestCase):
    def test_fixture_bank_is_valid(self):
        report = validate_bank(VALID_BANK, sandbox=SANDBOX)
        self.assertTrue(report["valid"], json.dumps(report, indent=2))
        # Alternative mutants (LOOP_SPEC §4.1) are validated alongside the main mutant of their decision.
        slug_matrix = next(t["matrix"] for t in report["tasks"] if t["id"] == "slugify")
        self.assertIn("mutant:separator--none", slug_matrix)
        self.assertEqual(
            slug_matrix["mutant:empty--default"]["heldout"],
            {"lowercase": True, "separator": True, "empty": False, "strip": True},
        )
        self.assertEqual([t["id"] for t in report["tasks"]], ["clamp-range", "slugify"])
        self.assertEqual(report["task_bank_sha256"], task_bank_sha256(VALID_BANK))
        tasks = load_bank(VALID_BANK)
        self.assertEqual([t["id"] for t in tasks], ["clamp-range", "slugify"])
        self.assertEqual(tasks[1]["module_name"], "lab_slugger")

    def test_on_disk_invalid_fixture_is_rejected_with_clear_messages(self):
        report = validate_bank(INVALID_BANK, sandbox=SANDBOX)
        self.assertFalse(report["valid"])
        self.assertEqual(
            task_errors(report, "leaky-mutant"),
            [
                "mutant 'separator' also fails the held-out checks of 'lowercase'",
                "mutant 'separator' also fails the visible checks of 'lowercase'"
                " (those checks carry information about this decision)",
            ],
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
            report = validate_bank(bank, sandbox=SANDBOX)
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

    def test_visible_check_that_exits_early_fails_instead_of_passing(self):
        # Exit 0 without the completion token is not a pass, so a snippet that ends the process early fails on
        # the reference itself, and the assembled visible_checks.py, which it would truncate, fails too.
        def change(decisions):
            decisions[0]["visible_check"] = "import sys\nsys.exit(0)\n"

        messages = self.defect(self.edit_decisions(change), "reference fails the visible check of 'lowercase'")
        self.assertIn("reference fails the assembled visible_checks.py with every visible check", messages)
        self.assertNotIn("mutant 'lowercase' passes its own visible check", messages)

    def test_visible_check_cannot_create_state_for_a_held_out_check(self):
        # Review finding 3: a visible check created a marker that a held-out check required, and the bank validated,
        # although its reference then failed held-out grading in a clean workspace.
        marker = (
            "import os, tempfile\n"
            "for folder in ('.', os.path.expanduser('~'), tempfile.gettempdir()):\n"
            "    try:\n"
            "        open(os.path.join(folder, 'lab-shared-marker'), 'w').close()\n"
            "    except OSError:\n"
            "        pass\n"
        )
        needs = (
            "import os, tempfile\n"
            "assert any(os.path.exists(os.path.join(f, 'lab-shared-marker'))"
            " for f in ('.', os.path.expanduser('~'), tempfile.gettempdir()))\n"
        )

        def change(decisions):
            for decision in decisions:
                decision["visible_check"] += marker
            decisions[0]["heldout_check"] += needs

        self.defect(self.edit_decisions(change), "reference fails the held-out check of 'lowercase'")

    def test_starter_failing_too_few_arbitrary_decisions(self):
        def change(decisions):
            decisions[1]["label"] = "inferable"  # separator
            decisions[3]["label"] = "arbitrary"  # strip, which the starter passes

        def edit(task):
            self.edit_decisions(change)(task)
            # An arbitrary decision needs an alternative mutant too: strip only leading spaces.
            (task / "mutants" / "strip--leading.py").write_text(
                'def slug(text):\n    if text == "":\n        raise ValueError("empty")\n'
                '    return text.lstrip().lower().replace(" ", "-")\n'
            )

        self.defect(edit, "starter fails 1 arbitrary held-out checks; it must fail at least 2")

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
        self.defect(
            lambda task: (task / "mutants" / "extra--alt.py").write_text("x = 1\n"),
            "mutants/extra--alt.py does not correspond to a decision",
        )
        self.defect(
            lambda task: shutil.copy(task / "mutants" / "separator--none.py", task / "mutants" / "separator--Bad.py"),
            "alternative name 'Bad'",
        )

    def test_every_arbitrary_decision_needs_two_mutants(self):
        self.defect(
            lambda task: (task / "mutants" / "empty--default.py").unlink(),
            "arbitrary decision 'empty' has 1 mutant; it needs at least 2",
        )

    def test_alternative_mutants_must_isolate_their_decision(self):
        def copy_reference(task):
            shutil.copy(task / "reference.py", task / "mutants" / "separator--none.py")

        self.defect(copy_reference, "mutant 'separator--none' passes its own held-out check")

        def also_breaks_lowercase(task):
            (task / "mutants" / "separator--none.py").write_text(
                'def slug(text):\n    if text == "":\n        raise ValueError("empty")\n'
                '    return text.strip().replace(" ", "")\n'
            )

        messages = self.defect(also_breaks_lowercase, "mutant 'separator--none' also fails the held-out checks")
        self.assertIn(
            "mutant 'separator--none' also fails the visible checks of 'lowercase'"
            " (those checks carry information about this decision)",
            messages,
        )

        def passes_own_visible(task):
            decisions = json.loads((task / "decisions.json").read_text())
            decisions[1]["visible_check"] = "from lab_slugger import slug\nassert slug('xy') == 'xy'\n"
            (task / "decisions.json").write_text(json.dumps(decisions))

        self.defect(passes_own_visible, "mutant 'separator--none' passes its own visible check")

    def test_labels_md_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / "bank"
            shutil.copytree(VALID_BANK, bank)
            (bank / "slugify" / "LABELS.md").write_text("separator: alternatives none, underscore, dot, plus\n{")
            report = validate_bank(bank, sandbox=SANDBOX)
        self.assertTrue(report["valid"], report)

    def test_malformed_task_json(self):
        self.defect(lambda task: (task / "task.json").write_text("{"), "task.json is not valid JSON")
        self.defect(self.edit_meta(lambda m: m.pop("brief")), "task.json is missing brief")
        self.defect(self.edit_meta(lambda m: m.update(id="other")), "does not match directory name 'slugify'")
        self.defect(self.edit_meta(lambda m: m.update(author="someone")), "author 'someone' must be one of")
        self.defect(self.edit_meta(lambda m: m.update(version="1")), "version must be an integer")

    def test_module_that_shadows_the_standard_library(self):
        self.defect(self.edit_meta(lambda m: m.update(module="json.py")), "shadows a standard-library module")
        self.defect(self.edit_meta(lambda m: m.update(module="sys.py")), "shadows a standard-library module")

    def test_module_that_collides_with_a_harness_file(self):
        # Review finding 7: module "visible_checks.py" validated, then workspace preparation overwrote it.
        for name in ("visible_checks.py", "prompt.py", "sitecustomize.py"):
            with self.subTest(name=name):
                self.defect(self.edit_meta(lambda m, name=name: m.update(module=name)), "collides with a harness file")

    def test_missing_module_field_is_reported_not_a_crash(self):
        self.defect(self.edit_meta(lambda m: m.pop("module")), "task.json is missing module")

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
            [sys.executable, "-m", "autonomy_lab.taskbank", "validate", str(VALID_BANK), *UNCONFINED_FLAG],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertTrue(ok.stdout.rstrip().endswith("VALID"))
        bad = subprocess.run(
            [sys.executable, "-m", "autonomy_lab.taskbank", "validate", str(INVALID_BANK), *UNCONFINED_FLAG],
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

    def test_cli_refuses_unconfined_grading_without_the_explicit_flag(self):
        with mock.patch.object(taskbank, "bwrap_unavailable_reason", return_value="bwrap is not installed"):
            with self.assertRaises(GradingError):
                default_grading_sandbox()
            self.assertIsNone(default_grading_sandbox(allow_unconfined=True))
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as caught:
                taskbank.main(["validate", str(VALID_BANK)])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("refusing to run agent-written code unconfined", err.getvalue())


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {task["module_name"]: task for task in load_bank(VALID_BANK)}
        self.slug = self.tasks["lab_slugger"]

    def grade_slug(self, module_text, extra=None):
        """Grade a workspace holding lab_slugger.py (and ``extra`` {relative path: text}) on the slugify task."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            if module_text is not None:
                (workspace / "lab_slugger.py").write_text(module_text)
            for relative, text in (extra or {}).items():
                (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
                (workspace / relative).write_text(text)
            results, details = grade_workspace(workspace, [self.slug], sandbox=SANDBOX)
        return results["slugify"], details["slugify"]

    def test_exactly_the_satisfied_decisions_pass(self):
        for module, task in self.tasks.items():
            ids = [d["id"] for d in task["decisions"]]
            for size in range(len(ids) + 1):
                for satisfied in itertools.combinations(ids, size):
                    with tempfile.TemporaryDirectory() as tmp, self.subTest(module=module, satisfied=satisfied):
                        (Path(tmp) / task["module"]).write_text(solution(module, satisfied))
                        results, details = grade_workspace(Path(tmp), [task], sandbox=SANDBOX)
                        self.assertEqual(results, {task["id"]: {d: d in satisfied for d in ids}})
                        self.assertEqual(set(details[task["id"]]), set(ids))

    def test_multi_task_workspace_grades_each_task(self):
        tasks = list(self.tasks.values())
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lab_slugger.py").write_text(solution("lab_slugger", ["empty"]))
            (Path(tmp) / "lab_ranger.py").write_text(solution("lab_ranger", ["order", "bool", "inclusive"]))
            results, _ = grade_workspace(Path(tmp), tasks, sandbox=SANDBOX)
        self.assertEqual(
            results,
            {
                "clamp-range": {"order": True, "bool": True, "open-bound": False, "inclusive": True},
                "slugify": {"lowercase": False, "separator": False, "empty": True, "strip": False},
            },
        )

    def test_import_time_exit_does_not_pass(self):
        # Review finding 1: a module that ends the process with status 0 at import passed every decision.
        for body in ("import os\nos._exit(0)\n", "import sys\nsys.exit(0)\n", "raise SystemExit\n"):
            with self.subTest(body=body):
                results, _ = self.grade_slug(body + self.slug["reference"])
                self.assertEqual(results, {d: False for d in results})

    def test_a_forged_completion_token_does_not_pass(self):
        outcome = run_check("m.py", "", "print('lab-done-' + '0' * 32)\nimport os\nos._exit(0)\n", sandbox=SANDBOX)
        self.assertFalse(outcome["passed"])
        self.assertEqual(outcome["exit_code"], 0)
        honest = run_check("m.py", "x = 1\n", "import m\nassert m.x == 1\nprint('fine')\n", sandbox=SANDBOX)
        self.assertTrue(honest["passed"], honest["output_tail"])
        self.assertEqual(honest["output_tail"].strip(), "fine")

    def test_only_the_designated_module_file_is_graded(self):
        # Review finding 7: a package with the module's import name shadowed the failing starter file.
        results, _ = self.grade_slug(self.slug["starter"], {"lab_slugger/__init__.py": self.slug["reference"]})
        self.assertEqual(results, SLUG_STARTER)
        # Other files in the workspace are never copied into a check's directory.
        results, _ = self.grade_slug(
            "from helper import slug\n", {"helper.py": self.slug["reference"], "visible_checks.py": "x = 1\n"}
        )
        self.assertEqual(results, {d: False for d in results})

    def test_a_module_that_is_not_a_regular_file_fails_every_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.py"
            outside.write_text(self.slug["reference"])
            for kind in ("symlink", "missing", "directory", "fifo", "huge"):
                workspace = Path(tmp) / kind
                workspace.mkdir()
                target = workspace / "lab_slugger.py"
                if kind == "symlink":
                    target.symlink_to(outside)
                elif kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                elif kind == "huge":
                    target.write_text(self.slug["reference"] + "#" * taskbank.MAX_MODULE_BYTES)
                with self.subTest(kind=kind):
                    started = time.monotonic()
                    results, details = grade_workspace(workspace, [self.slug], sandbox=SANDBOX)
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertEqual(results["slugify"], {d: False for d in results["slugify"]})
                    self.assertTrue(details["slugify"]["lowercase"]["output_tail"])
            self.assertIsNone(read_module(outside.parent, "outside.py")[1])

    def test_checks_share_no_state_and_no_bytecode_cache(self):
        # Review finding 3: candidate code poisoned the shared redirected bytecode cache, so later checks loaded a
        # planted reference. This module behaves like the starter unless it finds state left by an earlier check;
        # each run plants a reference .pyc for itself and markers in every writable place it can think of.
        poisoner = textwrap.dedent(
            """\
            import importlib._bootstrap_external as _external
            import importlib.util, os, tempfile
            _PLACES = [".", os.path.expanduser("~"), tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"]
            _seen = any(os.path.exists(os.path.join(p, "lab-poison-marker")) for p in _PLACES)
            for _place in _PLACES:
                try:
                    open(os.path.join(_place, "lab-poison-marker"), "w").close()
                except OSError:
                    pass
            try:
                _pyc = importlib.util.cache_from_source(__file__)
                os.makedirs(os.path.dirname(_pyc), exist_ok=True)
                _stat = os.stat(__file__)
                _code = compile(REFERENCE, __file__, "exec")
                with open(_pyc, "wb") as _handle:
                    _handle.write(_external._code_to_timestamp_pyc(_code, _stat.st_mtime, _stat.st_size))
            except OSError:
                pass
            if _seen:
                exec(REFERENCE)
            else:
                exec(STARTER)
            """
        )
        module = f"REFERENCE = {self.slug['reference']!r}\nSTARTER = {self.slug['starter']!r}\n" + poisoner
        results, _ = self.grade_slug(module)
        self.assertEqual(results, SLUG_STARTER)
        # The same holds during validation: a poisoned starter fails exactly what the clean starter fails.
        with tempfile.TemporaryDirectory() as tmp:
            bank = Path(tmp) / "bank"
            shutil.copytree(VALID_BANK, bank)
            (bank / "slugify" / "starter.py").write_text(module)
            report = validate_bank(bank, sandbox=SANDBOX)
        slug_report = next(t for t in report["tasks"] if t["id"] == "slugify")
        self.assertEqual(slug_report["matrix"]["starter"]["heldout"], SLUG_STARTER)
        self.assertTrue(report["valid"], report)

    def test_check_timeout_fails_the_decision(self):
        outcome = run_check("m.py", "", "while True:\n    pass\n", timeout=1, sandbox=SANDBOX)
        self.assertFalse(outcome["passed"])
        self.assertTrue(outcome["timed_out"])
        self.assertIsNone(outcome["exit_code"])
        self.assertLessEqual(outcome["seconds"], 1.3)

    def test_timeout_bounds_cleanup_even_when_a_descendant_keeps_the_pipes_open(self):
        # Review finding 8: after the group kill, a second unbounded communicate() waited for a detached child.
        detach = textwrap.dedent(
            """\
            import os, time
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    time.sleep(4)
                os._exit(0)
            os.wait()
            {tail}
            """
        )
        for tail, timeout in (("while True:\n    pass", 1), ("pass", 1)):
            for sandbox in {None, SANDBOX}:
                with self.subTest(tail=tail, sandboxed=sandbox is not None):
                    started = time.monotonic()
                    outcome = run_check("m.py", "", detach.format(tail=tail), timeout=timeout, sandbox=sandbox)
                    self.assertLessEqual(time.monotonic() - started, timeout + 0.3)
                    if sandbox is not None and tail == "pass":
                        # The PID namespace dies with the check, so the detached child cannot hold the pipes.
                        self.assertTrue(outcome["passed"], outcome)
                    elif tail != "pass":
                        self.assertTrue(outcome["timed_out"])

    def test_sandbox_setup_failure_is_not_a_candidate_failure(self):
        class BrokenSandbox:
            interpreter = sys.executable

            def wrap(self, command, check_dir):
                script = "import sys\nsys.stderr.write('bwrap: Can\\'t mount proc on /newroot/proc\\n')\nsys.exit(1)\n"
                return [sys.executable, "-c", script], dict(os.environ)

        outcome = run_check("m.py", "", "pass\n", sandbox=BrokenSandbox())
        self.assertFalse(outcome["passed"])
        self.assertIn("bwrap: Can't mount proc", outcome["sandbox_error"])
        modules = {"slugify": (self.slug["reference"].encode(), None)}
        with self.assertRaisesRegex(GradingError, "grading sandbox failed"):
            grade_modules(modules, [self.slug], sandbox=BrokenSandbox())
        # A candidate that writes the same text but has started cannot fake a sandbox failure.
        forged = run_check("m.py", "", "import sys\nsys.stderr.write('bwrap: no\\n')\nsys.exit(1)\n", sandbox=SANDBOX)
        self.assertIsNone(forged["sandbox_error"])
        self.assertFalse(forged["passed"])


@unittest.skipIf(BWRAP_REASON, f"bwrap unavailable: {BWRAP_REASON}")
class GradingSandboxTests(unittest.TestCase):
    """Review finding 2: grading ran agent-written code outside any confinement and exposed held-out source."""

    def test_candidate_code_cannot_reach_outside_its_check_directory(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as outside, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            leak = Path(outside) / "leaked-heldout.txt"
            escapes = textwrap.dedent(
                f"""\
                import __main__, os, socket, sys
                ESCAPED = []
                def attempt(name, action):
                    try:
                        action()
                    except Exception:
                        return
                    ESCAPED.append(name)
                def leak():
                    with open({str(leak)!r}, "w") as handle:
                        handle.write(repr(getattr(__main__, "_source", None)))
                def read_repo():
                    open({str(LAB / "runner.py")!r}).read()
                def list_home():
                    if os.listdir(os.path.expanduser("~")):
                        raise OSError("not empty")
                    os.listdir({str(Path.home())!r})
                    if "{Path(outside).name}" not in os.listdir({str(Path.home())!r}):
                        raise OSError("hidden")
                def connect():
                    socket.create_connection(("127.0.0.1", {listener.getsockname()[1]}), timeout=2).close()
                def write_root():
                    open("/etc/lab-probe", "w").close()
                def unix_socket():
                    socket.socket(socket.AF_UNIX).close()
                for name, action in [("leak", leak), ("read_repo", read_repo), ("list_home", list_home),
                                     ("connect", connect), ("write_root", write_root), ("unix", unix_socket)]:
                    attempt(name, action)
                if hasattr(__main__, "_source"):
                    ESCAPED.append("main_source")
                """
            )
            outcome = run_check(
                "probe.py", escapes, "import probe\nassert probe.ESCAPED == [], probe.ESCAPED\n", sandbox=SANDBOX
            )
            self.assertTrue(outcome["passed"], outcome["output_tail"])
            self.assertFalse(leak.exists())

    def test_the_escape_probe_would_notice_an_unconfined_check(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            probe = f"socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), timeout=2).close()\n"
            source = "import socket\n" + probe + f"open({str(LAB / 'runner.py')!r}).read()\n"
            self.assertTrue(run_check("m.py", "", source)["passed"])
            self.assertFalse(run_check("m.py", "", source, sandbox=SANDBOX)["passed"])

    def test_the_task_bank_and_results_directories_are_hidden(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as results:
            (Path(results) / "secret.txt").write_text("held-out")
            sandbox = taskbank.GradingSandbox(results_dirs=[results])
            source = f"import os\nassert not os.path.exists({str(Path(results) / 'secret.txt')!r})\n"
            self.assertTrue(run_check("m.py", "", source, sandbox=sandbox)["passed"])


if __name__ == "__main__":
    unittest.main()
