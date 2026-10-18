"""Task bank loading, hashing, mechanical validation and per-decision grading (LOOP_SPEC §4.1, T1).

A task bank is a directory with one sub-directory per task:

    task.json        {"id", "module", "brief", "version", "author"}
    starter.py       weak starting implementation, copied into the workspace as <module>
    reference.py     a solution that satisfies every decision
    mutants/<d>.py   the reference with only decision <d> violated (required for every decision)
    mutants/<d>--<alt>.py
                     optional further alternatives; a binary arbitrary decision has only mutants/<d>.py
    decisions.json   [{"id", "label", "contract", "visible_check", "heldout_check"}]
    neutral.md       filler: one neutral sentence per line

Validate a bank from the repository root:

    python3 -m autonomy_lab.taskbank validate autonomy_lab/tasks
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autonomy_lab.confine import (
    SYSTEM_PATH,
    WORK_DIR,
    ConfinementError,
    GradingSandbox,
    bwrap_unavailable_reason,
)
from autonomy_lab.design import build_visible_checks, filler_feasibility_errors

LABELS = ("inferable", "arbitrary", "preserve")
AUTHORS = ("coordinator", "user")
TASK_FIELDS = ("id", "module", "brief", "version", "author")
DECISION_FIELDS = ("id", "label", "contract", "visible_check", "heldout_check")
CHECK_TIMEOUT = 10
# Of each check's timeout, the share kept back for killing the process group and draining its pipes, so the whole
# check, cleanup included, ends within the timeout (LOOP_SPEC §4.2a).
KILL_GRACE = 0.5
MAX_MODULE_BYTES = 1 << 20
MIN_DECISIONS = 4
MAX_DECISIONS = 8
MIN_ARBITRARY = 2
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# mutants/<decision-id>.py or mutants/<decision-id>--<alt>.py (LOOP_SPEC §4.1, alternative mutants)
ALT_SEPARATOR = "--"
# Files the harness writes into a trial workspace, by import name: a task module may not collide with them.
RESERVED_MODULES = frozenset({"visible_checks", "prompt", "sitecustomize", "usercustomize", "__main__"})

# Runs one check in a fresh interpreter whose working directory holds nothing but a copy of the task module.
# stdin carries three lines: a ready marker, the completion token, then the check source. The ready marker is
# printed before any check or candidate code runs, so its absence means the sandbox itself failed to start. The
# token is printed only after the check's last statement, so a candidate that ends the process early (sys.exit or
# os._exit at import) cannot pass. The check directory is appended (not prepended) to sys.path so that the module
# cannot shadow the standard library; validation rejects module names the clean interpreter can already import.
# The token lives in a function local, not in __main__, so it is not trivially visible to the check's code. This
# defeats accidental early exits only; it is not proof against code that inspects the interpreter's frames.
_CHECK_BOOTSTRAP = (
    "import sys\n"
    "def _lab_check():\n"
    "    ready, token, source = sys.stdin.buffer.read().split(b'\\n', 2)\n"
    "    sys.stdout.write(ready.decode('ascii') + '\\n')\n"
    "    sys.stdout.flush()\n"
    "    sys.path.append(sys.argv[1])\n"
    "    del sys.argv[1:]\n"
    "    exec(compile(source.decode('utf-8'), '<check>', 'exec'), {'__name__': '__main__'})\n"
    "    sys.stdout.flush()\n"
    "    sys.stdout.write('\\n' + token.decode('ascii') + '\\n')\n"
    "    sys.stdout.flush()\n"
    "_lab_check()\n"
)


class TaskBankError(ValueError):
    """The bank cannot be loaded at all (as opposed to failing validation)."""


def task_bank_sha256(bank_dir):
    """SHA-256 over every file under the bank, sorted by relative path.

    The digest is taken over a manifest with one line per file,
    ``<relative posix path>\\0<sha256 of content>\\n``, in byte order of the
    UTF-8 encoded paths, so every path and every content byte is covered and
    no file boundary is ambiguous. A symlink anywhere below the bank root, to a
    file or a directory, is an error: os.walk does not descend into directory
    symlinks while loading follows them, so the hash would not cover what is
    loaded (LOOP_SPEC §4.2a, bank integrity).
    """
    root = Path(bank_dir)
    if not root.is_dir():
        raise TaskBankError(f"task bank {root} is not a directory")
    entries = []

    def walk_error(exc):
        raise TaskBankError(f"cannot read the task bank: {exc}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        dirnames.sort()
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise TaskBankError(f"task bank entry {Path(dirpath) / name} is a symlink")
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                raise TaskBankError(f"task bank entry {path} is not a regular file")
            rel = path.relative_to(root).as_posix()
            if "\n" in rel or "\0" in rel:
                raise TaskBankError(f"task bank path {rel!r} contains a newline or NUL")
            entries.append((rel.encode("utf-8"), path))
    digest = hashlib.sha256()
    for rel, path in sorted(entries):
        digest.update(rel + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def read_neutral(text):
    """Filler sentences: the non-blank lines of neutral.md, stripped, in file order."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _load_json(path, errors):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing {path.name}")
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name} is not valid JSON: {exc}")
    return None


