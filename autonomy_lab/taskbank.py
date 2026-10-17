"""Task bank loading, hashing, mechanical validation and per-decision grading (LOOP_SPEC §4.1, T1).

A task bank is a directory with one sub-directory per task:

    task.json        {"id", "module", "brief", "version", "author"}
    starter.py       weak starting implementation, copied into the workspace as <module>
    reference.py     a solution that satisfies every decision
    mutants/<d>.py   the reference with only decision <d> violated
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
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autonomy_lab.design import build_visible_checks, filler_feasibility_errors

LABELS = ("inferable", "arbitrary", "preserve")
AUTHORS = ("coordinator", "user")
TASK_FIELDS = ("id", "module", "brief", "version", "author")
DECISION_FIELDS = ("id", "label", "contract", "visible_check", "heldout_check")
CHECK_TIMEOUT = 10
MIN_DECISIONS = 4
MAX_DECISIONS = 8
MIN_ARBITRARY = 2
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Runs one check in a fresh interpreter. The workspace is appended (not
# prepended) to sys.path so that workspace files cannot shadow the standard
# library; validation rejects module names that the clean interpreter can
# already import, so the task module always resolves to the workspace copy.
_CHECK_BOOTSTRAP = (
    "import sys\n"
    "sys.path.append(sys.argv[1])\n"
    "del sys.argv[1:]\n"
    "_source = sys.stdin.read()\n"
    "exec(compile(_source, '<check>', 'exec'), {'__name__': '__main__'})\n"
)


class TaskBankError(ValueError):
    """The bank cannot be loaded at all (as opposed to failing validation)."""


def task_bank_sha256(bank_dir):
    """SHA-256 over every file under the bank, sorted by relative path.

    The digest is taken over a manifest with one line per file,
    ``<relative posix path>\\0<sha256 of content>\\n``, in byte order of the
    UTF-8 encoded paths, so every path and every content byte is covered and
    no file boundary is ambiguous.
    """
    root = Path(bank_dir)
    if not root.is_dir():
        raise TaskBankError(f"task bank {root} is not a directory")
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
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
        if "module" in meta and not (
            isinstance(module, str) and module.endswith(".py") and module[:-3].isidentifier()
        ):
            errors.append(f"task.json module {module!r} must be '<identifier>.py'")
        elif isinstance(module, str) and module[:-3] in sys.stdlib_module_names:
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
        for did in ids:
            text = _read_text(task_dir / "mutants" / f"{did}.py", f"mutants/{did}.py", errors)
            if text is not None:
                mutants[did] = text
    mutant_dir = task_dir / "mutants"
    if mutant_dir.is_dir():
        for path in sorted(mutant_dir.iterdir()):
            if path.suffix != ".py" or path.stem not in ids:
                errors.append(f"mutants/{path.name} does not correspond to a decision")
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


def task_dirs(bank_dir):
    root = Path(bank_dir)
    if not root.is_dir():
        raise TaskBankError(f"task bank {root} is not a directory")
    return sorted(p for p in root.iterdir() if p.is_dir())


def load_bank(bank_dir):
    """Load every task, sorted by id. Raises TaskBankError if any task cannot be loaded."""
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


def _kill(process):
    if os.name == "posix":
        # The group may already be gone; that is the desired end state.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()


def run_check(workspace, source, timeout=CHECK_TIMEOUT, pycache_dir=None):
    """Run one check snippet in its own `python -I` process with cwd=workspace.

    Passes exactly when the process exits 0 within the timeout. Bytecode is
    redirected to pycache_dir so a stale or planted __pycache__ in the
    workspace is never read.
    """
    workspace = Path(workspace).resolve()
    command = [sys.executable, "-I"]
    if pycache_dir is not None:
        command += ["-X", "pycache_prefix=" + str(pycache_dir)]
    command += ["-c", _CHECK_BOOTSTRAP, str(workspace)]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name == "posix"),
    )
    timed_out = False
    try:
        output, _ = process.communicate(source.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(process)
        output, _ = process.communicate()
    exit_code = None if timed_out else process.returncode
    return {
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": round(time.monotonic() - started, 3),
        "output_tail": output.decode("utf-8", errors="replace")[-1200:],
    }


def run_checks(jobs, workers=1, timeout=CHECK_TIMEOUT):
    """Run [(workspace, source), ...] and return results in the same order."""
    with tempfile.TemporaryDirectory(prefix="autonomy-lab-pycache-") as pycache:
        if workers <= 1:
            return [run_check(w, s, timeout, pycache) for w, s in jobs]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda job: run_check(job[0], job[1], timeout, pycache), jobs))


def grade_workspace(workspace, tasks, timeout=CHECK_TIMEOUT):
    """Grade every decision of every task with its own held-out check process.

    Returns (decision_results, details): {task_id: {decision_id: bool}} and the
    same shape holding each check's exit code, timeout flag, duration and output tail.
    """
    jobs = []
    keys = []
    for task in tasks:
        for decision in task["decisions"]:
            jobs.append((workspace, decision["heldout_check"]))
            keys.append((task["id"], decision["id"]))
    outcomes = run_checks(jobs, workers=1, timeout=timeout)
    results = {task["id"]: {} for task in tasks}
    details = {task["id"]: {} for task in tasks}
    for (task_id, decision_id), outcome in zip(keys, outcomes):
        results[task_id][decision_id] = outcome["passed"]
        details[task_id][decision_id] = outcome
    return results, details


def _module_importable_without_workspace(module_name):
    probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is None else 1)"
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, module_name], cwd=empty, capture_output=True, timeout=CHECK_TIMEOUT
        )
    return result.returncode != 0


def validate_task(task, workers=None):
    """Mechanical validation of one loaded task (LOOP_SPEC §4.1).

    Beyond the four §4.1 rules it also requires that the assembled
    visible_checks.py (every visible check) passes on the reference and fails on
    every mutant, that the starter passes `preserve` decisions, and that filler
    can reach the ±5% band for every set of contract decisions.
    Returns a report dict with "errors", "warnings" and the pass/fail matrix
    for the reference, the starter and every mutant.
    """
    workers = workers or min(8, os.cpu_count() or 1)
    errors = []
    warnings = []
    module = task["module"]
    decisions = task["decisions"]
    ids = [d["id"] for d in decisions]
    if _module_importable_without_workspace(task["module_name"]):
        errors.append(f"module {module} is importable without the workspace and would shadow the task module")

    candidates = {"reference": task["reference"], "starter": task["starter"]}
    candidates.update({f"mutant:{did}": text for did, text in task["mutants"].items()})
    all_visible = build_visible_checks([task], {task["id"]: {did: "check" for did in ids}})
    matrix = {}
    with tempfile.TemporaryDirectory(prefix="autonomy-lab-validate-") as tmp:
        jobs = []
        keys = []
        for index, (name, text) in enumerate(candidates.items()):
            workspace = Path(tmp) / f"c{index}"
            workspace.mkdir()
            (workspace / module).write_text(text, encoding="utf-8")
            for decision in decisions:
                for kind in ("heldout", "visible"):
                    jobs.append((workspace, decision[kind + "_check"]))
                    keys.append((name, kind, decision["id"]))
            # The assembled file must behave like its parts: a snippet that
            # exits early or leaks state would hide later checks.
            jobs.append((workspace, all_visible))
            keys.append((name, "visible_file", None))
        outcomes = run_checks(jobs, workers=workers)
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

    for did in ids:
        row = matrix[f"mutant:{did}"]
        if row["heldout"][did]:
            errors.append(f"mutant {did!r} passes its own held-out check")
        others = [o for o in ids if o != did and not row["heldout"][o]]
        if others:
            errors.append(f"mutant {did!r} also fails the held-out checks of {', '.join(map(repr, others))}")
        if row["visible"][did]:
            errors.append(f"mutant {did!r} passes its own visible check")
        if row["visible_file"]:
            errors.append(f"mutant {did!r} passes the assembled visible_checks.py with every visible check")
        leaks = [o for o in ids if o != did and not row["visible"][o]]
        if leaks:
            warnings.append(
                f"mutant {did!r} also fails the visible checks of {', '.join(map(repr, leaks))}"
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


def validate_bank(bank_dir, workers=None):
    """Load and mechanically validate a whole bank. Never raises for task defects."""
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
        report["tasks"].append(validate_task(task, workers=workers))
    report["errors"].extend(_bank_level_errors(loaded))
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
    digest = commands.add_parser("hash", help="print task_bank_sha256")
    digest.add_argument("bank", type=Path)
    args = parser.parse_args(argv)
    if args.command == "hash":
        try:
            print(task_bank_sha256(args.bank))
        except TaskBankError as exc:
            parser.error(str(exc))
        return 0
    report = validate_bank(args.bank)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
