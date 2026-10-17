"""Offline tests for the Pi runner's task-bank mode. Never invokes Pi or a model."""

import argparse
import contextlib
import filecmp
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autonomy_lab.design import canonical_json, generate_design
from autonomy_lab.pi_runner import classify_failure, run_task_bank, scan_events
from autonomy_lab.taskbank import load_bank, task_bank_sha256

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_BANK = FIXTURES / "bank_valid"
INVALID_BANK = FIXTURES / "bank_invalid"
ALL_DECISIONS = {
    "lab_slugger": ["lowercase", "separator", "empty", "strip"],
    "lab_ranger": ["order", "bool", "open-bound", "inclusive"],
}

# Stands in for the Pi CLI in a task-bank workspace. It writes each module so
# that it satisfies exactly the decisions in FAKE_PI_SATISFY, logs what it saw
# to FAKE_PI_LOG, and emits the events for FAKE_PI_MODE.
FAKE_BANK_PI_MAIN = """
import json, os, pathlib, sys
if "--version" in sys.argv:
    print("fake-pi 0")
    sys.exit(0)
here = pathlib.Path.cwd()
with open(os.environ["FAKE_PI_LOG"], "a") as log:
    log.write(json.dumps({
        "workspace": here.name,
        "design_exists": (here.parent.parent / "design.json").is_file(),
        "prompt_matches": sys.argv[-1] == (here / "prompt.txt").read_text(),
        "marker": os.environ.get("FAKE_PI_MARKER"),
    }) + "\\n")
for module, decisions in json.loads(os.environ.get("FAKE_PI_SATISFY", "{}")).items():
    path = here / (module + ".py")
    if path.exists():
        path.write_text(solution(module, decisions))
mode = os.environ.get("FAKE_PI_MODE", "ok")
first = False
if os.environ.get("FAKE_PI_STATE"):
    first = not os.path.exists(os.environ["FAKE_PI_STATE"])
    open(os.environ["FAKE_PI_STATE"], "a").close()
provider, model = os.environ["FAKE_PI_MODEL"].split("/", 1)
assistant = {"role": "assistant", "provider": provider, "model": model,
             "thinkingLevel": os.environ["FAKE_PI_THINKING"], "stopReason": "stop"}
if mode == "error_once" and first:
    assistant = dict(assistant, stopReason="error", errorMessage="502 Bad Gateway")
if mode == "tamper":
    (here / "visible_checks.py").write_text("print('tampered')\\n")
for event in (
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_end", "message": assistant},
    {"type": "turn_end"},
    {"type": "agent_settled", "aborted": False},
):
    print(json.dumps(event))
"""


def assistant(stop="stop", **fields):
    message = {"role": "assistant", "provider": "openai", "model": "example", "thinkingLevel": "high"}
    return {"type": "message_end", "message": dict(message, stopReason=stop, **fields)}


START = [{"type": "agent_start"}, {"type": "turn_start"}]
SETTLED = {"type": "agent_settled", "aborted": False}
OK = START + [assistant(), {"type": "turn_end"}, SETTLED]
PASSED = {"t": {"a": True, "b": True}}


