"""Bubblewrap confinement for benchmark Pi processes (LOOP_SPEC.md §4.2), and a confinement-signal scanner.

``confine(command, workspace)`` returns ``(command, env)``: the command wrapped in a ``bwrap`` profile, and the
complete environment to start it with. Inside the profile:

- the host root is rebuilt from read-only binds of each readable top-level directory;
- ``$HOME``, the repository (every worktree and its Git directory), ``/tmp``, ``/var/tmp``, ``/run``, the workspace's
  parent (sibling trials), the run directory in pi_runner's ``<run>/workspaces/<trial>`` layout, and any caller-supplied
  results directories are empty tmpfs mounts. ``/run`` is hidden because
  it holds daemon sockets (Docker, Tailscale, user agents) that would bypass the profile; only files that ``/etc``
  symlinks resolve into, such as the resolver's ``resolv.conf``, are bound back read-only;
- the trial workspace is bound read-write at ``/work``, which is also the working directory;
- the directory of the wrapped executable (the Pi installation) is bound read-only at its host path. That bind, and
  every caller-supplied one, is refused when it equals, contains or lies inside a protected tree (the repository and
  its worktrees, ``$HOME/.pi/agent/sessions``, the run root, the results directories, other workspaces), so an
  installation path cannot re-expose held-out code or another trial's data;
- ``PI_CODING_AGENT_DIR`` is ``/pi-agent``, a private tmpfs that holds only the real ``auth.json``, bound READ-ONLY;
- PID, IPC, UTS, cgroup and user namespaces are new, nested user namespaces are disabled, ``/proc`` and ``/dev`` are
  fresh, the environment is cleared to a minimal allowlist, and the network is shared because the model provider needs
  it;
- a classic-BPF seccomp filter (x86_64 only) returns ``EPERM`` for ``socket()`` with ``AF_UNIX``, for
  ``socketpair()`` with ``AF_UNIX`` and any type but ``SOCK_STREAM``, and for the ``io_uring`` syscalls: the shared
  network namespace would otherwise let the agent reach host *abstract* Unix sockets (user daemons and agents), which
  no mount can hide. A datagram socketpair is not connected for good: ``connect()`` re-targets it at any abstract
  name. A stream socketpair is (``connect()`` returns ``EISCONN``), so ``pipe``, stream ``socketpair`` and TCP keep
  working;
- the profile is launched through a short ``python3 -I -c`` exec shim that closes every inherited descriptor except
  stdin, stdout, stderr and the seccomp program, so a caller that leaks a descriptor to a hidden file (``pass_fds``,
  ``close_fds=False``, an inherited log file) cannot punch a hole through the mount profile.

OAuth refresh is a HOST responsibility. ``auth.json`` is read-only inside the profile, so a refresh attempted by the
confined process fails cleanly (the runner classifies it as an ``auth`` infrastructure failure) and can neither
corrupt the host file nor race a host Pi process over its lock directory. The runner calls :func:`prepare_host_auth`
before each trial to make the host token valid for at least the trial's duration.

This is confinement for trusted first-party tasks, not a hardened sandbox: the agent can read its own provider
credentials and the rest of the read-only host filesystem.

:class:`GradingSandbox` is the stricter grading profile (LOOP_SPEC.md §4.2a) for held-out and visible checks, which
run agent-written code: no network, no credentials, an empty read-only ``$HOME``, and only the check's own directory
writable. ``selftest`` runs the probes of both profiles.
"""

import argparse
import json
import os
import platform
import posixpath
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

