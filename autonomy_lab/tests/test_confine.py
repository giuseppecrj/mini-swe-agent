"""Tests for the bubblewrap confinement profile and the confinement-signal scanner. Never invokes Pi or a model.

Tests that run bwrap skip when it is missing or cannot create unprivileged user namespaces.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from autonomy_lab import confine as confine_module
from autonomy_lab.confine import (
    AGENT_DIR,
    WORK_DIR,
    ConfinementError,
    bwrap_unavailable_reason,
    classify_path,
    confine,
    run_selftest,
    sandbox_setup_failed,
    scan_confinement_signals,
)

LAB = Path(__file__).resolve().parents[1]
BWRAP_REASON = bwrap_unavailable_reason()


def fake_which(name, *args, **kwargs):
    if name == "bwrap":
        return "/usr/bin/bwrap"
    return sys.executable if name == "python-agent" else None


def operands(command, flag):
    """Return the single operand of every ``flag`` option in a bwrap command line."""
    end = command.index("--")
    return [command[i + 1] for i in range(1, end) if command[i] == flag]


def pairs(command, flag):
    """Return the (source, dest) operands of every ``flag`` option in a bwrap command line."""
    end = command.index("--")
    return [(command[i + 1], command[i + 2]) for i in range(1, end) if command[i] == flag]


class ProfileTests(unittest.TestCase):
    """The profile's command line, checked without running bwrap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.workspace = root / "run" / "workspaces" / "trial"
        self.workspace.mkdir(parents=True)
        (root / "run" / "logs").mkdir()
        self.run_root = root / "run"
        self.auth = root / "agent" / "auth.json"
        self.auth.parent.mkdir()
        self.auth.write_text("{}", encoding="utf-8")
        (self.auth.parent / "models.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(confine_module.shutil, "which", side_effect=fake_which)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def wrap(self, **kwargs):
        kwargs.setdefault("auth_file", self.auth)
        return confine(["python-agent", "--mode", "json", "--", "prompt"], self.workspace, **kwargs)

    def test_workspace_is_the_only_writable_host_bind(self):
        command, _ = self.wrap(results_dirs=[str(self.run_root)])
        binds = pairs(command, "--bind")
        self.assertEqual(binds, [(str(self.auth), f"{AGENT_DIR}/auth.json"), (str(self.workspace), WORK_DIR)])
        self.assertEqual(operands(command, "--chdir"), [WORK_DIR])
        self.assertEqual(operands(command, "--remount-ro"), ["/"])
        for source, dest in pairs(command, "--ro-bind"):
            if source != "/dev/null":
                self.assertEqual(source, dest)

    def test_home_repository_results_and_run_are_hidden(self):
        command, _ = self.wrap(results_dirs=[str(self.run_root)])
        tmpfs = operands(command, "--tmpfs")
        covered = lambda path: any(  # noqa: E731
            str(path) == t or str(path).startswith(t.rstrip("/") + "/") for t in tmpfs
        )
        for path in (Path.home(), LAB.parent, LAB / "runner.py", self.run_root / "logs", self.workspace.parent):
            self.assertTrue(covered(path), path)
        for path in ("/tmp", "/run", "/var/tmp", AGENT_DIR):
            self.assertIn(path, tmpfs)
        # Hidden paths come before the binds that re-expose the Pi installation and the workspace.
        self.assertLess(command.index(str(Path.home().resolve())), command.index(WORK_DIR))
        self.assertIn("/proc", operands(command, "--proc"))
        for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv"):
            self.assertIn(flag, command[: command.index("--")])

    def test_results_dirs_and_run_layout_are_hidden_outside_fresh_mounts(self):
        other = Path(self.tmp.name).resolve() / "other-results"
        other.mkdir()
        with mock.patch.object(confine_module, "FRESH_MOUNTS", ()):
            command, _ = self.wrap(results_dirs=[str(other)])
            tmpfs = operands(command, "--tmpfs")
            self.assertIn(str(self.run_root), tmpfs)
            self.assertIn(str(other), tmpfs)
            self.assertNotIn(str(self.workspace), tmpfs)
            loose = Path(self.tmp.name).resolve() / "loose" / "trial"
            loose.mkdir(parents=True)
            command, _ = confine(["python-agent"], loose, auth_file=self.auth)
            self.assertIn(str(loose.parent), operands(command, "--tmpfs"))
            self.assertNotIn(str(loose.parent.parent), operands(command, "--tmpfs"))

    def test_sessions_and_other_agent_files_are_never_mounted(self):
        command, _ = self.wrap()
        sources = [src for src, _ in pairs(command, "--ro-bind") + pairs(command, "--bind")]
        self.assertFalse([s for s in sources if "sessions" in s or s.startswith(str(Path.home() / ".pi"))])
        self.assertEqual(
            [d for _, d in pairs(command, "--bind") if d.startswith(AGENT_DIR)], [f"{AGENT_DIR}/auth.json"]
        )

    def test_extra_agent_files_are_read_only(self):
        command, _ = self.wrap(extra_agent_files=["models.json"])
        self.assertIn((str(self.auth.parent / "models.json"), f"{AGENT_DIR}/models.json"), pairs(command, "--ro-bind"))
        for bad in ("../auth.json", "auth.json", "missing.json", "sessions"):
            with self.assertRaises(ConfinementError, msg=bad):
                self.wrap(extra_agent_files=[bad])

    def test_command_runs_resolved_executable_with_minimal_environment(self):
        with mock.patch.dict(os.environ, {"PI_SESSION_FILE": "/secret/session.jsonl", "OPENAI_API_KEY": "x"}):
            command, env = self.wrap()
        tail = command[command.index("--") + 1 :]
        self.assertEqual(tail, [str(Path(sys.executable).resolve()), "--mode", "json", "--", "prompt"])
        self.assertEqual(env["PI_CODING_AGENT_DIR"], AGENT_DIR)
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("PI_SESSION_FILE", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(dict(pairs(command, "--setenv")), env)

    def test_setup_errors_raise_confinement_error(self):
        with self.assertRaises(ConfinementError):
            self.wrap(auth_file=self.auth.parent / "absent.json")
        with self.assertRaises(ConfinementError):
            confine(["python-agent"], self.workspace / "absent", auth_file=self.auth)
        with self.assertRaises(ConfinementError):
            confine(["no-such-binary"], self.workspace, auth_file=self.auth)
        with self.assertRaises(ConfinementError):
            confine([], self.workspace, auth_file=self.auth)
        with self.assertRaises(ConfinementError):
            self.wrap(results_dirs=["relative/results"])
        with self.assertRaises(ConfinementError):
            self.wrap(results_dirs=["/usr"])
        with self.assertRaises(ConfinementError):
            self.wrap(results_dirs=[str(self.workspace)])
        with mock.patch.object(confine_module.shutil, "which", return_value=None):
            with self.assertRaises(ConfinementError):
                self.wrap()

    def test_sandbox_setup_failure_is_recognised_from_stderr(self):
        self.assertTrue(sandbox_setup_failed(1, "bwrap: setting up uid map: Permission denied\n"))
        self.assertFalse(sandbox_setup_failed(0, "bwrap: warning\n"))
        self.assertFalse(sandbox_setup_failed(1, "Error: model not found\n"))
        self.assertFalse(sandbox_setup_failed(None, ""))


@unittest.skipIf(BWRAP_REASON, f"bwrap unavailable: {BWRAP_REASON}")
class BwrapTests(unittest.TestCase):
    """The T3 acceptance probes, run for real under bwrap with a fake agent and a fake credentials file."""

    def test_fake_agent_cannot_escape_and_can_use_its_workspace(self):
        ok, report = run_selftest(interpreter=sys.executable)
        failed = {name: report["probes"].get(name) for name, passed in report.get("checks", {}).items() if not passed}
        self.assertTrue(ok, json.dumps(failed or report, indent=2))
        for name in (
            "python_relative_runner",
            "python_absolute_runner",
            "shell_cd_parent_runner",
            "python_list_sessions",
            "shell_list_sessions",
            "workspace_read_write",
            "auth_written_back_on_host",
        ):
            self.assertIn(name, report["checks"])

    def test_same_probes_succeed_without_the_profile(self):
        # Shows the probes would notice a missing profile. Probes that depend on the user's Pi state are excluded.
        _, report = run_selftest(interpreter=sys.executable, confined=False)
        for name in (
            "python_relative_runner",
            "python_absolute_runner",
            "python_sibling_workspace",
            "python_run_logs",
            "python_proc_root_escape",
            "shell_relative_runner",
            "shell_absolute_runner",
            "shell_cd_parent_runner",
            "workspace_read_write",
        ):
            self.assertTrue(report["checks"][name], name)

    def test_killing_the_process_group_stops_the_confined_process(self):
        workspace = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(workspace)], check=False))
        marker = "confine-kill-" + uuid.uuid4().hex
        auth = workspace.parent / (marker + "-auth.json")
        auth.write_text("{}", encoding="utf-8")
        self.addCleanup(auth.unlink)
        command, env = confine(
            [sys.executable, "-c", f"import time; time.sleep(120)  # {marker}"],
            workspace,
            auth_file=auth,
            ro_binds=[sys.base_prefix],
        )
        process = subprocess.Popen(command, env=env, start_new_session=True)
        deadline = time.monotonic() + 10
        while not self.marked(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.marked(marker), "confined process did not start")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while self.marked(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.marked(marker), "confined process survived its process group")

    @staticmethod
    def marked(marker):
        for entry in Path("/proc").iterdir():
            try:
                if (
                    marker.encode() in (entry / "cmdline").read_bytes()
                    and b"bwrap" not in (entry / "cmdline").read_bytes()
                ):
                    return True
            except OSError:
                continue
        return False