class FailureClassificationTests(unittest.TestCase):
    def classify(self, events, timed_out=False, exit_code=0, stderr="", results=PASSED, idle=None, tail="", **extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi-events.jsonl"
            lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
            path.write_text("".join(line + "\n" for line in lines) + tail, encoding="utf-8")
            run = {"timed_out": timed_out, "exit_code": None if timed_out else exit_code, "stderr_tail": stderr}
            return classify_failure(dict(run, **extra), scan_events(path), results, idle)

    def test_clean_run_has_no_failure(self):
        self.assertEqual(self.classify(OK), (None, None))

    def test_agent_failures(self):
        self.assertEqual(self.classify(OK, results={"t": {"a": True, "b": False}}), (None, "heldout_failed"))
        length = START + [assistant("length"), {"type": "turn_end"}, SETTLED]
        self.assertEqual(self.classify(length), (None, "length"))
        busy = START + [assistant(), {"type": "tool_execution_start", "toolCallId": "1", "toolName": "bash"}]
        self.assertEqual(self.classify(busy, timed_out=True, idle=300), (None, "timeout"))
        # A recent pending request is not yet a stall.
        self.assertEqual(self.classify(START, timed_out=True, idle=60), (None, "timeout"))

    def test_provider_stall_is_an_infrastructure_failure(self):
        self.assertEqual(self.classify(START, timed_out=True, idle=300), ("provider_stall", None))
        self.assertEqual(self.classify([], timed_out=True, idle=600), ("provider_stall", None))
        empty_reply = START + [{"type": "message_start", "message": {"role": "assistant", "content": []}}]
        self.assertEqual(self.classify(empty_reply, timed_out=True, idle=200), ("provider_stall", None))
        tool_result = START + [assistant(), {"type": "message_end", "message": {"role": "toolResult"}}]
        self.assertEqual(self.classify(tool_result, timed_out=True, idle=200), ("provider_stall", None))
        streaming = START + [
            {"type": "message_update", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
        ]
        self.assertEqual(self.classify(streaming, timed_out=True, idle=200), (None, "timeout"))

    def test_provider_side_failures(self):
        error = START + [assistant("error", errorMessage="500 Internal Server Error"), SETTLED]
        self.assertEqual(self.classify(error), ("provider_error", None))
        # Infrastructure takes precedence over the held-out outcome.
        self.assertEqual(self.classify(error, results={"t": {"a": False}}), ("provider_error", None))
        retry = START + [
            {"type": "auto_retry_start", "attempt": 1},
            {"type": "auto_retry_end", "success": False, "finalError": "overloaded"},
            SETTLED,
        ]
        self.assertEqual(self.classify(retry), ("retry_exhausted", None))
        rejected = START + [assistant("error", errorMessage="401 Unauthorized: token expired"), SETTLED]
        self.assertEqual(self.classify(rejected), ("auth", None))

    def test_launch_failures(self):
        self.assertEqual(self.classify([], exit_code=1, stderr="No API key found for openai"), ("auth", None))
        self.assertEqual(self.classify([], exit_code=1, stderr="Segmentation fault"), ("spawn", None))
        self.assertEqual(self.classify([], spawn_error="No such file or directory: 'pi'"), ("spawn", None))
        self.assertEqual(self.classify([], sandbox_error="RuntimeError: bwrap missing"), ("sandbox", None))

    def test_exit_without_settling(self):
        self.assertEqual(self.classify(OK[:-1]), ("no_settle", None))
        aborted = OK[:-1] + [{"type": "agent_settled", "aborted": True}]
        self.assertEqual(self.classify(aborted), ("no_settle", None))

    def test_malformed_event_streams(self):
        for bad in (
            '{"type": "message_end", "message": {',
            "not json",
            "5",
            {"no": "type"},
            {"type": "message_end"},
            {"type": "message_end", "message": "text"},
            {"type": "message_end", "message": {"content": []}},
            {"type": "auto_retry_end"},
            {"type": "agent_settled", "aborted": "no"},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.classify(OK[:2] + [bad] + OK[2:]), ("malformed_events", None))
                self.assertEqual(
                    self.classify(OK[:2] + [bad] + OK[2:], timed_out=True, idle=1), ("malformed_events", None)
                )

    def test_line_cut_short_by_the_timeout_kill_is_not_malformed(self):
        partial = '{"type": "message_update", "mess'
        self.assertEqual(self.classify(START + [assistant()], timed_out=True, idle=5, tail=partial), (None, "timeout"))
        # Without a timeout, nothing explains a truncated line.
        self.assertEqual(self.classify(OK, tail=partial), ("malformed_events", None))


def make_fake(tmp):
    fake = Path(tmp) / "fake-pi"
    solutions = (FIXTURES / "fake_solutions.py").read_text(encoding="utf-8")
    fake.write_text("#!" + sys.executable + "\n" + solutions + FAKE_BANK_PI_MAIN, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def bank_args(tmp, **overrides):
    values = {
        "model": "openai/example",
        "thinking": "high",
        "pi_binary": str(make_fake(tmp)),
        "timeout": 30,
        "out": Path(tmp) / "out",
        "task_bank": VALID_BANK,
        "experiment": "T8",
        "design_seed": 1,
        "order_seed": None,
        "trials_per_task": None,
        "e3_unsettled": None,
        "e3_breadths": "1,2,4",
        "e3_trials": None,
        "t7_task": None,
        "design_only": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


RECORD_FIELDS = {
    # LOOP_SPEC §4.3
    "experiment",
    "design_id",
    "seed",
    "task_ids",
    "breadth",
    "channels",
    "filler",
    "arm",
    "decision_results",
    "labels",
    "task_bank_sha256",
    "infrastructure_failure",
    "agent_failure",
    "confinement_signals",
    "attempt",
    # carried over from the 2x2x2 records
    "trial_id",
    "accepted",
    "held_out_tests_passed",
    "task_scope_intact",
    "agent_completed",
    "runtime_matches_request",
    "model_requested",
    "thinking_requested",
    "grader_exit_code",
    "exit_code",
    "timed_out",
    "seconds",
    "settled",
    "assistant_errors",
    "total_tokens",
    "reported_cost_usd",
    "model_ids",
    "thinking_levels",
    "stderr_tail",
}
# A two-trial design (each fixture task once at breadth 1) for tests that do not need more.
SMALL = {
    "experiment": "E3",
    "e3_unsettled": "slugify:separator,clamp-range:order",
    "e3_breadths": "1",
    "e3_trials": "2",
}


class TaskBankRunTests(unittest.TestCase):
    def run_bank(self, tmp, env=None, confine=None, **overrides):
        log = Path(tmp) / "fake-log.jsonl"
        values = {"FAKE_PI_MODEL": "openai/example", "FAKE_PI_THINKING": "high", "FAKE_PI_LOG": str(log)}
        values.update(env or {})
        with mock.patch.dict(os.environ, values), contextlib.redirect_stdout(io.StringIO()):
            root = run_task_bank(bank_args(tmp, **overrides), confine=confine)
        records = [json.loads(line) for line in (root / "trials.jsonl").read_text().splitlines()]
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return root, records, calls

    def test_fake_pi_satisfying_k_of_n_decisions_yields_exactly_those_passes(self):
        satisfied = {"lab_slugger": ["lowercase", "strip"], "lab_ranger": ["order", "bool", "inclusive"]}
        with tempfile.TemporaryDirectory() as tmp:
            root, records, calls = self.run_bank(tmp, {"FAKE_PI_SATISFY": json.dumps(satisfied)})
            design_text = (root / "design.json").read_text(encoding="utf-8")
        design = json.loads(design_text)
        self.assertEqual(len(records), 6)
        expected = {
            "slugify": {"lowercase": True, "separator": False, "empty": False, "strip": True},
            "clamp-range": {"order": True, "bool": True, "open-bound": False, "inclusive": True},
        }
        labels = {t["id"]: {d["id"]: d["label"] for d in t["decisions"]} for t in load_bank(VALID_BANK)}
        for record in records:
            self.assertTrue(RECORD_FIELDS <= set(record), RECORD_FIELDS - set(record))
            task_id = record["task_ids"][0]
            self.assertEqual(record["labels"], {task_id: labels[task_id]})
            self.assertEqual(record["decision_results"], {task_id: expected[task_id]})
            self.assertEqual((record["infrastructure_failure"], record["agent_failure"]), (None, "heldout_failed"))
            self.assertFalse(record["accepted"])
            self.assertTrue(record["runtime_matches_request"])
            self.assertEqual(record["attempt"], 1)
            self.assertEqual(record["confinement_signals"], [])
            self.assertEqual(record["experiment"], "T8")
            self.assertEqual(record["design_id"], design["design_id"])
            self.assertEqual(record["task_bank_sha256"], task_bank_sha256(VALID_BANK))
        # design.json existed before the first trial, and trials ran in its execution order.
        self.assertTrue(all(call["design_exists"] and call["prompt_matches"] for call in calls))
        self.assertEqual([r["trial_id"] for r in records], design["execution_order"])
        regenerated = generate_design(load_bank(VALID_BANK), "T8", 1, task_bank_sha256(VALID_BANK))
        self.assertEqual(design_text, canonical_json(regenerated))

    def test_all_decisions_satisfied_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, records, _ = self.run_bank(tmp, {"FAKE_PI_SATISFY": json.dumps(ALL_DECISIONS)})
            summary = json.loads((root / "summary.json").read_text())
        self.assertTrue(all(r["accepted"] and r["agent_failure"] is None for r in records))
        self.assertEqual(summary["accepted_final_attempt"], 6)
        self.assertIsNone(summary["stop_reason"])

    def test_multi_task_trials_grade_every_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, records, _ = self.run_bank(
                tmp,
                {"FAKE_PI_SATISFY": json.dumps(ALL_DECISIONS)},
                experiment="E3",
                e3_unsettled="slugify:separator,clamp-range:order",
                e3_breadths="1,2",
                e3_trials="2",
            )
        pairs = [r for r in records if r["breadth"] == 2]
        self.assertEqual(len(pairs), 2)
        for record in pairs:
            self.assertEqual(sorted(record["decision_results"]), ["clamp-range", "slugify"])
            self.assertEqual(record["channels"]["slugify"]["separator"], "none")
            self.assertTrue(record["accepted"])

    def test_infrastructure_failure_is_rerun_once_with_the_same_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FAKE_PI_MODE": "error_once", "FAKE_PI_STATE": str(Path(tmp) / "state")}
            root, records, _ = self.run_bank(tmp, env, **SMALL)
            summary = json.loads((root / "summary.json").read_text())
        self.assertEqual(len(records), 3)
        first, rerun = records[0], records[1]
        self.assertEqual((first["infrastructure_failure"], first["attempt"]), ("provider_error", 1))
        self.assertEqual((rerun["infrastructure_failure"], rerun["attempt"]), (None, 2))
        self.assertEqual((rerun["trial_id"], rerun["seed"]), (first["trial_id"], first["seed"]))
        self.assertFalse(first["accepted"])
        self.assertEqual(summary["infrastructure_failures_first_attempt"], {"provider_error": 1})
        self.assertEqual(summary["infrastructure_reruns"], 1)
        self.assertEqual(summary["completed_trials"], 2)

    def test_confinement_hook_wraps_the_pi_command(self):
        seen = []

        def confine(command, workspace):
            seen.append((command[0], workspace.name))
            return command, dict(os.environ, FAKE_PI_MARKER="confined")

        with tempfile.TemporaryDirectory() as tmp:
            _, records, calls = self.run_bank(tmp, confine=confine, **SMALL)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(call["marker"] == "confined" for call in calls))
        self.assertTrue(all(r["infrastructure_failure"] is None for r in records))

    def test_confinement_hook_error_is_a_sandbox_failure(self):
        def confine(command, workspace):
            raise RuntimeError("bwrap: setting up uid map: Permission denied")

        with tempfile.TemporaryDirectory() as tmp:
            _, records, calls = self.run_bank(tmp, confine=confine, **SMALL)
        self.assertEqual(calls, [])
        self.assertEqual(len(records), 4)
        self.assertEqual({r["infrastructure_failure"] for r in records}, {"sandbox"})
        self.assertEqual([r["attempt"] for r in records[:2]], [1, 2])
        self.assertIn("Permission denied", records[0]["stderr_tail"])

    def test_changed_visible_checks_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, records, _ = self.run_bank(tmp, {"FAKE_PI_MODE": "tamper"})
            summary = json.loads((root / "summary.json").read_text())
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["spec_files_intact"])
        self.assertFalse(records[0]["task_scope_intact"])
        self.assertFalse(records[0]["accepted"])
        self.assertIn("safety stop", summary["stop_reason"])

    def test_invalid_bank_is_refused_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "failed mechanical validation"):
                run_task_bank(bank_args(tmp, task_bank=INVALID_BANK))
            self.assertFalse((Path(tmp) / "out").exists())


class TaskBankCliTests(unittest.TestCase):
    def design_only(self, out, seed):
        command = [
            sys.executable,
            "-m",
            "autonomy_lab.pi_runner",
            "--model",
            "openai/example",
            "--thinking",
            "high",
            "--task-bank",
            str(VALID_BANK),
            "--experiment",
            "E1",
            "--design-seed",
            str(seed),
            "--trials-per-task",
            "16",
            "--design-only",
            "--out",
            str(out),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.split("DESIGN ", 1)[1].strip())

    def assert_same_tree(self, left, right):
        comparison = filecmp.dircmp(left, right)
        self.assertEqual(comparison.left_only + comparison.right_only + comparison.diff_files, [])
        for name in comparison.common_dirs:
            self.assert_same_tree(Path(left) / name, Path(right) / name)

    def test_same_seed_gives_byte_identical_design_prompts_and_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.design_only(Path(tmp) / "a", 9)
            second = self.design_only(Path(tmp) / "b", 9)
            other = self.design_only(Path(tmp) / "c", 10)
            self.assertEqual((first / "design.json").read_bytes(), (second / "design.json").read_bytes())
            self.assert_same_tree(first / "prompts", second / "prompts")
            self.assertEqual(len(list((first / "prompts").iterdir())), 32)
            self.assertNotEqual((first / "design.json").read_bytes(), (other / "design.json").read_bytes())
            differing = [
                p.name
                for p in (first / "prompts").iterdir()
                if (p / "prompt.txt").read_bytes() != (other / "prompts" / p.name / "prompt.txt").read_bytes()
                or (p / "visible_checks.py").read_bytes()
                != (other / "prompts" / p.name / "visible_checks.py").read_bytes()
            ]
            self.assertTrue(differing)

    def test_experiment_without_task_bank_is_rejected(self):
        command = [sys.executable, "-m", "autonomy_lab.pi_runner", "--model", "a/b", "--thinking", "high"]
        result = subprocess.run(
            command + ["--experiment", "E1"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--experiment needs --task-bank", result.stderr)


if __name__ == "__main__":
    unittest.main()
