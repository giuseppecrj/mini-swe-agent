"""Offline tests for the Pi adapter. Never invokes Pi or a model."""

import json
import tempfile
import unittest
from pathlib import Path

from autonomy_lab.pi_runner import (
    parse_pi_events, pi_command, safe_trial_scope,
)
from autonomy_lab.runner import TASKS, build_prompt, prepare_workspace


class PiAdapterTests(unittest.TestCase):
    def test_pi_command_is_one_shot_with_explicit_tool_scope(self):
        command = pi_command("openai/example", "do work")
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

    def test_parse_pi_json_stream_with_usage_and_completion(self):
        events = [
            {"type": "session", "cwd": "/test"},
            {"type": "turn_end"},
            {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash"},
            {"type": "tool_execution_end", "toolCallId": "t1", "isError": False},
            {"type": "message_end", "message": {
                "role": "assistant", "provider": "openai", "model": "example",
                "stopReason": "stop",
                "usage": {"totalTokens": 210, "cost": {"total": 0.0123}}
            }},
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled", "aborted": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(json.dumps(e) for e in events) + "\n",
                encoding="utf-8"
            )
            metrics = parse_pi_events(path)
        self.assertTrue(metrics["settled"])
        self.assertFalse(metrics["aborted"])
        self.assertEqual(metrics["assistant_turns"], 1)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["tool_errors"], 0)
        self.assertEqual(metrics["total_tokens"], 210)
        self.assertEqual(metrics["reported_cost_usd"], 0.0123)
        self.assertEqual(metrics["model_ids"], ["openai/example"])

    def test_detects_aborted_agent_even_with_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps({"type": "agent_settled", "aborted": True}) + "\n",
                encoding="utf-8"
            )
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
            self.assertTrue(safe_trial_scope(
                workspace, TASKS[:1], checks, prompt_bytes
            ))
            (workspace / "visible_checks.py").write_text(
                "print('tampered')", encoding="utf-8"
            )
            self.assertFalse(safe_trial_scope(
                workspace, TASKS[:1], checks, prompt_bytes
            ))


if __name__ == "__main__":
    unittest.main()
