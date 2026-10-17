import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from autonomy_lab.runner import (
    TASKS, build_prompt, check_candidate, prepare_workspace, summarize
)


class ExperimentScaffoldingTests(unittest.TestCase):
    def test_depth_conditions_have_different_critical_decisions(self):
        shallow = build_prompt(TASKS[:1], "shallow", "basic")
        deep = build_prompt(TASKS[:1], "deep", "basic")
        self.assertNotIn("first-appearance order", shallow)
        self.assertIn("first-appearance order", deep)
        self.assertIn("visible_checks.py", shallow)

    def test_verification_changes_public_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            basic = root / "basic"
            rich = root / "rich"
            prepare_workspace(basic, TASKS[:1], "basic")
            prepare_workspace(rich, TASKS[:1], "rich")
            p1 = subprocess.run(
                [sys.executable, "visible_checks.py"], cwd=basic,
                capture_output=True, text=True
            )
            p2 = subprocess.run(
                [sys.executable, "visible_checks.py"], cwd=rich,
                capture_output=True, text=True
            )
            self.assertEqual(p1.returncode, 0)
            self.assertNotEqual(p2.returncode, 0)

    def test_hidden_grader_detects_unfixed_starter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prepare_workspace(root, TASKS[:1], "basic")
            accepted, grade = check_candidate(root, TASKS[:1], "mock")
            self.assertFalse(accepted)
            self.assertNotEqual(grade["exit_code"], 0)

    def test_mock_is_not_research_evidence(self):
        sample = {
            "breadth": 1, "depth": "shallow", "verification": "basic",
            "accepted": False, "agent_seconds": 0
        }
        result = summarize([sample], "mock")
        self.assertFalse(result["is_real_agent_evidence"])
        self.assertEqual(result["summary"][0]["trials"], 1)


if __name__ == "__main__":
    unittest.main()
