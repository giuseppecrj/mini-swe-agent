"""Tests for the bubblewrap confinement profile and the confinement-signal scanner. Never invokes Pi or a model.

Tests that run bwrap skip when it is missing or cannot create unprivileged user namespaces.
"""

import json
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from autonomy_lab import confine as confine_module
from autonomy_lab.confine import (
    AGENT_DIR,
    AUDIT_ARCH_X86_64,
    GRADING_HOME,
    SECCOMP_FD,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    SECCOMP_RET_KILL_PROCESS,
    WORK_DIR,
    ConfinementError,
    GradingSandbox,
    HostAuthError,
    bwrap_unavailable_reason,
    classify_path,
    confine,
    normalise_path,
    prepare_host_auth,
    run_grading_selftest,
    run_selftest,
    sandbox_setup_failed,
    scan_confinement_signals,
    seccomp_program,
    shim_command,
)

LAB = Path(__file__).resolve().parents[1]
BWRAP_REASON = bwrap_unavailable_reason()


def fake_which(name, *args, **kwargs):
    if name == "bwrap":
        return "/usr/bin/bwrap"
    return sys.executable if name == "python-agent" else None


def bwrap_argv(command):
    """Strip the descriptor-closing exec shim, returning the bwrap command line it execs."""
    for index, word in enumerate(command):
        if word.endswith("bwrap"):
            return command[index:]
    raise AssertionError(f"no bwrap in {command}")


def operands(command, flag):
    """Return the single operand of every ``flag`` option in a bwrap command line."""
    end = command.index("--")
    return [command[i + 1] for i in range(1, end) if command[i] == flag]


def pairs(command, flag):
    """Return the (source, dest) operands of every ``flag`` option in a bwrap command line."""
    end = command.index("--")
    return [(command[i + 1], command[i + 2]) for i in range(1, end) if command[i] == flag]