def _read_text(path, name, errors):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(f"missing {name}")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"cannot read {name}: {exc}")
    return None


def _schema_errors(task_dir, meta, decisions):
    errors = []
    if isinstance(meta, dict):
        missing = [k for k in TASK_FIELDS if k not in meta]
        if missing:
            errors.append("task.json is missing " + ", ".join(missing))
        extra = sorted(set(meta) - set(TASK_FIELDS))
        if extra:
            errors.append("task.json has unknown fields " + ", ".join(extra))
        if "id" in meta and meta["id"] != task_dir.name:
            errors.append(f"task.json id {meta['id']!r} does not match directory name {task_dir.name!r}")
        module = meta.get("module")
        if "module" in meta and not (isinstance(module, str) and module.endswith(".py") and module[:-3].isidentifier()):
            errors.append(f"task.json module {module!r} must be '<identifier>.py'")
        elif isinstance(module, str) and module[:-3] in RESERVED_MODULES:
            errors.append(f"task.json module {module!r} collides with a harness file")
        elif isinstance(module, str) and (
            module[:-3] in sys.stdlib_module_names or module[:-3] in sys.builtin_module_names
        ):
            errors.append(f"task.json module {module!r} shadows a standard-library module")
        if "brief" in meta and not (isinstance(meta["brief"], str) and meta["brief"].strip()):
            errors.append("task.json brief must be a non-empty string")
        if "version" in meta and not (isinstance(meta["version"], int) and not isinstance(meta["version"], bool)):
            errors.append("task.json version must be an integer")
        if "author" in meta and meta["author"] not in AUTHORS:
            errors.append(f"task.json author {meta['author']!r} must be one of " + ", ".join(AUTHORS))
    elif meta is not None:
        errors.append("task.json must be a JSON object")

    if decisions is None:
        return errors
    if not isinstance(decisions, list):
        errors.append("decisions.json must be a JSON list")
        return errors
    if not MIN_DECISIONS <= len(decisions) <= MAX_DECISIONS:
        errors.append(f"task has {len(decisions)} decisions; it needs {MIN_DECISIONS}-{MAX_DECISIONS}")
    seen = set()
    for index, decision in enumerate(decisions):
        where = f"decisions.json[{index}]"
        if not isinstance(decision, dict):
            errors.append(f"{where} must be a JSON object")
            continue
        missing = [k for k in DECISION_FIELDS if k not in decision]
        if missing:
            errors.append(f"{where} is missing " + ", ".join(missing))
        extra = sorted(set(decision) - set(DECISION_FIELDS))
        if extra:
            errors.append(f"{where} has unknown fields " + ", ".join(extra))
        did = decision.get("id")
        if not (isinstance(did, str) and _ID_PATTERN.match(did)):
            errors.append(f"{where} id {did!r} must match {_ID_PATTERN.pattern}")
        elif did in seen:
            errors.append(f"duplicate decision id {did!r}")
        seen.add(did)
        if "label" in decision and decision["label"] not in LABELS:
            errors.append(f"decision {did!r} label {decision['label']!r} must be one of " + ", ".join(LABELS))
        for key in ("contract", "visible_check", "heldout_check"):
            if key in decision and not (isinstance(decision[key], str) and decision[key].strip()):
                errors.append(f"decision {did!r} {key} must be a non-empty string")
        if isinstance(decision.get("contract"), str) and "\n" in decision["contract"].strip():
            errors.append(f"decision {did!r} contract must be a single line")
    arbitrary = sum(1 for d in decisions if isinstance(d, dict) and d.get("label") == "arbitrary")
    if arbitrary < MIN_ARBITRARY:
        errors.append(f"task has {arbitrary} arbitrary decisions; it needs at least {MIN_ARBITRARY}")
    return errors


