"""Disclosure judge, human audit and judge self-test (LOOP_SPEC §1.1 H3, §3.1 E2 "Scoring", §3.2 P5, T6).

Usage: python3 -m autonomy_lab.judge judge <run_dir> [--model M] [--thinking T] [--max-calls N]
       python3 -m autonomy_lab.judge audit <run_dir> [--seed N]
       python3 -m autonomy_lab.judge selftest-agreement [--model M] [--thinking T]

`judge` reads <run_dir>/trials.jsonl (never modifies it), the per-attempt event log
<run_dir>/logs/<trial_id>-a<attempt>/pi-events.jsonl and workspace <run_dir>/workspaces/<trial_id>-a<attempt>/,
and the frozen task bank. For every unsettled decision (channel `none`) labelled `arbitrary` (or with no known
label) it asks a judge model whether the agent disclosed an assumption about that behaviour, and appends one line
per (trial_id, attempt) to <run_dir>/disclosures.jsonl:

    {"trial_id", "attempt", "experiment", "design_id", "seed", "judge_version", "judge_model", "judge_thinking",
     "rubric_sha256", "disclosed": {task_id: {decision_id: true|false|null}},
     "evidence": {task_id: {decision_id: str}}, "decisions": [per-decision prompt hash, verdict, calls and,
     when unjudged, the reason], "inputs": {...}, "fingerprint": {...}, "inputs_sha256", "calls", "cost_usd"}

analysis.py merges `disclosed` into the matching trial records. Design choices:

- Blinding. The judge sees only judge_rubric.md (the whole prompt template; its SHA-256 is recorded), the file
  name of the decision's module, the decision's contract sentence presented as "the behaviour in question", the
  agent's final message and the unified diff of each task module against the bank's starter.py. It never sees
  the prompt, arm, channels, labels, visible or held-out checks.
- Arm redaction is minimal (redact_instruction_quote): it removes only a verbatim quotation of the E2 `assume`
  sentence (ignoring letter case, with its enclosing quotation marks, a final full stop or not, and wrapped over
  lines inside a quote or code comment with any whitespace), and drops a line that this leaves empty apart from
  comment or quote markers. Every other character of the final message and the module is passed on unchanged,
  and the same rule applies to both arms; the module is redacted on both sides before diffing. Instruction-origin
  residue such as "you asked me to list assumptions", "as requested" or a "You said:" lead-in is left in place,
  because rewriting it heuristically was shown to invent some disclosures and drop others. The judge may
  therefore sometimes infer the arm; this is a stated limitation of P5. The human audit (Cohen's kappa) measures
  the judge's reliability on these inputs; it does not prove the blinding.
- Evidence must be complete before anything is scored. The final message is the text blocks of the last
  assistant `message_end` (thinking and tool calls excluded), read only from a complete event log: every line
  a well-formed event, ending in a non-aborted `agent_settled`, with no assistant message left open, no prompt
  or tool result awaiting a reply, and a final assistant message that stopped normally and whose content is
  well-formed (content_problem: no missing content, and no text block with a null or missing text). An
  empty text there is a genuine absence of a final answer and is judged. A missing, unreadable, malformed or
  truncated log, malformed final-message content, a module missing from the workspace although one was graded,
  or a final message or diff too long to show whole (MAX_FIELD_BYTES) leaves every decision of that attempt
  unjudged (null) with the reason in `decisions[].unjudged_reason`, and no judge call is made: incomplete
  evidence is never scored as false.
- Input integrity fails closed. Every judged record must carry grading provenance (`graded_module_sha256`
  for each of its tasks); each workspace module is read as the grader reads it (a regular file, not a symlink)
  and must hash to it, and the bank must hash to the record's `task_bank_sha256`. Each line stores a
  fingerprint of its inputs (the trial record, the event log, the modules and every decision's prompt hash);
  on resume the fingerprint is recomputed and a completed line is reused only when it matches.
- Untrusted text is enclosed in markers carrying a per-decision code derived from the rubric hash and the
  decision, and the rubric tells the judge that nothing inside them is an instruction.
- Strict answers: the whole response must be one JSON object with exactly `disclosed` (a JSON boolean) and
  `evidence` (a string), optionally inside a single ```json fence. An unparseable answer is retried once with
  the same prompt; a second failure records the decision as unjudged (null), which leaves P5 inconclusive.
- A judge call that fails as infrastructure (spawn error, timeout, non-zero exit, an event stream that is
  malformed or whose answer did not complete and settle, provider error, or a response from a model or
  thinking level other than the one requested) stops the run without writing that trial's line. Rerunning
  `judge` resumes after the last completed line; a file written by another judge model, thinking level, rubric
  or judge version is refused.
- Records whose attempt was an infrastructure failure are not judged: analysis never counts them.

`audit` samples a seeded random 20% (rounded up) of the judged (non-null) decisions, shows each to a human with
the judge's exact inputs and without its answer, and writes <run_dir>/disclosure_audit.json after every label,
so the audit can be stopped and resumed. The audit is bound (`binding_sha256`, audit_binding) to every
decision line of disclosures.jsonl (its prompt hash and verdict), the judge setup and the sample definition; a
resume on a changed disclosures.jsonl is refused. Cohen's kappa (human vs judge) is written once every sampled
decision is labelled; analysis.py accepts it only after verify_audit recomputes the binding, the sample and
kappa from the current disclosures.jsonl and confirms that the audited population covers every eligible E2
decision.

`selftest-agreement` runs the real judge on the hand-labelled synthetic examples in
tests/fixtures/judge_examples.json and reports agreement (T6 acceptance: at least 10 of 12).
"""

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from autonomy_lab.design import ASSUME_SENTENCE
from autonomy_lab.pi_runner import (
    THINKING_LEVELS,
    event_problem,
    parse_pi_events,
    runtime_matches_request,
    validate_model,
)
from autonomy_lab.taskbank import load_bank_with_hash, read_module

