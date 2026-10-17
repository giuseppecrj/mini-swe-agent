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

from autonomy_lab import confine as confinement
from autonomy_lab import pi_runner
from autonomy_lab.confine import HostAuthError, grading_sandbox_or_none
from autonomy_lab.design import canonical_json, generate_design
from autonomy_lab.pi_runner import classify_failure, parse_pi_events, run_task_bank, scan_events
from autonomy_lab.taskbank import GradingError, load_bank, task_bank_sha256

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VALID_BANK = FIXTURES / "bank_valid"
INVALID_BANK = FIXTURES / "bank_invalid"
LAB = Path(__file__).resolve().parents[1]
# Grading runs in the bubblewrap grading sandbox wherever bwrap works; elsewhere the tests opt out explicitly.
SANDBOX_AVAILABLE = grading_sandbox_or_none() is not None
UNCONFINED_GRADING = [] if SANDBOX_AVAILABLE else ["--unconfined-grading-for-tests"]
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
if mode == "tamper_on_import":
    # Correct implementations that rewrite visible_checks.py by absolute path whenever they are imported.
    for module, decisions in json.loads(os.environ["FAKE_PI_SATISFY"]).items():
        path = here / (module + ".py")
        if path.exists():
            hook = "import pathlib\\ntry:\\n    pathlib.Path(%r).write_text('tampered')\\nexcept OSError:\\n    pass\\n"
            path.write_text(hook % str(here / "visible_checks.py") + solution(module, decisions))
if mode == "bad_stop" or (mode == "bad_stop_once" and first):
    assistant = dict(assistant, stopReason=[])
events = [{"type": "agent_start"}, {"type": "turn_start"}]
if mode == "bare_tool" or (mode == "bare_tool_once" and first):
    events.append({"type": "tool_execution_start"})
if mode == "escape":
    events.append({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
                   "args": {"command": "cat ../../autonomy_lab/runner.py"}})
    events.append({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "isError": True,
                   "result": {"content": [{"type": "text", "text": "cat: ../../autonomy_lab/runner.py: "
                                           "No such file or directory"}]}})