def load_task(task_dir):
    """Load one task. Returns (task, errors); task is None when it cannot be used at all."""
    task_dir = Path(task_dir)
    errors = []
    meta = _load_json(task_dir / "task.json", errors)
    decisions = _load_json(task_dir / "decisions.json", errors)
    errors.extend(_schema_errors(task_dir, meta, decisions))
    starter = _read_text(task_dir / "starter.py", "starter.py", errors)
    reference = _read_text(task_dir / "reference.py", "reference.py", errors)
    neutral_text = _read_text(task_dir / "neutral.md", "neutral.md", errors)
    neutral = read_neutral(neutral_text) if neutral_text is not None else []
    if neutral_text is not None and not neutral:
        errors.append("neutral.md has no sentences")
    mutants = {}
    ids = []
    if isinstance(decisions, list):
        ids = [d.get("id") for d in decisions if isinstance(d, dict) and isinstance(d.get("id"), str)]
        # One mutant per decision is enough, arbitrary ones included: a binary choice has a single natural
        # alternative (LOOP_SPEC §4.1). Further --<alt> mutants are optional.
        for did in ids:
            text = _read_text(task_dir / "mutants" / f"{did}.py", f"mutants/{did}.py", errors)
            if text is not None:
                mutants[did] = text
    mutant_dir = task_dir / "mutants"
    if mutant_dir.is_dir():
        for path in sorted(mutant_dir.iterdir()):
            decision, alt = mutant_decision(path.stem), path.stem.partition(ALT_SEPARATOR)[2]
            if path.suffix != ".py" or decision not in ids:
                errors.append(f"mutants/{path.name} does not correspond to a decision")
            elif path.stem != decision and not (_ID_PATTERN.match(alt) and ALT_SEPARATOR not in alt):
                errors.append(f"mutants/{path.name}: alternative name {alt!r} must match {_ID_PATTERN.pattern}")
            elif path.stem != decision:
                text = _read_text(path, f"mutants/{path.name}", errors)
                if text is not None:
                    mutants[path.stem] = text
    if errors or not isinstance(meta, dict) or not isinstance(decisions, list):
        return None, errors or ["task.json or decisions.json is unusable"]
    task = {
        "id": meta["id"],
        "module": meta["module"],
        "module_name": meta["module"][:-3],
        "brief": meta["brief"].strip(),
        "version": meta["version"],
        "author": meta["author"],
        "starter": starter,
        "reference": reference,
        "mutants": mutants,
        "decisions": [
            {
                "id": d["id"],
                "label": d["label"],
                "contract": d["contract"].strip(),
                "visible_check": d["visible_check"],
                "heldout_check": d["heldout_check"],
            }
            for d in decisions
        ],
        "neutral": neutral,
    }
    return task, []


def mutant_decision(name):
    """The decision a mutant (file stem ``<decision-id>`` or ``<decision-id>--<alt>``) violates."""
    return name.partition(ALT_SEPARATOR)[0]