WORK_DIR = "/work"
AGENT_DIR = "/pi-agent"
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
PASSTHROUGH_ENV = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
# Top-level host entries that get fresh or private mounts instead of a read-only bind.
SPECIAL_ROOT_ENTRIES = {"proc", "dev", "tmp", "run"}
# Mounted fresh and empty for every trial, so nothing beneath them needs hiding again.
FRESH_MOUNTS = ("/tmp", "/run")
# Paths that can never be hidden without breaking the profile.
UNHIDEABLE = {"/", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/dev", "/sys", "/run", "/var"}
# Shared directories that cannot be protected as a unit: every process keeps temporary files there, so a workspace
# created under one must not turn the whole mount into a protected tree. Subtrees of them still can be.
SHARED_ROOTS = frozenset({*UNHIDEABLE, *FRESH_MOUNTS, "/var/tmp"})
# Filesystem and metadata failures that mean "the profile cannot be built", not "the harness has a bug".
SETUP_ERRORS = (OSError, UnicodeDecodeError, ValueError)


class ConfinementError(RuntimeError):
    """The confinement profile could not be set up; the trial is a "sandbox" infrastructure failure."""


class HostAuthError(RuntimeError):
    """The host token could not be prepared for a trial; the trial is an "auth" infrastructure failure."""


# --- seccomp filter: host abstract Unix sockets survive the shared network namespace ---------------------------

# struct seccomp_data { int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6]; }
SECCOMP_DATA_NR = 0
SECCOMP_DATA_ARCH = 4
SECCOMP_DATA_ARG0 = 16  # low 32 bits of args[0] on a little-endian machine
SECCOMP_DATA_ARG1 = 24  # low 32 bits of args[1]; the kernel reads both as int, so the high bits never matter
AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
AF_UNIX = 1
SOCK_STREAM = 1
# The socket type without the SOCK_NONBLOCK and SOCK_CLOEXEC flags the type argument may carry.
SOCK_TYPE_MASK = 0xF
# x86_64 syscall numbers. io_uring can create and use sockets without ever calling socket().
NR_SOCKET = 41
NR_SOCKETPAIR = 53
NR_IO_URING = (425, 426, 427)  # io_uring_setup, io_uring_enter, io_uring_register
# Classic BPF: load a word at an absolute offset, compare, test bits, return an action.
BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JSET_K = 0x45
BPF_ALU_AND_K = 0x54
BPF_RET_K = 0x06
# The descriptor the exec shim hands to bwrap's --seccomp.
SECCOMP_FD = 3


def seccomp_program():
    """Assemble the profile's classic-BPF seccomp program as a ``struct sock_filter`` array for ``--seccomp``.

    Kills anything that is not the x86_64 ABI (including x32, which renumbers syscalls), returns ``EPERM`` for
    ``socket()`` with ``AF_UNIX``, for ``socketpair()`` with ``AF_UNIX`` unless the type is ``SOCK_STREAM`` (flags
    aside), and for the ``io_uring`` syscalls, and allows everything else. ``pipe()`` is a different syscall and keeps
    working, as do the stream socketpairs that subprocess plumbing relies on.
    """
    machine = platform.machine()
    if machine != "x86_64":
        raise ConfinementError(f"the seccomp filter is assembled for x86_64 only, not {machine}")
    code = []
    labels = {}

    def emit(op, k=0, jt="next", jf="next"):
        code.append((op, k, jt, jf))

    emit(BPF_LD_W_ABS, SECCOMP_DATA_ARCH)
    emit(BPF_JEQ_K, AUDIT_ARCH_X86_64, jf="kill")  # another architecture would renumber every syscall below
    emit(BPF_LD_W_ABS, SECCOMP_DATA_NR)
    emit(BPF_JSET_K, X32_SYSCALL_BIT, jt="kill")
    emit(BPF_JEQ_K, NR_SOCKET, jt="unix_domain")
    emit(BPF_JEQ_K, NR_SOCKETPAIR, jt="unix_pair")
    for number in NR_IO_URING:
        emit(BPF_JEQ_K, number, jt="deny")
    emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    labels["unix_domain"] = len(code)
    emit(BPF_LD_W_ABS, SECCOMP_DATA_ARG0)
    emit(BPF_JEQ_K, AF_UNIX, jt="deny", jf="allow")
    labels["unix_pair"] = len(code)
    emit(BPF_LD_W_ABS, SECCOMP_DATA_ARG0)
    emit(BPF_JEQ_K, AF_UNIX, jf="allow")
    # A datagram pair can connect() to a host abstract name; a stream pair is connected for good (EISCONN).
    # SOCK_RAW counts as datagram for AF_UNIX, and SOCK_SEQPACKET has no use here, so only stream is allowed.
    emit(BPF_LD_W_ABS, SECCOMP_DATA_ARG1)
    emit(BPF_ALU_AND_K, SOCK_TYPE_MASK)
    emit(BPF_JEQ_K, SOCK_STREAM, jt="allow", jf="deny")
    labels["deny"] = len(code)
    emit(BPF_RET_K, SECCOMP_RET_ERRNO | EPERM)
    labels["allow"] = len(code)
    emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    labels["kill"] = len(code)
    emit(BPF_RET_K, SECCOMP_RET_KILL_PROCESS)
    program = b""
    for index, (op, k, jt, jf) in enumerate(code):
        jumps = []
        for target in (jt, jf):
            destination = index + 1 if target == "next" else labels[target]
            offset = destination - index - 1
            if not 0 <= offset <= 255:
                raise ConfinementError(f"seccomp jump to {target} is out of range")
            jumps.append(offset)
        program += struct.pack("=HBBI", op, jumps[0], jumps[1], k)
    return program


# --- exec shim: inherited descriptors bypass the hidden mounts --------------------------------------------------

# Runs on the host in place of bwrap, whatever the caller's Popen flags are. It closes every descriptor the caller
# leaked, feeds the seccomp program to bwrap through a pipe (no temporary file, nothing on disk), and execs bwrap.
# bwrap reads the program during setup and does not pass it on, so the confined process keeps only 0, 1 and 2.
SHIM_PREFIX = "confine-shim: "
EXEC_SHIM = "\n".join(
    (
        "import os, sys",
        "try:",
        "    seccomp_fd = int(sys.argv[1])",
        "    program = bytes.fromhex(sys.argv[2])",
        "    try:",
        "        limit = max(1 << 16, os.sysconf('SC_OPEN_MAX'))",
        "    except (OSError, ValueError):",
        "        limit = 1 << 16",
        "    os.closerange(3, limit)",
        "    read_fd, write_fd = os.pipe()",
        "    os.write(write_fd, program)",
        "    os.close(write_fd)",
        "    if read_fd != seccomp_fd:",
        "        os.dup2(read_fd, seccomp_fd)",
        "        os.close(read_fd)",
        "    os.set_inheritable(seccomp_fd, True)",
        "    os.execv(sys.argv[3], sys.argv[3:])",
        "except BaseException as exc:",
        # Report like bwrap does, so sandbox_setup_failed() classifies this as a profile failure, not a model failure.
        "    sys.stderr.write({prefix!r} + type(exc).__name__ + ': ' + str(exc)[:200] + chr(10))",
        "    os._exit(125)",
    )
).format(prefix=SHIM_PREFIX)


def shim_command(bwrap_command, program):
    """Prepend the descriptor-closing exec shim to a bwrap command line."""
    return [sys.executable, "-I", "-c", EXEC_SHIM, str(SECCOMP_FD), program.hex(), *bwrap_command]


def default_auth_file():
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    return Path(agent_dir).expanduser() / "auth.json"


def find_bwrap():
    found = shutil.which("bwrap")
    if found is None:
        raise ConfinementError("bubblewrap (bwrap) is not installed or not on PATH")
    return found


def bwrap_unavailable_reason():
    """Return None when bwrap can create the namespaces the profile uses, else a reason string."""
    try:
        bwrap = find_bwrap()
    except ConfinementError as exc:
        return str(exc)
    probe = [bwrap, "--unshare-all", "--share-net", "--unshare-user", "--disable-userns", "--ro-bind", "/", "/"]
    probe += ["--proc", "/proc", "--dev", "/dev", "true"]
    try:
        result = subprocess.run(probe, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bwrap probe failed: {exc}"
    if result.returncode != 0:
        return "bwrap cannot create namespaces: " + (result.stderr.strip() or f"exit {result.returncode}")
    return None


def sandbox_setup_failed(exit_code, stderr_text):
    """True when a wrapped process failed because the profile itself could not be set up (bwrap or the exec shim)."""
    if exit_code in (0, None):
        return False
    return any(line.startswith(("bwrap: ", SHIM_PREFIX)) for line in (stderr_text or "").splitlines())


def _is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def overlaps(path, other):
    """True when one of two absolute path strings or Paths equals or contains the other."""
    return _is_within(str(path), str(other)) or _is_within(str(other), str(path))


def repository_trees():
    """Every checkout and Git directory of the repository holding this harness (its worktrees included)."""
    return sorted(_git_paths(Path(__file__).resolve().parent))


def _git_paths(start):
    """Return every checkout and Git directory of the repository containing ``start``, without running git.

    Unreadable, corrupt or non-UTF-8 Git metadata is a profile setup failure, not a crash: a worktree whose
    ``gitdir`` or ``commondir`` cannot be decoded would otherwise escape as ``UnicodeDecodeError``.
    """
    try:
        return _git_paths_unchecked(start)
    except SETUP_ERRORS as exc:
        raise ConfinementError(f"cannot read the Git metadata around {start}: {exc!r}") from exc


def _git_paths_unchecked(start):
    paths = set()
    for directory in [start, *start.parents]:
        marker = directory / ".git"
        if not marker.exists():
            continue
        paths.add(directory)
        if marker.is_dir():
            common = marker
        else:
            match = re.match(r"gitdir:\s*(.+)", marker.read_text(encoding="utf-8", errors="replace").strip())
            if not match:
                break
            gitdir = (directory / match.group(1)).resolve()
            paths.add(gitdir)
            commondir = gitdir / "commondir"
            if commondir.is_file():
                common = (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve()
            else:
                common = gitdir
        paths.add(common)
        if common.name == ".git":
            paths.add(common.parent)
        worktrees = common / "worktrees"
        if worktrees.is_dir():
            for entry in worktrees.iterdir():
                link = entry / "gitdir"
                if link.is_file():
                    paths.add(Path(link.read_text(encoding="utf-8").strip()).resolve().parent)
        break
    return paths


def _run_file_binds():
    """Bind back the /run files that /etc symlinks point to (e.g. systemd-resolved's resolv.conf), and nothing else."""
    binds = []
    try:
        for entry in sorted(Path("/etc").iterdir()):
            if not entry.is_symlink():
                continue
            target = entry.resolve()
            if _is_within(str(target), "/run") and target.is_file():
                binds += ["--ro-bind", str(target), str(target)]
    except OSError as exc:
        raise ConfinementError(f"cannot inspect /etc symlinks: {exc}") from exc
    return binds


def _protected_trees(workspace, results_dirs):
    """Return the trees that must never be readable inside the profile, as resolved absolute path strings.

    These are the held-out grader code and other trials' data: the repository with every worktree and Git directory,
    the user's real Pi sessions, the caller's results directories, and the sibling workspaces and run root of
    pi_runner's ``<run>/workspaces/<trial>`` layout. Hiding them with a tmpfs is not enough; no later bind may
    re-expose them, so :func:`confine` also rejects binds that overlap this set.

    A shared root such as ``/tmp`` is never protected as a whole, only the specific directories below it: the
    profile replaces those mounts with an empty tmpfs anyway, and protecting all of ``/tmp`` because a workspace was
    created there would reject the credentials file of every caller that uses a temporary directory.
    """
    trees = {
        Path.home().resolve() / ".pi" / "agent" / "sessions",
        *_git_paths(Path(__file__).resolve().parent),
    }
    if workspace is not None:
        trees.update(_git_paths(workspace))
    for item in results_dirs:
        path = Path(item).expanduser()
        if not path.is_absolute():
            raise ConfinementError(f"results directory must be absolute: {item}")
        if str(path) in SHARED_ROOTS:
            raise ConfinementError(f"refusing to protect the shared directory {path}")
        trees.add(path)
    # Sibling trial workspaces, unless the workspace sits directly in a system directory. In pi_runner's
    # <run>/workspaces/<trial> layout, the whole run directory, including every trial's logs.
    if workspace is not None and str(workspace.parent) not in SHARED_ROOTS:
        trees.add(workspace.parent)
        if workspace.parent.name == "workspaces" and str(workspace.parent.parent) not in SHARED_ROOTS:
            trees.add(workspace.parent.parent)
    resolved = _resolved(trees)
    if workspace is not None and str(workspace) in resolved:
        raise ConfinementError("the workspace itself cannot be a protected path")
    return sorted(resolved)


def _resolved(paths):
    try:
        return {str(Path(path).resolve()) for path in paths}
    except SETUP_ERRORS as exc:
        raise ConfinementError(f"cannot resolve a profile path: {exc!r}") from exc


def _hidden_paths(workspace, protected):
    # /tmp and /run are always fresh tmpfs mounts; see confine().
    candidates = {*protected, *_resolved({Path.home(), Path("/var/tmp")})}
    hidden = []
    for path in sorted(candidates):
        if path in UNHIDEABLE:
            raise ConfinementError(f"refusing to hide system path {path}")
        if workspace is not None and path == str(workspace):
            raise ConfinementError("the workspace itself cannot be a hidden path")
        if not os.path.lexists(path) or any(_is_within(path, kept) for kept in [*FRESH_MOUNTS, *hidden]):
            continue
        hidden.append(path)
    return hidden


def _check_bind(path, protected, description, same_path=True):
    """Reject a bind that would re-expose protected data, or that cannot work where it is asked to appear.

    A bind is refused when it equals a protected tree, lies inside one, or contains one: binding
    ``executable.parent`` back after hiding its parent silently re-exposed a protected results directory.

    With ``same_path`` (a bind that appears at its own host path, such as the Pi installation), a source under a
    mount the profile replaces is refused too, because that path is empty inside the profile and ``execvp`` would
    fail with ENOENT. Single files bound into the private agent directory are exempt: they appear elsewhere.
    """
    for tree in protected:
        if path == tree or _is_within(path, tree) or _is_within(tree, path):
            raise ConfinementError(f"{description} {path} overlaps the protected path {tree}")
    if not same_path:
        return
    for mount in FRESH_MOUNTS:
        if _is_within(path, mount):
            raise ConfinementError(
                f"{description} {path} lies under {mount}, which the profile mounts fresh and empty, so the "
                f"confined process would fail with ENOENT. Use a copy outside {mount} instead."
            )


def confine(
    command,
    workspace,
    *,
    results_dirs=(),
    auth_file=None,
    extra_agent_files=(),
    ro_binds=(),
):
    """Wrap ``command`` (a Pi command line) so it runs confined, with ``workspace`` at ``/work``.

    ``results_dirs`` are further host paths to hide, such as the run root holding other trials' logs.
    ``extra_agent_files`` are agent-directory files (e.g. ``models.json``) to expose read-only; by default only
    ``auth.json`` is exposed. ``ro_binds`` are extra host paths bound read-only at the same path. Any of them that
    would re-expose protected data, or that lives under a mount the profile replaces, is rejected.
    Returns ``(wrapped_command, env)``; ``env`` is the complete environment, not a delta.
    """
    if not command:
        raise ConfinementError("empty command")
    bwrap = find_bwrap()
    program = seccomp_program()
    try:
        workspace = Path(workspace).resolve()
        if not workspace.is_dir():
            raise ConfinementError(f"workspace is not a directory: {workspace}")
    except SETUP_ERRORS as exc:
        raise ConfinementError(f"cannot use the workspace {workspace}: {exc!r}") from exc
    found = shutil.which(command[0])
    if found is None:
        raise ConfinementError(f"executable not found: {command[0]}")
    executable = Path(found).resolve()
    auth = Path(auth_file).expanduser() if auth_file is not None else default_auth_file()
    if not auth.is_file():
        raise ConfinementError(f"Pi credentials file not found: {auth}")
    agent_files = []
    for name in extra_agent_files:
        source = auth.parent / name
        if "/" in name or name in ("", ".", "..", "auth.json") or not source.is_file():
            raise ConfinementError(f"agent file is not an existing plain file name: {name}")
        agent_files.append((source, name))

    protected = _protected_trees(workspace, results_dirs)
    hidden = _hidden_paths(workspace, protected)
    # Every host path the profile re-exposes by name, checked before anything is mounted.
    extra_paths = []
    for path in _ordered_resolved(ro_binds):
        _check_bind(path, protected, "read-only bind")
        extra_paths.append(path)
    _check_bind(str(executable.parent), protected, "the Pi installation directory")
    _check_bind(str(auth.resolve()), protected, "the credentials file", same_path=False)
    for source, _ in agent_files:
        _check_bind(str(Path(source).resolve()), protected, "agent file", same_path=False)

    args = [bwrap, "--unshare-all", "--share-net", "--unshare-user", "--disable-userns"]
    args += ["--die-with-parent", "--new-session", "--seccomp", str(SECCOMP_FD)]
    args += ["--proc", "/proc", "--dev", "/dev"]
    args += _host_view(hidden, [str(executable.parent), *extra_paths])
    args += ["--perms", "0700", "--tmpfs", AGENT_DIR]
    # Read-only: a token refresh is the host's job (prepare_host_auth), so the confined process can neither corrupt
    # auth.json nor race a host Pi process over its lock directory. A refresh inside fails cleanly instead.
    args += ["--ro-bind", str(auth), f"{AGENT_DIR}/auth.json"]
    for source, name in agent_files:
        args += ["--ro-bind", str(source), f"{AGENT_DIR}/{name}"]
    args += ["--bind", str(workspace), WORK_DIR, "--chdir", WORK_DIR, "--remount-ro", "/"]

    env = {
        "PATH": SYSTEM_PATH,
        "HOME": str(Path.home()),
        "TERM": "dumb",
        "TMPDIR": "/tmp",
        "PI_CODING_AGENT_DIR": AGENT_DIR,
        "PI_CODING_AGENT_SESSION_DIR": f"{AGENT_DIR}/sessions",
        "PI_SKIP_VERSION_CHECK": "1",
    }
    for name in ("USER", "LOGNAME", *PASSTHROUGH_ENV):
        if os.environ.get(name):
            env[name] = os.environ[name]
    # --clearenv keeps the profile's environment minimal even if the caller starts bwrap with its own environment.
    args.append("--clearenv")
    for name, value in sorted(env.items()):
        args += ["--setenv", name, value]
    return shim_command([*args, "--", str(executable), *command[1:]], program), env


# --- grading profile (LOOP_SPEC §4.2a) --------------------------------------------------------------------------

GRADING_HOME = "/grading-home"


class GradingSandbox:
    """Bubblewrap profile for one grading check: agent-written code runs here, so it gets less than Pi does.

    Compared with :func:`confine`, the grading profile has no network (``--unshare-all`` without ``--share-net``), no
    agent directory or credentials, an empty read-only ``$HOME`` at ``/grading-home``, and every tmpfs it mounts
    (``/tmp``, ``/run``, ``/dev`` and the hidden trees) remounted read-only, so the check's own directory, bound at
    ``/work``, is its only writable location. The same trees stay hidden (the repository with every worktree, the
    user's Pi sessions, ``$HOME``, and the caller's ``results_dirs`` such as the run root and the task bank), and the
    same seccomp filter and descriptor-closing shim apply.

    The host-side work (protected trees, the host root view) is done once in the constructor; :meth:`wrap` only adds
    the per-check bind. The interpreter is the real path of ``interpreter`` (default: this one); its installation
    prefix is bound back read-only when it lies under a hidden tree, such as a uv-managed Python under ``$HOME``.
    """

    def __init__(self, results_dirs=(), interpreter=None):
        self._bwrap = find_bwrap()
        self._program = seccomp_program()
        try:
            self.interpreter = os.path.realpath(interpreter or sys.executable)
            prefix = os.path.realpath(sys.base_prefix if interpreter is None else Path(self.interpreter).parents[1])
            executable = os.access(self.interpreter, os.X_OK)
        except SETUP_ERRORS as exc:
            raise ConfinementError(f"cannot resolve the grading interpreter: {exc!r}") from exc
        if not executable:
            raise ConfinementError(f"grading interpreter is not executable: {self.interpreter}")
        self.protected = _protected_trees(None, results_dirs)
        hidden = _hidden_paths(None, self.protected)
        rebinds = [prefix] if _is_within(self.interpreter, prefix) else [prefix, str(Path(self.interpreter).parent)]
        for path in rebinds:
            _check_bind(path, self.protected, "the grading interpreter")
        args = [self._bwrap, "--unshare-all", "--unshare-user", "--disable-userns"]
        args += ["--die-with-parent", "--new-session", "--seccomp", str(SECCOMP_FD)]
        args += ["--proc", "/proc", "--dev", "/dev"]
        args += _host_view(hidden, rebinds)
        args += ["--tmpfs", GRADING_HOME]
        self._writable_mounts = [*FRESH_MOUNTS, *(h for h in hidden if Path(h).is_dir()), GRADING_HOME, "/dev"]
        self._prefix = args
        self.env = {
            "PATH": SYSTEM_PATH,
            "HOME": GRADING_HOME,
            "TERM": "dumb",
            "TMPDIR": WORK_DIR,
            "LANG": "C.UTF-8",
        }

    def wrap(self, command, check_dir):
        """Wrap ``command`` (its first word must be :attr:`interpreter`) to run with ``check_dir`` at ``/work``."""
        if not command or command[0] != self.interpreter:
            raise ConfinementError("the grading profile only runs its own interpreter")
        try:
            check_dir = Path(check_dir).resolve()
            if not check_dir.is_dir():
                raise ConfinementError(f"check directory is not a directory: {check_dir}")
        except SETUP_ERRORS as exc:
            raise ConfinementError(f"cannot use the check directory {check_dir}: {exc!r}") from exc
        for tree in self.protected:
            if _is_within(str(check_dir), tree) or _is_within(tree, str(check_dir)):
                raise ConfinementError(f"check directory {check_dir} overlaps the protected path {tree}")
        args = [*self._prefix, "--bind", str(check_dir), WORK_DIR, "--chdir", WORK_DIR, "--remount-ro", "/"]
        for path in self._writable_mounts:
            args += ["--remount-ro", path]
        args.append("--clearenv")
        for name, value in sorted(self.env.items()):
            args += ["--setenv", name, value]
        return shim_command([*args, "--", *command], self._program), dict(self.env)


def grading_sandbox_or_none(results_dirs=()):
    """A :class:`GradingSandbox`, or None when bwrap cannot run here. For tests that may fall back to unconfined."""
    if bwrap_unavailable_reason():
        return None
    return GradingSandbox(results_dirs=results_dirs)


def _host_view(hidden, rebinds):
    """bwrap options for the shared host view: fresh mounts, the read-only host root, the hidden trees, and the
    ``rebinds`` (host paths that must stay visible at their own path even though they lie under a hidden tree)."""
    args = []
    for path in FRESH_MOUNTS:
        args += ["--tmpfs", path]
    try:
        root_entries = sorted(Path("/").iterdir())
        for entry in root_entries:
            if entry.name in SPECIAL_ROOT_ENTRIES:
                continue
            if entry.is_symlink():
                args += ["--symlink", os.readlink(entry), str(entry)]
            elif os.access(entry, os.R_OK | os.X_OK):
                args += ["--ro-bind", str(entry), str(entry)]
    except OSError as exc:
        raise ConfinementError(f"cannot enumerate the host root: {exc}") from exc
    args += _run_file_binds()
    for path in hidden:
        if Path(path).is_dir():
            args += ["--tmpfs", path]
        else:
            args += ["--ro-bind", "/dev/null", path]
    for path in rebinds:
        if not Path(path).exists():
            raise ConfinementError(f"read-only bind source does not exist: {path}")
        if any(_is_within(path, h) for h in hidden):
            args += ["--ro-bind", path, path]
    return args


def _ordered_resolved(paths):
    seen = []
    for path in paths:
        try:
            resolved = str(Path(path).resolve())
        except SETUP_ERRORS as exc:
            raise ConfinementError(f"cannot resolve the read-only bind {path}: {exc!r}") from exc
        if resolved not in seen:
            seen.append(resolved)
    return seen


def prepare_host_auth(provider, model, min_expiry_seconds, pi_binary="pi"):
    """Make the host's OAuth token valid for at least ``min_expiry_seconds``, before a trial starts.

    ``auth.json`` is read-only inside the profile, so the confined Pi cannot refresh it. The runner calls this on the
    host, where a refresh is serialised by Pi's own lock, with a minimum validity that covers the whole trial.

    The token itself is never captured: stdout goes to ``/dev/null``. stderr is read only to pick a coarse failure
    category from Pi's fixed messages; it is never copied into an exception or a log, since a failing credential
    helper may print anything there. Raises :class:`HostAuthError` when the token cannot be prepared.
    """
    duration = _expiry_duration(min_expiry_seconds)
    command = [
        pi_binary,
        "auth",
        "print-bearer-token",
        "--provider",
        str(provider),
        "--model",
        str(model),
        "--min-expiry",
        duration,
    ]
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,  # the bearer token must never be captured, logged or printed
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise HostAuthError(f"cannot run '{pi_binary} auth print-bearer-token': {reason}") from None
    except subprocess.SubprocessError as exc:
        # from None: a TimeoutExpired carries the captured stderr, which must not reach a traceback either.
        raise HostAuthError(f"cannot run '{pi_binary} auth print-bearer-token': {type(exc).__name__}") from None
    if done.returncode != 0:
        category = _auth_failure_category(done.stderr)
        raise HostAuthError(
            f"cannot prepare the host token for {provider}/{model} ({duration}): "
            f"pi exited with status {done.returncode} ({category})"
        )
    return duration


# Fixed phrases from Pi's auth command errors, mapped to a coarse category. Matching picks the category; the stderr
# text itself is never reported.
AUTH_FAILURE_CATEGORIES = (
    ("credential unavailable", ("Failed to resolve credential",)),
    ("unknown provider or model", ("Unknown provider", "Use --list-models", "Unable to resolve")),
    ("usage error", ("Unknown option", "Unknown auth command", "--min-expiry must use", "only accept")),
)


def _auth_failure_category(stderr):
    """Return a fixed category for a failed ``pi auth`` call, never any of its stderr text."""
    text = (stderr or b"").decode("utf-8", errors="replace")
    for category, phrases in AUTH_FAILURE_CATEGORIES:
        if any(phrase in text for phrase in phrases):
            return category
    return "unrecognised failure"


def _expiry_duration(min_expiry_seconds):
    """Format whole seconds as a Pi auth duration (``30m``, ``1h``, ``90s``)."""
    try:
        seconds = int(min_expiry_seconds)
    except (TypeError, ValueError) as exc:
        raise HostAuthError(f"minimum token validity must be a number of seconds: {min_expiry_seconds!r}") from exc
    if seconds != min_expiry_seconds or seconds <= 0:
        raise HostAuthError(f"minimum token validity must be a positive whole number of seconds: {min_expiry_seconds}")
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --- confinement-signal scanner -------------------------------------------------------------------------------

SYSTEM_PREFIXES = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys", "/tmp", "/var/tmp")
PATH_KEYS = {"path", "file_path", "filePath", "cwd", "directory", "dir"}
CONTENT_KEYS = {"content", "oldText", "newText", "old_string", "new_string", "text"}
WORD_SPLIT = re.compile(r"[\s'\"`;|&<>()\[\],=:]+")
HOME_CALL = re.compile(
    r"Path\.home\(\)|expanduser\(|getenv\(\s*['\"]HOME|environ\[\s*['\"]HOME|environ\.get\(\s*['\"]HOME"
)
QUOTED_HOME = re.compile(r"[\"'](\$HOME|\$\{HOME\})[\"']")
# A denied operation is a signal on its own, whatever path it names: a confined `touch /etc/x` fails on a system path
# that the allowlist deliberately ignores, but the denial still shows the agent probing outside its workspace.
DENIED_REASONS = (
    ("read-only file system", re.compile(r"read-only file system|\bEROFS\b", re.IGNORECASE)),
    ("permission denied", re.compile(r"permission denied|\bEACCES\b", re.IGNORECASE)),
    ("operation not permitted", re.compile(r"operation not permitted|\bEPERM\b", re.IGNORECASE)),
)
# A missing file is only a signal when the path it names is outside the workspace; inside, it is ordinary work.
MISSING_FILE = re.compile(r"no such file or directory|not a directory|\bENOENT\b|\bENOTDIR\b", re.IGNORECASE)
URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)
# A file URL names a local path (curl, browsers and urllib all open it), so it is classified, not dropped. It ends
# where a shell word would, and never on the ":" or "." an error message puts after it.
FILE_URL = re.compile(r"\bfile://(?:[^\s'\"`;|&<>()\[\],\\]*[^\s'\"`;|&<>()\[\],\\:.])?", re.IGNORECASE)
# Errors raised by a malformed event rather than by a bug in the scanner.
SCAN_ERRORS = (AttributeError, KeyError, TypeError, ValueError)


def normalise_path(word):
    """Collapse repeated leading slashes, ``/./`` and ``..`` so ``//home/g`` and ``/home/./g`` compare as /home/g."""
    if not word:
        return word
    return posixpath.normpath(re.sub(r"^/{2,}", "/", word))


def file_url_path(url):
    """Return the local path a ``file://`` URL names: ``file:///a`` and ``file://localhost/a`` are ``/a``.

    Any other authority is kept as a leading ``//host`` path, which Linux resolves as ``/host``.
    """
    rest = url[len("file://") :]
    try:
        parts = urllib.parse.urlsplit("file://" + rest)
        authority, path = parts.netloc, parts.path
    except ValueError:  # an unbalanced IPv6 bracket in the authority
        authority, _, path = rest.partition("/")
        path = "/" + path
    path = urllib.parse.unquote(path)
    if authority.lower() in ("", "localhost"):
        return path
    return "//" + urllib.parse.unquote(authority) + path


def classify_path(word, workspace=WORK_DIR):
    """Return the signal kind for one path-like word, or None when it stays in the workspace or the system."""
    if not word:
        return None
    if word[:7].lower() == "file://":
        word = file_url_path(word)
    if word.startswith(("~", "$HOME", "${HOME}")):
        return "home_path"
    parts = word.split("/")
    if word.startswith("/"):
        # "//host.like/x" is an absolute path on Linux, not a URL authority: URLs are removed before splitting.
        normalised = normalise_path(word)
        if _is_within(normalised, workspace):
            return None
        if ".." in parts:
            return "parent_relative_path"
        if len(normalised) < 2 or not (normalised[1].isalnum() or normalised[1] in "._~-"):
            return None
        if any(_is_within(normalised, prefix) for prefix in SYSTEM_PREFIXES):
            return None
        return "absolute_path"
    # Relative paths only escape when they climb above their starting directory.
    if ".." in parts and normalise_path(word).split("/")[0] == "..":
        return "parent_relative_path"
    return None


def _words(text):
    return [word for word in WORD_SPLIT.split(text) if word]


def _scan_text(text, workspace):
    # Keep a quoted "$HOME" attached to the path that follows it, as the shell does.
    text = QUOTED_HOME.sub(r"\1", text)
    # File URLs are local paths; other URLs are not filesystem paths, so they are dropped whole.
    hits = [(kind, url) for url in FILE_URL.findall(text) if (kind := classify_path(url, workspace))]
    text = URL.sub(" ", FILE_URL.sub(" ", text))
    hits += [(kind, word) for word in _words(text) if (kind := classify_path(word, workspace))]
    match = HOME_CALL.search(text)
    if match:
        hits.append(("home_path", match.group(0)))
    return hits


def _scan_args(value, workspace, key=None):
    if isinstance(value, dict):
        return [hit for k, v in value.items() for hit in _scan_args(v, workspace, k)]
    if isinstance(value, list):
        return [hit for item in value for hit in _scan_args(item, workspace, key)]
    if not isinstance(value, str) or key in CONTENT_KEYS:
        return []
    if key in PATH_KEYS:
        kind = classify_path(value.strip(), workspace)
        return [(kind, value.strip())] if kind else []
    return _scan_text(value, workspace)


def _result_text(result):
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        if isinstance(content, str):
            return content
    return ""


def _iter_events(events):
    if isinstance(events, (str, os.PathLike)):
        path = Path(events)
        if not path.exists():
            return
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
        return
    for event in events:
        if isinstance(event, dict):
            yield event


def scan_confinement_signals(events, workspace=WORK_DIR):
    """Scan a Pi JSON event log (path or iterable of event dicts) for confinement-violation signals.

    Returns a de-duplicated list of ``{"kind", "detail"}``. Kinds: ``parent_relative_path``, ``absolute_path`` and
    ``home_path`` for tool arguments that point outside the workspace; ``denied_path`` for a tool result reporting a
    missing file outside the workspace; ``denied_operation`` for a result the kernel or the profile refused
    (read-only filesystem, permission denied, operation not permitted) on any path, including system paths the
    argument allowlist ignores; and ``malformed_event`` for an event the scanner could not read. Hits are heuristic
    and need review by hand.
    """
    signals = []
    seen = set()
    started = {}

    def add(kind, tool, word):
        detail = f"{tool}: {word}"[:200]
        if (kind, detail) not in seen:
            seen.add((kind, detail))
            signals.append({"kind": kind, "detail": detail})

    for event in _iter_events(events):
        try:
            _scan_event(event, workspace, started, add)
        except SCAN_ERRORS as exc:
            # A malformed event (an unhashable toolCallId, a result of the wrong shape) must not end the scan.
            add("malformed_event", _event_tool(event), f"{type(exc).__name__}: {exc}")
    return signals


def _event_tool(event):
    try:
        name = event.get("toolName")
    except SCAN_ERRORS:
        return "?"
    return name if isinstance(name, str) and name else "?"


def _scan_event(event, workspace, started, add):
    kind = event.get("type")
    tool = _event_tool(event)
    if kind == "tool_execution_start":
        hits = _scan_args(event.get("args"), workspace)
        started[event.get("toolCallId")] = hits
        for hit_kind, word in hits:
            add(hit_kind, tool, word)
    elif kind == "tool_execution_end":
        # Paths the matching call already asked for, used when the error message itself names no outside path.
        fallback = [word for _, word in started.get(event.get("toolCallId"), [])[:1]]
        for hit_kind, detail in _scan_result(_result_text(event.get("result")), workspace, fallback):
            add(hit_kind, tool, detail)


def _scan_result(text, workspace, fallback):
    """Return (kind, detail) pairs for the denied-operation and missing-path outcomes in one tool result."""
    hits = []
    for line in text.splitlines():
        outside = [word for _, word in _scan_text(line, workspace)]
        reason = next((name for name, pattern in DENIED_REASONS if pattern.search(line)), None)
        if reason:
            targets = outside or _path_words(line) or fallback or [line.strip()]
            hits.append(("denied_operation", f"{reason}: {targets[0]}"))
        elif MISSING_FILE.search(line):
            hits += [("denied_path", word) for word in outside or fallback]
    return hits


def _path_words(text):
    """Path-like words in an error message, so a denial on a system path still reports what was touched."""
    files = [file_url_path(url) for url in FILE_URL.findall(text)]
    return files + [word for word in _words(URL.sub(" ", FILE_URL.sub(" ", text))) if "/" in word or word == ".."]


# --- self-test -------------------------------------------------------------------------------------------------

# Run in place of Pi under the profile. Each probe reports whether it succeeded; it never prints file contents.
FAKE_AGENT = r"""
import ctypes, json, os, socket, subprocess, sys
cfg = json.loads(sys.argv[1])
results = {}
def probe(name, fn):
    try:
        fn()
        results[name] = {"succeeded": True, "error": ""}
    except Exception as exc:
        results[name] = {"succeeded": False, "error": type(exc).__name__ + ": " + str(exc)[:160]}
def sh(script):
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise OSError(done.stderr.strip()[-160:] or "exit %d" % done.returncode)
def read(path):
    with open(path, "rb") as handle:
        handle.read(1)
for name, path in cfg["python_reads"].items():
    probe(name, lambda path=path: read(path))
for name, path in cfg["stats"].items():
    probe(name, lambda path=path: os.stat(path))
def any_stat(paths):
    errors = []
    for path in paths:
        try:
            return os.stat(path)
        except OSError as exc:
            errors.append(exc)
    raise errors[0]
for name, paths in cfg["any_stats"].items():
    probe(name, lambda paths=paths: any_stat(paths))
for name, script in cfg["shell"].items():
    probe(name, lambda script=script: sh(script))
probe("python_list_sessions", lambda: os.listdir(cfg["sessions"]))
probe("write_outside_workspace", lambda: open("/etc/confine-probe", "w").close())
probe("create_root_entry", lambda: os.mkdir("/confine-probe"))

# The network namespace is shared, so host abstract Unix sockets have no mount to hide behind: only seccomp stops them.
def unix_socket():
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
def connect_abstract():
    handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        handle.connect(b"\x00" + cfg["abstract_socket"].encode())
    finally:
        handle.close()
# socket(AF_UNIX) is refused, but a datagram socketpair can still connect() to an abstract name and send to it.
def socketpair_datagram_to_abstract():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        left.connect(b"\x00" + cfg["abstract_datagram_socket"].encode())
        left.send(b"probe")
    finally:
        left.close()
        right.close()
def io_uring_setup():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p]
    params = ctypes.create_string_buffer(256)
    ring = libc.syscall(425, 8, params)
    if ring < 0:
        raise OSError(ctypes.get_errno(), "io_uring_setup rejected")
    os.close(ring)
def connect_tcp():
    with socket.create_connection(("127.0.0.1", cfg["tcp_port"]), timeout=10) as handle:
        handle.send(b"ping")
def socketpair_works():
    left, right = socket.socketpair()
    left.send(b"x")
    assert right.recv(1) == b"x"
    left.close()
    right.close()
def pipe_works():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"x")
    assert os.read(read_fd, 1) == b"x"
    os.close(read_fd)
    os.close(write_fd)
def clean_fd_table():
    targets = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            targets[name] = os.readlink("/proc/self/fd/" + name)
        except OSError:
            continue
    # The handle listdir itself holds points at this /proc/<pid>/fd directory; anything else was inherited.
    extra = sorted(n for n, t in targets.items() if n not in ("0", "1", "2") and not t.endswith("/fd"))
    if extra:
        raise OSError("inherited descriptors: " + ", ".join(n + " -> " + targets[n] for n in extra))
probe("create_unix_socket", unix_socket)
probe("connect_host_abstract_socket", connect_abstract)
probe("socketpair_datagram_to_host_abstract", socketpair_datagram_to_abstract)
probe("io_uring_setup", io_uring_setup)
probe("outbound_tcp", connect_tcp)
probe("socketpair_works", socketpair_works)
probe("pipe_works", pipe_works)
probe("clean_fd_table", clean_fd_table)
probe("auth_read", lambda: read(cfg["agent_dir"] + "/auth.json"))
def own_workspace():
    with open("task.txt", encoding="utf-8") as handle:
        assert handle.read() == cfg["task_text"]
    with open("out.txt", "w", encoding="utf-8") as handle:
        handle.write("written inside")
probe("workspace_read_write", own_workspace)
# Pi refreshes a token by taking a lock directory beside auth.json and rewriting the file. The lock still works
# (the agent directory is a tmpfs) but the rewrite must fail: refresh is the host's job, see prepare_host_auth.
def auth_refresh():
    lock = cfg["agent_dir"] + "/auth.json.lock"
    os.mkdir(lock)
    try:
        with open(cfg["agent_dir"] + "/auth.json", "w", encoding="utf-8") as handle:
            handle.write(cfg["auth_new"])
    finally:
        os.rmdir(lock)
probe("auth_refresh", auth_refresh)
results["cwd"] = os.getcwd()
results["leaked_env"] = sorted(k for k in os.environ if k.startswith("PI_") and k not in cfg["allowed_env"])
print(json.dumps(results))
"""

# Probes that must fail inside the profile.
MUST_FAIL = (
    "python_relative_runner",
    "python_absolute_runner",
    "python_main_checkout_runner",
    "python_sibling_workspace",
    "python_run_logs",
    "python_proc_root_escape",
    "stat_real_auth_path",
    "shell_relative_runner",
    "shell_sibling_workspace",
    "shell_absolute_runner",
    "shell_cd_parent_runner",
    "shell_list_sessions",
    "shell_list_sessions_abs",
    "python_list_sessions",
    "write_outside_workspace",
    "create_root_entry",
    "create_unix_socket",
    "connect_host_abstract_socket",
    "socketpair_datagram_to_host_abstract",
    "auth_refresh",
)
MUST_SUCCEED = (
    "workspace_read_write",
    "auth_read",
    "outbound_tcp",
    "socketpair_works",
    "pipe_works",
    "clean_fd_table",
)
# Host writes that fail even without the profile, so the unconfined control does not expect them to succeed.
HOST_DENIED = ("write_outside_workspace", "create_root_entry")
# Must fail under the profile, but may also be unavailable on the bare host, so the control does not require them.
CONFINED_ONLY = ("io_uring_setup",)


def run_selftest(base=None, interpreter="/usr/bin/python3", confined=True):
    """Run the fake agent and return (ok, report). It uses a fake credentials file, never the real one.

    With ``confined`` (the acceptance test), ok means every escape probe failed, the workspace stayed usable, the
    credentials stayed readable but unchanged, and pipes, stream socketpairs and TCP still work. Without it (the
    control), ok means the escape probes succeed on the bare host, which shows the probes are able to detect a missing
    profile.
    """
    repo_lab = Path(__file__).resolve().parent
    if base is None:
        base = repo_lab / "local-results"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="confine-selftest-", dir=base))
    listeners = []
    temporary = [root]
    try:
        workspace = root / "workspaces" / "trial"
        sibling = root / "workspaces" / "sibling"
        logs = root / "logs" / "trial"
        for path in (workspace, sibling, logs):
            path.mkdir(parents=True)
        (workspace / "task.txt").write_text("task data", encoding="utf-8")
        (sibling / "secret.txt").write_text("other trial", encoding="utf-8")
        (logs / "pi-events.jsonl").write_text("{}\n", encoding="utf-8")
        # The credentials file must sit outside every protected tree, exactly as the real ~/.pi/agent/auth.json does:
        # confine() refuses to bind a file out of the repository, so a fake one cannot live under the run root.
        auth_base = Path.home() / ".cache" / "autonomy-lab"
        auth_base.mkdir(parents=True, exist_ok=True)
        fake_agent = Path(tempfile.mkdtemp(prefix="confine-selftest-agent-", dir=auth_base))
        temporary.append(fake_agent)
        fake_auth = fake_agent / "auth.json"
        fake_auth.write_text("{}", encoding="utf-8")
        runner = repo_lab / "runner.py"
        main_checkout = sorted(p for p in _git_paths(repo_lab) if (p / "autonomy_lab" / "runner.py").is_file())
        main_runner = main_checkout[0] / "autonomy_lab" / "runner.py" if main_checkout else runner
        relative_runner = os.path.relpath(runner, workspace)
        sessions = Path.home() / ".pi" / "agent" / "sessions"
        # Daemon sockets such as /run/docker.sock would let the agent escape through a privileged service.
        try:
            run_sockets = sorted(str(p) for p in Path("/run").glob("**/*.sock") if p.is_socket())[:20]
        except OSError:
            run_sockets = []
        # A harmless host abstract socket, reachable through the shared network namespace unless seccomp stops it,
        # and a loopback TCP listener, which must keep working because the model provider needs the network.
        abstract_name = "confine-selftest-" + os.urandom(8).hex()
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abstract.bind(b"\x00" + abstract_name.encode())
        abstract.listen(2)
        listeners.append(abstract)
        datagram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        datagram.bind(b"\x00" + abstract_name.encode() + b"-dgram")
        datagram.setblocking(False)
        listeners.append(datagram)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(2)
        listeners.append(tcp)
        shell_probes = {}
        if shutil.which("unshare"):
            # A nested user namespace is the first step towards remounting the profile's hidden paths.
            shell_probes["shell_nested_user_namespace"] = "unshare -r true"
        cfg = {
            "python_reads": {
                "python_relative_runner": relative_runner,
                "python_absolute_runner": str(runner),
                "python_main_checkout_runner": str(main_runner),
                "python_sibling_workspace": "../sibling/secret.txt",
                "python_run_logs": str(logs / "pi-events.jsonl"),
                "python_proc_root_escape": f"/proc/{os.getpid()}/root{runner}",
            },
            # Existence only: the real credentials are never opened.
            "stats": {"stat_real_auth_path": str(default_auth_file())},
            "any_stats": {"stat_host_run_sockets": run_sockets} if run_sockets else {},
            "shell": {
                "shell_relative_runner": f"cat {relative_runner}",
                "shell_sibling_workspace": f"cat {sibling / 'secret.txt'}",
                "shell_absolute_runner": f"cat {runner}",
                "shell_cd_parent_runner": (
                    "for i in $(seq 12); do cd ..; cat autonomy_lab/runner.py && exit 0; done; exit 1"
                ),
                "shell_list_sessions": "ls ~/.pi/agent/sessions",
                "shell_list_sessions_abs": f"ls {sessions}",
                **shell_probes,
            },
            "sessions": str(sessions),
            "abstract_socket": abstract_name,
            "abstract_datagram_socket": abstract_name + "-dgram",
            "tcp_port": tcp.getsockname()[1],
            "task_text": "task data",
            "agent_dir": AGENT_DIR if confined else str(fake_auth.parent),
            "auth_new": '{"rewritten": true}',
            "allowed_env": ["PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_SKIP_VERSION_CHECK"],
        }
        command = [interpreter, "-I", "-c", FAKE_AGENT, json.dumps(cfg)]
        if confined:
            ro_binds = [sys.base_prefix] if not interpreter.startswith("/usr/") else []
            command, env = confine(command, workspace, results_dirs=[str(root)], auth_file=fake_auth, ro_binds=ro_binds)
            done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
        else:
            done = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=120)
        try:
            results = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return False, {"error": "fake agent produced no report", "exit": done.returncode, "stderr": done.stderr}
        optional = (*(("stat_host_run_sockets",) if run_sockets else ()), *shell_probes)
        if not confined:
            names = (*MUST_FAIL, *MUST_SUCCEED, *optional)
            checks = {name: results[name]["succeeded"] for name in names if name not in HOST_DENIED}
            return all(checks.values()), {"checks": checks, "probes": results}
        checks = {}
        for name in (*MUST_FAIL, *CONFINED_ONLY, *optional):
            checks[name] = not results[name]["succeeded"]
        for name in MUST_SUCCEED:
            checks[name] = results[name]["succeeded"]
        checks["cwd_is_work"] = results["cwd"] == WORK_DIR
        checks["no_leaked_pi_env"] = results["leaked_env"] == []
        checks["workspace_write_visible_on_host"] = (workspace / "out.txt").is_file()
        checks["auth_unchanged_on_host"] = fake_auth.read_text(encoding="utf-8") == "{}"
        checks["host_abstract_datagram_received_nothing"] = _received_nothing(datagram)
        report = {"checks": checks, "probes": {k: v for k, v in results.items() if isinstance(v, dict)}}
        return all(checks.values()), report
    finally:
        for listener in listeners:
            listener.close()
        for path in temporary:
            shutil.rmtree(path, ignore_errors=True)


