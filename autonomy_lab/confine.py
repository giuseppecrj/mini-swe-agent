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
- the directory of the wrapped executable (the Pi installation) is bound read-only at its host path;
- ``PI_CODING_AGENT_DIR`` is ``/pi-agent``, a private tmpfs that holds only the real ``auth.json``, bound read-write;
- PID, IPC, UTS, cgroup and user namespaces are new, nested user namespaces are disabled, ``/proc`` and ``/dev`` are
  fresh, the environment is cleared to a minimal allowlist, and the network is shared because the model provider needs
  it.

Pi rewrites ``auth.json`` in place (``writeFileSync`` under a ``proper-lockfile`` lock directory beside it), so a
single-file bind keeps OAuth refresh write-back working. The lock directory lands in the private tmpfs, so a refresh
inside the profile is not serialised against Pi processes on the host. Trials run with concurrency 1; the harness
should avoid refreshing the same provider on the host while a trial runs.

This is confinement for trusted first-party tasks, not a hardened sandbox: the agent can read its own provider
credentials and the rest of the read-only host filesystem.
"""

import argparse
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
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


class ConfinementError(RuntimeError):
    """The confinement profile could not be set up; the trial is a "sandbox" infrastructure failure."""


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
    """True when a wrapped process failed because bwrap itself could not set up the profile."""
    if exit_code in (0, None):
        return False
    return any(line.startswith("bwrap: ") for line in (stderr_text or "").splitlines())


def _is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _git_paths(start):
    """Return every checkout and Git directory of the repository containing ``start``, without running git."""
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


def _hidden_paths(workspace, results_dirs):
    home = Path.home().resolve()
    # /tmp is always a fresh tmpfs; see confine().
    candidates = {
        home,
        Path("/var/tmp"),
        *_git_paths(Path(__file__).resolve().parent),
        *_git_paths(workspace),
    }
    for item in results_dirs:
        path = Path(item).expanduser()
        if not path.is_absolute():
            raise ConfinementError(f"results directory must be absolute: {item}")
        candidates.add(path.resolve())
    # Sibling trial workspaces, unless the workspace sits directly in a system directory. In pi_runner's
    # <run>/workspaces/<trial> layout, the whole run directory, including every trial's logs.
    if str(workspace.parent) not in UNHIDEABLE:
        candidates.add(workspace.parent)
    if workspace.parent.name == "workspaces" and str(workspace.parent.parent) not in UNHIDEABLE:
        candidates.add(workspace.parent.parent)
    hidden = []
    for path in sorted({str(p.resolve()) for p in candidates}):
        if path in UNHIDEABLE:
            raise ConfinementError(f"refusing to hide system path {path}")
        if path == str(workspace):
            raise ConfinementError("the workspace itself cannot be a hidden path")
        if not os.path.lexists(path) or any(_is_within(path, kept) for kept in [*FRESH_MOUNTS, *hidden]):
            continue
        hidden.append(path)
    return hidden


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
    ``auth.json`` is exposed. ``ro_binds`` are extra host paths bound read-only at the same path.
    Returns ``(wrapped_command, env)``; ``env`` is the complete environment, not a delta.
    """
    if not command:
        raise ConfinementError("empty command")
    bwrap = find_bwrap()
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise ConfinementError(f"workspace is not a directory: {workspace}")
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

    hidden = _hidden_paths(workspace, results_dirs)
    args = [bwrap, "--unshare-all", "--share-net", "--unshare-user", "--disable-userns"]
    args += ["--die-with-parent", "--new-session"]
    args += ["--proc", "/proc", "--dev", "/dev"]
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
    for path in [executable.parent, *(Path(p).resolve() for p in ro_binds)]:
        if not path.exists():
            raise ConfinementError(f"read-only bind source does not exist: {path}")
        if any(_is_within(str(path), h) for h in hidden):
            args += ["--ro-bind", str(path), str(path)]
    args += ["--perms", "0700", "--tmpfs", AGENT_DIR]
    args += ["--bind", str(auth), f"{AGENT_DIR}/auth.json"]
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
    return args + ["--", str(executable), *command[1:]], env


# --- confinement-signal scanner -------------------------------------------------------------------------------

SYSTEM_PREFIXES = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys", "/tmp", "/var/tmp")
PATH_KEYS = {"path", "file_path", "filePath", "cwd", "directory", "dir"}
CONTENT_KEYS = {"content", "oldText", "newText", "old_string", "new_string", "text"}
WORD_SPLIT = re.compile(r"[\s'\"`;|&<>()\[\],=:]+")
HOME_CALL = re.compile(
    r"Path\.home\(\)|expanduser\(|getenv\(\s*['\"]HOME|environ\[\s*['\"]HOME|environ\.get\(\s*['\"]HOME"
)
QUOTED_HOME = re.compile(r"[\"'](\$HOME|\$\{HOME\})[\"']")
ERROR_LINE = re.compile(
    r"permission denied|no such file or directory|read-only file system|operation not permitted|not a directory"
    r"|\bE(?:ACCES|NOENT|ROFS|PERM)\b",
    re.IGNORECASE,
)