def task_dirs(bank_dir):
    root = Path(bank_dir)
    if not root.is_dir():
        raise TaskBankError(f"task bank {root} is not a directory")
    entries = sorted(root.iterdir())
    links = [p.name for p in entries if p.is_symlink()]
    if links:
        raise TaskBankError(f"task bank {root} contains symlinks: {', '.join(links)}")
    return [p for p in entries if p.is_dir()]


def load_bank_with_hash(bank_dir):
    """Load every task and return (tasks, task_bank_sha256), checking the bank did not change while loading.

    The hash is taken before and after loading; a difference raises TaskBankError, so the returned hash covers
    exactly the bytes that were loaded.
    """
    before = task_bank_sha256(bank_dir)
    tasks = load_bank(bank_dir)
    if task_bank_sha256(bank_dir) != before:
        raise TaskBankError(f"task bank {bank_dir} changed while it was being loaded")
    return tasks, before


def load_bank(bank_dir):
    """Load every task, sorted by id. Raises TaskBankError if any task cannot be loaded."""
    task_bank_sha256(bank_dir)  # rejects symlinks and special files before anything is read
    tasks = []
    problems = []
    for task_dir in task_dirs(bank_dir):
        task, errors = load_task(task_dir)
        if errors:
            problems.extend(f"{task_dir.name}: {e}" for e in errors)
        else:
            tasks.append(task)
    if problems:
        raise TaskBankError("task bank cannot be loaded:\n  " + "\n  ".join(problems))
    if not tasks:
        raise TaskBankError(f"task bank {bank_dir} contains no tasks")
    problems = _bank_level_errors(tasks)
    if problems:
        raise TaskBankError("task bank is inconsistent:\n  " + "\n  ".join(problems))
    return tasks


def _bank_level_errors(tasks):
    errors = []
    modules = {}
    for task in tasks:
        modules.setdefault(task["module"], []).append(task["id"])
    for module, owners in sorted(modules.items()):
        if len(owners) > 1:
            errors.append(
                f"module {module} is used by several tasks ({', '.join(owners)});"
                " multi-task trials need distinct modules"
            )
    return errors


class GradingError(RuntimeError):
    """A check could not be run at all (for example, the grading sandbox failed to start): not a candidate failure."""


def _kill(process):
    if os.name == "posix":
        # The group may already be gone; that is the desired end state.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _communicate(process, data, deadline):
    """communicate() bounded by ``deadline``, then by KILL_GRACE for killing the group and draining the pipes.

    Returns (stdout, stderr, timed_out). A descendant that left the process group can keep a pipe open forever;
    after the grace period its pipes are closed and abandoned, so cleanup never extends the check without bound.
    Under the grading sandbox no descendant can outlive the check: its PID namespace dies with it.
    """
    try:
        stdout, stderr = process.communicate(data, timeout=max(0.0, deadline - time.monotonic()))
        return stdout, stderr, False
    except subprocess.TimeoutExpired:
        pass
    _kill(process)
    try:
        stdout, stderr = process.communicate(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.05)
        stdout, stderr = b"", b""
    return stdout or b"", stderr or b"", True