def run_seccomp(program, arch, nr, args=()):
    """Interpret a classic-BPF seccomp program against one syscall and return its action.

    Implements only the instructions the profile uses, so an unexpected opcode fails the test instead of being
    guessed at. ``struct seccomp_data`` is laid out as on x86_64.
    """
    args = (*args, *(0,) * (6 - len(args)))
    data = struct.pack("<iIQ6Q", nr if nr < 1 << 31 else nr - (1 << 32), arch, 0, *args)
    code = [struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    accumulator, pc = 0, 0
    while True:
        op, jt, jf, k = code[pc]
        if op == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            accumulator = struct.unpack_from("<I", data, k)[0]
        elif op == 0x54:  # BPF_ALU | BPF_AND | BPF_K
            accumulator &= k
        elif op == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += jt if accumulator == k else jf
        elif op == 0x45:  # BPF_JMP | BPF_JSET | BPF_K
            pc += jt if accumulator & k else jf
        elif op == 0x06:  # BPF_RET | BPF_K
            return k
        else:
            raise AssertionError(f"unexpected opcode {op:#x} at {pc}")
        pc += 1


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
        self.assertEqual(pairs(command, "--bind"), [(str(self.workspace), WORK_DIR)])
        self.assertEqual(operands(command, "--chdir"), [WORK_DIR])
        self.assertEqual(operands(command, "--remount-ro"), ["/"])
        for source, dest in pairs(command, "--ro-bind"):
            if source not in ("/dev/null", str(self.auth)):
                self.assertEqual(source, dest)

    def test_credentials_are_bound_read_only_so_a_refresh_inside_cannot_write_through(self):
        command, _ = self.wrap()
        self.assertIn((str(self.auth), f"{AGENT_DIR}/auth.json"), pairs(command, "--ro-bind"))
        self.assertNotIn(str(self.auth), [source for source, _ in pairs(command, "--bind")])

    def test_seccomp_filter_is_passed_to_bwrap_and_covers_unix_sockets_and_io_uring(self):
        command, _ = self.wrap()
        self.assertEqual(operands(bwrap_argv(command), "--seccomp"), [str(SECCOMP_FD)])
        program = seccomp_program()
        self.assertEqual(len(program) % 8, 0)
        instructions = [struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
        # The architecture is checked first, before any syscall number is read.
        self.assertEqual(instructions[0], (0x20, 0, 0, 4))
        self.assertEqual(instructions[1][0::3], (0x15, AUDIT_ARCH_X86_64))
        eperm = SECCOMP_RET_ERRNO | 1  # EPERM, not a kill, so the agent sees a normal error
        AF_UNIX, AF_INET, AF_INET6 = 1, 2, 10
        STREAM, DGRAM, RAW, SEQPACKET = 1, 2, 3, 5
        NONBLOCK, CLOEXEC = 0o4000, 0o2000000
        cases = [
            # Another architecture or the x32 ABI renumbers every syscall, so it is killed outright.
            ("aarch64 socket", 0xC00000B7, 41, (AF_UNIX, STREAM), SECCOMP_RET_KILL_PROCESS),
            ("x32 read", AUDIT_ARCH_X86_64, 0x40000000, (), SECCOMP_RET_KILL_PROCESS),
            ("x32 socket", AUDIT_ARCH_X86_64, 0x40000000 | 41, (AF_UNIX, STREAM), SECCOMP_RET_KILL_PROCESS),
            ("read", AUDIT_ARCH_X86_64, 0, (), SECCOMP_RET_ALLOW),
            ("pipe2", AUDIT_ARCH_X86_64, 293, (), SECCOMP_RET_ALLOW),
            # socket() is judged on its domain; the kernel reads it as an int, so high bits do not matter.
            ("socket unix stream", AUDIT_ARCH_X86_64, 41, (AF_UNIX, STREAM), eperm),
            ("socket unix dgram", AUDIT_ARCH_X86_64, 41, (AF_UNIX, DGRAM | CLOEXEC), eperm),
            ("socket unix, high bits", AUDIT_ARCH_X86_64, 41, ((1 << 32) | AF_UNIX, STREAM), eperm),
            # AF_VSOCK reaches host-local listeners even from an unshared network namespace (VMADDR_CID_LOCAL).
            ("socket vsock stream", AUDIT_ARCH_X86_64, 41, (40, STREAM), eperm),
            ("socket vsock dgram, cloexec", AUDIT_ARCH_X86_64, 41, (40, DGRAM | CLOEXEC), eperm),
            ("socket inet", AUDIT_ARCH_X86_64, 41, (AF_INET, STREAM), SECCOMP_RET_ALLOW),
            ("socket inet6 dgram", AUDIT_ARCH_X86_64, 41, (AF_INET6, DGRAM), SECCOMP_RET_ALLOW),
            # socketpair(): a stream pair is connected for good; any other Unix pair can connect() elsewhere.
            ("socketpair unix stream", AUDIT_ARCH_X86_64, 53, (AF_UNIX, STREAM), SECCOMP_RET_ALLOW),
            (
                "socketpair unix stream, flags",
                AUDIT_ARCH_X86_64,
                53,
                (AF_UNIX, STREAM | NONBLOCK | CLOEXEC),
                SECCOMP_RET_ALLOW,
            ),
            ("socketpair unix dgram", AUDIT_ARCH_X86_64, 53, (AF_UNIX, DGRAM), eperm),
            ("socketpair unix dgram, cloexec", AUDIT_ARCH_X86_64, 53, (AF_UNIX, DGRAM | CLOEXEC), eperm),
            ("socketpair unix dgram, flags", AUDIT_ARCH_X86_64, 53, (AF_UNIX, DGRAM | NONBLOCK | CLOEXEC), eperm),
            ("socketpair unix raw", AUDIT_ARCH_X86_64, 53, (AF_UNIX, RAW), eperm),  # AF_UNIX turns RAW into DGRAM
            ("socketpair unix seqpacket", AUDIT_ARCH_X86_64, 53, (AF_UNIX, SEQPACKET), eperm),
            # The kernel reads the type as an int: a stream type in the high half does not make a dgram pair pass.
            ("socketpair unix dgram, high bits", AUDIT_ARCH_X86_64, 53, (AF_UNIX, (STREAM << 32) | DGRAM), eperm),
            ("socketpair unix, high domain bits", AUDIT_ARCH_X86_64, 53, ((1 << 32) | AF_UNIX, DGRAM), eperm),
            ("socketpair inet dgram", AUDIT_ARCH_X86_64, 53, (AF_INET, DGRAM), SECCOMP_RET_ALLOW),
            # io_uring can create and use sockets without socket().
            ("io_uring_setup", AUDIT_ARCH_X86_64, 425, (), eperm),
            ("io_uring_enter", AUDIT_ARCH_X86_64, 426, (), eperm),
            ("io_uring_register", AUDIT_ARCH_X86_64, 427, (), eperm),
        ]
        for name, arch, nr, args, expected in cases:
            self.assertEqual(run_seccomp(program, arch, nr, args), expected, name)

    def test_seccomp_filter_refuses_architectures_it_was_not_assembled_for(self):
        with mock.patch.object(confine_module.platform, "machine", return_value="aarch64"):
            with self.assertRaises(ConfinementError):
                seccomp_program()
            with self.assertRaises(ConfinementError):
                self.wrap()

    def test_profile_is_launched_through_a_descriptor_closing_shim(self):
        command, _ = self.wrap()
        self.assertEqual(command[:3], [sys.executable, "-I", "-c"])
        self.assertIn("os.closerange(3, limit)", command[3])
        self.assertIn("os.execv", command[3])
        self.assertEqual(command[4], str(SECCOMP_FD))
        self.assertEqual(bytes.fromhex(command[5]), seccomp_program())
        self.assertEqual(command[6], "/usr/bin/bwrap")

    def test_binds_that_re_expose_a_protected_tree_are_rejected(self):
        results = Path(self.tmp.name).resolve() / "results"
        (results / "trial-logs").mkdir(parents=True)
        installed = results / "pi-install"
        installed.mkdir()
        (installed / "pi").write_text("#!/bin/sh\n", encoding="utf-8")
        # The reviewer's case: binding the executable's own directory back after hiding it re-exposed the results.
        with mock.patch.object(confine_module.shutil, "which", return_value=str(installed / "pi")):
            with self.assertRaises(ConfinementError) as caught:
                self.wrap(results_dirs=[str(results)])
        self.assertIn("overlaps the protected path", str(caught.exception))
        # Equal to, inside, and containing a protected tree are all refused, as is the repository and $HOME.
        for bad in (str(results), str(results / "trial-logs"), str(results.parent), str(Path.home()), str(LAB)):
            with self.assertRaises(ConfinementError, msg=bad):
                self.wrap(results_dirs=[str(results)], ro_binds=[bad])
        # A bind inside hidden-but-unprotected $HOME, where the real Pi installation lives, is still re-exposed.
        cache = Path.home() / ".cache" / "autonomy-lab"
        cache.mkdir(parents=True, exist_ok=True)
        allowed = Path(tempfile.mkdtemp(prefix="confine-test-bind-", dir=cache))
        self.addCleanup(shutil.rmtree, allowed, True)
        command, _ = self.wrap(results_dirs=[str(results)], ro_binds=[str(allowed)])
        self.assertIn((str(allowed), str(allowed)), pairs(command, "--ro-bind"))

    def test_credentials_and_agent_files_inside_a_protected_tree_are_rejected(self):
        with self.assertRaises(ConfinementError) as caught:
            self.wrap(results_dirs=[str(self.auth.parent)])
        self.assertIn("credentials file", str(caught.exception))
        # An agent file is judged by what it resolves to, so a symlink into the repository is refused too.
        link = self.auth.parent / "linked.json"
        link.symlink_to(LAB / "runner.py")
        with self.assertRaises(ConfinementError) as caught:
            self.wrap(extra_agent_files=["linked.json"])
        self.assertIn("agent file", str(caught.exception))

    def test_executables_and_binds_under_a_fresh_mount_are_rejected(self):
        for directory in ("/tmp", "/run"):
            with mock.patch.object(confine_module.shutil, "which", return_value=f"{directory}/pi-install/pi"):
                with self.assertRaises(ConfinementError) as caught:
                    self.wrap()
            self.assertIn("ENOENT", str(caught.exception))
            self.assertIn(directory, str(caught.exception))
        with self.assertRaises(ConfinementError):
            self.wrap(ro_binds=["/run/toolchain"])

    def test_unreadable_git_metadata_raises_confinement_error(self):
        root = Path(self.tmp.name).resolve() / "repo"
        workspace = root / "ws"
        workspace.mkdir(parents=True)
        gitdir = root / "gitdir"
        gitdir.mkdir()
        (root / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        (gitdir / "commondir").write_bytes(b"\xff\xfe/not-utf8\n")
        with self.assertRaises(ConfinementError) as caught:
            confine(["python-agent"], workspace, auth_file=self.auth)
        self.assertIn("Git metadata", str(caught.exception))
        # A worktree pointer that is not UTF-8 is the same class of failure, not a UnicodeDecodeError.
        (gitdir / "commondir").write_text(str(gitdir), encoding="utf-8")
        worktree = gitdir / "worktrees" / "wt"
        worktree.mkdir(parents=True)
        (worktree / "gitdir").write_bytes(b"\xff\xfe\n")
        with self.assertRaises(ConfinementError):
            confine(["python-agent"], workspace, auth_file=self.auth)
        # So is metadata the harness cannot read at all.
        shutil.rmtree(gitdir / "worktrees")
        (gitdir / "commondir").chmod(0o000)
        self.addCleanup((gitdir / "commondir").chmod, 0o644)
        if os.access(gitdir / "commondir", os.R_OK):
            self.skipTest("running as root: unreadable metadata cannot be simulated")
        with self.assertRaises(ConfinementError):
            confine(["python-agent"], workspace, auth_file=self.auth)

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
            [d for _, d in pairs(command, "--ro-bind") if d.startswith(AGENT_DIR)], [f"{AGENT_DIR}/auth.json"]
        )
        self.assertEqual([d for _, d in pairs(command, "--bind") if d.startswith(AGENT_DIR)], [])

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
        for shared in ("/tmp", "/var/tmp", "/run"):
            with self.assertRaises(ConfinementError, msg=shared):
                self.wrap(results_dirs=[shared])
        with mock.patch.object(confine_module.shutil, "which", return_value=None):
            with self.assertRaises(ConfinementError):
                self.wrap()

    def test_sandbox_setup_failure_is_recognised_from_stderr(self):
        self.assertTrue(sandbox_setup_failed(1, "bwrap: setting up uid map: Permission denied\n"))
        self.assertTrue(sandbox_setup_failed(125, "confine-shim: OSError: too many open files\n"))
        self.assertFalse(sandbox_setup_failed(0, "bwrap: warning\n"))
        self.assertFalse(sandbox_setup_failed(1, "Error: model not found\n"))
        self.assertFalse(sandbox_setup_failed(None, ""))

    def test_a_shim_failure_is_reported_as_a_profile_failure(self):
        # The shim never silently execs without its descriptor work: a broken invocation is a sandbox failure.
        command = shim_command(["/bin/true"], seccomp_program())
        command[4] = "not-a-descriptor"
        done = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 125)
        self.assertTrue(done.stderr.startswith("confine-shim: ValueError"), done.stderr)
        self.assertTrue(sandbox_setup_failed(done.returncode, done.stderr))


class GradingProfileTests(unittest.TestCase):
    """The grading profile's command line (LOOP_SPEC §4.2a), checked without running bwrap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.check_dir = root / "check"
        self.check_dir.mkdir()
        self.results = root / "results"
        self.results.mkdir()
        patcher = mock.patch.object(confine_module.shutil, "which", side_effect=fake_which)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(confine_module, "FRESH_MOUNTS", ("/run",)):
            self.sandbox = GradingSandbox(results_dirs=[str(self.results)])

    def wrap(self):
        return self.sandbox.wrap([self.sandbox.interpreter, "-I", "-c", "pass"], self.check_dir)

    def test_no_network_no_credentials_and_only_the_check_directory_is_writable(self):
        command, env = self.wrap()
        command = bwrap_argv(command)
        options = command[: command.index("--")]
        self.assertIn("--unshare-all", options)
        self.assertNotIn("--share-net", options)
        self.assertEqual(pairs(command, "--bind"), [(str(self.check_dir), WORK_DIR)])
        self.assertFalse([d for _, d in pairs(command, "--ro-bind") if d.startswith(AGENT_DIR)])
        self.assertNotIn("PI_CODING_AGENT_DIR", env)
        self.assertEqual(env["HOME"], GRADING_HOME)
        self.assertEqual(env["TMPDIR"], WORK_DIR)
        # Every tmpfs the profile mounts is remounted read-only, after the check directory is bound.
        remounted = operands(command, "--remount-ro")
        for path in ("/", GRADING_HOME, "/dev", *operands(command, "--tmpfs")):
            self.assertIn(path, remounted)
        self.assertGreater(command.index("--remount-ro"), command.index(WORK_DIR))
        for flag in ("--die-with-parent", "--new-session", "--clearenv", "--disable-userns"):
            self.assertIn(flag, options)
        self.assertEqual(operands(command, "--seccomp"), [str(SECCOMP_FD)])

    def test_repository_home_sessions_and_results_are_hidden(self):
        command, _ = self.wrap()
        tmpfs = operands(bwrap_argv(command), "--tmpfs")
        covered = lambda path: any(  # noqa: E731
            str(path) == t or str(path).startswith(t.rstrip("/") + "/") for t in tmpfs
        )
        for path in (Path.home(), LAB.parent, LAB / "runner.py", self.results):
            self.assertTrue(covered(path), path)

    def test_it_is_launched_through_the_descriptor_closing_shim(self):
        command, _ = self.wrap()
        self.assertEqual(command[:3], [sys.executable, "-I", "-c"])
        self.assertEqual(command[3], confine_module.EXEC_SHIM)

    def test_only_its_own_interpreter_and_unprotected_check_directories_are_accepted(self):
        with self.assertRaisesRegex(ConfinementError, "only runs its own interpreter"):
            self.sandbox.wrap(["/bin/sh", "-c", "true"], self.check_dir)
        inside = self.results / "check"
        inside.mkdir()
        with self.assertRaisesRegex(ConfinementError, "overlaps the protected path"):
            self.sandbox.wrap([self.sandbox.interpreter], inside)
        with self.assertRaisesRegex(ConfinementError, "not a directory"):
            self.sandbox.wrap([self.sandbox.interpreter], self.check_dir / "missing")

    def test_an_interpreter_inside_the_repository_is_refused(self):
        # A (hypothetical) interpreter whose installation lies in the checkout would re-expose the repository.
        inside = LAB / "tests" / "fixtures" / "bin" / "python3"
        with mock.patch.object(confine_module.os, "access", return_value=True):
            with self.assertRaisesRegex(ConfinementError, "the grading interpreter .* overlaps the protected path"):
                GradingSandbox(interpreter=str(inside))


class HostAuthTests(unittest.TestCase):
    """prepare_host_auth keeps the host token valid for a whole trial without ever capturing it."""

    TOKEN = "ya29.FAKE-BEARER-TOKEN-VALUE"

    def fake_pi(self, exit_code=0, stderr=""):
        """Write a stand-in ``pi`` that records how it was called and prints a token to stdout."""
        directory = Path(tempfile.mkdtemp(prefix="confine-fake-pi-"))
        self.addCleanup(shutil.rmtree, directory, True)
        record = directory / "call.json"
        script = directory / "pi"
        script.write_text(
            textwrap.dedent(f"""\
                #!{sys.executable}
                import json, os, sys
                try:
                    stdout = os.readlink("/proc/self/fd/1")
                except OSError:
                    stdout = "?"
                with open({str(record)!r}, "w", encoding="utf-8") as handle:
                    json.dump({{"argv": sys.argv[1:], "stdout": stdout}}, handle)
                print({self.TOKEN!r})
                sys.stderr.write({stderr!r})
                sys.exit({exit_code})
                """),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script, record

    def test_refreshes_the_host_token_without_capturing_it(self):
        script, record = self.fake_pi()
        duration = prepare_host_auth("openai-codex", "gpt-5.6-sol", 900, pi_binary=str(script))
        self.assertEqual(duration, "15m")
        call = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(
            call["argv"],
            [
                "auth",
                "print-bearer-token",
                "--provider",
                "openai-codex",
                "--model",
                "gpt-5.6-sol",
                "--min-expiry",
                "15m",
            ],
        )
        # The token is printed to stdout, and stdout is /dev/null: it is never captured, logged or returned.
        self.assertEqual(call["stdout"], "/dev/null")
        self.assertNotIn(self.TOKEN, duration)

    def test_duration_uses_the_units_the_cli_accepts(self):
        script, record = self.fake_pi()
        for seconds, expected in ((3600, "1h"), (7200, "2h"), (900, "15m"), (60, "1m"), (90, "90s"), (1, "1s")):
            self.assertEqual(prepare_host_auth("p", "m", seconds, pi_binary=str(script)), expected)
            self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["argv"][-1], expected)
        for bad in (0, -60, 1.5, "15m"):
            with self.assertRaises(HostAuthError, msg=bad):
                prepare_host_auth("p", "m", bad, pi_binary=str(script))

    def test_failure_reports_only_the_exit_status_never_stderr(self):
        # A failing credential helper may print a token to stderr; none of that text may reach the exception.
        leaked = "sk-FAKE-STDERR-TOKEN-0123456789"
        for stderr, category in (
            (f"Error: Failed to resolve credential: {leaked}\n", "credential unavailable"),
            (f'\x1b[31mError: Unknown provider "{leaked}". Use --list-models\x1b[39m\n', "unknown provider or model"),
            (f"{leaked}\n", "unrecognised failure"),
        ):
            script, _ = self.fake_pi(exit_code=1, stderr=stderr)
            with self.assertLogs(level="DEBUG") as logs, self.assertRaises(HostAuthError) as caught:
                logging.getLogger().debug("start")  # assertLogs needs one record; the call itself must log none
                prepare_host_auth("openai-codex", "gpt-5.6-sol", 900, pi_binary=str(script))
            message = str(caught.exception)
            self.assertEqual(
                message,
                f"cannot prepare the host token for openai-codex/gpt-5.6-sol (15m): pi exited with status 1 ({category})",
            )
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn(leaked, "\n".join(logs.output))
            self.assertNotIn("resolve credential", message)
            self.assertNotIn(self.TOKEN, message)

    def test_a_timeout_does_not_carry_stderr_into_the_exception(self):
        script, _ = self.fake_pi()
        timeout = subprocess.TimeoutExpired([str(script)], 120, output=None, stderr=b"sk-FAKE-STDERR-TOKEN")
        with mock.patch.object(confine_module.subprocess, "run", side_effect=timeout):
            with self.assertRaises(HostAuthError) as caught:
                prepare_host_auth("p", "m", 900, pi_binary=str(script))
        self.assertIn("TimeoutExpired", str(caught.exception))
        self.assertNotIn("sk-FAKE", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_a_missing_pi_binary_is_an_auth_failure_not_a_crash(self):
        with self.assertRaises(HostAuthError):
            prepare_host_auth("p", "m", 900, pi_binary=str(Path(tempfile.gettempdir()) / "no-such-pi-binary"))


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
            "create_unix_socket",
            "connect_host_abstract_socket",
            "socketpair_datagram_to_host_abstract",
            "host_abstract_datagram_received_nothing",
            "io_uring_setup",
            "auth_refresh",
            "clean_fd_table",
            "outbound_tcp",
            "socketpair_works",
            "pipe_works",
            "auth_unchanged_on_host",
        ):
            self.assertIn(name, report["checks"])

    def test_grading_profile_probes_cannot_escape(self):
        ok, report = run_grading_selftest()
        failed = {name: report["probes"].get(name) for name, passed in report.get("checks", {}).items() if not passed}
        self.assertTrue(ok, json.dumps(failed or report, indent=2))
        self.assertIn("outbound_tcp", report["checks"])
        self.assertIn("read_results_dir", report["checks"])

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
            "create_unix_socket",
            "connect_host_abstract_socket",
            "socketpair_datagram_to_host_abstract",
        ):
            self.assertTrue(report["checks"][name], name)

    def fixture(self, auth_text="{}"):
        """A workspace and a credentials file outside every protected tree, plus a directory the profile hides."""
        root = Path(tempfile.mkdtemp(prefix="confine-fixture-"))
        self.addCleanup(shutil.rmtree, root, True)
        workspace = root / "workspaces" / "trial"
        workspace.mkdir(parents=True)
        # The agent directory sits outside the run root, as the real ~/.pi/agent does: confine() refuses to bind a
        # credentials file out of a protected tree.
        agent = Path(tempfile.mkdtemp(prefix="confine-agent-"))
        self.addCleanup(shutil.rmtree, agent, True)
        auth = agent / "auth.json"
        auth.write_text(auth_text, encoding="utf-8")
        return root, workspace, auth

    def probe(self, script, workspace, auth, pass_fds=(), **kwargs):
        """Run a probe script under the profile and return its JSON report."""
        command, env = confine(
            [sys.executable, "-I", "-c", script],
            workspace,
            auth_file=auth,
            ro_binds=[sys.base_prefix],
            **kwargs,
        )
        done = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=120, pass_fds=pass_fds, stdin=subprocess.DEVNULL
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1]), done

    LEAK_PROBE = textwrap.dedent("""
        import json, os
        targets = {}
        for name in os.listdir("/proc/self/fd"):
            try:
                targets[name] = os.readlink("/proc/self/fd/" + name)
            except OSError:
                continue
        report = {"fds": sorted(n for n, t in targets.items() if not t.endswith("/fd")), "targets": targets}
        try:
            os.lseek(LEAKED, 0, 0)
            report["through_fd"] = os.read(LEAKED, 64).decode("utf-8", "replace")
        except OSError as exc:
            report["through_fd"] = "error: " + str(exc)
        try:
            with open(HIDDEN, encoding="utf-8") as handle:
                report["by_path"] = handle.read()
        except OSError as exc:
            report["by_path"] = "error: " + str(exc)
        print(json.dumps(report))
        """)

    def leak_setup(self):
        root, workspace, auth = self.fixture()
        hidden = root / "held-out"
        hidden.mkdir()
        secret = hidden / "grader.py"
        secret.write_text("HELD-OUT-GRADER-CODE", encoding="utf-8")
        # A high descriptor number, so it cannot collide with the seccomp descriptor and look closed by accident.
        raw = os.open(secret, os.O_RDONLY)
        leaked = 21
        os.dup2(raw, leaked)
        os.close(raw)
        os.set_inheritable(leaked, True)
        self.addCleanup(os.close, leaked)
        script = self.LEAK_PROBE.replace("LEAKED", str(leaked)).replace("HIDDEN", repr(str(secret)))
        return root, workspace, auth, script, leaked

    def test_inherited_descriptors_cannot_bypass_the_hidden_mounts(self):
        root, workspace, auth, script, leaked = self.leak_setup()
        report, _ = self.probe(script, workspace, auth, results_dirs=[str(root)], pass_fds=(leaked,))
        # The caller leaked a descriptor to a hidden file; the shim closed it before bwrap ever started.
        self.assertEqual(report["fds"], ["0", "1", "2"])
        self.assertTrue(report["through_fd"].startswith("error: "), report["through_fd"])
        self.assertNotIn("HELD-OUT-GRADER-CODE", report["through_fd"])
        self.assertNotIn("HELD-OUT-GRADER-CODE", report["by_path"])
        # The seccomp program is handed over through a pipe, which must not be visible either.
        inherited = {n: t for n, t in report["targets"].items() if n not in ("0", "1", "2")}
        self.assertFalse([t for t in inherited.values() if t.startswith("pipe:")], report["targets"])

    def test_the_descriptor_probe_would_notice_a_missing_shim(self):
        root, workspace, auth, script, leaked = self.leak_setup()
        command, env = confine(
            [sys.executable, "-I", "-c", script], workspace, auth_file=auth, ro_binds=[sys.base_prefix]
        )
        # Drop the shim (and the seccomp descriptor it feeds) to launch bwrap the way the reviewer did.
        bare = bwrap_argv(command)
        index = bare.index("--seccomp")
        del bare[index : index + 2]
        done = subprocess.run(
            bare, env=env, capture_output=True, text=True, timeout=120, pass_fds=(leaked,), stdin=subprocess.DEVNULL
        )
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertIn(str(leaked), report["fds"])
        self.assertEqual(report["through_fd"], "HELD-OUT-GRADER-CODE")
        self.assertTrue(report["by_path"].startswith("error: "), report["by_path"])

    def test_host_abstract_sockets_are_unreachable_while_pipes_and_tcp_still_work(self):
        root, workspace, auth = self.fixture()
        name = "confine-test-" + uuid.uuid4().hex
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abstract.bind(b"\x00" + name.encode())
        abstract.listen(2)
        self.addCleanup(abstract.close)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(2)
        self.addCleanup(tcp.close)
        script = textwrap.dedent(f"""
            import json, os, socket, subprocess
            report = {{}}
            def probe(name, call):
                try:
                    call()
                    report[name] = "ok"
                except Exception as exc:
                    report[name] = type(exc).__name__ + ": " + str(exc)[:80]
            def connect_abstract():
                handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    handle.connect(b"\\x00" + {name!r}.encode())
                finally:
                    handle.close()
            def tcp_out():
                with socket.create_connection(("127.0.0.1", {tcp.getsockname()[1]}), timeout=10) as handle:
                    handle.send(b"ping")
            def pair():
                left, right = socket.socketpair()
                left.send(b"x")
                assert right.recv(1) == b"x"
            def pipe():
                read_fd, write_fd = os.pipe()
                os.write(write_fd, b"x")
                assert os.read(read_fd, 1) == b"x"
            probe("unix_socket", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            probe("connect_host_abstract", connect_abstract)
            probe("datagram_unix_socket", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
            probe("socketpair", pair)
            probe("pipe", pipe)
            probe("tcp", tcp_out)
            probe("bash", lambda: subprocess.run(["bash", "-c", "echo hi"], capture_output=True, check=True))
            probe("python", lambda: subprocess.run(["python3", "-I", "-c", "print(1)"], capture_output=True,
                                                   check=True))
            print(json.dumps(report))
            """)
        report, _ = self.probe(script, workspace, auth, results_dirs=[str(root)])
        for denied in ("unix_socket", "connect_host_abstract", "datagram_unix_socket"):
            self.assertIn("PermissionError", report[denied], f"{denied}: {report[denied]}")
        for allowed in ("socketpair", "pipe", "tcp", "bash", "python"):
            self.assertEqual(report[allowed], "ok", allowed)

    def test_a_datagram_socketpair_cannot_reach_a_host_abstract_socket(self):
        # The reviewer's bypass: socket(AF_UNIX) is refused, but socketpair(AF_UNIX, SOCK_DGRAM) was not, and a
        # datagram pair can connect() to any abstract name in the shared network namespace and send to it.
        root, workspace, auth = self.fixture()
        name = "confine-test-dgram-" + uuid.uuid4().hex
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(b"\x00" + name.encode())
        listener.setblocking(False)
        self.addCleanup(listener.close)
        stream_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream_listener.bind(b"\x00" + name.encode() + b"-stream")
        stream_listener.listen(2)
        stream_listener.setblocking(False)
        self.addCleanup(stream_listener.close)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(2)
        self.addCleanup(tcp.close)
        script = textwrap.dedent(f"""
            import json, os, socket, subprocess
            report = {{}}
            def probe(name, call):
                try:
                    call()
                    report[name] = "ok"
                except Exception as exc:
                    report[name] = type(exc).__name__ + ": " + str(exc)[:80]
            def bypass(kind, target={name!r}):
                def run():
                    try:
                        left, right = socket.socketpair(socket.AF_UNIX, kind)
                    except OSError as exc:
                        raise OSError(exc.errno, "at socketpair: " + exc.strerror) from None
                    left.connect(b"\\x00" + target.encode())
                    left.send(b"escaped")
                return run
            def stream_pair(kind):
                def run():
                    left, right = socket.socketpair(socket.AF_UNIX, kind)
                    left.send(b"x")
                    assert right.recv(1) == b"x"
                return run
            def pipe():
                read_fd, write_fd = os.pipe()
                os.write(write_fd, b"x")
                assert os.read(read_fd, 1) == b"x"
            def tcp_out():
                with socket.create_connection(("127.0.0.1", {tcp.getsockname()[1]}), timeout=10) as handle:
                    handle.send(b"ping")
            def run_child(argv):
                done = subprocess.run(argv, input="in", capture_output=True, text=True, check=True)
                assert done.stdout.strip() == "in", done.stdout
            probe("dgram", bypass(socket.SOCK_DGRAM))
            probe("dgram_nonblock_cloexec", bypass(socket.SOCK_DGRAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC))
            probe("raw", bypass(socket.SOCK_RAW))
            probe("seqpacket", bypass(socket.SOCK_SEQPACKET))
            probe("default_socketpair", stream_pair(socket.SOCK_STREAM))
            probe("stream_nonblock_cloexec", stream_pair(socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC))
            probe("stream_pair_connect", bypass(socket.SOCK_STREAM, {name + "-stream"!r}))
            probe("pipe", pipe)
            probe("tcp", tcp_out)
            probe("bash", lambda: run_child(["bash", "-c", "cat"]))
            probe("python", lambda: run_child(["python3", "-I", "-c", "import sys; print(sys.stdin.read())"]))
            print(json.dumps(report))
            """)
        report, _ = self.probe(script, workspace, auth, results_dirs=[str(root)])
        for denied in ("dgram", "dgram_nonblock_cloexec", "raw", "seqpacket"):
            self.assertTrue(
                report[denied].startswith("PermissionError: [Errno 1] at socketpair"), f"{denied}: {report}"
            )
        for allowed in ("default_socketpair", "stream_nonblock_cloexec", "pipe", "tcp", "bash", "python"):
            self.assertEqual(report[allowed], "ok", f"{allowed}: {report}")
        # A stream pair is allowed because it is already connected for good, even to a host stream listener.
        self.assertIn("[Errno 106]", report["stream_pair_connect"])  # EISCONN
        with self.assertRaises(BlockingIOError):
            listener.recv(64)
        with self.assertRaises(BlockingIOError):
            stream_listener.accept()

    def test_the_datagram_probe_reaches_the_host_without_the_profile(self):
        # Control for the test above: unconfined, the same bypass delivers its datagram.
        name = "confine-test-dgram-" + uuid.uuid4().hex
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(b"\x00" + name.encode())
        listener.settimeout(10)
        self.addCleanup(listener.close)
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.connect(b"\x00" + name.encode())
        left.send(b"escaped")
        self.assertEqual(listener.recv(64), b"escaped")

    def test_a_refresh_inside_the_sandbox_fails_and_never_touches_the_host_file(self):
        original = '{"host": "original"}'
        root, workspace, auth = self.fixture(auth_text=original)
        script = textwrap.dedent(f"""
            import json, os
            report = {{}}
            def probe(name, call):
                try:
                    call()
                    report[name] = "ok"
                except Exception as exc:
                    report[name] = type(exc).__name__ + ": " + str(exc)[:80]
            def read_auth():
                with open("{AGENT_DIR}/auth.json", encoding="utf-8") as handle:
                    assert json.load(handle) == {{"host": "original"}}
            def refresh():
                # What Pi does: take the lock directory beside auth.json, then rewrite the file in place.
                os.mkdir("{AGENT_DIR}/auth.json.lock")
                with open("{AGENT_DIR}/auth.json", "w", encoding="utf-8") as handle:
                    handle.write('{{"rewritten": true}}')
            probe("read_auth", read_auth)
            probe("refresh", refresh)
            print(json.dumps(report))
            """)
        report, _ = self.probe(script, workspace, auth, results_dirs=[str(root)])
        # The agent may read its own credentials, but a refresh fails cleanly: the runner calls prepare_host_auth.
        self.assertEqual(report["read_auth"], "ok")
        self.assertIn("Error", report["refresh"])
        self.assertIn("read-only", report["refresh"].lower())
        self.assertEqual(auth.read_text(encoding="utf-8"), original)

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
            start("1", "bash", command="cat /home/user/project/autonomy_lab/runner.py"),
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
            start("1", "bash", command="ls /home/user/.pi/agent/sessions"),
            end("1", "bash", "ls: cannot access '/home/user/.pi/agent/sessions': No such file or directory", True),
            start("2", "bash", command="python3 -c \"open('../x/runner.py')\""),
            end("2", "bash", "FileNotFoundError: [Errno 2] No such file or directory: '../x/runner.py'", True),
            start("3", "bash", command="cd .. && cat autonomy_lab/runner.py"),
            end("3", "bash", "cat: autonomy_lab/runner.py: No such file or directory", True),
            start("4", "write", path="/etc/x", content="data"),
            end("4", "write", "EROFS: read-only file system, open '/etc/x'", True),
            start("5", "bash", command="touch /work/ok"),
            end("5", "bash", "touch: cannot touch 'ok': Permission denied", True),
        ]
        signals = scan_confinement_signals(events)
        denied = [s["detail"] for s in signals if s["kind"] == "denied_path"]
        self.assertEqual(denied, ["bash: /home/user/.pi/agent/sessions", "bash: ../x/runner.py", "bash: .."])
        # The write to /etc/x was refused by the profile; /etc is on the argument allowlist, the denial is not.
        self.assertIn({"kind": "denied_operation", "detail": "write: read-only file system: /etc/x"}, signals)
        # No path in the message and none in the arguments: the message itself is the detail.
        self.assertIn(
            {
                "kind": "denied_operation",
                "detail": "bash: permission denied: touch: cannot touch 'ok': Permission denied",
            },
            signals,
        )

    def test_denied_operation_is_a_signal_even_when_the_path_is_on_the_system_allowlist(self):
        # The reviewer's case: a confined `touch /etc/x` yielded no signal at all, because /etc is allowlisted.
        events = [
            start("1", "bash", command="touch /etc/x"),
            end("1", "bash", "touch: cannot touch '/etc/x': Read-only file system", True),
            start("2", "bash", command="mkdir /opt/pkg"),
            end("2", "bash", "mkdir: cannot create directory '/opt/pkg': Permission denied", True),
            start("3", "bash", command="mount -o remount,rw /"),
            end("3", "bash", "mount: /: mount point not mounted or bad option. Operation not permitted", True),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual({s["kind"] for s in signals}, {"denied_operation", "absolute_path"})
        details = [s["detail"] for s in signals if s["kind"] == "denied_operation"]
        self.assertEqual(
            details,
            [
                "bash: read-only file system: /etc/x",
                "bash: permission denied: /opt/pkg",
                "bash: operation not permitted: /",
            ],
        )

    def test_doubled_leading_slash_and_dot_segments_are_recognised(self):
        # The reviewer's case: `//home/...` was treated as a non-path and dropped.
        events = [
            start("1", "bash", command="cat //home/user/project/autonomy_lab/runner.py"),
            start("2", "read", path="///home/user/.pi/agent/sessions/x.jsonl"),
            start("3", "read", path="/home/./user/notes.txt"),
            start("4", "bash", command="cat /work/./sub/../ledger.py; curl https://example.com/a/b"),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual({s["kind"] for s in signals}, {"absolute_path"})
        details = [s["detail"] for s in signals]
        self.assertIn("bash: //home/user/project/autonomy_lab/runner.py", details)
        self.assertIn("read: ///home/user/.pi/agent/sessions/x.jsonl", details)
        self.assertIn("read: /home/./user/notes.txt", details)
        self.assertEqual(len(signals), 3)

    def test_host_like_double_slash_paths_and_file_urls_are_recognised(self):
        # The reviewer's cases: `//host.like/...` was taken for a URL authority, and file:// URLs were dropped whole.
        events = [
            start("1", "bash", command="cat //home.dir/g/notes.txt"),
            start("2", "bash", command="curl 'file:///home/user/.pi/agent/auth.json'; curl https://example.com/a/b"),
            start("3", "bash", command="python3 -c \"urlopen('file://localhost/srv/repo/runner.py')\""),
            start("4", "read", path="file:///home/user/x.jsonl"),
            start("5", "bash", command="curl file:///work/ledger.py file:///usr/share/x"),
            start("6", "bash", command="curl file:///home/user/missing"),
            end("6", "bash", "curl: (37) Couldn't open file file:///home/user/missing: No such file or directory", True),
        ]
        signals = scan_confinement_signals(events)
        self.assertEqual(
            signals,
            [
                {"kind": "absolute_path", "detail": "bash: //home.dir/g/notes.txt"},
                {"kind": "absolute_path", "detail": "bash: file:///home/user/.pi/agent/auth.json"},
                {"kind": "absolute_path", "detail": "bash: file://localhost/srv/repo/runner.py"},
                {"kind": "absolute_path", "detail": "read: file:///home/user/x.jsonl"},
                {"kind": "absolute_path", "detail": "bash: file:///home/user/missing"},
                {"kind": "denied_path", "detail": "bash: file:///home/user/missing"},
            ],
        )

    def test_normalise_path(self):
        cases = {
            "//home/user": "/home/user",
            "///home/user": "/home/user",
            "/home/./user": "/home/user",
            "/work/sub/../ledger.py": "/work/ledger.py",
            "//work/./x/..": "/work",
            "../x": "../x",
            "": "",
        }
        for word, expected in cases.items():
            self.assertEqual(normalise_path(word), expected, word)

    def test_malformed_events_are_reported_instead_of_crashing_the_scan(self):
        events = [
            {"type": "tool_execution_start", "toolCallId": [], "toolName": "bash", "args": {"command": "ls .."}},
            {"type": "tool_execution_end", "toolCallId": {}, "toolName": "bash", "result": "No such file"},
            {"type": "tool_execution_start", "toolCallId": "2", "toolName": 7, "args": {"path": "../runner.py"}},
            start("3", "read", path="~/secret"),
        ]
        signals = scan_confinement_signals(events)
        kinds = [s["kind"] for s in signals]
        self.assertEqual(kinds.count("malformed_event"), 2)
        self.assertTrue(any("TypeError" in s["detail"] for s in signals if s["kind"] == "malformed_event"))
        # Scanning continues: the well-formed events after the malformed ones are still classified.
        self.assertIn({"kind": "parent_relative_path", "detail": "?: ../runner.py"}, signals)
        self.assertIn({"kind": "home_path", "detail": "read: ~/secret"}, signals)

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
            "/home/user": "absolute_path",
            "/": None,
            "//": None,
            "/usr/bin/python3": None,
            "/work": None,
            "/workspace": "absolute_path",
            "~": "home_path",
            "$HOME/x": "home_path",
            "x.py": None,
            "...": None,
            "//home/user": "absolute_path",
            "///home/user": "absolute_path",
            "/home/./user": "absolute_path",
            "//work/x": None,
            "//usr/bin/python3": None,
            # Linux resolves a doubled leading slash as an absolute path, whatever follows it.
            "//example.com/a/b": "absolute_path",
            "//registry.npmjs.org:443/x": "absolute_path",
            "//host.like/etc": "absolute_path",
            # File URLs name local paths.
            "file:///home/user/.pi/agent/auth.json": "absolute_path",
            "file://localhost/home/user": "absolute_path",
            "FILE:///srv/repo": "absolute_path",
            "file://host.like/x": "absolute_path",
            "file:///work/%2e%2e/home": "parent_relative_path",
            "file:///work/ledger.py": None,
            "file:///usr/lib/x": None,
            "file://": None,
        }
        for word, kind in cases.items():
            self.assertEqual(classify_path(word), kind, word)


if __name__ == "__main__":
    unittest.main()