def _received_nothing(listener):
    """True when a non-blocking host listener has no datagram waiting."""
    try:
        listener.recv(64)
    except BlockingIOError:
        return True
    return False


# Run as a grading check under the grading profile. Each probe reports whether it succeeded; nothing is printed
# from any file it reads.
GRADING_PROBE = r"""
import json, os, socket, sys
cfg = json.loads(sys.argv[1])
results = {}
def probe(name, fn):
    try:
        fn()
        results[name] = {"succeeded": True, "error": ""}
    except Exception as exc:
        results[name] = {"succeeded": False, "error": type(exc).__name__ + ": " + str(exc)[:160]}
def read(path):
    with open(path, "rb") as handle:
        handle.read(1)
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
def nonempty(path):
    if not os.listdir(path):
        raise OSError("empty")
def tcp():
    socket.create_connection(("127.0.0.1", cfg["tcp_port"]), timeout=5).close()
def abstract():
    handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        handle.connect(b"\x00" + cfg["abstract_socket"].encode())
    finally:
        handle.close()
def clean_fds():
    targets = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            targets[name] = os.readlink("/proc/self/fd/" + name)
        except OSError:
            continue  # the descriptor listdir itself used, already closed
    extra = sorted(n for n, t in targets.items() if n not in ("0", "1", "2") and not t.endswith("/fd"))
    if extra:
        raise OSError("inherited descriptors: " + ", ".join(n + " -> " + targets[n] for n in extra))
probe("read_repository", lambda: read(cfg["runner"]))
probe("read_results_dir", lambda: read(cfg["results_secret"]))
probe("list_sessions", lambda: os.listdir(cfg["sessions"]))
probe("home_not_empty", lambda: nonempty(os.environ["HOME"]))
probe("write_etc", lambda: write("/etc/grading-probe"))
probe("write_tmp", lambda: write("/tmp/grading-probe"))
probe("write_home", lambda: write(os.environ["HOME"] + "/grading-probe"))
probe("write_dev_shm", lambda: write("/dev/shm/grading-probe"))
probe("outbound_tcp", tcp)
probe("connect_host_abstract_socket", abstract)
probe("write_own_directory", lambda: write("own-output.txt"))
probe("read_task_module", lambda: read("probe_module.py"))
probe("clean_fd_table", clean_fds)
results["cwd"] = os.getcwd()
print(json.dumps(results))
"""
GRADING_MUST_FAIL = (
    "read_repository",
    "read_results_dir",
    "list_sessions",
    "home_not_empty",
    "write_etc",
    "write_tmp",
    "write_home",
    "write_dev_shm",
    "outbound_tcp",
    "connect_host_abstract_socket",
)
GRADING_MUST_SUCCEED = ("write_own_directory", "read_task_module", "clean_fd_table")


