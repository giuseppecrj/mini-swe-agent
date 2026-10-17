"""Run controlled Autonomy Lab experiments through the user's installed Pi CLI.

Requires an explicit --model provider/id and --thinking level, so that Pi's
ambient default model or settings cannot silently change the treatment.
A local Pi process is NOT a security sandbox: see SESSION_HANDOFF.md.

With --task-bank DIR the runner instead validates a LOOP_SPEC §4.1 task bank,
writes a seeded design.json (--experiment, --design-seed), runs every trial,
grades each decision with its own held-out check and classifies failures (§2.4).
Add --design-only to write the design and prompts without running Pi.
"""

import argparse
import csv
import hashlib
import importlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from autonomy_lab.design import EXPERIMENTS, canonical_json, generate_design, trial_files
from autonomy_lab.runner import TASKS, build_prompt, check_candidate, prepare_workspace
from autonomy_lab.taskbank import format_report, grade_workspace, load_bank, task_bank_sha256, validate_bank

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def validate_model(model):
    """Accept only an exact provider/model-id, without Pi's ':<thinking>' shorthand."""
    provider, _, model_id = (model or "").partition("/")
    if not provider or not model_id:
        raise ValueError("--model must be an exact provider/model-id")
    # Model IDs may legitimately contain ':' (e.g. 'gpt-oss:20b'), so reject
    # only a trailing thinking-level suffix.
    if ":" in model_id and model_id.rpartition(":")[2] in THINKING_LEVELS:
        raise ValueError("pass the thinking level with --thinking, not as a --model suffix")
    return model


def parse_pi_events(path):
    """Extract measurable events without assuming that process exit 0 means model success."""
    metrics = {
        "settled": False,
        "aborted": False,
        "assistant_errors": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "assistant_turns": 0,
        "retries": 0,
        "total_tokens": 0,
        "reported_cost_usd": None,
        "model_ids": [],
        "thinking_levels": [],
        "assistant_messages": 0,
        "assistant_messages_missing_runtime": 0,
        "invalid_json_lines": 0,
    }
    model_ids = set()
    thinking_levels = set()
    costs = []
    if not path.exists():
        return metrics
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                metrics["invalid_json_lines"] += 1
                continue
            if not isinstance(event, dict):
                metrics["invalid_json_lines"] += 1
                continue
            kind = event.get("type")
            if kind == "agent_settled":
                metrics["settled"] = True
                metrics["aborted"] = bool(event.get("aborted", False))
            elif kind == "turn_end":
                metrics["assistant_turns"] += 1
            elif kind == "tool_execution_start":
                metrics["tool_calls"] += 1
            elif kind == "tool_execution_end" and event.get("isError"):
                metrics["tool_errors"] += 1
            elif kind == "auto_retry_start":
                metrics["retries"] += 1
            elif kind == "message_end":
                message = event.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                metrics["assistant_messages"] += 1
                if message.get("stopReason") in {"error", "aborted", "length"}:
                    metrics["assistant_errors"] += 1
                provider = message.get("provider")
                model = message.get("model")
                thinking = message.get("thinkingLevel")
                if not (provider and model and thinking):
                    metrics["assistant_messages_missing_runtime"] += 1
                if model:
                    model_ids.add((str(provider) + "/" if provider else "") + str(model))
                if thinking:
                    thinking_levels.add(str(thinking))
                usage = message.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                try:
                    metrics["total_tokens"] += int(usage.get("totalTokens") or 0)
                except (TypeError, ValueError):
                    pass
                cost = usage.get("cost")
                total_cost = cost.get("total") if isinstance(cost, dict) else None
                if isinstance(total_cost, (float, int)):
                    costs.append(float(total_cost))
    metrics["model_ids"] = sorted(model_ids)
    metrics["thinking_levels"] = sorted(thinking_levels)
    if costs:
        metrics["reported_cost_usd"] = round(sum(costs), 6)
    return metrics


def pi_command(model, thinking, prompt, pi_binary="pi"):
    # Disable ambient skills, extensions, context files, MCP, and project-local
    # configuration so that a treatment cannot accidentally see the handoff.
    return [
        pi_binary,
        "--mode",
        "json",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-mcp",
        "--no-approve",
        "--tools",
        "read,bash,edit,write",
        "--model",
        model,
        "--thinking",
        thinking,
        "--",
        prompt,
    ]


