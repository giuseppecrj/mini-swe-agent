"""Controlled, reproducible plan-depth x verifiability x breadth experiment.

Run without model credentials:
  python -m autonomy_lab.runner --backend mock --out autonomy_lab_results

Run with mini-SWE-agent inside an isolated Docker container:
  docker build -t autonomy-lab-agent -f autonomy_lab/Dockerfile .
  OPENAI_API_KEY=... python -m autonomy_lab.runner --backend docker-mini \
      --model openai/gpt-5 --out autonomy_lab_results

This is research scaffolding. Mock runs are NOT model-performance evidence.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

TASKS = [
    {
        "id": "ordered-merge",
        "file": "merge_ids.py",
        "starter": """def merge_ids(left, right):
    return sorted(set(left + right))
""",
        "brief": "Fix merge_ids(left, right) so it correctly merges two sequences without duplicate IDs.",
        "contract": (
            "merge_ids(left, right) returns a NEW list containing each ID exactly once, "
            "in first-appearance order across left then right. Inputs must not be mutated; "
            "empty sequences are valid. IDs are hashable and may be strings or integers."
        ),
        "basic_tests": """from merge_ids import merge_ids
assert merge_ids(["a"], ["a", "b"]) == ["a", "b"]
""",
        "rich_tests": """from merge_ids import merge_ids
assert merge_ids(["z", "a"], ["a", "m"]) == ["z", "a", "m"]
assert merge_ids([], [3, 2, 3]) == [3, 2]
left = ["b", "a"]
assert merge_ids(left, ["b"]) == ["b", "a"]
assert left == ["b", "a"]
""",
        "hidden_tests": """from merge_ids import merge_ids
assert merge_ids(["b", "a"], ["a", "c"]) == ["b", "a", "c"]
assert merge_ids([], [3, 2, 3]) == [3, 2]
assert merge_ids(["k", "k"], ["z"]) == ["k", "z"]
a, b = [2, 1], [1, 3]
assert merge_ids(a, b) == [2, 1, 3]
assert a == [2, 1] and b == [1, 3]
""",
    },
    {
        "id": "atomic-ledger",
        "file": "ledger.py",
        "starter": """def transfer(balances, sender, recipient, amount):
    balances[sender] -= amount
    balances[recipient] = balances.get(recipient, 0) + amount
    return balances
""",
        "brief": "Fix transfer() edge cases and prevent incorrect balances when moving funds.",
        "contract": (
            "transfer(balances, sender, recipient, amount) returns a NEW balances dict, "
            "and never changes the input, even on error. amount is a strictly positive "
            "integer (bool and float disallowed). sender and recipient are different "
            "nonempty strings. Raise ValueError for invalid amount, accounts, or "
            "insufficient funds; raise KeyError if sender is missing. Recipient may "
            "be new and starts at 0. Successful transfers preserve the total balance."
        ),
        "basic_tests": """from ledger import transfer
a = transfer({"alice": 8, "bob": 1}, "alice", "bob", 2)
assert a["alice"] == 6 and a["bob"] == 3
""",
        "rich_tests": """from ledger import transfer
before = {"alice": 8, "bob": 1}
after = transfer(before, "alice", "bob", 2)
assert before == {"alice": 8, "bob": 1}
assert after == {"alice": 6, "bob": 3}
for bad in (-1, 0, 2.5, True):
    try:
        transfer({"alice": 8}, "alice", "bob", bad)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid amounts must fail")
""",
        "hidden_tests": """from ledger import transfer
original = {"a": 5, "b": 4}
result = transfer(original, "a", "b", 3)
assert original == {"a": 5, "b": 4}
assert result == {"a": 2, "b": 7}
assert sum(result.values()) == sum(original.values())
assert transfer({"a": 3}, "a", "new", 2) == {"a": 1, "new": 2}
for value in (-2, 0, 2.0, True):
    try:
        transfer({"a": 3}, "a", "b", value)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid amount accepted")
for sender, recipient, amount in (("a", "a", 1), ("a", "b", 4), ("", "b", 1)):
    before = {"a": 3}
    try:
        transfer(before, sender, recipient, amount)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid transfer accepted")
    assert before == {"a": 3}
try:
    transfer({"a": 1}, "missing", "a", 1)
except KeyError:
    pass
else:
    raise AssertionError("missing sender accepted")