def run_grading_selftest():
    """Run a probe as a grading check under :class:`GradingSandbox` and return (ok, report).

    ok means the probe could not reach the repository, a results directory, the Pi sessions, the network or a host
    abstract socket, could not write anywhere but its own directory, saw an empty ``$HOME``, and could still read
    its task module and write its own directory.
    """
    listeners = []
    auth_base = Path.home() / ".cache" / "autonomy-lab"
    auth_base.mkdir(parents=True, exist_ok=True)
    results = Path(tempfile.mkdtemp(prefix="grading-selftest-results-", dir=auth_base))
    check_dir = Path(tempfile.mkdtemp(prefix="grading-selftest-check-"))
    try:
        (results / "secret.txt").write_text("held-out", encoding="utf-8")
        (check_dir / "probe_module.py").write_text("x = 1\n", encoding="utf-8")
        abstract_name = "grading-selftest-" + os.urandom(8).hex()
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        abstract.bind(b"\x00" + abstract_name.encode())
        abstract.listen(2)
        listeners.append(abstract)
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(2)
        listeners.append(tcp)
        cfg = {
            "runner": str(Path(__file__).resolve().parent / "runner.py"),
            "results_secret": str(results / "secret.txt"),
            "sessions": str(Path.home() / ".pi" / "agent" / "sessions"),
            "tcp_port": tcp.getsockname()[1],
            "abstract_socket": abstract_name,
        }
        sandbox = GradingSandbox(results_dirs=[str(results)])
        command, env = sandbox.wrap([sandbox.interpreter, "-I", "-c", GRADING_PROBE, json.dumps(cfg)], check_dir)
        done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        try:
            probes = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return False, {"error": "grading probe produced no report", "exit": done.returncode, "stderr": done.stderr}
        checks = {name: not probes[name]["succeeded"] for name in GRADING_MUST_FAIL}
        checks.update({name: probes[name]["succeeded"] for name in GRADING_MUST_SUCCEED})
        checks["cwd_is_work"] = probes["cwd"] == WORK_DIR
        checks["own_write_visible_on_host"] = (check_dir / "own-output.txt").is_file()
        report = {"checks": checks, "probes": {k: v for k, v in probes.items() if isinstance(v, dict)}}
        return all(checks.values()), report
    finally:
        for listener in listeners:
            listener.close()
        shutil.rmtree(results, ignore_errors=True)
        shutil.rmtree(check_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Bubblewrap confinement for benchmark Pi processes.")
    sub = parser.add_subparsers(dest="action", required=True)
    selftest = sub.add_parser("selftest", help="run a fake agent under the profile and check every probe")
    selftest.add_argument("--control", action="store_true", help="run the probes unconfined; they should succeed")
    scan = sub.add_parser("scan", help="print confinement signals for a Pi JSON event log")
    scan.add_argument("events", type=Path)
    args = parser.parse_args()
    if args.action == "scan":
        print(json.dumps(scan_confinement_signals(args.events), indent=2))
        return 0
    reason = bwrap_unavailable_reason()
    if reason:
        print("SKIP " + reason)
        return 2
    ok, report = run_selftest(confined=not args.control)
    for name, passed in report.get("checks", {}).items():
        print(("PASS " if passed else "FAIL ") + name)
    if "error" in report:
        print("FAIL " + json.dumps(report))
    if not args.control:
        # The grading profile (LOOP_SPEC §4.2a) has no unconfined control: its probes are the same escapes.
        grading_ok, grading_report = run_grading_selftest()
        for name, passed in grading_report.get("checks", {}).items():
            print(("PASS " if passed else "FAIL ") + "grading_" + name)
        if "error" in grading_report:
            print("FAIL " + json.dumps(grading_report))
        ok = ok and grading_ok
    print(("control" if args.control else "selftest") + " " + ("passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
