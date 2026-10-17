"""Run controlled Autonomy Lab experiments through the user's installed Pi CLI.

Requires an explicit --model provider/id and --thinking level, so that Pi's
ambient default model or settings cannot silently change the treatment.
A local Pi process is NOT a security sandbox: see SESSION_HANDOFF.md.
"""

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from autonomy_lab.runner import TASKS, build_prompt, check_candidate, prepare_workspace

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
                message = event.get("message") or {}
                if message.get("role") != "assistant":
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
                usage = message.get("usage") or {}
                metrics["total_tokens"] += int(usage.get("totalTokens") or 0)
                total_cost = (usage.get("cost") or {}).get("total")
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
        pi_binary, "--mode", "json", "--no-session",
        "--no-extensions", "--no-skills", "--no-prompt-templates",
        "--no-context-files", "--no-mcp", "--no-approve",
        "--tools", "read,bash,edit,write",
        "--model", model, "--thinking", thinking,
        "--", prompt,
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


def execute_pi(workspace, log_dir, prompt, model, thinking, pi_binary, timeout):
    # Harness logs live outside the agent's working directory so they do not
    # appear in its file listings. This prevents accidental exposure only: an
    # agent that deliberately reads ../../logs can still find them.
    log_dir.mkdir(parents=True, exist_ok=True)
    events_path = log_dir / "pi-events.jsonl"
    stderr_path = log_dir / "pi-stderr.log"
    command = pi_command(model, thinking, prompt, pi_binary=pi_binary)
    started = time.monotonic()
    expired = False
    with events_path.open("w", encoding="utf-8") as events, stderr_path.open(
        "w", encoding="utf-8"
    ) as errors:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=events,
            stderr=errors,
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
    result = subprocess.run(
        [pi_binary, "--version"], text=True, capture_output=True, timeout=15
    )
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def git_head():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
            stderr=subprocess.DEVNULL, timeout=5
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
    (root / "run-metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

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
                            workspace, log_dir, prompt, args.model, args.thinking,
                            args.pi_binary, args.timeout,
                        )
                    except OSError as exc:
                        run = {
                            "exit_code": None, "timed_out": False, "seconds": 0,
                            "settled": False, "aborted": False,
                            "assistant_errors": 1, "tool_calls": 0, "tool_errors": 0,
                            "assistant_turns": 0, "retries": 0,
                            "total_tokens": 0, "reported_cost_usd": None,
                            "model_ids": [], "thinking_levels": [],
                            "assistant_messages": 0,
                            "assistant_messages_missing_runtime": 0,
                            "invalid_json_lines": 0,
                            "stderr_tail": str(exc),
                        }
                    scope_ok = safe_trial_scope(
                        workspace, selected, expected_checks, expected_prompt
                    )
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
                        "trial_id": trial_id, "breadth": breadth, "depth": depth,
                        "verification": verification, "repetition": repetition,
                        "accepted": accepted, "held_out_tests_passed": passed,
                        "task_scope_intact": scope_ok, "agent_completed": complete,
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
                        "DONE " + trial_id + " accepted=" + str(accepted)
                        + " turns=" + str(run["assistant_turns"]),
                        flush=True,
                    )

    summaries = []
    for breadth in breadths:
        for depth in depths:
            for verification in verifications:
                rows = [
                    t for t in results
                    if t["breadth"] == breadth
                    and t["depth"] == depth
                    and t["verification"] == verification
                ]
                summaries.append({
                    "breadth": breadth, "depth": depth,
                    "verification": verification, "trials": len(rows),
                    "accepted": sum(int(t["accepted"]) for t in rows),
                    "accepted_pct": round(
                        100 * sum(int(t["accepted"]) for t in rows) / len(rows), 2
                    ),
                    "runtime_mismatches": sum(
                        int(not t["runtime_matches_request"]) for t in rows
                    ),
                    "mean_agent_seconds": round(
                        sum(t["seconds"] for t in rows) / len(rows), 2
                    ),
                    "mean_agent_turns": round(
                        sum(t["assistant_turns"] for t in rows) / len(rows), 2
                    ),
                })
    summary = {
        "run_id": root.name, "backend": "pi-local",
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", required=True,
        help="Exact Pi provider/model-id, e.g. openai-codex/gpt-5.6-sol",
    )
    parser.add_argument(
        "--thinking", required=True, choices=THINKING_LEVELS,
        help="Pi thinking level, pinned so Pi settings cannot change it",
    )
    parser.add_argument("--pi-binary", default="pi", help="Path to Pi CLI")
    parser.add_argument("--breadths", default="1,2")
    parser.add_argument("--depths", default="shallow,deep")
    parser.add_argument("--verifications", default="basic,rich")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per trial")
    parser.add_argument(
        "--out", type=Path, default=Path("autonomy_lab/local-results")
    )
    args = parser.parse_args()
    try:
        run_experiment(args)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