def runtime_matches_request(run, model, thinking):
    """True only when every assistant message reports the requested model and thinking.

    A message missing its provider, model or thinking level, or any unparseable
    event line, counts as unverified rather than as a match.
    """
    return (
        run["assistant_messages"] > 0
        and run["assistant_messages_missing_runtime"] == 0
        and run["invalid_json_lines"] == 0
        and run["model_ids"] == [model]
        and run["thinking_levels"] == [thinking]
    )


class ConfinementSetupError(RuntimeError):
    """The confinement wrapper could not prepare the Pi command (an infrastructure failure)."""


def execute_pi(workspace, log_dir, prompt, model, thinking, pi_binary, timeout, confine=None):
    # Harness logs live outside the agent's working directory so they do not
    # appear in its file listings. This prevents accidental exposure only: an
    # agent that deliberately reads ../../logs can still find them.
    log_dir.mkdir(parents=True, exist_ok=True)
    events_path = log_dir / "pi-events.jsonl"
    stderr_path = log_dir / "pi-stderr.log"
    command = pi_command(model, thinking, prompt, pi_binary=pi_binary)
    env = None
    if confine is not None:
        # confine(command, workspace) -> (command, env); env None inherits ours.
        try:
            command, env = confine(command, workspace)
        except Exception as exc:
            raise ConfinementSetupError(f"{type(exc).__name__}: {exc}") from exc
    started = time.monotonic()
    expired = False
    with events_path.open("w", encoding="utf-8") as events, stderr_path.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=events,
            stderr=errors,
            env=env,
            start_new_session=(os.name == "posix"),
        )
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            expired = True
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            exit_code = None
    return {
        "exit_code": exit_code,
        "timed_out": expired,
        "seconds": round(time.monotonic() - started, 3),
        "stderr_tail": stderr_path.read_text(encoding="utf-8", errors="replace")[-1200:],
        **parse_pi_events(events_path),
    }


def safe_trial_scope(workspace, selected, expected_checks, expected_prompt):
    """Detect alteration or deletion of public tests or the specification."""
    try:
        if (workspace / "visible_checks.py").read_bytes() != expected_checks:
            return False
        if (workspace / "prompt.txt").read_bytes() != expected_prompt:
            return False
        # Modules are the only allowed source edits. Data/log files created by
        # the runner are not used to determine acceptance.
        return all((workspace / task["file"]).is_file() for task in selected)
    except OSError:
        return False