""",
    },
]


def build_prompt(selected, depth, verification):
    sections = [
        "Implement the following independent Python changes in the current directory.",
        "Edit only the task modules; do not change visible_checks.py.",
        "Do not inspect directories outside the current workspace.",
        "You must run 'python visible_checks.py' and address failures before finishing.",
        "There is no interactive human available. Stop if you cannot proceed safely.",
    ]
    for task in selected:
        sections.extend(
            [
                "",
                "Task: " + task["id"] + " (" + task["file"] + ")",
                task["contract"] if depth == "deep" else task["brief"],
            ]
        )
    if verification == "rich":
        sections.append("The included checks cover boundary behavior and must pass.")
    else:
        sections.append("The included checks are only a basic smoke test.")
    return "\n".join(sections)


def prepare_workspace(path, selected, verification):
    path.mkdir(parents=True, exist_ok=True)
    for task in selected:
        (path / task["file"]).write_text(task["starter"], encoding="utf-8")
    checks = "\n".join(
        task["rich_tests"] if verification == "rich" else task["basic_tests"]
        for task in selected
    )
    (path / "visible_checks.py").write_text(checks + "\nprint('visible checks passed')\n", encoding="utf-8")


def run_command(command, cwd, timeout, env=None):
    start = time.monotonic()
    try:
        p = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True,
            timeout=timeout, check=False
        )
        return {
            "exit_code": p.returncode,
            "timed_out": False,
            "seconds": round(time.monotonic() - start, 3),
            "log_tail": (p.stdout + "\n" + p.stderr)[-1500:],
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": None, "timed_out": True,
            "seconds": round(time.monotonic() - start, 3),
            "log_tail": "execution timed out",
        }


def docker_agent_command(workspace, prompt, model, cost_limit):
    mount = "type=bind,src=" + str(workspace.resolve()) + ",dst=/work"
    return [
        "docker", "run", "--rm", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=256m",
        "--mount", mount, "--workdir", "/work",
        "--user", str(os.getuid()) + ":" + str(os.getgid()),
        "--network", "bridge", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "192",
        "--memory", "3g", "--cpus", "2",
        "--env", "OPENAI_API_KEY", "--env", "HOME=/tmp",
        "autonomy-lab-agent", "--model", model, "--task", prompt,
        "--yolo", "--exit-immediately", "--cost-limit", str(cost_limit),
        "--output", "/work/trajectory.json",
    ]


def run_agent(backend, workspace, prompt, model, timeout, cost_limit):
    if backend == "mock":
        return {
            "exit_code": 0, "timed_out": False,
            "seconds": 0, "log_tail": "Mock backend: no source changed; wiring test only",
        }
    command = docker_agent_command(workspace, prompt, model, cost_limit)
    return run_command(command, workspace, timeout)


def check_candidate(workspace, selected, backend):
    # Acceptance checks are NEVER copied into the agent's mounted /work.
    checks = "\n".join(task["hidden_tests"] for task in selected)
    if backend == "docker-mini":
        command = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "256m",
            "--mount", "type=bind,src=" + str(workspace.resolve()) + ",dst=/work,readonly",
            "--workdir", "/work", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONPATH=/work", "python:3.11-slim",
            "python", "-c", checks,
        ]
    else:
        command = [sys.executable, "-c", checks]
    outcome = run_command(command, workspace, 12)
    return outcome["exit_code"] == 0, outcome


def summarize(trials, backend):
    grouped = {}
    for trial in trials:
        key = (trial["breadth"], trial["depth"], trial["verification"])
        grouped.setdefault(key, []).append(trial)
    summaries = []
    for (breadth, depth, verification), rows in sorted(grouped.items()):
        summaries.append({
            "breadth": breadth,
            "depth": depth,
            "verification": verification,
            "trials": len(rows),
            "accepted": sum(int(x["accepted"]) for x in rows),
            "accepted_pct": round(
                100 * sum(int(x["accepted"]) for x in rows) / len(rows), 2
            ),
            "mean_agent_seconds": round(
                statistics.mean(x["agent_seconds"] for x in rows), 3
            ),
        })
    return {
        "backend": backend,
        "is_real_agent_evidence": backend == "docker-mini",
        "human_interventions_per_trial": 0,
        "note": (
            "A mock run validates only the infrastructure, not the research hypothesis."
            if backend == "mock"
            else "Human intervention is disallowed, not experimentally varied."
        ),
        "summary": summaries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["mock", "docker-mini"], default="mock")
    parser.add_argument("--model", help="LiteLLM model id, e.g. openai/gpt-5")
    parser.add_argument("--breadths", default="1,2", help="Comma-separated task counts")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--cost-limit", type=float, default=1.0)
    parser.add_argument("--out", type=Path, default=Path("autonomy_lab_results"))
    args = parser.parse_args()
    breadths = [int(x) for x in args.breadths.split(",")]
    if args.repetitions < 1 or any(n < 1 or n > len(TASKS) for n in breadths):
        parser.error("repetitions must be >=1 and breadths must be 1 or 2")
    if args.backend == "docker-mini" and not (args.model and os.getenv("OPENAI_API_KEY")):
        parser.error("docker-mini requires --model and OPENAI_API_KEY")
    args.out.mkdir(parents=True, exist_ok=True)

    trials = []
    for breadth in breadths:
        for depth in ("shallow", "deep"):
            for verification in ("basic", "rich"):
                for repetition in range(1, args.repetitions + 1):
                    selected = TASKS[:breadth]
                    trial_id = f"b{breadth}-{depth}-{verification}-r{repetition}"
                    workspace = args.out / "workspaces" / trial_id
                    prepare_workspace(workspace, selected, verification)
                    prompt = build_prompt(selected, depth, verification)
                    (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
                    result = run_agent(
                        args.backend, workspace, prompt, args.model,
                        args.timeout, args.cost_limit
                    )
                    accepted, grade = check_candidate(workspace, selected, args.backend)
                    trial = {
                        "id": trial_id, "breadth": breadth, "depth": depth,
                        "verification": verification, "repetition": repetition,
                        "backend": args.backend, "model": args.model,
                        "accepted": accepted, "agent_exit_code": result["exit_code"],
                        "agent_timed_out": result["timed_out"],
                        "agent_seconds": result["seconds"],
                        "grader_exit_code": grade["exit_code"],
                        "grader_timed_out": grade["timed_out"],
                        "agent_log_tail": result["log_tail"],
                        "grader_log_tail": grade["log_tail"],
                    }
                    trials.append(trial)
                    print(json.dumps({
                        "trial": trial_id, "accepted": accepted,
                        "agent_exit": result["exit_code"]
                    }), flush=True)

    (args.out / "trials.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in trials), encoding="utf-8"
    )
    summary = summarize(trials, args.backend)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (args.out / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary["summary"][0].keys()))
        writer.writeheader()
        writer.writerows(summary["summary"])
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