def start(call_id, tool, **args):
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": args}


def end(call_id, tool, text, is_error=False):
    result = {"content": [{"type": "text", "text": text}], "details": {}}
    return {
        "type": "tool_execution_end",
        "toolCallId": call_id,
        "toolName": tool,
        "result": result,
        "isError": is_error,
    }


class SignalScanTests(unittest.TestCase):
    """Synthetic Pi event streams covering every confinement-signal kind."""

    def test_parent_relative_paths(self):
        events = [
            start("1", "bash", command="cd .. && cat autonomy_lab/runner.py"),
            start("2", "read", path="../../autonomy_lab/runner.py"),
            start("3", "bash", command="python3 -c \"open('../runner.py')\""),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual({s["kind"] for s in signals}, {"parent_relative_path"})
        self.assertIn({"kind": "parent_relative_path", "detail": "read: ../../autonomy_lab/runner.py"}, signals)
        self.assertEqual(len(signals), 3)

    def test_absolute_non_system_paths(self):
        events = [
            start("1", "bash", command="cat /home/g/Projects/mini-swe-agent/autonomy_lab/runner.py"),
            start("2", "read", path="/pi-agent/auth.json"),
            start("3", "bash", command="PYTHONPATH=/srv/repo python3 -m x"),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual([s["kind"] for s in signals], ["absolute_path"] * 3)
        self.assertEqual(signals[1]["detail"], "read: /pi-agent/auth.json")

    def test_home_relative_paths(self):
        events = [
            start("1", "bash", command="ls ~/.pi/agent/sessions"),
            start("2", "bash", command='ls "$HOME"/.pi; ls ${HOME}/x'),
            start("3", "bash", command="python3 -c 'from pathlib import Path; print(list(Path.home().iterdir()))'"),
            start("4", "read", path="~/notes.txt"),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual({s["kind"] for s in signals}, {"home_path"})
        details = [s["detail"] for s in signals]
        self.assertIn("bash: ~/.pi/agent/sessions", details)
        self.assertIn("bash: Path.home()", details)
        self.assertIn("read: ~/notes.txt", details)

    def test_denied_paths_from_error_results(self):
        events = [
            start("1", "bash", command="ls /home/g/.pi/agent/sessions"),
            end("1", "bash", "ls: cannot access '/home/g/.pi/agent/sessions': No such file or directory", True),
            start("2", "bash", command="python3 -c \"open('../x/runner.py')\""),
            end("2", "bash", "FileNotFoundError: [Errno 2] No such file or directory: '../x/runner.py'", True),
            start("3", "bash", command="cd .. && cat autonomy_lab/runner.py"),
            end("3", "bash", "cat: autonomy_lab/runner.py: No such file or directory", True),
            start("4", "write", path="/etc/x", content="data"),
            end("4", "write", "EROFS: read-only file system, open '/etc/x'", True),
            start("5", "bash", command="touch /work/ok"),
            end("5", "bash", "touch: cannot touch 'ok': Permission denied", True),
        ]
        denied = [s["detail"] for s in scan_confinement_signals(events) if s["kind"] == "denied_path"]
        self.assertEqual(
            denied,
            ["bash: /home/g/.pi/agent/sessions", "bash: ../x/runner.py", "bash: .."],
        )

    def test_workspace_and_system_paths_are_not_signals(self):
        events = [
            start("1", "bash", command="cd /work && python3 -I visible_checks.py 2>/dev/null; ls /usr/lib /tmp/x"),
            start("2", "read", path="/work/ledger.py"),
            start("3", "read", path="ledger.py"),
            start("4", "bash", command="curl https://example.com/a/b; echo 'Loading...'; x=$((4 / 2))"),
            start("5", "write", path="ledger.py", content="import os\nopen('../../secret')\n"),
            start("6", "edit", path="ledger.py", edits=[{"oldText": "~/a", "newText": "/home/x"}]),
            start("7", "bash", command="ls /work/sub/../ledger.py"),
            end("7", "bash", "ls: cannot access 'nothing': No such file or directory", True),
            {"type": "message_end", "message": {"role": "assistant", "content": "cat ../../runner.py"}},
        ]
        self.assertEqual(scan_confinement_signals(events), [])

    def test_reads_event_log_files_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi-events.jsonl"
            lines = [json.dumps(start(str(i), "bash", command="cat ../runner.py")) for i in range(3)]
            path.write_text("\n".join([*lines, "not json", ""]), encoding="utf-8")
            self.assertEqual(
                scan_confinement_signals(path), [{"kind": "parent_relative_path", "detail": "bash: ../runner.py"}]
            )
            self.assertEqual(scan_confinement_signals(Path(tmp) / "absent.jsonl"), [])

    def test_classify_path(self):
        cases = {
            "../x": "parent_relative_path",
            "..": "parent_relative_path",
            "a/../../b": "parent_relative_path",
            "sub/../ledger.py": None,
            "/work/../home": "parent_relative_path",
            "/home/g": "absolute_path",
            "/": None,
            "/usr/bin/python3": None,
            "/work": None,
            "/workspace": "absolute_path",
            "~": "home_path",
            "$HOME/x": "home_path",
            "x.py": None,
            "...": None,
            "//comment": None,
        }
        for word, kind in cases.items():
            self.assertEqual(classify_path(word), kind, word)


if __name__ == "__main__":
    unittest.main()