def run_check(module, module_source, source, timeout=CHECK_TIMEOUT, sandbox=None):
    """Run one check snippet against one copy of a task module, isolated from every other check (LOOP_SPEC §4.2a).

    ``module`` is the module file name (``lab_x.py``) and ``module_source`` its bytes or text. The check runs in a
    fresh temporary directory holding only that copy, as ``python -I -B``, so no bytecode cache is shared or reused
    (inside the sandbox the standard library's own cache is read-only). With a
    :class:`~autonomy_lab.confine.GradingSandbox` it runs confined (no network, read-only root, empty ``$HOME``, only
    its own directory writable); with ``sandbox=None`` it runs unconfined, which is for tests only.

    Passes exactly when the process exits 0 AND prints this check's random completion token after the check's last
    statement. ``timeout`` bounds the whole check, including killing its process group and draining its output.
    ``sandbox_error`` is set when the sandbox itself failed to start; the check then did not run at all.
    """
    if isinstance(module_source, str):
        module_source = module_source.encode("utf-8")
    ready = "lab-ready-" + secrets.token_hex(8)
    token = "lab-done-" + secrets.token_hex(16)
    started = time.monotonic()
    deadline = started + max(0.0, timeout - KILL_GRACE)
    check_dir = Path(tempfile.mkdtemp(prefix="autonomy-lab-check-"))
    try:
        target = check_dir / module
        target.write_bytes(module_source)
        target.chmod(0o644)
        inside = WORK_DIR if sandbox is not None else str(check_dir)
        interpreter = sandbox.interpreter if sandbox is not None else sys.executable
        # -B: no bytecode is written, and the fresh directory holds no __pycache__, so the module is always compiled
        # from this copy. (A pycache_prefix would also redirect the standard library and recompile it every check.)
        command = [interpreter, "-I", "-B", "-c", _CHECK_BOOTSTRAP, inside]
        if sandbox is not None:
            try:
                command, env = sandbox.wrap(command, check_dir)
            except ConfinementError as exc:
                return _outcome(False, None, False, started, "", sandbox_error=f"ConfinementError: {exc}")
        else:
            env = {"PATH": SYSTEM_PATH, "HOME": str(check_dir), "TMPDIR": str(check_dir), "LANG": "C.UTF-8"}
        try:
            process = subprocess.Popen(
                command,
                cwd=check_dir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return _outcome(False, None, False, started, "", sandbox_error=f"cannot start the check: {exc}")
        payload = (ready + "\n" + token + "\n").encode("ascii") + source.encode("utf-8")
        stdout, stderr, timed_out = _communicate(process, payload, deadline)
    finally:
        shutil.rmtree(check_dir, ignore_errors=True)
    out_lines = stdout.decode("utf-8", errors="replace").splitlines()
    err_text = stderr.decode("utf-8", errors="replace")
    exit_code = None if timed_out else process.returncode
    started_ok = ready in out_lines
    sandbox_error = None
    if sandbox is not None and not started_ok:
        # The ready marker is flushed before any check or candidate code runs, so a check that never printed it
        # failed inside the sandbox (setup error, kill, hang), whatever its stderr says: never a candidate failure.
        if timed_out:
            reason = "timed out before the check started"
        else:
            reason = f"exited with status {exit_code} before the check started"
        detail = err_text.strip()[-300:]
        sandbox_error = f"{reason}: {detail}" if detail else reason
    shown = [line for line in out_lines if line not in (ready, token)]
    tail = ("\n".join(shown) + ("\n" if shown and err_text else "") + err_text)[-1200:]
    passed = exit_code == 0 and started_ok and token in out_lines
    return _outcome(passed, exit_code, timed_out, started, tail, sandbox_error=sandbox_error)


def _outcome(passed, exit_code, timed_out, started, tail, sandbox_error=None):
    return {
        "passed": passed,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "output_tail": tail,
        "sandbox_error": sandbox_error,
    }


def run_checks(jobs, workers=1, timeout=CHECK_TIMEOUT, sandbox=None):
    """Run [(module, module_source, check_source), ...], each in isolation, and return results in the same order."""
    if workers <= 1:
        return [run_check(m, text, source, timeout, sandbox) for m, text, source in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda job: run_check(*job, timeout=timeout, sandbox=sandbox), jobs))


def read_module(workspace, module):
    """Return (bytes, None) for the designated module file in ``workspace``, or (None, reason) when it is unusable.

    Only a regular file at exactly ``<workspace>/<module>`` counts: a symlink, a directory, a FIFO or a file larger
    than MAX_MODULE_BYTES is unusable, and a package or any other file with the same import name is never looked at.
    """
    path = Path(workspace) / module
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return None, f"cannot open {module}: {exc.strerror or exc}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, f"{module} is not a regular file"
        if info.st_size > MAX_MODULE_BYTES:
            return None, f"{module} is larger than {MAX_MODULE_BYTES} bytes"
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
            if sum(map(len, chunks)) > MAX_MODULE_BYTES:
                return None, f"{module} is larger than {MAX_MODULE_BYTES} bytes"
        return b"".join(chunks), None
    except OSError as exc:
        return None, f"cannot read {module}: {exc.strerror or exc}"
    finally:
        os.close(fd)