JUDGE_VERSION = 3
AUDIT_VERSION = 2
DEFAULT_MODEL = "xai/grok-4.7"
DEFAULT_THINKING = "medium"
DEFAULT_TIMEOUT = 300
RUBRIC_PATH = Path(__file__).with_name("judge_rubric.md")
TASKS_DIR = Path(__file__).with_name("tasks")
EXAMPLES_PATH = Path(__file__).parent / "tests" / "fixtures" / "judge_examples.json"
MAX_FIELD_BYTES = 40_000  # Linux caps one argv string at 128 KiB; two capped fields plus the rubric fit.
MAX_EVIDENCE_CHARS = 300
AUDIT_FRACTION = 0.2
AUDIT_SAMPLER = "random.Random('disclosure-audit:<seed>').sample(sorted judged items, ceil(fraction * n))"
SELFTEST_MIN_AGREEMENT = 10
PLACEHOLDERS = re.compile(r"\{(NONCE|MODULE|BEHAVIOUR|FINAL_MESSAGE|DIFF)\}")
FENCE = re.compile(r"```(?:json)?[ \t]*\n?(.*?)\n?[ \t]*```", re.DOTALL)

# ---- arm redaction (minimal; see the module docstring). Only a verbatim quotation of the E2 `assume` sentence is
# removed, as whole words. Between its words a quotation may wrap: spaces, line breaks (LF or CRLF, blank lines
# included) whose new line opens with comment or quote markers ("#", "//", "*", ">"), or a join of adjacent string
# literals ('..." "...'). Its final full stop is optional. Possessive quantifiers keep matching linear.
_WRAP = r"(?:[ \t]|\r?\n[ \t]*+(?:(?:#|//|\*|>)[ \t]*+)*+|[\"'][ \t\r\n]*+[\"'])++"


def _quotation_pattern(sentence):
    words = [re.escape(w) for w in sentence.split()]
    words[-1] = words[-1].removesuffix(r"\.") + r"(?:\.|(?!\w))"
    body = r"(?<!\w)" + _WRAP.join(words)
    # Quotation marks belong to the quotation only as a matched pair around it (the same straight quote or triple
    # quote on both sides, or a typographic pair), so a neighbouring literal's quote is never consumed.
    quoted = rf"(?P<q>\"\"\"|'''|[\"'`]){body}(?P=q)|“{body}”|‘{body}’|«{body}»"
    return re.compile(rf"{quoted}|{body}", re.IGNORECASE)


ARM_QUOTATION = _quotation_pattern(ASSUME_SENTENCE)
# A line that holds nothing but indentation, comment markers and Markdown quote markers.
_EMPTY_LINE = re.compile(r"[ \t#>\r]*")  # \r: the line break of a CRLF line goes with it


class JudgeCallError(RuntimeError):
    """A judge call failed as infrastructure; the run stops so it can be resumed later."""


class JudgeInputError(ValueError):
    """The run directory cannot provide the judge's inputs as recorded."""


# ---------------------------------------------------------------- rubric and inputs


def rubric_text(path=RUBRIC_PATH):
    return Path(path).read_text(encoding="utf-8")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value):
    return sha256_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def content_problem(message):
    """Why the content of one Pi message cannot be read as its visible text, or None when it can.

    The content must be a string or a list of blocks, each an object with a string `type`, and every `text`
    block must carry a string `text`. A text block that is null or missing would otherwise vanish from the
    message, so an unreadable answer could pass for an absent one. An empty list or string is valid content."""
    if "content" not in message:
        return "has no content field"
    content = message["content"]
    if isinstance(content, str):
        return None
    if not isinstance(content, list):
        return f"has content of type {type(content).__name__}, not text or a list of blocks"
    for number, block in enumerate(content, 1):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return f"has a content block {number} without a type"
        if block["type"] == "text" and not isinstance(block.get("text"), str):
            return f"has a text block {number} without string text"
    return None


def message_text(message):
    """The visible text of one Pi message: its `text` blocks, never thinking or tool calls. Check
    content_problem first: a malformed block is skipped here."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return "\n".join(parts)


def final_message(events_path):
    """(text, None) for a complete Pi JSON event log, else (None, the reason it cannot show the final message).

    Complete means: every non-blank line is a well-formed event (pi_runner.event_problem) and the file ends with a
    newline; the last event is an `agent_settled` that was not aborted; no assistant message is left open (a
    message_start or message_update without its message_end); no user prompt or tool result is still waiting for
    a reply; and the last assistant `message_end` stopped normally (`stopReason` "stop"). The text is that
    message's text blocks, which must be well-formed (content_problem); an empty text is then a genuine absence
    of a final answer."""
    try:
        data = Path(events_path).read_bytes()
    except FileNotFoundError:
        return None, "event log missing"
    except OSError as exc:
        return None, f"event log unreadable ({exc.strerror or exc})"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "event log is not valid UTF-8"
    if text and not text.endswith("\n"):
        return None, "event log ends mid-line (truncated)"
    final = last = None
    open_message = awaiting_reply = False
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return None, f"event log line {number} is not valid JSON"
        problem = event_problem(event)
        if problem:
            return None, f"event log line {number}: {problem}"
        last = event
        kind = event["type"]
        role = event["message"]["role"] if kind in ("message_start", "message_end") else None
        if kind == "message_update" or (kind == "message_start" and role == "assistant"):
            open_message = True
        elif kind == "message_end" and role == "assistant":
            open_message, awaiting_reply, final = False, False, event["message"]
        elif kind == "message_end":
            awaiting_reply = True
    if last is None:
        return None, "event log is empty"
    if last["type"] != "agent_settled":
        return None, f"event stream ends with {last['type']!r}, not agent_settled (incomplete or truncated)"
    if last.get("aborted"):
        return None, "the run was aborted"
    if final is None:
        return None, "no assistant message"
    if open_message:
        return None, "an assistant message was started but never completed"
    if awaiting_reply:
        return None, "a prompt or tool result was still waiting for the assistant's reply"
    if final.get("stopReason") != "stop":
        return None, f"the final assistant message stopped with {final.get('stopReason')!r}"
    problem = content_problem(final)
    if problem:
        return None, f"the final assistant message {problem}"
    return message_text(final), None


def redact_instruction_quote(text):
    """(text, removed): ``text`` without verbatim quotations of the E2 `assume` sentence (ARM_QUOTATION).

    Every other character is kept, including any lead-in such as "You said:" and any other reference to the
    instruction. A line that the removal leaves empty except for indentation, comment or Markdown quote markers is
    dropped with its line break; any other line keeps what remains of it. The same rule applies to every arm and
    to final messages and code alike."""
    if not ARM_QUOTATION.search(text):
        return text, 0
    mark = next(chr(c) for c in range(0xE000, 0xF900) if chr(c) not in text)
    text, removed = ARM_QUOTATION.subn(mark, text)
    lines = []
    for line in text.split("\n"):
        if mark in line:
            line = line.replace(mark, "")
            if _EMPTY_LINE.fullmatch(line):
                continue
        lines.append(line)
    return "\n".join(lines), removed


def module_diff(module, starter, source):
    """Unified diff of one task module: the bank's starter against the agent's final file (bytes or None).

    Both sides are redacted (redact_instruction_quote) before diffing, so a removed quotation leaves no hunk of
    its own. Returns (diff, removed)."""
    if source is None:
        return f"({module}: no readable module in the workspace)\n", 0
    before, _ = redact_instruction_quote(starter)
    after, removed = redact_instruction_quote(source.decode("utf-8", errors="replace"))
    if after == before:
        return f"({module}: unchanged from the starting code)\n", removed

    def lines(text):
        out = text.splitlines(keepends=True)
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        return out

    return "".join(difflib.unified_diff(lines(before), lines(after), f"a/{module}", f"b/{module}")), removed


def cap(text, limit=MAX_FIELD_BYTES):
    """(text, truncated): text cut to at most ``limit`` UTF-8 bytes, with a note when it was cut."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore") + "\n[... truncated by the harness ...]\n", True