events += [
    {"type": "message_end", "message": assistant},
    {"type": "turn_end"},
    {"type": "agent_settled", "aborted": False},
]
for event in events:
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
        # Pi's wire message_update is a delta without `message` (Pi 1.3.0 docs/json.md).
        streaming = START + [
            {
                "type": "message_update",
                "usage": {"output": 0},
                "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "hi"},
            }
        ]
        self.assertEqual(self.classify(streaming, timed_out=True, idle=200), (None, "timeout"))

    def test_a_real_pi_stream_shape_is_not_malformed(self):
        # Shapes from Pi 1.3.0 docs/json.md. The previous validator required `message` on message_update, which
        # Pi's JSON mode never sends, so every streamed trial would have been classified malformed_events.
        usage = {"input": 100, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 101, "cost": {"total": 0}}
        final = dict(
            assistant()["message"], content=[{"type": "text", "text": "done"}], usage=usage, api="x", timestamp=1
        )
        stream = [
            {"type": "session", "version": 3, "id": "uuid", "timestamp": "2024-12-03T14:00:00.000Z", "cwd": "/work"},
            *START,
            {"type": "message_start", "message": {"role": "user", "content": "Review this", "timestamp": 1}},
            {"type": "message_end", "message": {"role": "user", "content": "Review this", "timestamp": 1}},
            {"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}},
            {
                "type": "message_update",
                "usage": usage,
                "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "Hello"},
            },
            {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash", "args": {"command": "ls"}},
            {
                "type": "tool_execution_update",
                "toolCallId": "call_1",
                "toolName": "bash",
                "args": {"command": "ls"},
                "partialResult": {"content": [], "details": {}},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "call_1",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "a.py"}], "details": {}},
                "isError": False,
            },
            {"type": "message_end", "message": final},
            {"type": "turn_end", "message": final, "toolResults": []},
            {"type": "agent_end", "messages": [final], "willRetry": False},
            {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000, "errorMessage": "529"},
            {"type": "auto_retry_end", "success": True, "attempt": 2},
            SETTLED,
        ]
        self.assertEqual(self.classify(stream), (None, None))

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
            # Review finding 6: these were accepted, or crashed parse_pi_events.
            {"type": "tool_execution_start"},
            {"type": "tool_execution_start", "toolCallId": "1"},
            {"type": "tool_execution_start", "toolCallId": 1, "toolName": "bash"},
            {"type": "tool_execution_end", "toolName": "bash", "isError": False},
            assistant([]),
            assistant(5),
            {"type": "message_end", "message": {"role": "assistant", "provider": "openai", "model": "example"}},
            {"type": "message_end", "message": dict(assistant()["message"], model=["example"])},
            {"type": "message_end", "message": dict(assistant()["message"], usage=[1])},
            {"type": "message_update", "usage": {}},
            {"type": "message_update", "assistantMessageEvent": {"delta": "x"}},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.classify(OK[:2] + [bad] + OK[2:]), ("malformed_events", None))
                self.assertEqual(
                    self.classify(OK[:2] + [bad] + OK[2:], timed_out=True, idle=1), ("malformed_events", None)
                )
                # Metric extraction validates first: a malformed event is counted, never read or crashed on.
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "pi-events.jsonl"
                    line = bad if isinstance(bad, str) else json.dumps(bad)
                    path.write_text(line + "\n", encoding="utf-8")
                    metrics = parse_pi_events(path)
                self.assertEqual(metrics["invalid_json_lines"] + metrics["malformed_events"], 1)
                self.assertEqual(metrics["assistant_messages"], 0)

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
        # Pi is a fake script here; tests of the default profile patch confine.confine instead.
        "unconfined_pi_for_tests": True,
        "unconfined_grading_for_tests": not SANDBOX_AVAILABLE,
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
    def run_bank(self, tmp, env=None, confine=None, auth=None, **overrides):
        """Run the fake-Pi task bank; ``auth`` replaces prepare_host_auth (default: a recorder that succeeds)."""
        log = Path(tmp) / "fake-log.jsonl"
        values = {"FAKE_PI_MODEL": "openai/example", "FAKE_PI_THINKING": "high", "FAKE_PI_LOG": str(log)}
        values.update(env or {})
        self.auth_calls = []

        def record_auth(provider, model, seconds, pi_binary="pi"):
            self.auth_calls.append((provider, model, seconds))
            return "15m"

        with (
            mock.patch.dict(os.environ, values),
            mock.patch.object(confinement, "prepare_host_auth", side_effect=auth or record_auth),
            contextlib.redirect_stdout(io.StringIO()),
        ):
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

    def test_pi_is_confined_by_default_and_the_host_token_is_prepared_before_each_attempt(self):
        events = []

        def fake_confine(command, workspace, results_dirs=()):
            events.append(("confine", workspace.name, tuple(results_dirs)))
            return command, dict(os.environ, FAKE_PI_MARKER="confined")

        def auth(provider, model, seconds, pi_binary="pi"):
            events.append(("auth", provider, model, seconds))
            return "15m"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(confinement, "confine", side_effect=fake_confine):
            env = {"FAKE_PI_MODE": "error_once", "FAKE_PI_STATE": str(Path(tmp) / "state")}
            root, records, calls = self.run_bank(tmp, env, auth=auth, unconfined_pi_for_tests=False, **SMALL)
            metadata = json.loads((root / "run-metadata.json").read_text())
            out = (Path(tmp) / "out").resolve()
        self.assertEqual(len(records), 3)  # one rerun after the provider error
        self.assertEqual([e[0] for e in events], ["auth", "confine"] * 3)
        for event in events:
            if event[0] == "auth":
                self.assertEqual(event[1:], ("openai", "example", 30 + 300))
            else:
                self.assertEqual(set(event[2]), {str(root), str(out), str(VALID_BANK.resolve())})
        self.assertTrue(all(call["marker"] == "confined" for call in calls))
        self.assertTrue(all(r["pi_confined"] for r in records))
        self.assertIn("autonomy_lab.confine.confine", metadata["confinement"])
        self.assertEqual(metadata["host_auth_min_validity_seconds"], 330)

    def test_host_auth_failure_is_an_auth_failure_and_is_rerun(self):
        attempts = []

        def flaky(provider, model, seconds, pi_binary="pi"):
            attempts.append(provider)
            if len(attempts) == 1:
                raise HostAuthError("cannot prepare the host token for openai/example (5m): token expired")
            return "5m"

        with tempfile.TemporaryDirectory() as tmp:
            root, records, calls = self.run_bank(tmp, auth=flaky, **SMALL)
            summary = json.loads((root / "summary.json").read_text())
        self.assertEqual(len(records), 3)
        self.assertEqual((records[0]["infrastructure_failure"], records[0]["attempt"]), ("auth", 1))
        self.assertIn("token expired", records[0]["stderr_tail"])
        self.assertEqual((records[1]["infrastructure_failure"], records[1]["attempt"]), (None, 2))
        self.assertEqual(len(calls), 2)  # Pi never started for the failed attempt
        self.assertEqual(summary["infrastructure_failures_first_attempt"], {"auth": 1})

    def test_a_profile_that_fails_to_start_is_a_sandbox_failure(self):
        def broken_profile(command, workspace):
            script = "import sys\nsys.stderr.write('bwrap: Creating new namespace failed: EPERM\\n')\nsys.exit(1)\n"
            return [sys.executable, "-c", script], dict(os.environ)

        with tempfile.TemporaryDirectory() as tmp:
            _, records, _ = self.run_bank(tmp, confine=broken_profile, **SMALL)
        self.assertEqual(len(records), 4)
        self.assertEqual({r["infrastructure_failure"] for r in records}, {"sandbox"})

    def test_a_grading_sandbox_failure_is_a_sandbox_failure(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pi_runner, "grade_modules", side_effect=GradingError("bwrap: cannot mount")),
        ):
            _, records, _ = self.run_bank(tmp, {"FAKE_PI_SATISFY": json.dumps(ALL_DECISIONS)}, **SMALL)
        self.assertEqual({r["infrastructure_failure"] for r in records}, {"sandbox"})
        self.assertFalse(any(r["accepted"] for r in records))

    def test_confinement_signals_are_recorded_from_the_event_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, records, _ = self.run_bank(tmp, {"FAKE_PI_MODE": "escape"}, **SMALL)
            summary = json.loads((root / "summary.json").read_text())
        kinds = {signal["kind"] for signal in records[0]["confinement_signals"]}
        self.assertIn("parent_relative_path", kinds)
        self.assertEqual(summary["attempts_with_confinement_signals"], 2)

    def test_malformed_events_are_an_infrastructure_failure_not_a_crash(self):
        for mode in ("bare_tool_once", "bad_stop_once"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(mode=mode):
                env = {"FAKE_PI_MODE": mode, "FAKE_PI_STATE": str(Path(tmp) / "state")}
                env["FAKE_PI_SATISFY"] = json.dumps(ALL_DECISIONS)
                _, records, _ = self.run_bank(tmp, env, **SMALL)
                self.assertEqual(len(records), 3)
                self.assertEqual(records[0]["infrastructure_failure"], "malformed_events")
                self.assertTrue(records[0]["malformed_events"])
                self.assertFalse(records[0]["accepted"])
                self.assertFalse(records[0]["runtime_matches_request"])
                self.assertEqual(records[1]["infrastructure_failure"], None)
                self.assertTrue(records[1]["accepted"])

    def test_tampering_during_grading_is_a_safety_stop(self):
        # Review finding 5: correct implementations that rewrote visible_checks.py when imported were accepted with
        # spec_files_intact=true. Unconfined, the post-grading hash catches the rewrite.
        env = {"FAKE_PI_MODE": "tamper_on_import", "FAKE_PI_SATISFY": json.dumps(ALL_DECISIONS)}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pi_runner, "default_grading_sandbox", return_value=None),
        ):
            root, records, _ = self.run_bank(tmp, env, **SMALL)
            summary = json.loads((root / "summary.json").read_text())
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["spec_files_intact_after_agent"])
        self.assertFalse(record["spec_files_intact_after_grading"])
        self.assertFalse(record["spec_files_intact"])
        self.assertFalse(record["accepted"])
        self.assertFalse(record["grading_confined"])
        self.assertIn("safety stop", summary["stop_reason"])

    @unittest.skipUnless(SANDBOX_AVAILABLE, "bwrap unavailable")
    def test_the_grading_sandbox_prevents_tampering_during_grading(self):
        env = {"FAKE_PI_MODE": "tamper_on_import", "FAKE_PI_SATISFY": json.dumps(ALL_DECISIONS)}
        with tempfile.TemporaryDirectory() as tmp:
            root, records, _ = self.run_bank(tmp, env, **SMALL)
            summary = json.loads((root / "summary.json").read_text())
            checks = [p.read_text() for p in (root / "workspaces").glob("*/visible_checks.py")]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["spec_files_intact"] and r["accepted"] and r["grading_confined"] for r in records))
        self.assertNotIn("tampered", "".join(checks))
        self.assertIsNone(summary["stop_reason"])

    def test_grading_is_refused_unconfined_without_the_explicit_flag(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("autonomy_lab.taskbank.bwrap_unavailable_reason", return_value="bwrap is not installed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to run agent-written code unconfined"):
                run_task_bank(bank_args(tmp, unconfined_grading_for_tests=False))
            self.assertFalse((Path(tmp) / "out").exists())
            # With the flag, the run proceeds unconfined and says so.
            root, records, _ = self.run_bank(tmp, unconfined_grading_for_tests=True, **SMALL)
            metadata = json.loads((root / "run-metadata.json").read_text())
        self.assertFalse(any(r["grading_confined"] for r in records))
        self.assertIn("UNCONFINED", metadata["grading_confinement"])

    def test_output_directory_defaults_outside_the_repository_and_refuses_one_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            for inside in (LAB / "local-results", LAB.parent, LAB / "tests"):
                with self.subTest(out=inside), self.assertRaisesRegex(ValueError, "overlaps the repository"):
                    run_task_bank(bank_args(tmp, out=inside, design_only=True))
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(tmp) / "state")}):
                self.assertEqual(pi_runner.default_out_dir(), Path(tmp) / "state" / "autonomy-lab")
                with contextlib.redirect_stdout(io.StringIO()):
                    root = run_task_bank(bank_args(tmp, out=None, design_only=True))
            self.assertEqual(root.parent, (Path(tmp) / "state" / "autonomy-lab").resolve())
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            self.assertEqual(pi_runner.default_out_dir(), Path.home() / ".local" / "state" / "autonomy-lab")


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
            *UNCONFINED_GRADING,
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