def pi_version(pi_binary):
    result = subprocess.run([pi_binary, "--version"], text=True, capture_output=True, timeout=15)
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def git_head():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def run_experiment(args):
    if shutil.which(args.pi_binary) is None:
        raise RuntimeError(
            f"Pi CLI '{args.pi_binary}' is not installed or not on PATH. "
            "Run 'pi --help' in the local terminal and configure model authentication."
        )
    if args.repetitions < 1 or args.timeout < 1:
        raise ValueError("--repetitions and --timeout must be positive")
    validate_model(args.model)
    if args.thinking not in THINKING_LEVELS:
        raise ValueError("--thinking must be one of " + ", ".join(THINKING_LEVELS))
    breadths = [int(x) for x in args.breadths.split(",")]
    depths = args.depths.split(",")
    verifications = args.verifications.split(",")
    if not breadths or any(x not in range(1, len(TASKS) + 1) for x in breadths):
        raise ValueError("--breadths must be 1 or 2")
    if any(x not in ("shallow", "deep") for x in depths):
        raise ValueError("--depths must be shallow and/or deep")
    if any(x not in ("basic", "rich") for x in verifications):
        raise ValueError("--verifications must be basic and/or rich")

    # Always create a new run namespace; existing trials are never re-used.
    label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = args.out.resolve() / f"run-{label}-{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=False)
    (root / "workspaces").mkdir()
    (root / "logs").mkdir()
    version = pi_version(args.pi_binary)
    metadata = {
        "schema_version": "0.2.0",
        "backend": "pi-local",
        "model_requested": args.model,
        "thinking_requested": args.thinking,
        "pi_version": version,
        "git_commit": git_head(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_environment": "local process using caller's OS permissions; NOT sandboxed",
        "model_calls_issued": True,
        "human_interventions_allowed": 0,
        "experiment_limitations": [
            "Trial workspaces are isolated from one another, not from host OS.",
            "Held-out tests are not OS-level hidden from a full-access local Pi process.",
            "Two toy tasks are insufficient to estimate an autonomy scaling law.",
            "Only one deterministic task ordering is used; task complexity is confounded.",
            "Token cost is provider-reported and may be unavailable or incomplete.",
        ],
    }
    (root / "run-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    results = []
    for breadth in breadths:
        for depth in depths:
            for verification in verifications:
                for repetition in range(1, args.repetitions + 1):
                    selected = TASKS[:breadth]
                    trial_id = f"b{breadth}-{depth}-{verification}-r{repetition}"
                    workspace = root / "workspaces" / trial_id
                    log_dir = root / "logs" / trial_id
                    prepare_workspace(workspace, selected, verification)
                    prompt = build_prompt(selected, depth, verification)
                    prompt_path = workspace / "prompt.txt"
                    prompt_path.write_text(prompt, encoding="utf-8")
                    expected_checks = (workspace / "visible_checks.py").read_bytes()
                    expected_prompt = prompt_path.read_bytes()

                    print("START " + trial_id, flush=True)
                    try:
                        run = execute_pi(
                            workspace,
                            log_dir,
                            prompt,
                            args.model,
                            args.thinking,
                            args.pi_binary,
                            args.timeout,
                        )
                    except OSError as exc:
                        run = {
                            "exit_code": None,
                            "timed_out": False,
                            "seconds": 0,
                            "settled": False,
                            "aborted": False,
                            "assistant_errors": 1,
                            "tool_calls": 0,
                            "tool_errors": 0,
                            "assistant_turns": 0,
                            "retries": 0,
                            "total_tokens": 0,
                            "reported_cost_usd": None,
                            "model_ids": [],
                            "thinking_levels": [],
                            "assistant_messages": 0,
                            "assistant_messages_missing_runtime": 0,
                            "invalid_json_lines": 0,
                            "stderr_tail": str(exc),
                        }
                    scope_ok = safe_trial_scope(workspace, selected, expected_checks, expected_prompt)
                    passed, grade = check_candidate(workspace, selected, "mock")
                    complete = bool(
                        run["exit_code"] == 0
                        and run["settled"]
                        and not run["aborted"]
                        and not run["timed_out"]
                        and run["assistant_errors"] == 0
                    )
                    runtime_ok = runtime_matches_request(run, args.model, args.thinking)
                    accepted = passed and complete and scope_ok and runtime_ok
                    record = {
                        "trial_id": trial_id,
                        "breadth": breadth,
                        "depth": depth,
                        "verification": verification,
                        "repetition": repetition,
                        "accepted": accepted,
                        "held_out_tests_passed": passed,
                        "task_scope_intact": scope_ok,
                        "agent_completed": complete,
                        "runtime_matches_request": runtime_ok,
                        "model_requested": args.model,
                        "thinking_requested": args.thinking,
                        "grader_exit_code": grade["exit_code"],
                        **run,
                    }
                    results.append(record)
                    # Append after each trial so interruptions leave recoverable data.
                    with (root / "trials.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record) + "\n")
                    print(
                        "DONE " + trial_id + " accepted=" + str(accepted) + " turns=" + str(run["assistant_turns"]),
                        flush=True,
                    )

    summaries = []
    for breadth in breadths:
        for depth in depths:
            for verification in verifications:
                rows = [
                    t
                    for t in results
                    if t["breadth"] == breadth and t["depth"] == depth and t["verification"] == verification
                ]
                summaries.append(
                    {
                        "breadth": breadth,
                        "depth": depth,
                        "verification": verification,
                        "trials": len(rows),
                        "accepted": sum(int(t["accepted"]) for t in rows),
                        "accepted_pct": round(100 * sum(int(t["accepted"]) for t in rows) / len(rows), 2),
                        "runtime_mismatches": sum(int(not t["runtime_matches_request"]) for t in rows),
                        "mean_agent_seconds": round(sum(t["seconds"] for t in rows) / len(rows), 2),
                        "mean_agent_turns": round(sum(t["assistant_turns"] for t in rows) / len(rows), 2),
                    }
                )
    summary = {
        "run_id": root.name,
        "backend": "pi-local",
        "is_real_agent_evidence": True,
        "limitations": metadata["experiment_limitations"],
        "summary": summaries,
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (root / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    print("RESULTS " + str(root), flush=True)
    return root


# ---------------------------------------------------------------------------
# Task-bank mode (LOOP_SPEC §2.3, §2.4, §4.1, §4.3)

INFRASTRUCTURE_FAILURES = (
    "provider_error",
    "retry_exhausted",
    "spawn",
    "auth",
    "sandbox",
    "no_settle",
    "malformed_events",
    "provider_stall",
)
AGENT_FAILURES = ("timeout", "length", "heldout_failed")
STALL_SECONDS = 120

# Fields an event must carry, by type, for the harness to trust it:
# (field, type, required). Deliberately limited to what parse_pi_events and
# the classifier read, so a well-formed Pi stream is never flagged.
EVENT_FIELDS = {
    "message_start": (("message", dict, True),),
    "message_update": (("message", dict, True),),
    "message_end": (("message", dict, True),),
    "auto_retry_end": (("success", bool, True),),
    "agent_settled": (("aborted", bool, False),),
    "tool_execution_end": (("isError", bool, False),),
}
# Last events that mean Pi is waiting for the provider to start answering.
PENDING_EVENTS = ("session", "agent_start", "turn_start", "auto_retry_start")
AUTH_PATTERN = re.compile(
    r"\b40[13]\b|unauthori[sz]ed|authentication|authenticate|api[ _-]?key|oauth|credential|not logged in"
    r"|login required|forbidden|token (?:has )?expired|invalid[ _]token",
    re.IGNORECASE,
)


def _event_problem(event):
    if not isinstance(event, dict):
        return "event is not a JSON object"
    kind = event.get("type")
    if not isinstance(kind, str) or not kind:
        return "event has no type"
    for field, expected, required in EVENT_FIELDS.get(kind, ()):
        if field not in event:
            if required:
                return f"{kind} without {field}"
        elif not isinstance(event[field], expected):
            return f"{kind} {field} is not a {expected.__name__}"
    message = event.get("message")
    if isinstance(message, dict) and kind in EVENT_FIELDS and not isinstance(message.get("role"), str):
        return f"{kind} message without a role"
    return None


def scan_events(path):
    """Structural scan of a Pi JSON event stream for failure classification (§2.4)."""
    scan = {
        "events": 0,
        "invalid_lines": [],
        "malformed": [],
        "last_line": 0,
        "final_line_unterminated": False,
        "last_event": None,
        "settled": False,
        "settled_aborted": False,
        "retry_failed": False,
        "provider_errors": 0,
        "aborted_messages": 0,
        "length_stops": 0,
        "error_messages": [],
    }
    if not path.exists():
        return scan
    text = path.read_text(encoding="utf-8", errors="replace")
    scan["final_line_unterminated"] = bool(text) and not text.endswith("\n")
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        scan["last_line"] = number
        try:
            event = json.loads(line)
        except ValueError:
            scan["invalid_lines"].append(number)
            continue
        problem = _event_problem(event)
        if problem:
            scan["malformed"].append(f"line {number}: {problem}")
            continue
        scan["events"] += 1
        scan["last_event"] = event
        kind = event["type"]
        if kind == "agent_settled":
            scan["settled"] = True
            scan["settled_aborted"] = bool(event.get("aborted", False))
        elif kind == "auto_retry_end" and not event["success"]:
            scan["retry_failed"] = True
            scan["error_messages"].append(str(event.get("finalError") or ""))
        elif kind == "message_end" and event["message"]["role"] == "assistant":
            message = event["message"]
            stop = message.get("stopReason")
            if stop == "error":
                scan["provider_errors"] += 1
                scan["error_messages"].append(str(message.get("errorMessage") or ""))
            elif stop == "aborted":
                scan["aborted_messages"] += 1
            elif stop == "length":
                scan["length_stops"] += 1
    return scan


def _has_output(message):
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and (part.get("text") or part.get("thinking") or part.get("type") == "toolCall"):
                return True
    usage = message.get("usage")
    return isinstance(usage, dict) and bool(usage.get("output"))


def pending_request(event):
    """True when the last event leaves Pi waiting for an assistant reply that has produced no tokens."""
    if event is None:
        return True
    kind = event["type"]
    if kind in PENDING_EVENTS:
        return True
    message = event.get("message")
    if kind == "message_end":
        # A user prompt or tool result was just sent; the reply is outstanding.
        return message["role"] != "assistant"
    if kind in ("message_start", "message_update") and message["role"] == "assistant":
        return not _has_output(message)
    return False


def classify_failure(run, scan, decision_results, idle_seconds=None):
    """Return (infrastructure_failure, agent_failure) for one attempt, by where the failure came from (§2.4).

    Infrastructure causes take precedence. A timeout is a provider stall when
    the last event is a pending request with no tokens and nothing arrived for
    more than STALL_SECONDS (idle_seconds). A SIGKILL on timeout may cut the
    final line short; that one line alone is not a malformed stream.
    """
    if run.get("sandbox_error"):
        return "sandbox", None
    if run.get("spawn_error"):
        return "spawn", None
    invalid = list(scan["invalid_lines"])
    if run["timed_out"] and scan["final_line_unterminated"] and invalid and invalid[-1] == scan["last_line"]:
        invalid.pop()
    if invalid or scan["malformed"]:
        return "malformed_events", None
    stderr = run.get("stderr_tail") or ""
    if scan["events"] == 0 and not run["timed_out"]:
        return ("auth" if AUTH_PATTERN.search(stderr) else "spawn"), None
    if (scan["provider_errors"] or scan["retry_failed"]) and AUTH_PATTERN.search(" ".join(scan["error_messages"])):
        return "auth", None
    if scan["retry_failed"]:
        return "retry_exhausted", None
    if scan["provider_errors"]:
        return "provider_error", None
    if run["timed_out"]:
        if pending_request(scan["last_event"]) and idle_seconds is not None and idle_seconds > STALL_SECONDS:
            return "provider_stall", None
        return None, "timeout"
    if not scan["settled"] or scan["settled_aborted"] or scan["aborted_messages"]:
        return ("auth" if not scan["settled"] and AUTH_PATTERN.search(stderr) else "no_settle"), None
    if scan["length_stops"]:
        return None, "length"
    if not all(passed for task in decision_results.values() for passed in task.values()):
        return None, "heldout_failed"
    return None, None


def _failed_launch(message):
    return {
        "exit_code": None,
        "timed_out": False,
        "seconds": 0,
        "settled": False,
        "aborted": False,
        "assistant_errors": 1,
        "tool_calls": 0,
        "tool_errors": 0,
        "assistant_turns": 0,
        "retries": 0,
        "total_tokens": 0,
        "reported_cost_usd": None,
        "model_ids": [],
        "thinking_levels": [],
        "assistant_messages": 0,
        "assistant_messages_missing_runtime": 0,
        "invalid_json_lines": 0,
        "stderr_tail": message,
    }


def _parse_e3_unsettled(text):
    mapping = {}
    for item in filter(None, (part.strip() for part in (text or "").split(","))):
        task_id, sep, decision_id = item.partition(":")
        if not sep or not task_id or not decision_id or task_id in mapping:
            raise ValueError("--e3-unsettled must be 'task:decision,task:decision' with each task once")
        mapping[task_id] = decision_id
    return mapping


def _parse_ints(text, flag):
    try:
        return [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise ValueError(f"{flag} must be comma-separated integers") from None


def _e3_breadth_trials(args):
    breadths = _parse_ints(args.e3_breadths, "--e3-breadths")
    counts = _parse_ints(args.e3_trials or "", "--e3-trials")
    if len(counts) == 1:
        counts = counts * len(breadths)
    if len(counts) != len(breadths) or len(set(breadths)) != len(breadths):
        raise ValueError("--e3-trials needs one count, or one per distinct --e3-breadths value")
    return dict(zip(breadths, counts))


def load_confine(spec):
    """Import a confinement wrapper given as 'package.module:function', or return None."""
    if not spec:
        return None
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise ValueError("--confine must be 'package.module:function'")
    try:
        return getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"--confine {spec}: {exc}") from exc


def _run_bank_trial(root, trial, attempt, tasks_by_id, design, args, confine):
    tasks = [tasks_by_id[task_id] for task_id in trial["task_ids"]]
    name = f"{trial['trial_id']}-a{attempt}"
    workspace = root / "workspaces" / name
    log_dir = root / "logs" / name
    workspace.mkdir(parents=True)
    for task in tasks:
        (workspace / task["module"]).write_text(task["starter"], encoding="utf-8")
    prompt, checks, filler_counts = trial_files(trial, tasks_by_id)
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != trial["prompt_sha256"] or (
        hashlib.sha256(checks.encode("utf-8")).hexdigest() != trial["visible_checks_sha256"]
    ):
        raise RuntimeError(f"{trial['trial_id']}: regenerated prompt or checks differ from design.json")
    (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
    (workspace / "visible_checks.py").write_text(checks, encoding="utf-8")
    expected_prompt = (workspace / "prompt.txt").read_bytes()
    expected_checks = (workspace / "visible_checks.py").read_bytes()

    print(f"START {name}", flush=True)
    failure_context = {}
    try:
        run = execute_pi(workspace, log_dir, prompt, args.model, args.thinking, args.pi_binary, args.timeout, confine)
    except ConfinementSetupError as exc:
        run = _failed_launch(str(exc))
        failure_context["sandbox_error"] = str(exc)
    except OSError as exc:
        run = _failed_launch(str(exc))
        failure_context["spawn_error"] = str(exc)
    events_path = log_dir / "pi-events.jsonl"
    idle_seconds = None
    if run["timed_out"]:
        last_write = events_path.stat().st_mtime if events_path.exists() and events_path.stat().st_size else None
        idle_seconds = round(time.time() - last_write, 3) if last_write is not None else run["seconds"]
    scan = scan_events(events_path)

    selected = [{"file": task["module"]} for task in tasks]
    scope_ok = safe_trial_scope(workspace, selected, expected_checks, expected_prompt)
    try:
        spec_intact = (workspace / "prompt.txt").read_bytes() == expected_prompt and (
            workspace / "visible_checks.py"
        ).read_bytes() == expected_checks
    except OSError:
        spec_intact = False
    decision_results, details = grade_workspace(workspace, tasks)
    (log_dir / "grading.json").write_text(json.dumps(details, indent=2, sort_keys=True), encoding="utf-8")
    infrastructure, agent = classify_failure({**run, **failure_context}, scan, decision_results, idle_seconds)
    heldout_passed = all(passed for task in decision_results.values() for passed in task.values())
    complete = bool(
        run["exit_code"] == 0
        and run["settled"]
        and not run["aborted"]
        and not run["timed_out"]
        and run["assistant_errors"] == 0
    )
    runtime_ok = runtime_matches_request(run, args.model, args.thinking)
    record = {
        "trial_id": trial["trial_id"],
        "experiment": design["experiment"],
        "design_id": design["design_id"],
        "seed": trial["seed"],
        "task_ids": trial["task_ids"],
        "breadth": trial["breadth"],
        "channels": trial["channels"],
        "filler": trial["filler"],
        "arm": trial["arm"],
        "decision_results": decision_results,
        "labels": {task["id"]: {d["id"]: d["label"] for d in task["decisions"]} for task in tasks},
        "task_bank_sha256": design["task_bank_sha256"],
        "infrastructure_failure": infrastructure,
        "agent_failure": agent,
        "confinement_signals": [],
        "attempt": attempt,
        "accepted": heldout_passed and complete and scope_ok and runtime_ok and infrastructure is None,
        "held_out_tests_passed": heldout_passed,
        "task_scope_intact": scope_ok,
        "spec_files_intact": spec_intact,
        "agent_completed": complete,
        "runtime_matches_request": runtime_ok,
        "model_requested": args.model,
        "thinking_requested": args.thinking,
        # Per-decision grading has one exit code per check; see heldout_checks.
        "grader_exit_code": None,
        "depth": None,
        "verification": None,
        "repetition": None,
        "filler_sentences": filler_counts,
        "prompt_sha256": trial["prompt_sha256"],
        "visible_checks_sha256": trial["visible_checks_sha256"],
        "heldout_checks": {
            task_id: {
                decision_id: {k: outcome[k] for k in ("exit_code", "timed_out", "seconds")}
                for decision_id, outcome in outcomes.items()
            }
            for task_id, outcomes in details.items()
        },
        "stall_idle_seconds": idle_seconds,
        "malformed_events": scan["malformed"][:20],
        **run,
    }
    with (root / "trials.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    print(
        f"DONE {name} accepted={record['accepted']} infrastructure={infrastructure} agent={agent}",
        flush=True,
    )
    return record


def _bank_summary(root, design, records, stop_reason):
    final = {}
    for record in records:
        final[record["trial_id"]] = record
    first = [r for r in records if r["attempt"] == 1]
    infrastructure = {}
    for record in first:
        if record["infrastructure_failure"]:
            key = record["infrastructure_failure"]
            infrastructure[key] = infrastructure.get(key, 0) + 1
    agent = {}
    for record in final.values():
        if record["agent_failure"]:
            agent[record["agent_failure"]] = agent.get(record["agent_failure"], 0) + 1
    return {
        "run_id": root.name,
        "backend": "pi-local",
        "is_real_agent_evidence": True,
        "experiment": design["experiment"],
        "design_id": design["design_id"],
        "task_bank_sha256": design["task_bank_sha256"],
        "planned_trials": len(design["trials"]),
        "completed_trials": len(final),
        "attempts": len(records),
        "infrastructure_failures_first_attempt": dict(sorted(infrastructure.items())),
        "infrastructure_failure_rate_first_attempt": (
            round(sum(infrastructure.values()) / len(first), 4) if first else None
        ),
        "infrastructure_reruns": sum(1 for r in records if r["attempt"] == 2),
        "infrastructure_failures_after_rerun": sum(1 for r in final.values() if r["infrastructure_failure"]),
        "agent_failures_final_attempt": dict(sorted(agent.items())),
        "accepted_final_attempt": sum(1 for r in final.values() if r["accepted"]),
        "runtime_mismatches": sum(1 for r in records if not r["runtime_matches_request"]),
        "stop_reason": stop_reason,
    }


def run_task_bank(args, confine=None):
    """Generate a seeded design over a validated task bank, then run, grade and classify every trial.

    `confine(command, workspace) -> (command, env)` wraps the Pi command, for
    example in a confinement profile; an exception from it is recorded as the
    infrastructure failure "sandbox".
    """
    design_only = getattr(args, "design_only", False)
    if not design_only:
        if shutil.which(args.pi_binary) is None:
            raise RuntimeError(f"Pi CLI '{args.pi_binary}' is not installed or not on PATH.")
        if args.timeout < 1:
            raise ValueError("--timeout must be positive")
    validate_model(args.model)
    if args.thinking not in THINKING_LEVELS:
        raise ValueError("--thinking must be one of " + ", ".join(THINKING_LEVELS))
    if args.experiment is None or args.design_seed is None:
        raise ValueError("--task-bank needs --experiment and --design-seed")

    bank = Path(args.task_bank).resolve()
    report = validate_bank(bank)
    if not report["valid"]:
        raise ValueError("task bank failed mechanical validation:\n" + format_report(report))
    tasks = load_bank(bank)
    tasks_by_id = {task["id"]: task for task in tasks}
    design = generate_design(
        tasks,
        args.experiment,
        args.design_seed,
        report["task_bank_sha256"],
        trials_per_task=args.trials_per_task,
        e3_unsettled=_parse_e3_unsettled(args.e3_unsettled) if args.experiment == "E3" else None,
        e3_breadth_trials=_e3_breadth_trials(args) if args.experiment == "E3" else None,
        t7_task=args.t7_task,
        order_seed=args.order_seed,
    )

    label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = args.out.resolve() / f"run-{label}-{uuid.uuid4().hex[:6]}"
    root.mkdir(parents=True, exist_ok=False)
    # The full assignment, with every seed, is fixed on disk before any trial runs.
    (root / "design.json").write_text(canonical_json(design), encoding="utf-8")
    (root / "bank-validation.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if design_only:
        for trial in design["trials"]:
            prompt, checks, _ = trial_files(trial, tasks_by_id)
            folder = root / "prompts" / trial["trial_id"]
            folder.mkdir(parents=True)
            (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
            (folder / "visible_checks.py").write_text(checks, encoding="utf-8")
        print("DESIGN " + str(root), flush=True)
        return root

    (root / "workspaces").mkdir()
    (root / "logs").mkdir()
    confinement = getattr(confine, "__qualname__", repr(confine)) if confine is not None else None
    metadata = {
        "schema_version": "0.3.0",
        "backend": "pi-local",
        "mode": "task-bank",
        "model_requested": args.model,
        "thinking_requested": args.thinking,
        "pi_version": pi_version(args.pi_binary),
        "python_version": sys.version.split()[0],
        "git_commit": git_head(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": design["experiment"],
        "design_id": design["design_id"],
        "design_seed": design["master_seed"],
        "order_seed": design["order_seed"],
        "task_bank": str(bank),
        "task_bank_sha256": design["task_bank_sha256"],
        "timeout_seconds": args.timeout,
        "confinement": confinement,
        "execution_environment": (
            "local process using caller's OS permissions; NOT sandboxed"
            if confine is None
            else "Pi command wrapped by confinement hook " + str(confinement)
        ),
        "model_calls_issued": True,
        "human_interventions_allowed": 0,
        "experiment_limitations": [
            "Held-out checks are not OS-level hidden unless a confinement hook is applied.",
            "confinement_signals are not yet recorded (empty lists).",
            "Token cost is provider-reported and may be unavailable or incomplete.",
        ],
    }
    (root / "run-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    trials_by_id = {trial["trial_id"]: trial for trial in design["trials"]}
    records = []
    stop_reason = None
    for trial_id in design["execution_order"]:
        if task_bank_sha256(bank) != design["task_bank_sha256"]:
            stop_reason = "task bank changed during the run"
            break
        record = _run_bank_trial(root, trials_by_id[trial_id], 1, tasks_by_id, design, args, confine)
        records.append(record)
        if record["infrastructure_failure"] and record["spec_files_intact"]:
            # One rerun with the same seed; both attempts are kept.
            record = _run_bank_trial(root, trials_by_id[trial_id], 2, tasks_by_id, design, args, confine)
            records.append(record)
        if not record["spec_files_intact"]:
            stop_reason = f"{trial_id} changed prompt.txt or visible_checks.py (LOOP_SPEC §8.3 safety stop)"
            break

    summary = _bank_summary(root, design, records, stop_reason)
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if stop_reason:
        print("STOPPED " + stop_reason, flush=True)
    print("RESULTS " + str(root), flush=True)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        required=True,
        help="Exact Pi provider/model-id, e.g. openai-codex/gpt-5.6-sol",
    )
    parser.add_argument(
        "--thinking",
        required=True,
        choices=THINKING_LEVELS,
        help="Pi thinking level, pinned so Pi settings cannot change it",
    )
    parser.add_argument("--pi-binary", default="pi", help="Path to Pi CLI")
    parser.add_argument("--breadths", default="1,2")
    parser.add_argument("--depths", default="shallow,deep")
    parser.add_argument("--verifications", default="basic,rich")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per trial")
    parser.add_argument("--out", type=Path, default=Path("autonomy_lab/local-results"))
    bank = parser.add_argument_group("task-bank mode (LOOP_SPEC §4); the 2x2x2 options above are then ignored")
    bank.add_argument("--task-bank", type=Path, help="Task bank directory; selects task-bank mode")
    bank.add_argument("--experiment", choices=EXPERIMENTS)
    bank.add_argument("--design-seed", type=int, help="Recorded master seed for the design")
    bank.add_argument("--order-seed", type=int, help="Execution-order seed (default: derived from the design seed)")
    bank.add_argument("--trials-per-task", type=int, help="E1: multiple of 8; E2: even (half plain, half assume)")
    bank.add_argument("--e3-unsettled", help="E3: 'task:decision,...', one unsettled arbitrary decision per task")
    bank.add_argument("--e3-breadths", default="1,2,4")
    bank.add_argument("--e3-trials", help="E3: trials per breadth, one value or one per breadth")
    bank.add_argument("--t7-task", help="T7: task id (default: chosen from the design seed)")
    bank.add_argument("--confine", help="Confinement hook 'package.module:function' mapping (command, workspace)")
    bank.add_argument("--design-only", action="store_true", help="Write design.json and prompts, run nothing")
    args = parser.parse_args()
    try:
        if args.task_bank is None:
            if args.experiment is not None:
                raise ValueError("--experiment needs --task-bank")
            run_experiment(args)
            return
        root = run_task_bank(args, confine=load_confine(args.confine))
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        stopped = json.loads((root / "summary.json").read_text(encoding="utf-8")).get("stop_reason")
    except (OSError, ValueError):
        stopped = None  # --design-only writes no summary
    if stopped:
        sys.exit(3)


if __name__ == "__main__":
    main()