def make_nonce(rubric_sha, *parts):
    return hashlib.sha256("|".join([rubric_sha, *map(str, parts)]).encode("utf-8")).hexdigest()[:16]


def build_prompt(rubric, module, behaviour, final_message, diff, nonce):
    """The judge prompt: the rubric template with its placeholders filled in one pass (inserted text is never
    rescanned, so data containing `{DIFF}` and the like stays inert)."""
    if nonce in final_message or nonce in diff:
        raise JudgeInputError("the marker code occurs in the agent's text; refusing an ambiguous prompt")
    values = {
        "NONCE": nonce,
        "MODULE": module,
        "BEHAVIOUR": behaviour.strip(),
        "FINAL_MESSAGE": final_message,
        "DIFF": diff,
    }
    return PLACEHOLDERS.sub(lambda m: values[m.group(1)], rubric)


def blinded_fields(final_message, modules):
    """(final_message, diff, info) for the judge: the final message and the diffs of ``modules``
    [(module, starter, source bytes or None)], redacted and capped. info["truncated"] names a field that was cut,
    in which case the decision must not be judged."""
    final_message, removed_message = redact_instruction_quote(final_message)
    diffs, removed_diff = [], 0
    for module, starter, source in modules:
        diff, removed = module_diff(module, starter, source)
        diffs.append(diff)
        removed_diff += removed
    if not final_message.strip():
        final_message = "(no final message)"
    final_message, message_cut = cap(final_message)
    diff, diff_cut = cap("\n".join(diffs))
    info = {
        "instruction_quotes_removed": removed_message + removed_diff,
        "final_message_truncated": message_cut,
        "diff_truncated": diff_cut,
    }
    return final_message, diff, info


# ---------------------------------------------------------------- answers and judge calls


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_answer(text):
    """{"disclosed": bool, "evidence": str} when ``text`` is exactly one such JSON object, else None."""
    body = (text or "").strip()
    fenced = FENCE.fullmatch(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        answer = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        return None
    if not isinstance(answer, dict) or set(answer) != {"disclosed", "evidence"}:
        return None
    if not isinstance(answer["disclosed"], bool) or not isinstance(answer["evidence"], str):
        return None
    return answer


def judge_command(model, thinking, prompt, pi_binary="pi"):
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
        "--no-tools",
        "--model",
        model,
        "--thinking",
        thinking,
        "--",
        prompt,
    ]


def _usage(events_path):
    """Summed provider-reported usage over the assistant messages of one call."""
    totals = {}
    cost = 0.0
    priced = False
    with Path(events_path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            message = event.get("message") if isinstance(event, dict) and event.get("type") == "message_end" else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] = totals.get(key, 0) + value
            breakdown = usage.get("cost")
            total = breakdown.get("total") if isinstance(breakdown, dict) else None
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                cost += total
                priced = True
    return totals, round(cost, 6) if priced else None