def classify_path(word, workspace=WORK_DIR):
    """Return the signal kind for one path-like word, or None when it stays in the workspace or the system."""
    if not word:
        return None
    if word.startswith(("~", "$HOME", "${HOME}")):
        return "home_path"
    parts = word.split("/")
    if word.startswith("/") and not word.startswith("//"):
        if _is_within(posixpath.normpath(word), workspace):
            return None
        if ".." in parts:
            return "parent_relative_path"
        if len(word) < 2 or not (word[1].isalnum() or word[1] in "._~-"):
            return None
        if any(_is_within(posixpath.normpath(word), prefix) for prefix in SYSTEM_PREFIXES):
            return None
        return "absolute_path"
    # Relative paths only escape when they climb above their starting directory.
    if ".." in parts and posixpath.normpath(word).split("/")[0] == "..":
        return "parent_relative_path"
    return None


def _words(text):
    return [word for word in WORD_SPLIT.split(text) if word]


def _scan_text(text, workspace):
    # Keep a quoted "$HOME" attached to the path that follows it, as the shell does.
    text = QUOTED_HOME.sub(r"\1", text)
    hits = [(kind, word) for word in _words(text) if (kind := classify_path(word, workspace))]
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
    ``home_path`` for tool arguments that point outside the workspace, and ``denied_path`` for tool results that report
    a permission or missing-file error on such a path. Hits are heuristic and need review by hand.
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
        kind = event.get("type")
        tool = str(event.get("toolName") or "?")
        if kind == "tool_execution_start":
            hits = _scan_args(event.get("args"), workspace)
            started[event.get("toolCallId")] = hits
            for hit_kind, word in hits:
                add(hit_kind, tool, word)
        elif kind == "tool_execution_end":
            error_lines = [line for line in _result_text(event.get("result")).splitlines() if ERROR_LINE.search(line)]
            denied = [word for line in error_lines for _, word in _scan_text(line, workspace)]
            if error_lines and not denied:
                denied = [word for _, word in started.get(event.get("toolCallId"), [])[:1]]
            for word in denied:
                add("denied_path", tool, word)
    return signals


# --- self-test -------------------------------------------------------------------------------------------------

# Run in place of Pi under the profile. Each probe reports whether it succeeded; it never prints file contents.
FAKE_AGENT = r"""
import json, os, subprocess, sys
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
def own_workspace():
    with open("task.txt", encoding="utf-8") as handle:
        assert handle.read() == cfg["task_text"]
    with open("out.txt", "w", encoding="utf-8") as handle:
        handle.write("written inside")
probe("workspace_read_write", own_workspace)
def auth_write_through():
    lock = cfg["agent_dir"] + "/auth.json.lock"
    os.mkdir(lock)
    with open(cfg["agent_dir"] + "/auth.json", "w", encoding="utf-8") as handle:
        handle.write(cfg["auth_new"])
    os.rmdir(lock)
probe("auth_write_through", auth_write_through)
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
)
MUST_SUCCEED = ("workspace_read_write", "auth_write_through")
# Host writes that fail even without the profile, so the unconfined control does not expect them to succeed.
HOST_DENIED = ("write_outside_workspace", "create_root_entry")


def run_selftest(base=None, interpreter="/usr/bin/python3", confined=True):
    """Run the fake agent and return (ok, report). It uses a fake credentials file, never the real one.

    With ``confined`` (the acceptance test), ok means every escape probe failed and the workspace and the credentials
    write-back worked. Without it (the control), ok means the escape probes succeed on the bare host, which shows the
    probes are able to detect a missing profile.
    """
    repo_lab = Path(__file__).resolve().parent
    if base is None:
        base = repo_lab / "local-results"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="confine-selftest-", dir=base))
    try:
        workspace = root / "workspaces" / "trial"
        sibling = root / "workspaces" / "sibling"
        logs = root / "logs" / "trial"
        for path in (workspace, sibling, logs):
            path.mkdir(parents=True)
        (workspace / "task.txt").write_text("task data", encoding="utf-8")
        (sibling / "secret.txt").write_text("other trial", encoding="utf-8")
        (logs / "pi-events.jsonl").write_text("{}\n", encoding="utf-8")
        fake_auth = root / "fake-agent" / "auth.json"
        fake_auth.parent.mkdir()
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
        for name in (*MUST_FAIL, *optional):
            checks[name] = not results[name]["succeeded"]
        for name in MUST_SUCCEED:
            checks[name] = results[name]["succeeded"]
        checks["cwd_is_work"] = results["cwd"] == WORK_DIR
        checks["no_leaked_pi_env"] = results["leaked_env"] == []
        checks["workspace_write_visible_on_host"] = (workspace / "out.txt").is_file()
        checks["auth_written_back_on_host"] = fake_auth.read_text(encoding="utf-8") == cfg["auth_new"]
        report = {"checks": checks, "probes": {k: v for k, v in results.items() if isinstance(v, dict)}}
        return all(checks.values()), report
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
    print(("control" if args.control else "selftest") + " " + ("passed" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
