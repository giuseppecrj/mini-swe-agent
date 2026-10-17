"""Offline tests for the Pi adapter. Never invokes Pi or a model."""

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autonomy_lab.pi_runner import (
    parse_pi_events,
    pi_command,
    run_experiment,
    runtime_matches_request,
    safe_trial_scope,
    validate_model,
)
from autonomy_lab.runner import TASKS, build_prompt, prepare_workspace

# Stands in for the Pi CLI: fixes merge_ids.py and reports the model and
# thinking level named by FAKE_PI_MODEL / FAKE_PI_THINKING.
FAKE_PI = """
import json, os, pathlib, sys
if "--version" in sys.argv:
    print("fake-pi 0")
    sys.exit(0)
pathlib.Path("merge_ids.py").write_text(
    "def merge_ids(left, right):\\n    return list(dict.fromkeys([*left, *right]))\\n"
)
provider, model = os.environ["FAKE_PI_MODEL"].split("/", 1)
for event in (
    {"type": "turn_end"},
    {"type": "message_end", "message": {
        "role": "assistant", "provider": provider, "model": model,
        "thinkingLevel": os.environ["FAKE_PI_THINKING"], "stopReason": "stop"}},
    {"type": "agent_settled", "aborted": False},
):
    print(json.dumps(event))
"""


class PiAdapterTests(unittest.TestCase):
    def test_pi_command_is_one_shot_with_explicit_tool_scope(self):
        command = pi_command("openai/example", "medium", "do work")
        self.assertEqual(command[0], "pi")
        self.assertIn("--mode", command)
        self.assertIn("--no-session", command)
        self.assertIn("--no-extensions", command)
        self.assertIn("--no-skills", command)
        self.assertIn("--no-context-files", command)
        self.assertIn("--no-mcp", command)
        self.assertIn("--tools", command)
        self.assertEqual(command[-2:], ["--", "do work"])
        self.assertEqual(command[command.index("--model") + 1], "openai/example")
        self.assertEqual(command[command.index("--thinking") + 1], "medium")

    def test_model_must_be_exact_provider_id_without_thinking_suffix(self):
        self.assertEqual(validate_model("openai-codex/gpt-5.6-sol"), "openai-codex/gpt-5.6-sol")
        self.assertEqual(validate_model("mac-studio/gpt-oss:20b-128k"), "mac-studio/gpt-oss:20b-128k")
        for bad in (None, "", "sonnet", "openai/", "/gpt-5", "openai-codex/gpt-5.6-sol:high"):
            with self.assertRaises(ValueError):
                validate_model(bad)

    def parse_lines(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return parse_pi_events(path)

    def test_runtime_must_match_requested_model_and_thinking(self):
        def assistant(**fields):
            message = {"role": "assistant", "stopReason": "stop", **fields}
            return json.dumps({"type": "message_end", "message": message})

        full = assistant(provider="openai", model="example", thinkingLevel="high")
        run = self.parse_lines([full, full])
        self.assertTrue(runtime_matches_request(run, "openai/example", "high"))
        self.assertFalse(runtime_matches_request(run, "openai/other", "high"))
        self.assertFalse(runtime_matches_request(run, "openai/example", "low"))
        # A later message without its thinking level is unverified, not a match.
        missing = assistant(provider="openai", model="example")
        run = self.parse_lines([full, missing])
        self.assertEqual(run["assistant_messages_missing_runtime"], 1)
        self.assertFalse(runtime_matches_request(run, "openai/example", "high"))
        # An unparseable line could hide a different model.
        run = self.parse_lines([full, '{"type": "message_end", "message": {'])
        self.assertFalse(runtime_matches_request(run, "openai/example", "high"))
        # No assistant message at all is no evidence.
        run = self.parse_lines([json.dumps({"type": "agent_settled", "aborted": False})])
        self.assertFalse(runtime_matches_request(run, "openai/example", "high"))

    def test_parse_pi_json_stream_with_usage_and_completion(self):
        events = [
            {"type": "session", "cwd": "/test"},
            {"type": "turn_end"},
            {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"},
            {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "isError": False},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "provider": "openai",
                    "model": "example",
                    "stopReason": "stop",
                    "thinkingLevel": "high",
                    "usage": {"totalTokens": 210, "cost": {"total": 0.0123}},
                },
            },
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled", "aborted": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
            metrics = parse_pi_events(path)
        self.assertTrue(metrics["settled"])
        self.assertFalse(metrics["aborted"])
        self.assertEqual(metrics["assistant_turns"], 1)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["tool_errors"], 0)
        self.assertEqual(metrics["total_tokens"], 210)
        self.assertEqual(metrics["reported_cost_usd"], 0.0123)
        self.assertEqual(metrics["model_ids"], ["openai/example"])
        self.assertEqual(metrics["thinking_levels"], ["high"])
        self.assertEqual(metrics["malformed_events"], 0)

    def test_detects_aborted_agent_even_with_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({"type": "agent_settled", "aborted": True}) + "\n", encoding="utf-8")
            metrics = parse_pi_events(path)
            self.assertTrue(metrics["aborted"])

    def test_changed_public_checks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prepare_workspace(workspace, TASKS[:1], "rich")
            prompt = build_prompt(TASKS[:1], "deep", "rich")
            (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
            checks = (workspace / "visible_checks.py").read_bytes()
            prompt_bytes = (workspace / "prompt.txt").read_bytes()
            self.assertTrue(safe_trial_scope(workspace, TASKS[:1], checks, prompt_bytes))
            (workspace / "visible_checks.py").write_text("print('tampered')", encoding="utf-8")
            self.assertFalse(safe_trial_scope(workspace, TASKS[:1], checks, prompt_bytes))

    def run_fake_trial(self, tmp, reported_model):
        fake = Path(tmp) / "fake-pi"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_PI, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        args = argparse.Namespace(
            model="openai/example",
            thinking="high",
            pi_binary=str(fake),
            breadths="1",
            depths="deep",
            verifications="rich",
            repetitions=1,
            timeout=30,
            out=Path(tmp) / "out",
        )
        env = {"FAKE_PI_MODEL": reported_model, "FAKE_PI_THINKING": "high"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            root = run_experiment(args)
        trial = json.loads((root / "trials.jsonl").read_text(encoding="utf-8"))
        return root, trial

    def test_harness_logs_are_outside_agent_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, trial = self.run_fake_trial(tmp, "openai/example")
            self.assertTrue(trial["accepted"])
            self.assertTrue(trial["runtime_matches_request"])
            workspace = root / "workspaces" / trial["trial_id"]
            logs = root / "logs" / trial["trial_id"]
            self.assertTrue((logs / "pi-events.jsonl").is_file())
            self.assertTrue((logs / "pi-stderr.log").is_file())
            self.assertFalse((workspace / "pi-events.jsonl").exists())
            self.assertFalse((workspace / "pi-stderr.log").exists())

    def test_wrong_runtime_model_is_not_accepted_even_if_code_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, trial = self.run_fake_trial(tmp, "openai/other")
            self.assertTrue(trial["held_out_tests_passed"])
            self.assertTrue(trial["agent_completed"])
            self.assertFalse(trial["runtime_matches_request"])
            self.assertFalse(trial["accepted"])
            summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["summary"][0]["runtime_mismatches"], 1)


if __name__ == "__main__":
    unittest.main()