def snapshot_modules(workspace, tasks):
    """{task_id: (bytes or None, reason or None)} for each task's designated module, read once."""
    return {task["id"]: read_module(workspace, task["module"]) for task in tasks}


def grade_modules(modules, tasks, timeout=CHECK_TIMEOUT, sandbox=None, workers=None):
    """Grade every decision of every task from module snapshots, each held-out check in its own isolated run.

    ``modules`` is :func:`snapshot_modules` output. Checks come from the loaded bank, never from a workspace.
    Returns (decision_results, details): {task_id: {decision_id: bool}} and the same shape holding each check's
    exit code, timeout flag, duration and output tail. A task whose module is unusable fails every decision.
    Raises GradingError when the sandbox could not start a check, which is not the candidate's failure.
    Checks share no state, so they may run in parallel (``workers``, default up to 4).
    """
    jobs = []
    keys = []
    results = {task["id"]: {} for task in tasks}
    details = {task["id"]: {} for task in tasks}
    for task in tasks:
        source, reason = modules[task["id"]]
        for decision in task["decisions"]:
            if source is None:
                results[task["id"]][decision["id"]] = False
                details[task["id"]][decision["id"]] = {
                    "passed": False,
                    "exit_code": None,
                    "timed_out": False,
                    "seconds": 0.0,
                    "output_tail": reason,
                    "sandbox_error": None,
                }
                continue
            jobs.append((task["module"], source, decision["heldout_check"]))
            keys.append((task["id"], decision["id"]))
    workers = workers or min(4, os.cpu_count() or 1)
    outcomes = run_checks(jobs, workers=workers, timeout=timeout, sandbox=sandbox)
    for (task_id, decision_id), outcome in zip(keys, outcomes):
        if outcome["sandbox_error"]:
            raise GradingError(f"grading sandbox failed for {task_id}/{decision_id}: {outcome['sandbox_error']}")
        results[task_id][decision_id] = outcome["passed"]
        details[task_id][decision_id] = outcome
    return results, details


def grade_workspace(workspace, tasks, timeout=CHECK_TIMEOUT, sandbox=None, workers=None):
    """Snapshot each task's designated module from ``workspace``, then grade it with :func:`grade_modules`."""
    return grade_modules(snapshot_modules(workspace, tasks), tasks, timeout=timeout, sandbox=sandbox, workers=workers)


def default_grading_sandbox(results_dirs=(), allow_unconfined=False):
    """The grading sandbox to use, or None (unconfined) only when ``allow_unconfined`` and bwrap is unavailable.

    Raises GradingError when bwrap is unavailable and unconfined grading was not explicitly allowed.
    """
    reason = bwrap_unavailable_reason()
    if reason is None:
        try:
            return GradingSandbox(results_dirs=results_dirs)
        except ConfinementError as exc:
            reason = str(exc)
    if allow_unconfined:
        return None
    raise GradingError(
        f"the grading sandbox is unavailable ({reason}); refusing to run agent-written code unconfined."
        " Pass --unconfined-grading-for-tests only for tests on a host without bubblewrap."
    )


def _module_importable_without_workspace(module_name, interpreter=None):
    probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is None else 1)"
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(
            [interpreter or sys.executable, "-I", "-c", probe, module_name],
            cwd=empty,
            capture_output=True,
            timeout=CHECK_TIMEOUT,
        )
    return result.returncode != 0