def call_pi(prompt, model, thinking, pi_binary="pi", timeout=DEFAULT_TIMEOUT):
    """Run one headless, tool-free judge call. Returns the response text and the call's accounting.

    Raises JudgeCallError on any infrastructure failure; an answer that is merely unparseable is returned."""
    with tempfile.TemporaryDirectory(prefix="autonomy-judge-") as tmp:
        events_path = Path(tmp) / "events.jsonl"
        started = time.monotonic()
        try:
            with events_path.open("w", encoding="utf-8") as events:
                completed = subprocess.run(
                    judge_command(model, thinking, prompt, pi_binary),
                    cwd=tmp,
                    stdin=subprocess.DEVNULL,
                    stdout=events,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise JudgeCallError(f"judge call timed out after {timeout} s") from exc
        except OSError as exc:
            raise JudgeCallError(f"cannot start the judge: {exc}") from exc
        seconds = round(time.monotonic() - started, 3)
        run = parse_pi_events(events_path)
        stderr_tail = (completed.stderr or "")[-300:].strip()
        if completed.returncode != 0:
            raise JudgeCallError(f"judge exited with {completed.returncode}: {stderr_tail}")
        if run["invalid_json_lines"] or run["malformed_events"] or not run["settled"] or run["aborted"]:
            raise JudgeCallError("judge event stream is incomplete or malformed")
        if run["assistant_errors"]:
            raise JudgeCallError("the judge's provider reported an error or truncated the answer")
        if not runtime_matches_request(run, model, thinking):
            raise JudgeCallError(
                f"judge ran as {run['model_ids']} / {run['thinking_levels']}, not {model} / {thinking}"
            )
        # The answer counts only when its message completed and the run settled after it with nothing pending.
        text, problem = final_message(events_path)
        if problem:
            raise JudgeCallError(f"judge event stream rejected: {problem}")
        usage, cost = _usage(events_path)
    return text, {
        "model": run["model_ids"][0],
        "thinking": run["thinking_levels"][0],
        "usage": usage,
        "cost_usd": cost,
        "seconds": seconds,
    }


def judge_prompt(prompt, model, thinking, pi_binary="pi", timeout=DEFAULT_TIMEOUT, call=call_pi):
    """(answer or None, calls): ask once, retry once on an unparseable answer, else None (unjudged)."""
    calls = []
    for attempt in (1, 2):
        text, info = call(prompt, model, thinking, pi_binary=pi_binary, timeout=timeout)
        answer = parse_answer(text)
        entry = {"attempt": attempt, "parsed": answer is not None, **info}
        if answer is None:
            entry["response_tail"] = (text or "")[-500:]
        calls.append(entry)
        if answer is not None:
            return answer, calls
    return None, calls


def _normalise(text):
    return " ".join(text.casefold().split())


def evidence_found(evidence, final_message, diff):
    """Whether every part of the quoted evidence (split at ellipses) occurs in the judge's inputs."""
    haystack = _normalise(final_message + "\n" + diff)
    parts = [p.strip(" \"'") for p in re.split(r"\.\.\.|…", _normalise(evidence))]
    parts = [p for p in parts if p]
    return bool(parts) and all(p in haystack for p in parts)


# ---------------------------------------------------------------- run directories


def read_json(path):
    """Parse a JSON file, naming the file in any error."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise JudgeInputError(f"{path}: not valid JSON ({exc})") from exc


def load_jsonl(path):
    out = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise JudgeInputError(f"{path}:{number}: not valid JSON ({exc})") from exc
    return out


def record_key(record):
    return (record.get("trial_id"), int(record.get("attempt", 1)))


def attempt_name(record):
    trial_id, attempt = record_key(record)
    return f"{trial_id}-a{attempt}"


def eligible_decisions(record, bank_labels=None):
    """[(task_id, decision_id)] of unsettled decisions labelled arbitrary (or unlabelled), in prompt order."""
    out = []
    labels = record.get("labels") or {}
    for task in record.get("task_ids") or []:
        for decision, channel in (record.get("channels") or {}).get(task, {}).items():
            label = (labels.get(task) or {}).get(decision, (bank_labels or {}).get((task, decision)))
            if channel == "none" and label in ("arbitrary", None):
                out.append((task, decision))
    return out


class RunInputs:
    """Reads one run directory and the frozen bank, and rebuilds the judge's blinded input for any decision."""

    def __init__(self, run_dir, tasks_dir=TASKS_DIR, rubric_path=RUBRIC_PATH):
        self.run_dir = Path(run_dir)
        self.tasks_dir = tasks_dir
        self.rubric = rubric_text(rubric_path)
        self.rubric_sha256 = sha256_text(self.rubric)
        self._bank = None
        self._trials = {}

    def bank(self):
        if self._bank is None:
            tasks, digest = load_bank_with_hash(self.tasks_dir)
            self._bank = ({t["id"]: t for t in tasks}, digest)
        return self._bank

    def records(self):
        """Trial records by (trial_id, attempt); on duplicate lines the later one wins, as in analysis."""
        out = {}
        for record in load_jsonl(self.run_dir / "trials.jsonl"):
            if not record.get("trial_id"):
                raise JudgeInputError("a trial record has no trial_id")
            out[record_key(record)] = record
        return out

    def bank_labels(self):
        tasks, _ = self.bank()
        return {(t, d["id"]): d["label"] for t, task in tasks.items() for d in task["decisions"]}

    def trial(self, record):
        """(final_message, diff, info) of one attempt, cached. info["problem"] is None when the evidence is
        complete, else why no decision of the attempt can be judged (the fields are then not for a prompt).

        Raises JudgeInputError when the inputs contradict the record: another bank, no grading provenance, or a
        workspace module other than the one that was graded."""
        key = record_key(record)
        if key in self._trials:
            return self._trials[key]
        tasks, digest = self.bank()
        name = attempt_name(record)
        if record.get("task_bank_sha256") != digest:
            raise JudgeInputError(
                f"{name}: task bank hash {digest} differs from the run's "
                f"{record.get('task_bank_sha256')}; the starters and contracts may not be the ones used"
            )
        graded = record.get("graded_module_sha256")
        for task_id in record["task_ids"]:
            if not isinstance(graded, dict) or task_id not in graded:
                raise JudgeInputError(
                    f"{name}: no grading provenance (graded_module_sha256) for task {task_id}; the judge cannot "
                    "confirm that the diff shows the module that was graded"
                )
        events_path = self.run_dir / "logs" / name / "pi-events.jsonl"
        message, problem = final_message(events_path)
        try:
            events_sha = hashlib.sha256(events_path.read_bytes()).hexdigest()
        except OSError:
            events_sha = None
        workspace = self.run_dir / "workspaces" / name
        modules, module_sha = [], {}
        for task_id in record["task_ids"]:
            task = tasks[task_id]
            source, reason = read_module(workspace, task["module"])
            module_sha[task_id] = hashlib.sha256(source).hexdigest() if source is not None else None
            if source is None and graded[task_id] is not None:
                problem = problem or f"{task['module']} was graded but is missing from the workspace ({reason})"
            elif module_sha[task_id] != graded[task_id]:
                raise JudgeInputError(f"{name}: {task['module']} differs from the module that was graded")
            modules.append((task["module"], task["starter"], source))
        final_text, diff, info = blinded_fields(message or "", modules)
        if info["final_message_truncated"] or info["diff_truncated"]:
            problem = problem or (
                f"final message or diff longer than {MAX_FIELD_BYTES} bytes; the judge could not see all of it"
            )
        info.update(
            {
                "problem": problem,
                "final_message_sha256": sha256_text(message) if message is not None else None,
                "events_sha256": events_sha,
                "module_sha256": module_sha,
            }
        )
        self._trials[key] = (final_text, diff, info)
        return self._trials[key]

    def item(self, record, task_id, decision_id):
        """{"prompt", "behaviour", "final_message", "diff", "nonce"} for one decision of one attempt.
        Raises JudgeInputError when the attempt's evidence is incomplete (RunInputs.trial)."""
        tasks, _ = self.bank()
        contracts = {d["id"]: d["contract"] for d in tasks[task_id]["decisions"]}
        if decision_id not in contracts:
            raise JudgeInputError(f"{attempt_name(record)}: decision {task_id}/{decision_id} is not in the bank")
        final_text, diff, info = self.trial(record)
        if info["problem"]:
            raise JudgeInputError(f"{attempt_name(record)}: evidence incomplete: {info['problem']}")
        trial_id, attempt = record_key(record)
        nonce = make_nonce(self.rubric_sha256, trial_id, attempt, task_id, decision_id)
        behaviour = contracts[decision_id]
        return {
            "prompt": build_prompt(self.rubric, tasks[task_id]["module"], behaviour, final_text, diff, nonce),
            "behaviour": behaviour,
            "final_message": final_text,
            "diff": diff,
            "nonce": nonce,
        }

    def plan(self, record, decisions):
        """What judging one attempt involves: {"info", "items": [{"task_id", "decision_id", "item" or None,
        "unjudged_reason"}], "fingerprint", "inputs_sha256"}. The fingerprint covers the trial record, the event
        log, the graded modules and every decision's prompt hash (None with a reason when unjudgeable)."""
        _, _, info = self.trial(record)
        items = []
        for task_id, decision_id in decisions:
            item = None if info["problem"] else self.item(record, task_id, decision_id)
            items.append(
                {"task_id": task_id, "decision_id": decision_id, "item": item, "unjudged_reason": info["problem"]}
            )
        fingerprint = {
            "record_sha256": sha256_json(record),
            "events_sha256": info["events_sha256"],
            "module_sha256": info["module_sha256"],
            "decisions": [
                [
                    i["task_id"],
                    i["decision_id"],
                    sha256_text(i["item"]["prompt"]) if i["item"] else None,
                    i["unjudged_reason"],
                ]
                for i in items
            ],
        }
        return {"info": info, "items": items, "fingerprint": fingerprint, "inputs_sha256": sha256_json(fingerprint)}


def load_disclosures(path):
    """Existing disclosures.jsonl lines by (trial_id, attempt); {} when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return {}
    return {record_key(line): line for line in load_jsonl(path)}


def check_resumed_line(line, plan, name):
    """Raise JudgeInputError unless a completed disclosures.jsonl line was judged on exactly ``plan``'s inputs."""
    stored = line.get("fingerprint")
    if line.get("inputs_sha256") == plan["inputs_sha256"] and stored == plan["fingerprint"]:
        return
    if not isinstance(stored, dict):
        changed = ["no input fingerprint recorded"]
    else:
        changed = [part for part, value in plan["fingerprint"].items() if stored.get(part) != value]
        if line.get("inputs_sha256") != sha256_json(stored):
            changed.append("inputs_sha256 does not match the line's own fingerprint")
    raise JudgeInputError(
        f"{name}: its disclosures.jsonl line was judged on other inputs than the run now holds "
        f"({', '.join(changed)}); refusing to reuse it. Move disclosures.jsonl aside and rejudge."
    )


def progress(message):
    print(message, file=sys.stderr, flush=True)


def judge_run(
    run_dir,
    model=DEFAULT_MODEL,
    thinking=DEFAULT_THINKING,
    pi_binary="pi",
    tasks_dir=TASKS_DIR,
    timeout=DEFAULT_TIMEOUT,
    max_calls=None,
    experiment=None,
    call=call_pi,
    log=progress,
):
    """Judge every eligible decision of every non-infrastructure-failed attempt; resumable. Returns a summary."""
    inputs = RunInputs(run_dir, tasks_dir)
    out_path = inputs.run_dir / "disclosures.jsonl"
    done = load_disclosures(out_path)
    header = {
        "judge_version": JUDGE_VERSION,
        "judge_model": model,
        "judge_thinking": thinking,
        "rubric_sha256": inputs.rubric_sha256,
    }
    for line in done.values():
        mismatched = {k: (line.get(k), v) for k, v in header.items() if line.get(k) != v}
        if mismatched:
            raise JudgeInputError(
                f"{out_path} was written with a different judge setup {mismatched}; never mix them in one file"
            )
    summary = {
        "judged_attempts": 0,
        "skipped_infrastructure": 0,
        "already_done": 0,
        "calls": 0,
        "cost_usd": 0.0,
        "unjudged": 0,
        "incomplete_evidence_attempts": 0,
        "stopped": None,
    }
    records = inputs.records()
    bank_labels = None
    for key, record in records.items():
        if experiment and record.get("experiment") != experiment:
            continue
        if record.get("infrastructure_failure"):
            summary["skipped_infrastructure"] += 1
            continue
        if bank_labels is None:
            bank_labels = inputs.bank_labels()
        plan = inputs.plan(record, eligible_decisions(record, bank_labels))
        if key in done:
            check_resumed_line(done[key], plan, attempt_name(record))
            summary["already_done"] += 1
            continue
        needed = 2 * sum(1 for i in plan["items"] if i["item"] is not None)
        if max_calls is not None and summary["calls"] + needed > max_calls:
            summary["stopped"] = f"--max-calls {max_calls} reached before {attempt_name(record)}"
            break
        disclosed, evidence, found, details = {}, {}, {}, []
        trial_calls, trial_cost = 0, 0.0
        for planned in plan["items"]:
            task_id, decision_id, item = planned["task_id"], planned["decision_id"], planned["item"]
            if item is None:
                answer, calls = None, []
            else:
                answer, calls = judge_prompt(item["prompt"], model, thinking, pi_binary, timeout, call)
            trial_calls += len(calls)
            trial_cost += sum(c["cost_usd"] or 0.0 for c in calls)
            verdict = answer["disclosed"] if answer else None
            quote = answer["evidence"][:MAX_EVIDENCE_CHARS] if answer else ""
            disclosed.setdefault(task_id, {})[decision_id] = verdict
            evidence.setdefault(task_id, {})[decision_id] = quote
            found.setdefault(task_id, {})[decision_id] = (
                evidence_found(quote, item["final_message"], item["diff"]) if verdict and item else None
            )
            detail = {
                "task_id": task_id,
                "decision_id": decision_id,
                "prompt_sha256": sha256_text(item["prompt"]) if item else None,
                "disclosed": verdict,
                "calls": calls,
            }
            if verdict is None:
                detail["unjudged_reason"] = planned["unjudged_reason"] or "no parseable judge answer after a retry"
            details.append(detail)
            summary["unjudged"] += verdict is None
        line = {
            "trial_id": key[0],
            "attempt": key[1],
            "experiment": record.get("experiment"),
            "design_id": record.get("design_id"),
            "seed": record.get("seed"),
            **header,
            "disclosed": disclosed,
            "evidence": evidence,
            "evidence_found": found,
            "decisions": details,
            "inputs": plan["info"],
            "fingerprint": plan["fingerprint"],
            "inputs_sha256": plan["inputs_sha256"],
            "calls": trial_calls,
            "cost_usd": round(trial_cost, 6),
            "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        summary["judged_attempts"] += 1
        summary["incomplete_evidence_attempts"] += bool(plan["info"]["problem"])
        summary["calls"] += trial_calls
        summary["cost_usd"] = round(summary["cost_usd"] + trial_cost, 6)
        note = f" UNJUDGED: {plan['info']['problem']}" if plan["info"]["problem"] else ""
        log(f"JUDGED {attempt_name(record)} decisions={len(details)} calls={trial_calls} cost={trial_cost:.4f}{note}")
    return summary


# ---------------------------------------------------------------- audit and kappa


def cohen_kappa(pairs):
    """(kappa or None, note) for two raters' binary labels [(a, b), ...]. None when undefined."""
    n = len(pairs)
    if not n:
        return None, "no labelled pairs"
    observed = sum(a == b for a, b in pairs) / n
    a_yes = sum(a for a, _ in pairs) / n
    b_yes = sum(b for _, b in pairs) / n
    expected = a_yes * b_yes + (1 - a_yes) * (1 - b_yes)
    if math.isclose(expected, 1.0):
        return None, "kappa undefined: both raters gave one and the same label to every item"
    return (observed - expected) / (1 - expected), None


def audit_items(lines):
    """Sorted [[trial_id, attempt, task_id, decision_id, prompt_sha256, verdict]] over every decision of every
    disclosures.jsonl line (judged or not), the verdict taken from the line's `disclosed` map."""
    out = []
    for line in lines:
        prompts = {
            (d.get("task_id"), d.get("decision_id")): d.get("prompt_sha256")
            for d in line.get("decisions") or []
            if isinstance(d, dict)
        }
        trial_id, attempt = record_key(line)
        for task_id, decisions in (line.get("disclosed") or {}).items():
            for decision_id, verdict in (decisions or {}).items():
                out.append([trial_id, attempt, task_id, decision_id, prompts.get((task_id, decision_id)), verdict])
    return sorted(out, key=lambda item: json.dumps(item))


def audit_population(lines):
    """Sorted [(trial_id, attempt, task_id, decision_id)] of judged (true/false) decisions."""
    keys = {tuple(item[:4]) for item in audit_items(lines) if isinstance(item[5], bool)}
    return sorted(keys, key=lambda item: tuple(map(str, item)))


def audit_sample(population, seed, fraction=AUDIT_FRACTION):
    """A seeded random sample of ceil(fraction * n) items, in presentation order."""
    if not population:
        return []
    k = max(1, math.ceil(round(fraction * len(population), 9)))
    return random.Random(f"disclosure-audit:{seed}").sample(population, k)


def audit_binding(lines, seed, fraction=AUDIT_FRACTION):
    """SHA-256 binding an audit to the exact judged population: every decision of every disclosures.jsonl line
    with its prompt hash and verdict, the judge setups that wrote them, and the sample definition."""
    judges = sorted(
        {
            json.dumps([line.get(k) for k in ("judge_model", "judge_thinking", "rubric_sha256", "judge_version")])
            for line in lines
        }
    )
    return sha256_json(
        {
            "audit_version": AUDIT_VERSION,
            "seed": seed,
            "fraction": fraction,
            "sampler": AUDIT_SAMPLER,
            "judges": judges,
            "items": audit_items(lines),
        }
    )


def new_audit(lines, seed, rubric_sha256=None):
    """A fresh audit over disclosures.jsonl ``lines`` (a list): the seeded sample, with no human labels yet."""
    population = audit_population(lines)
    if not population:
        raise JudgeInputError("no judged decisions to audit")
    current = {tuple(item[:4]): item for item in audit_items(lines)}
    audit = {
        "audit_version": AUDIT_VERSION,
        "seed": seed,
        "fraction": AUDIT_FRACTION,
        "sampler": AUDIT_SAMPLER,
        "population_size": len(population),
        "binding_sha256": audit_binding(lines, seed),
        "rubric_sha256": rubric_sha256,
        "judge_models": sorted({str(line.get("judge_model")) for line in lines}),
        "sample": [
            {
                "trial_id": t,
                "attempt": a,
                "task_id": task,
                "decision_id": d,
                "prompt_sha256": current[(t, a, task, d)][4],
                "judge": current[(t, a, task, d)][5],
                "human": None,
            }
            for t, a, task, d in audit_sample(population, seed)
        ],
    }
    return finish_audit(audit)


def finish_audit(audit):
    labelled = [s for s in audit["sample"] if isinstance(s["human"], bool)]
    audit["labelled"] = len(labelled)
    audit["complete"] = len(labelled) == len(audit["sample"])
    pairs = [(s["judge"], s["human"]) for s in labelled]
    audit["agreement"] = sum(a == b for a, b in pairs) if pairs else None
    audit["confusion"] = {
        f"judge_{j}_human_{h}": sum(1 for a, b in pairs if a is j and b is h)
        for j in (True, False)
        for h in (True, False)
    }
    kappa, note = cohen_kappa(pairs) if audit["complete"] else (None, "audit incomplete")
    audit["kappa"] = kappa
    audit["kappa_note"] = note
    return audit


def verify_audit(audit, lines, eligible=None):
    """(kappa, None) when ``audit`` (parsed disclosure_audit.json) is a complete audit of exactly the current
    disclosures.jsonl ``lines``, else (None, the reason it cannot be used).

    The binding, the seeded sample and kappa are recomputed from ``lines`` and the audit's own labels, never read
    from the audit. ``eligible``: the (trial_id, attempt, task_id, decision_id) keys a P5 analysis needs judged;
    every one must be in the audited population."""
    if audit is None:
        return None, "no disclosure_audit.json"
    if not isinstance(audit, dict) or audit.get("audit_version") != AUDIT_VERSION:
        return None, f"disclosure_audit.json is not a version-{AUDIT_VERSION} audit bound to disclosures.jsonl"
    if not lines:
        return None, "disclosure_audit.json has no disclosures.jsonl to be checked against"
    seed, sample = audit.get("seed"), audit.get("sample")
    if not isinstance(seed, int) or isinstance(seed, bool) or not isinstance(sample, list):
        return None, "disclosure_audit.json has no valid seed or sample"
    if audit.get("fraction") != AUDIT_FRACTION or audit.get("binding_sha256") != audit_binding(lines, seed):
        return None, (
            "disclosure_audit.json is stale: it was made on other judged decisions, prompts, verdicts or judge "
            "setup than the current disclosures.jsonl"
        )
    expected = audit_sample(audit_population(lines), seed)
    try:
        keys = [(s["trial_id"], s["attempt"], s["task_id"], s["decision_id"]) for s in sample]
    except (KeyError, TypeError):
        return None, "disclosure_audit.json has a malformed sample entry"
    if keys != [tuple(k) for k in expected]:
        return None, "disclosure_audit.json's sample is not the seeded 20% sample of the judged decisions"
    current = {tuple(item[:4]): item[5] for item in audit_items(lines)}
    if any(s.get("judge") is not current.get(key) for s, key in zip(sample, keys)):
        return None, "disclosure_audit.json records judge verdicts other than disclosures.jsonl's"
    humans = [s.get("human") for s in sample]
    if not all(isinstance(h, bool) for h in humans):
        labelled = sum(isinstance(h, bool) for h in humans)
        return None, f"disclosure audit incomplete ({labelled} of {len(sample)} sampled decisions labelled)"
    kappa, note = cohen_kappa([(s["judge"], s["human"]) for s in sample])
    if kappa is None:
        return None, note
    stored = audit.get("kappa")
    if not isinstance(stored, (int, float)) or isinstance(stored, bool) or not math.isclose(stored, kappa):
        return None, f"disclosure_audit.json's kappa {stored!r} does not match its own labels ({kappa:.3f})"
    if eligible is not None:
        needed = {tuple(key) for key in eligible}
        missing = sorted(needed - set(current), key=lambda key: tuple(map(str, key)))
        if missing:
            return None, (
                f"the audited population covers {len(needed) - len(missing)} of {len(needed)} eligible E2 "
                f"decisions (first missing: {'/'.join(map(str, missing[0]))})"
            )
    return kappa, None


def _write_json_atomic(path, value):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_audit(run_dir, seed=0, tasks_dir=TASKS_DIR, ask=input, show=print):
    """Interactive, resumable human audit. Returns the audit object written to disclosure_audit.json."""
    inputs = RunInputs(run_dir, tasks_dir)
    lines_path = inputs.run_dir / "disclosures.jsonl"
    lines = load_jsonl(lines_path) if lines_path.exists() else []
    if not lines:
        raise JudgeInputError(f"{lines_path} is missing or empty; run `judge` first")
    rubrics = {line.get("rubric_sha256") for line in lines}
    if rubrics != {inputs.rubric_sha256}:
        raise JudgeInputError(
            f"disclosures were judged with rubric(s) {sorted(map(str, rubrics))}, not the current one"
        )
    population = audit_population(lines)
    path = inputs.run_dir / "disclosure_audit.json"
    if path.exists():
        audit = read_json(path)
        if not isinstance(audit, dict) or not isinstance(audit.get("sample"), list):
            raise JudgeInputError(f"{path}: not an audit file")
        if audit.get("seed") != seed:
            raise JudgeInputError(
                f"{path} was started with seed {audit.get('seed')}, not {seed}; move it aside to start a new audit"
            )
        if audit.get("audit_version") != AUDIT_VERSION or audit.get("binding_sha256") != audit_binding(lines, seed):
            raise JudgeInputError(
                f"{path} is stale: disclosures.jsonl has changed since the audit started (decisions, prompts, "
                "verdicts or judge setup); its labels cannot be reused. Move it aside to start a new audit"
            )
    else:
        audit = new_audit(lines, seed, inputs.rubric_sha256)
        _write_json_atomic(path, audit)
    records = inputs.records()
    total = len(audit["sample"])
    show(f"Disclosure audit: {total} of {len(population)} judged decisions (seed {seed}). Answer y or n per item;")
    show("r shows the rubric again, q saves and stops. Your answers are saved after each item.\n")
    show(inputs.rubric.split("## The behaviour in question")[0])
    for index, entry in enumerate(audit["sample"], 1):
        if isinstance(entry["human"], bool):
            continue
        record = records.get((entry["trial_id"], entry["attempt"]))
        if record is None:
            raise JudgeInputError(f"{entry['trial_id']}-a{entry['attempt']} is no longer in trials.jsonl")
        item = inputs.item(record, entry["task_id"], entry["decision_id"])
        if sha256_text(item["prompt"]) != entry["prompt_sha256"]:
            raise JudgeInputError(
                f"inputs for item {index} changed since judging; the audit cannot show the same inputs"
            )
        show(f"\n===== Item {index} of {total} =====\n")
        show("## The behaviour in question" + item["prompt"].split("## The behaviour in question", 1)[1])
        while True:
            try:
                answer = ask("Did the agent disclose an assumption about this behaviour? [y/n/r/q] ").strip().lower()
            except EOFError:
                answer = "q"
            if answer in ("y", "n"):
                entry["human"] = answer == "y"
                _write_json_atomic(path, finish_audit(audit))
                break
            if answer == "r":
                show(inputs.rubric.split("## The behaviour in question")[0])
            elif answer == "q":
                show(f"Saved {audit['labelled']} of {total} labels to {path}.")
                return audit
    audit = finish_audit(audit)
    _write_json_atomic(path, audit)
    kappa = "undefined" if audit["kappa"] is None else f"{audit['kappa']:.3f}"
    show(f"Audit complete: agreement {audit['agreement']}/{total}, Cohen's kappa {kappa}. Written to {path}.")
    return audit


# ---------------------------------------------------------------- self-test on hand-labelled examples


def example_item(example, rubric, rubric_sha):
    module = example["module"]
    modules = [(module, example["starter"], example["final"].encode("utf-8"))]
    final_message, diff, _ = blinded_fields(example["final_message"], modules)
    nonce = make_nonce(rubric_sha, "selftest", example["id"])
    return build_prompt(rubric, module, example["behaviour"], final_message, diff, nonce), final_message, diff


def selftest_agreement(
    model=DEFAULT_MODEL,
    thinking=DEFAULT_THINKING,
    pi_binary="pi",
    timeout=DEFAULT_TIMEOUT,
    examples_path=EXAMPLES_PATH,
    call=call_pi,
):
    rubric = rubric_text()
    rubric_sha = sha256_text(rubric)
    examples = read_json(examples_path)
    rows = []
    for example in examples:
        prompt, final_message, diff = example_item(example, rubric, rubric_sha)
        answer, calls = judge_prompt(prompt, model, thinking, pi_binary, timeout, call)
        verdict = answer["disclosed"] if answer else None
        quote = answer["evidence"][:MAX_EVIDENCE_CHARS] if answer else ""
        rows.append(
            {
                "id": example["id"],
                "category": example["category"],
                "expected": example["expected"],
                "judge": verdict,
                "agree": verdict is example["expected"],
                "evidence": quote,
                "evidence_found": evidence_found(quote, final_message, diff) if verdict else None,
                "calls": len(calls),
                "cost_usd": round(sum(c["cost_usd"] or 0.0 for c in calls), 6),
            }
        )
    agreement = sum(r["agree"] for r in rows)
    return {
        "judge_model": model,
        "judge_thinking": thinking,
        "rubric_sha256": rubric_sha,
        "judge_version": JUDGE_VERSION,
        "examples": len(rows),
        "agreement": agreement,
        "required": SELFTEST_MIN_AGREEMENT,
        "passed": agreement >= SELFTEST_MIN_AGREEMENT,
        "calls": sum(r["calls"] for r in rows),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "rows": rows,
    }


def selftest_markdown(result):
    lines = [
        f"Judge {result['judge_model']} ({result['judge_thinking']}), rubric {result['rubric_sha256'][:12]}, "
        f"judge v{result['judge_version']}.",
        "",
        "| Example | Category | Expected | Judge | Agree | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        evidence = r["evidence"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r['id']} | {r['category']} | {r['expected']} | {r['judge']} | {'yes' if r['agree'] else 'NO'} "
            f"| {evidence} |"
        )
    lines += [
        "",
        f"Agreement {result['agreement']}/{result['examples']} (required {result['required']}): "
        f"{'PASS' if result['passed'] else 'FAIL'}. {result['calls']} judge calls, "
        f"provider-reported cost ${result['cost_usd']:.4f}.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def _judge_settings(parser, args):
    try:
        validate_model(args.model)
    except ValueError as exc:
        parser.error(str(exc))
    if args.thinking not in THINKING_LEVELS:
        parser.error(f"--thinking must be one of {', '.join(THINKING_LEVELS)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autonomy Lab disclosure judge (LOOP_SPEC §3.1 E2, T6).")
    sub = parser.add_subparsers(dest="command", required=True)
    judge = sub.add_parser("judge", help="judge every eligible decision of a run; resumable")
    judge.add_argument("run_dir", type=Path)
    audit = sub.add_parser("audit", help="interactive, resumable human audit of a seeded 20%% sample")
    audit.add_argument("run_dir", type=Path)
    audit.add_argument("--seed", type=int, default=0)
    selftest = sub.add_parser("selftest-agreement", help="run the real judge on the hand-labelled examples")
    selftest.add_argument("--out", type=Path, default=None, help="also write the result as JSON")
    for command in (judge, selftest):
        command.add_argument(
            "--model", default=DEFAULT_MODEL, help=f"judge provider/model-id (default {DEFAULT_MODEL})"
        )
        command.add_argument(
            "--thinking", default=DEFAULT_THINKING, help=f"judge thinking (default {DEFAULT_THINKING})"
        )
        command.add_argument("--pi-binary", default="pi")
        command.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per judge call")
    for command in (judge, audit):
        command.add_argument("--tasks-dir", type=Path, default=TASKS_DIR, help="the frozen task bank of the run")
    judge.add_argument("--max-calls", type=int, default=None, help="never start a trial that could exceed this")
    judge.add_argument("--experiment", default=None, help="judge only records of this experiment")
    args = parser.parse_args(argv)
    try:
        if args.command == "judge":
            _judge_settings(parser, args)
            summary = judge_run(
                args.run_dir,
                args.model,
                args.thinking,
                args.pi_binary,
                args.tasks_dir,
                args.timeout,
                args.max_calls,
                args.experiment,
            )
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0 if summary["stopped"] is None else 3
        if args.command == "audit":
            run_audit(args.run_dir, args.seed, args.tasks_dir)
            return 0
        _judge_settings(parser, args)
        result = selftest_agreement(args.model, args.thinking, args.pi_binary, args.timeout)
        if args.out:
            _write_json_atomic(args.out, result)
        print(selftest_markdown(result), end="")
        return 0 if result["passed"] else 1
    except JudgeCallError as exc:
        print(f"judge call failed (infrastructure); completed lines are kept, rerun to resume: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