def validate_task(task, workers=None, sandbox=None):
    """Mechanical validation of one loaded task (LOOP_SPEC §4.1).

    Every check runs through the same isolated path as grading (:func:`run_check`, one fresh directory and
    interpreter per check, confined by ``sandbox``), so a visible check cannot create state for a held-out check.
    Every mutant, including each ``<decision>--<alt>`` alternative, must fail exactly its own decision's held-out
    check, fail its own visible check and pass every other visible check. Beyond the §4.1 rules it also requires
    that the assembled visible_checks.py (every visible check) passes on the reference and fails on every mutant,
    that the starter passes `preserve` decisions, and that filler can reach the ±5% band for every set of contract
    decisions. Returns a report dict with "errors", "warnings" and the pass/fail matrix for the reference, the
    starter and every mutant.
    """
    workers = workers or min(16, os.cpu_count() or 1)
    errors = []
    warnings = []
    module = task["module"]
    decisions = task["decisions"]
    ids = [d["id"] for d in decisions]
    interpreter = sandbox.interpreter if sandbox is not None else None
    if _module_importable_without_workspace(task["module_name"], interpreter):
        errors.append(f"module {module} is importable without the workspace and would shadow the task module")

    candidates = {"reference": task["reference"], "starter": task["starter"]}
    candidates.update({f"mutant:{name}": text for name, text in sorted(task["mutants"].items())})
    all_visible = build_visible_checks([task], {task["id"]: {did: "check" for did in ids}})
    matrix = {}
    jobs = []
    keys = []
    for name, text in candidates.items():
        for decision in decisions:
            for kind in ("heldout", "visible"):
                jobs.append((module, text, decision[kind + "_check"]))
                keys.append((name, kind, decision["id"]))
        # The assembled file must behave like its parts: a snippet that
        # exits early or leaks state would hide later checks.
        jobs.append((module, text, all_visible))
        keys.append((name, "visible_file", None))
    outcomes = run_checks(jobs, workers=workers, sandbox=sandbox)
    sandbox_errors = sorted({o["sandbox_error"] for o in outcomes if o["sandbox_error"]})
    if sandbox_errors:
        errors.append("the grading sandbox failed to start: " + sandbox_errors[0])
        return {"id": task["id"], "valid": False, "errors": errors, "warnings": warnings, "matrix": {}}
    for (name, kind, did), outcome in zip(keys, outcomes):
        entry = matrix.setdefault(name, {"heldout": {}, "visible": {}, "visible_file": None})
        if kind == "visible_file":
            entry["visible_file"] = outcome["passed"]
        else:
            entry[kind][did] = outcome["passed"]
        if outcome["timed_out"]:
            warnings.append(f"{name} {kind} check {did or ''} timed out after {CHECK_TIMEOUT} s".replace("  ", " "))

    reference = matrix["reference"]
    for did in ids:
        if not reference["heldout"][did]:
            errors.append(f"reference fails the held-out check of {did!r}")
        if not reference["visible"][did]:
            errors.append(f"reference fails the visible check of {did!r}")
    if not reference["visible_file"]:
        errors.append("reference fails the assembled visible_checks.py with every visible check")

    for name in sorted(task["mutants"]):
        did = mutant_decision(name)
        row = matrix[f"mutant:{name}"]
        if row["heldout"][did]:
            errors.append(f"mutant {name!r} passes its own held-out check")
        others = [o for o in ids if o != did and not row["heldout"][o]]
        if others:
            errors.append(f"mutant {name!r} also fails the held-out checks of {', '.join(map(repr, others))}")
        if row["visible"][did]:
            errors.append(f"mutant {name!r} passes its own visible check")
        if row["visible_file"]:
            errors.append(f"mutant {name!r} passes the assembled visible_checks.py with every visible check")
        leaks = [o for o in ids if o != did and not row["visible"][o]]
        if leaks:
            errors.append(
                f"mutant {name!r} also fails the visible checks of {', '.join(map(repr, leaks))}"
                " (those checks carry information about this decision)"
            )

    starter = matrix["starter"]
    failed = [did for did in ids if not starter["heldout"][did]]
    labels = {d["id"]: d["label"] for d in decisions}
    if 2 * len(failed) < len(ids):
        errors.append(f"starter fails {len(failed)} of {len(ids)} held-out checks; it must fail at least half")
    failed_arbitrary = [did for did in failed if labels[did] == "arbitrary"]
    if len(failed_arbitrary) < MIN_ARBITRARY:
        errors.append(
            f"starter fails {len(failed_arbitrary)} arbitrary held-out checks; it must fail at least {MIN_ARBITRARY}"
        )
    for did in ids:
        if labels[did] == "preserve" and not starter["heldout"][did]:
            errors.append(f"starter fails the held-out check of preserve decision {did!r}")
        if labels[did] == "preserve" and not starter["visible"][did]:
            warnings.append(f"starter fails the visible check of preserve decision {did!r}")

    errors.extend(filler_feasibility_errors(task))
    return {"id": task["id"], "valid": not errors, "errors": errors, "warnings": warnings, "matrix": matrix}


def validate_bank(bank_dir, workers=None, sandbox=None):
    """Load and mechanically validate a whole bank. Never raises for task defects.

    ``sandbox`` is the :class:`~autonomy_lab.confine.GradingSandbox` every check runs in; None runs them
    unconfined (tests only). The report's hash is taken before loading and checked again afterwards.
    """
    root = Path(bank_dir)
    report = {"bank": str(root), "task_bank_sha256": None, "valid": False, "errors": [], "tasks": []}
    try:
        report["task_bank_sha256"] = task_bank_sha256(root)
        dirs = task_dirs(root)
    except TaskBankError as exc:
        report["errors"].append(str(exc))
        return report
    if not dirs:
        report["errors"].append("task bank contains no tasks")
    loaded = []
    for task_dir in dirs:
        task, errors = load_task(task_dir)
        if errors:
            report["tasks"].append({"id": task_dir.name, "valid": False, "errors": errors, "warnings": []})
            continue
        loaded.append(task)
        report["tasks"].append(validate_task(task, workers=workers, sandbox=sandbox))
    report["errors"].extend(_bank_level_errors(loaded))
    try:
        if task_bank_sha256(root) != report["task_bank_sha256"]:
            report["errors"].append("task bank changed during validation")
    except TaskBankError as exc:
        report["errors"].append(str(exc))
    report["valid"] = not report["errors"] and all(t["valid"] for t in report["tasks"])
    return report


def format_report(report):
    lines = [f"task bank {report['bank']}", f"task_bank_sha256 {report['task_bank_sha256']}"]
    lines.extend("ERROR " + e for e in report["errors"])
    for task in report["tasks"]:
        lines.append(f"{'ok     ' if task['valid'] else 'INVALID'} {task['id']}")
        lines.extend("  ERROR " + e for e in task["errors"])
        lines.extend("  warning " + w for w in task["warnings"])
    lines.append("VALID" if report["valid"] else "INVALID")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autonomy Lab task bank tools (LOOP_SPEC §4.1)")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="mechanically validate a task bank")
    validate.add_argument("bank", type=Path)
    validate.add_argument("--json", action="store_true", help="print the full JSON report")
    validate.add_argument(
        "--unconfined-grading-for-tests",
        action="store_true",
        help="run checks without the bubblewrap grading sandbox when it is unavailable (tests only)",
    )
    digest = commands.add_parser("hash", help="print task_bank_sha256")
    digest.add_argument("bank", type=Path)
    args = parser.parse_args(argv)
    if args.command == "hash":
        try:
            print(task_bank_sha256(args.bank))
        except TaskBankError as exc:
            parser.error(str(exc))
        return 0
    try:
        sandbox = default_grading_sandbox(
            results_dirs=[str(args.bank.resolve())], allow_unconfined=args.unconfined_grading_for_tests
        )
    except (GradingError, ConfinementError) as exc:
        parser.error(str(exc))
    report = validate_bank(args.bank, sandbox=sandbox)
    report["grading_confined"] = sandbox is not None
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
