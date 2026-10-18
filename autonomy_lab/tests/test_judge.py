"""Offline tests for the disclosure judge (T6). A fake Pi stands in for the judge model; no network calls."""

import hashlib
import inspect
import io
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import autonomy_lab.judge as judge
from autonomy_lab.design import ASSUME_SENTENCE
from autonomy_lab.taskbank import load_bank_with_hash

FIXTURES = Path(__file__).parent / "fixtures"
BANK = FIXTURES / "bank_valid"
_BANK_TASKS, BANK_SHA = load_bank_with_hash(BANK)
TASKS = {t["id"]: t for t in _BANK_TASKS}

# Frozen judge inputs (LOOP_SPEC §3.3, T6 "the rubric is frozen before E2"). selftest-agreement (xai/grok-4.7,
# medium, judge v3 with minimal redaction) scored 12/12 on exactly these (2026-10-18, 12 calls, provider-reported
# $0.0694). If either digest changes, rerun `python3 -m autonomy_lab.judge selftest-agreement` and record the new
# result before updating them.
RUBRIC_SHA256 = "67708d42fbadab4235767b5ce9efff82e73668294df5d0359be30900c69c9cb9"
SELFTEST_PROMPTS_SHA256 = "0bf6a09b84813dbe614c5a298e02349f202a2eafcca5fd59a807b9d4cbb886cd"

# The fake judge. FAKE_JUDGE_MODE: `rule` answers disclosed=true exactly when the final-message data contains
# "Assumption: <the behaviour sentence>"; `garbage` never answers in JSON; `garbage-once` does so on its first call
# only; `wrong-model` reports another model; `fail` exits non-zero; `trailing-start` settles after a valid answer and
# then starts another assistant message that never ends; `unfinished` starts a message after the answer and settles
# without ending it. Every prompt is appended to FAKE_JUDGE_LOG.
FAKE_JUDGE = r"""
import json, os, re, sys
argv = sys.argv[1:]
prompt = argv[-1]
model = argv[argv.index("--model") + 1]
thinking = argv[argv.index("--thinking") + 1]
mode = os.environ.get("FAKE_JUDGE_MODE", "rule")
with open(os.environ["FAKE_JUDGE_LOG"]) as log:
    calls = sum(1 for _ in log) + 1
with open(os.environ["FAKE_JUDGE_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv[:-1], "prompt": prompt}) + "\n")
if mode == "fail":
    sys.exit(1)
section = prompt.split("## The behaviour in question\n\nModule: `", 1)[1]
behaviour = section.split("`\n\n", 1)[1].split("\n\nUse this sentence only", 1)[0]
nonce = re.search(r"<<<BEGIN FINAL MESSAGE (\w+)>>>", prompt).group(1)
message = prompt.split(f"<<<BEGIN FINAL MESSAGE {nonce}>>>\n", 1)[1].split(f"\n<<<END FINAL MESSAGE {nonce}>>>", 1)[0]
quote = "Assumption: " + behaviour
answer = json.dumps({"disclosed": quote in message, "evidence": quote if quote in message else ""})
if mode == "garbage" or (mode == "garbage-once" and calls == 1):
    answer = "I believe the agent disclosed it."
provider, _, model_id = model.partition("/")
if mode == "wrong-model":
    model_id = "other"
assistant = {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": answer}],
             "provider": provider, "model": model_id, "thinkingLevel": thinking, "stopReason": "stop",
             "usage": {"input": 100, "output": 10, "totalTokens": 110, "cost": {"total": 0.001}}}
events = [{"type": "agent_start"}, {"type": "message_end", "message": assistant}, {"type": "agent_settled"}]
started = {"type": "message_start", "message": {"role": "assistant", "content": []}}
if mode == "trailing-start":
    events.append(started)
elif mode == "unfinished":
    events.insert(2, started)
for event in events:
    print(json.dumps(event))
"""


def make_fake(tmp):
    fake = Path(tmp) / "fake-judge"
    fake.write_text("#!" + sys.executable + "\n" + FAKE_JUDGE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


def assistant_event(content, **extra):
    message = {"role": "assistant", "content": content, "provider": "p", "model": "m", "thinkingLevel": "high"}
    return {"type": "message_end", "message": {**message, "stopReason": "stop", **extra}}


def events_for(final_text):
    """An agent event stream: an early message, a tool call, and a final message with thinking and text."""
    return [
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "the prompt"}]}},
        assistant_event([{"type": "text", "text": "Let me look."}, {"type": "toolCall", "name": "bash"}]),
        {"type": "message_end", "message": {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]}},
        assistant_event([{"type": "thinking", "thinking": "SECRET THOUGHT"}, {"type": "text", "text": final_text}]),
        {"type": "agent_settled"},
    ]


def labels(task_id):
    return {d["id"]: d["label"] for d in TASKS[task_id]["decisions"]}


class Run:
    """A synthetic run directory over the valid fixture bank."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "logs").mkdir(parents=True)
        (self.root / "workspaces").mkdir()
        self.records = []

    def add(self, trial_id, task_id, channels, final_text, module_text, attempt=1, arm=None, **fields):
        name = f"{trial_id}-a{attempt}"
        (self.root / "logs" / name).mkdir()
        (self.root / "workspaces" / name).mkdir()
        events = "".join(json.dumps(e) + "\n" for e in events_for(final_text))
        (self.root / "logs" / name / "pi-events.jsonl").write_text(events, encoding="utf-8")
        module = TASKS[task_id]["module"]
        (self.root / "workspaces" / name / module).write_text(module_text, encoding="utf-8")
        record = {
            "trial_id": trial_id,
            "experiment": "E1" if arm is None else "E2",
            "design_id": "d",
            "seed": len(self.records),
            "task_ids": [task_id],
            "channels": {task_id: channels},
            "arm": arm,
            "labels": {task_id: labels(task_id)},
            "task_bank_sha256": BANK_SHA,
            "infrastructure_failure": None,
            "attempt": attempt,
            "graded_module_sha256": {task_id: hashlib.sha256(module_text.encode("utf-8")).hexdigest()},
            **fields,
        }
        self.records.append(record)
        return record

    def write(self):
        text = "".join(json.dumps(r) + "\n" for r in self.records)
        (self.root / "trials.jsonl").write_text(text, encoding="utf-8")
        return text


def contract(task_id, decision_id):
    return next(d["contract"] for d in TASKS[task_id]["decisions"] if d["id"] == decision_id)


# The arm sentence wrapped over two comment lines, as an agent might paste it.
ARM_HEAD, ARM_TAIL = ASSUME_SENTENCE.split(" any ")
CLAMP_DONE = (
    "def clamp(x, lo, hi):\n"
    "    # " + ARM_HEAD + "\n"
    "    # any " + ARM_TAIL + "\n"
    "    if lo is not None and hi is not None and lo > hi:\n"
    "        raise ValueError('lo > hi')\n"
    "    return max(lo, min(x, hi))\n"
)


def standard_run(root):
    run = Run(root)
    none4 = dict.fromkeys(labels("clamp-range"), "none")
    run.add(
        "e2-t0",
        "clamp-range",
        none4,
        "Fixed clamp.\n\nAs asked: " + ASSUME_SENTENCE + "\nAssumption: " + contract("clamp-range", "order"),
        CLAMP_DONE,
        arm="assume",
    )
    slug_none = dict.fromkeys(labels("slugify"), "none")
    run.add("e2-t1", "slugify", slug_none, "", "x\n", arm="plain", infrastructure_failure="provider_error")
    run.add(
        "e2-t1", "slugify", slug_none, "Changed slug.", "def slug(text):\n    return text\n", attempt=2, arm="plain"
    )
    mixed = {"order": "contract", "bool": "none", "open-bound": "check", "inclusive": "none"}
    run.add("e1-t0", "clamp-range", mixed, "Assumption: " + contract("clamp-range", "bool"), CLAMP_DONE)
    return run


class FakeJudgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = make_fake(self.tmp.name)
        self.log = Path(self.tmp.name) / "judge-log.jsonl"
        self.log.touch()
        self.env = unittest.mock.patch.dict("os.environ", {"FAKE_JUDGE_LOG": str(self.log), "FAKE_JUDGE_MODE": "rule"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.run_dir = Path(self.tmp.name) / "run"

    def mode(self, mode):
        os.environ["FAKE_JUDGE_MODE"] = mode

    def prompts(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def judge(self, **kwargs):
        kwargs.setdefault("pi_binary", str(self.fake))
        kwargs.setdefault("tasks_dir", BANK)
        return judge.judge_run(self.run_dir, log=lambda *_: None, **kwargs)

    def lines(self):
        return judge.load_jsonl(self.run_dir / "disclosures.jsonl")


class ExtractionTests(unittest.TestCase):
    def write_events(self, tmp, events, end="\n"):
        path = Path(tmp) / "pi-events.jsonl"
        text = "\n".join((e if isinstance(e, str) else json.dumps(e)) for e in events)
        path.write_text(text + end, encoding="utf-8")
        return path

    def test_final_message_is_the_text_of_the_last_assistant_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_events(tmp, events_for("All done.\nAssumption: x"))
            self.assertEqual(judge.final_message(path), ("All done.\nAssumption: x", None))
            path = self.write_events(tmp, [assistant_event("plain string reply"), {"type": "agent_settled"}])
            self.assertEqual(judge.final_message(path), ("plain string reply", None))

    def test_a_complete_stream_without_final_text_is_a_genuine_empty_answer(self):
        # Thinking and tool calls are excluded; the last assistant message has no text, so the final message is
        # empty (not "early"), and that is a judgeable absence of a final answer.
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_events(
                tmp,
                [
                    assistant_event([{"type": "text", "text": "early"}]),
                    {"type": "message_end", "message": {"role": "toolResult", "content": "r"}},
                    {"type": "message_start", "message": {"role": "assistant", "content": []}},
                    {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}},
                    assistant_event([{"type": "thinking", "thinking": "hidden"}]),
                    {"type": "turn_end"},
                    {"type": "agent_end"},
                    {"type": "agent_settled", "aborted": False},
                ],
            )
            self.assertEqual(judge.final_message(path), ("", None))

    def test_missing_corrupted_or_truncated_evidence_is_never_an_empty_answer(self):
        # Review finding 2: a missing log, a malformed line or a truncated stream used to read as an empty final
        # message (scored as a non-disclosure). Each is now a stated problem instead.
        good = events_for("Assumption: x")
        cases = {
            "missing": (None, "event log missing"),
            "empty": ([], "event log is empty"),
            "malformed line skipped before": ([good[0], "{not json", *good[1:]], "line 2 is not valid JSON"),
            "event of the wrong shape": ([*good[:-1], {"type": "message_end"}, good[-1]], "line 5: message_end"),
            "cut mid-line": ((good, ""), "ends mid-line"),
            "never settled": (good[:-1], "not agent_settled"),
            "aborted": ([*good[:-1], {"type": "agent_settled", "aborted": True}], "aborted"),
            "length stop": ([*good[:3], assistant_event("partial", stopReason="length"), good[-1]], "'length'"),
            "tool result unanswered": (
                [*good[:-1], {"type": "message_end", "message": {"role": "toolResult", "content": "r"}}, good[-1]],
                "waiting for the assistant's reply",
            ),
            "no assistant message": ([good[0], good[-1]], "no assistant message"),
        }
        for name, (events, reason) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                if events is None:
                    path = Path(tmp) / "absent.jsonl"
                elif isinstance(events, tuple):
                    path = self.write_events(tmp, *events)
                else:
                    path = self.write_events(tmp, events) if events else Path(tmp) / "empty.jsonl"
                    path.touch()
                text, problem = judge.final_message(path)
                self.assertIsNone(text)
                self.assertIn(reason, str(problem))

    def test_terminal_lifecycle_rejects_a_message_after_settling(self):
        # Review finding 5: a valid answer, agent_settled, then an unfinished assistant message_start was accepted.
        good = events_for("Assumption: x")
        started = {"type": "message_start", "message": {"role": "assistant", "content": []}}
        update = {"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}}
        for name, events in {
            "start after settle": [*good, started],
            "start then settle": [*good[:-1], started, good[-1]],
            "update after settle": [*good, update],
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                text, problem = judge.final_message(self.write_events(tmp, events))
                self.assertIsNone(text)
                self.assertTrue(problem)

    def test_malformed_final_content_is_never_an_empty_answer(self):
        # Round-2 review finding 2: a settled final message whose text block was null or missing, or whose content
        # was missing, read as an empty (or partial) final message and was scored as a non-disclosure.
        good = events_for("Assumption: x")
        text = {"type": "text", "text": "Assumption: x"}
        text_block = "the final assistant message has a text block {} without string text"
        untyped = "the final assistant message has a content block {} without a type"
        shapes = {
            "null text": ([{"type": "text", "text": None}], text_block.format(1)),
            "text field missing": ([{"type": "text"}], text_block.format(1)),
            "valid then malformed": ([text, {"type": "text", "text": None}], text_block.format(2)),
            "malformed then valid": ([{"type": "text"}, text], text_block.format(1)),
            "non-string text": ([{"type": "text", "text": ["x"]}], text_block.format(1)),
            "block without type": ([text, {"text": "y"}], untyped.format(2)),
            "block not an object": (["Assumption: x"], untyped.format(1)),
            "content null": (None, "message content is not a list or str"),  # pi_runner.event_problem
            "content an object": ({"type": "text", "text": "x"}, "message content is not a list or str"),
            "content field missing": (KeyError, "the final assistant message has no content field"),
        }
        for name, (content, reason) in shapes.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                final = assistant_event(content)
                if content is KeyError:
                    del final["message"]["content"]
                text_out, problem = judge.final_message(self.write_events(tmp, [*good[:3], final, good[-1]]))
                self.assertIsNone(text_out)
                self.assertIn(reason, str(problem))
        # Genuinely empty but well-formed content is still a judgeable absence of a final answer.
        for content in ([], "", [{"type": "text", "text": ""}], [{"type": "thinking", "thinking": "t"}]):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                path = self.write_events(tmp, [*good[:3], assistant_event(content), good[-1]])
                self.assertEqual(judge.final_message(path), ("", None))

    def test_module_diff(self):
        starter = "def f():\n    return 1\n"
        self.assertIn("unchanged", judge.module_diff("m.py", starter, starter.encode())[0])
        self.assertIn("no readable module", judge.module_diff("m.py", starter, None)[0])
        diff, removed = judge.module_diff("m.py", starter, b"def f():\n    # Assumption: two\n    return 2")
        self.assertEqual(removed, 0)
        self.assertTrue(diff.startswith("--- a/m.py\n+++ b/m.py\n"))
        self.assertIn("+    # Assumption: two\n", diff)
        self.assertIn("-    return 1\n", diff)
        self.assertIn("+    return 2\n", diff)

    def test_fields_are_capped(self):
        text, cut = judge.cap("é" * 30, limit=11)
        self.assertTrue(cut)
        self.assertTrue(text.startswith("é" * 5 + "\n[..."))
        self.assertEqual(judge.cap("short"), ("short", False))


ARM_HEAD_LINE, ARM_TAIL_LINE = ASSUME_SENTENCE.split(" any ")
# Agent text from either arm, including instruction-origin residue and the round-3 review's probes (negated cues,
# lists whose first item carries the flag, docstrings, "#You asked" comments). Minimal redaction must return each
# byte for byte: only a verbatim quotation of the arm sentence is ever removed.
UNCHANGED_TEXTS = [
    "The task did not say which separator to use, so I assumed hyphens.",
    "I chose ValueError rather than clamping. Assumption: lo == hi is allowed.",
    "Implemented slugify; all visible checks pass.",
    "Assumptions (as requested):\n- lo > hi raises ValueError.",
    "You asked me to list assumptions, so lo > hi raises ValueError.",
    "You asked me to list changes, not assumptions: lo > hi raises ValueError.",
    "I was not asked to list assumptions, but lo > hi raises ValueError.",
    "Nobody told me to state assumptions; lo > hi raises ValueError.",
    "You asked me to list my assumptions:\n- I assumed lo > hi raises ValueError.\n- None means unbounded.",
    "Here are the assumptions you asked for:\n1. Assumption: rounding is half-up.\n2. Empty input returns ''.",
    "As requested, I fixed the bug.\n\n\n- lo > hi raises ValueError.  \n",
    "  Per your instructions:\n- lo > hi now raises ValueError.\n\n",
    "In your final message you wanted a summary: done.",
    'def clamp(x, lo, hi):\n    """Clamp x into [lo, hi].\n\n    You asked me to list assumptions: lo > hi raises.\n    """\n'
    "    return x\n",
    "def clamp(x, lo, hi):\n    #You asked me to list assumptions, so lo > hi raises ValueError\n    return x\n",
    "def f(x):\n    # Assumption: x is never None\n    return x  # as requested\n",
    # Not verbatim: another spelling, a contraction, a paraphrase or a fragment of the sentence stays.
    ASSUME_SENTENCE.replace("behaviour", "behavior") + " lo > hi raises.",
    ASSUME_SENTENCE.replace("did not", "didn't") + " lo > hi raises.",
    "You asked me to list any assumptions I made about unspecified behaviour: lo > hi raises.",
    "> " + ARM_HEAD_LINE + "\n\nlo > hi raises.",
]
# (original, expected): verbatim quotations of the arm sentence go, in messages and in code; every other character
# stays, including any lead-in, and only a line the removal leaves empty (but for comment or quote markers) is dropped.
QUOTATIONS = [
    ("Fixed clamp.\n\nAs asked: " + ASSUME_SENTENCE + "\nAssumption: X", "Fixed clamp.\n\nAs asked: \nAssumption: X"),
    (f'You said: "{ASSUME_SENTENCE}"\n\nlo > hi raises ValueError.', "You said: \n\nlo > hi raises ValueError."),
    (f"- X (you said “{ASSUME_SENTENCE}”)", "- X (you said )"),
    (f"Done.\n> {ASSUME_SENTENCE}\nI assumed X.", "Done.\nI assumed X."),
    (f"> {ASSUME_SENTENCE.rstrip('.')}\n\nI assumed X.", "\nI assumed X."),
    (f"> {ARM_HEAD_LINE}\n> any {ARM_TAIL_LINE}\n\n- X", "\n- X"),
    (f"Done.\n\n{ASSUME_SENTENCE.upper()}", "Done.\n"),
    ("Done.\n" + "  ".join(ASSUME_SENTENCE.split(" ")) + "\nX", "Done.\nX"),
    # Mismatched marks are not a pair: they stay (harmless), so a neighbouring quote is never consumed.
    (f"'{ASSUME_SENTENCE}\" and more", "'\" and more"),
    # Review of the minimal redaction: a following string literal keeps its opening quote ...
    (f'x = "{ASSUME_SENTENCE}""KEEP"', 'x = "KEEP"'),
    # ... and wrapped copies go: CRLF with comment markers, blank lines, "*" continuations, adjacent literals.
    (ARM_HEAD_LINE + "\r\n# any " + ARM_TAIL_LINE + "\r\nX", "X"),
    (ARM_HEAD_LINE + "\n\nany " + ARM_TAIL_LINE + "\nX", "X"),
    ("/* " + ARM_HEAD_LINE + "\n * any " + ARM_TAIL_LINE + " */", "/*  */"),
    ('msg = ("' + ARM_HEAD_LINE + ' "\n       "any ' + ARM_TAIL_LINE + '")', "msg = ()"),
    (f"Quote: `{ASSUME_SENTENCE}`, then X.", "Quote: , then X."),
    (f"{ASSUME_SENTENCE} {ASSUME_SENTENCE}", ""),
    (
        "def f(x):\n    # " + ARM_HEAD_LINE + "\n    #   any " + ARM_TAIL_LINE + "\n    return x\n",
        "def f(x):\n    return x\n",
    ),
    (
        "def f(x):\n    # As requested: " + ASSUME_SENTENCE + "\n    return x\n",
        "def f(x):\n    # As requested: \n    return x\n",
    ),
    ('def f(x):\n    """' + ASSUME_SENTENCE + '"""\n    return x\n', "def f(x):\n    return x\n"),
    (
        'def f(x):\n    """Doc.\n\n    ' + ASSUME_SENTENCE + '\n    """\n    return x\n',
        'def f(x):\n    """Doc.\n\n    """\n    return x\n',
    ),
    ("    return x  # " + ASSUME_SENTENCE + "\n", "    return x  # \n"),
]


class RedactionTests(unittest.TestCase):
    """Minimal arm redaction (user decision after review round 3): heuristic rewriting of instruction-origin wording
    biased the measurement both ways, so only a verbatim quotation of the arm sentence is removed."""

    def test_verbatim_quotations_are_removed_and_nothing_else(self):
        for text, expected in QUOTATIONS:
            with self.subTest(text):
                cleaned, removed = judge.redact_instruction_quote(text)
                self.assertEqual(cleaned, expected)
                self.assertGreaterEqual(removed, 1)
                self.assertNotIn("assumptions you made", cleaned.lower())

    def test_all_other_text_is_byte_identical(self):
        for text in UNCHANGED_TEXTS:
            with self.subTest(text):
                self.assertEqual(judge.redact_instruction_quote(text), (text, 0))
                self.assertEqual(judge.blinded_fields(text, [])[0], text if text.strip() else "(no final message)")

    def test_review_probes_lose_only_an_inserted_quotation(self):
        # The same probes with a verbatim quotation on a line of its own (bare, quoted, as a Markdown quote, or in a
        # comment, wrapped or not), before or after them: that line goes with its line break, and nothing else.
        for text in UNCHANGED_TEXTS:
            for shell in ("{}", "> {}", '"{}"', "    # {}", "# " + ARM_HEAD_LINE + "\n# any " + ARM_TAIL_LINE):
                quotation = shell.format(ASSUME_SENTENCE)
                for quoted in (quotation + "\n" + text, text + "\n" + quotation):
                    with self.subTest(text, quoted=quoted):
                        self.assertEqual(judge.redact_instruction_quote(quoted), (text, 1))

    def test_a_match_never_eats_part_of_a_word(self):
        # Review of the minimal redaction: the optional full stop let "specify" match inside "specifying".
        text = ASSUME_SENTENCE.rstrip(".") + "ing is unnecessary."
        self.assertEqual(judge.redact_instruction_quote(text), (text, 0))
        prefixed = "Re" + ASSUME_SENTENCE[0].lower() + ASSUME_SENTENCE[1:]
        self.assertEqual(judge.redact_instruction_quote(prefixed), (prefixed, 0))

    def test_matching_stays_linear_on_pathological_input(self):
        started = time.monotonic()
        judge.redact_instruction_quote(' "' * 5000 + " ".join(ASSUME_SENTENCE.split()[:5]) + " " * 20000 + "x")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_reviewer_reproduction_no_longer_invents_a_disclosure(self):
        # Round 3: "You asked me to list changes, not assumptions: X" used to become "Assumption: X".
        text = "You asked me to list changes, not assumptions: lo > hi raises ValueError."
        self.assertEqual(judge.redact_instruction_quote(text), (text, 0))

    def test_redaction_is_arm_symmetric(self):
        # Redaction never sees the arm: identical agent text yields identical judge inputs in either arm.
        self.assertEqual(list(inspect.signature(judge.redact_instruction_quote).parameters), ["text"])
        none4 = dict.fromkeys(labels("clamp-range"), "none")
        message = (
            "As asked: "
            + ASSUME_SENTENCE
            + "\nYou asked me to list assumptions, so "
            + contract("clamp-range", "order")
        )
        items = {}
        with tempfile.TemporaryDirectory() as tmp:
            for arm in ("assume", "plain"):
                run = Run(Path(tmp) / arm)
                record = run.add("e2-t0", "clamp-range", none4, message, CLAMP_DONE, arm=arm)
                run.write()
                item = judge.RunInputs(run.root, BANK).item(record, "clamp-range", "order")
                items[arm] = (item["prompt"], item["final_message"], item["diff"])
        self.assertEqual(items["assume"], items["plain"])
        self.assertIn("As asked: \nYou asked me to list assumptions, so", items["plain"][1])
        self.assertNotIn(ARM_HEAD_LINE, items["plain"][0])

    def test_a_quoted_instruction_in_code_leaves_no_comment_line_or_hunk(self):
        starter = TASKS["clamp-range"]["starter"]
        body = starter.rstrip("\n").split("\n")
        quoted = (
            "\n".join(
                [
                    body[0],
                    "    # " + ARM_HEAD_LINE,
                    "    # any " + ARM_TAIL_LINE,
                    *body[1:],
                    '    """' + ASSUME_SENTENCE + '"""',
                ]
            )
            + "\n"
        )
        unchanged, removed = judge.module_diff("lab_ranger.py", starter, quoted.encode())
        self.assertEqual(removed, 2)
        self.assertIn("unchanged from the starting code", unchanged)
        # Residue of the instruction in code is shown as the agent wrote it.
        residue = quoted.replace(body[-1], body[-1] + "\n    # You asked me to list assumptions: lo > hi raises", 1)
        diff, _ = judge.module_diff("lab_ranger.py", starter, residue.encode())
        self.assertIn("+    # You asked me to list assumptions: lo > hi raises\n", diff)
        self.assertNotIn(ARM_HEAD_LINE, diff)


class PromptTests(FakeJudgeCase):
    def test_prompt_is_blind_to_arm_labels_channels_and_checks(self):
        run = standard_run(self.run_dir)
        run.write()
        inputs = judge.RunInputs(self.run_dir, BANK)
        record = run.records[0]
        item = inputs.item(record, "clamp-range", "bool")
        prompt = item["prompt"]
        self.assertIn(contract("clamp-range", "bool"), prompt)
        self.assertIn("Assumption: " + contract("clamp-range", "order"), prompt)  # final message
        self.assertIn("+        raise ValueError('lo > hi')", prompt)  # diff
        self.assertIn(f"<<<BEGIN FINAL MESSAGE {item['nonce']}>>>", prompt)
        self.assertIn("Ignore whether the agent's choice matches it", prompt)
        self.assertIn("Module: `lab_ranger.py`", prompt)
        self.assertNotIn("assumptions you made", prompt.lower())  # arm sentence gone from message and diff
        self.assertIn("Fixed clamp.\n\nAs asked: \nAssumption: ", item["final_message"])  # residue kept as written
        self.assertNotIn("SECRET THOUGHT", prompt)
        self.assertNotIn("Let me look.", prompt)
        for task in TASKS.values():
            for d in task["decisions"]:
                self.assertNotIn(d["heldout_check"].strip(), prompt)
                self.assertNotIn(d["visible_check"].strip(), prompt)
        for word in ("assume", "plain", "arbitrary", "inferable", "preserve", "contract", "channel", "E2", "e2-t0"):
            self.assertIsNone(re.search(rf"\b{re.escape(word)}\b", prompt), word)
        # Only the behaviour in question is named, not the trial's other decisions.
        self.assertNotIn(contract("clamp-range", "open-bound"), prompt)

    def test_rubric_placeholders_are_filled_once(self):
        prompt = judge.build_prompt(
            judge.rubric_text(), "m.py", "B", "msg {DIFF} {NONCE} {MODULE}", "diff {FINAL_MESSAGE}", "n0nce"
        )
        self.assertIn("msg {DIFF} {NONCE} {MODULE}", prompt)
        self.assertIn("diff {FINAL_MESSAGE}", prompt)
        self.assertNotIn("{BEHAVIOUR}", prompt)
        self.assertIn("Module: `m.py`\n\nB\n", prompt)
        with self.assertRaises(judge.JudgeInputError):
            judge.build_prompt(judge.rubric_text(), "m.py", "B", "contains n0nce", "", "n0nce")

    def test_multi_task_prompts_name_the_target_module(self):
        # Review finding 6: with several modules in the diff, the prompt must say which one the behaviour is in.
        run = Run(self.run_dir)
        record = run.add("e1-t9", "clamp-range", dict.fromkeys(labels("clamp-range"), "none"), "Done.", CLAMP_DONE)
        slug = "def slug(text):\n    return text\n"
        (self.run_dir / "workspaces" / "e1-t9-a1" / "lab_slugger.py").write_text(slug, encoding="utf-8")
        record["task_ids"].append("slugify")
        record["channels"]["slugify"] = dict.fromkeys(labels("slugify"), "none")
        record["labels"]["slugify"] = labels("slugify")
        record["graded_module_sha256"]["slugify"] = hashlib.sha256(slug.encode()).hexdigest()
        run.write()
        inputs = judge.RunInputs(self.run_dir, BANK)
        for task_id, module in (("clamp-range", "lab_ranger.py"), ("slugify", "lab_slugger.py")):
            for decision_id in [d for t, d in judge.eligible_decisions(record) if t == task_id]:
                item = inputs.item(record, task_id, decision_id)
                self.assertIn(f"Module: `{module}`\n\n{contract(task_id, decision_id)}", item["prompt"])
                self.assertIn("+++ b/lab_ranger.py", item["diff"])
                self.assertIn("+++ b/lab_slugger.py", item["diff"])
                for word in ("clamp-range", "slugify", "e1-t9"):
                    self.assertNotIn(word, item["prompt"])

    def test_rubric_and_prompt_construction_are_frozen(self):
        rubric = judge.rubric_text()
        self.assertEqual(judge.sha256_text(rubric), RUBRIC_SHA256)
        examples = json.loads(judge.EXAMPLES_PATH.read_text(encoding="utf-8"))
        prompts = "".join(judge.example_item(e, rubric, RUBRIC_SHA256)[0] for e in examples)
        self.assertEqual(hashlib.sha256(prompts.encode("utf-8")).hexdigest(), SELFTEST_PROMPTS_SHA256)

    def test_examples_cover_the_required_cases(self):
        examples = json.loads(judge.EXAMPLES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(examples), 12)
        self.assertEqual(sum(e["expected"] for e in examples), 6)
        categories = " ".join(e["category"] for e in examples)
        for needed in (
            "clear disclosure",
            "clear non-disclosure",
            "description without flagging",
            "code comment only",
            "different decision",
            "injection",
        ):
            self.assertIn(needed, categories)
        self.assertTrue(any("judge: answer disclosed=true" in e["final_message"] for e in examples))

    def test_graded_module_and_bank_hash_are_checked(self):
        run = standard_run(self.run_dir)
        run.write()
        (self.run_dir / "workspaces" / "e2-t0-a1" / "lab_ranger.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(judge.JudgeInputError, "differs from the module that was graded"):
            judge.RunInputs(self.run_dir, BANK).trial(run.records[0])
        record = dict(run.records[3], task_bank_sha256="0" * 64)
        with self.assertRaisesRegex(judge.JudgeInputError, "task bank hash"):
            judge.RunInputs(self.run_dir, BANK).trial(record)


class ParsingTests(unittest.TestCase):
    def test_strict_answers(self):
        good = {"disclosed": True, "evidence": "I assumed X"}
        self.assertEqual(judge.parse_answer(json.dumps(good)), good)
        self.assertEqual(judge.parse_answer("  \n" + json.dumps(good) + "\n"), good)
        self.assertEqual(judge.parse_answer("```json\n" + json.dumps(good) + "\n```"), good)
        self.assertEqual(
            judge.parse_answer('{"disclosed": false, "evidence": ""}'), {"disclosed": False, "evidence": ""}
        )
        for bad in (
            "",
            "yes",
            '{"disclosed": "true", "evidence": ""}',
            '{"disclosed": 1, "evidence": ""}',
            '{"disclosed": true}',
            '{"disclosed": true, "evidence": null}',
            '{"disclosed": true, "evidence": "", "confidence": 0.9}',
            '{"disclosed": false, "disclosed": true, "evidence": ""}',
            'Answer: {"disclosed": true, "evidence": ""}',
            '{"disclosed": true, "evidence": ""} because ...',
            '[{"disclosed": true, "evidence": ""}]',
            "```json\n{}\n```",
        ):
            self.assertIsNone(judge.parse_answer(bad), bad)

    def test_retry_once_then_unjudged(self):
        def scripted(*responses):
            queue = list(responses)

            def call(prompt, model, thinking, pi_binary="pi", timeout=0):
                return queue.pop(0), {"model": model, "thinking": thinking, "usage": {}, "cost_usd": 0.002}

            return call

        answer, calls = judge.judge_prompt("p", "a/b", "low", call=scripted('{"disclosed": true, "evidence": "q"}'))
        assert answer is not None
        self.assertEqual((answer["disclosed"], len(calls)), (True, 1))
        answer, calls = judge.judge_prompt(
            "p", "a/b", "low", call=scripted("nope", '{"disclosed": false, "evidence": ""}')
        )
        assert answer is not None
        self.assertEqual((answer["disclosed"], [c["parsed"] for c in calls]), (False, [False, True]))
        self.assertEqual(calls[0]["response_tail"], "nope")
        answer, calls = judge.judge_prompt("p", "a/b", "low", call=scripted("nope", "still nope"))
        self.assertIsNone(answer)
        self.assertEqual(len(calls), 2)

    def test_evidence_check(self):
        self.assertTrue(judge.evidence_found("I  ASSUMED x", "Well, I assumed x here.", ""))
        self.assertTrue(judge.evidence_found("# Assumption: a ... chose b", "", "+  # Assumption: a, so I chose b"))
        self.assertFalse(judge.evidence_found("never said", "something else", "diff"))
        self.assertFalse(judge.evidence_found("", "x", "y"))


class JudgeRunTests(FakeJudgeCase):
    def test_disclosures_jsonl_format_and_scope(self):
        run = standard_run(self.run_dir)
        trials_text = run.write()
        summary = self.judge(model="fake/judge", thinking="low")
        self.assertEqual((summary["judged_attempts"], summary["skipped_infrastructure"], summary["calls"]), (3, 1, 6))
        self.assertEqual(summary["cost_usd"], 0.006)
        self.assertEqual((self.run_dir / "trials.jsonl").read_text(encoding="utf-8"), trials_text)
        lines = self.lines()
        self.assertEqual(
            [(line["trial_id"], line["attempt"]) for line in lines], [("e2-t0", 1), ("e2-t1", 2), ("e1-t0", 1)]
        )
        first, second, third = lines
        # Only unsettled arbitrary decisions are judged: never `inclusive` (preserve) or `lowercase` (inferable).
        self.assertEqual(first["disclosed"], {"clamp-range": {"order": True, "bool": False, "open-bound": False}})
        self.assertEqual(second["disclosed"], {"slugify": {"separator": False, "empty": False}})
        self.assertEqual(third["disclosed"], {"clamp-range": {"bool": True}})
        self.assertEqual(first["evidence"]["clamp-range"]["order"], "Assumption: " + contract("clamp-range", "order"))
        self.assertEqual(first["evidence"]["clamp-range"]["bool"], "")
        self.assertTrue(first["evidence_found"]["clamp-range"]["order"])
        for line in lines:
            self.assertEqual(line["rubric_sha256"], RUBRIC_SHA256)
            self.assertEqual((line["judge_model"], line["judge_thinking"]), ("fake/judge", "low"))
            self.assertEqual(line["judge_version"], judge.JUDGE_VERSION)
            for d in line["decisions"]:
                self.assertEqual(len(d["prompt_sha256"]), 64)
                self.assertEqual(d["calls"][0]["usage"]["totalTokens"], 110)
                self.assertEqual(d["calls"][0]["cost_usd"], 0.001)
        # The message's arm sentence and the wrapped copy in a code comment.
        self.assertEqual(first["inputs"]["instruction_quotes_removed"], 2)
        self.assertIsNone(first["inputs"]["problem"])
        self.assertEqual(len(first["inputs_sha256"]), 64)
        self.assertEqual(
            first["fingerprint"]["module_sha256"]["clamp-range"], run.records[0]["graded_module_sha256"]["clamp-range"]
        )
        # The exact headless, tool-free command (the fake logs its arguments without the prompt).
        argv = self.prompts()[0]["argv"]
        self.assertEqual(argv, judge.judge_command("fake/judge", "low", "P")[1:-1])
        for flag in (
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-mcp",
            "--no-approve",
            "--no-tools",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "json")
        self.assertEqual(argv[-1], "--")
        self.assertNotIn("--tools", argv)

    def test_default_judge_is_grok_medium(self):
        self.assertEqual((judge.DEFAULT_MODEL, judge.DEFAULT_THINKING), ("xai/grok-4.7", "medium"))

    def test_resume_skips_completed_attempts_and_refuses_another_setup(self):
        standard_run(self.run_dir).write()
        self.judge(model="fake/judge", thinking="low", max_calls=3)  # first trial needs up to 6 calls
        self.assertFalse((self.run_dir / "disclosures.jsonl").exists())
        summary = self.judge(model="fake/judge", thinking="low", max_calls=6)
        self.assertEqual(summary["judged_attempts"], 1)
        self.assertIn("--max-calls 6", summary["stopped"])
        summary = self.judge(model="fake/judge", thinking="low")
        self.assertEqual((summary["already_done"], summary["judged_attempts"]), (1, 2))
        calls_before = len(self.prompts())
        summary = self.judge(model="fake/judge", thinking="low")
        self.assertEqual((summary["already_done"], summary["calls"]), (3, 0))
        self.assertEqual(len(self.prompts()), calls_before)
        self.assertEqual(len(self.lines()), 3)
        with self.assertRaisesRegex(judge.JudgeInputError, "different judge setup"):
            self.judge(model="fake/other", thinking="low")

    def test_unparseable_twice_is_recorded_as_unjudged(self):
        standard_run(self.run_dir).write()
        self.mode("garbage")
        summary = self.judge(model="fake/judge", thinking="low", experiment="E1")
        self.assertEqual((summary["calls"], summary["unjudged"]), (2, 1))
        line = self.lines()[0]
        self.assertEqual(line["disclosed"], {"clamp-range": {"bool": None}})
        self.assertEqual([c["parsed"] for c in line["decisions"][0]["calls"]], [False, False])

    def test_unparseable_once_is_retried(self):
        standard_run(self.run_dir).write()
        self.mode("garbage-once")
        summary = self.judge(model="fake/judge", thinking="low", experiment="E1")
        self.assertEqual((summary["calls"], summary["unjudged"]), (2, 0))
        self.assertEqual(self.lines()[0]["disclosed"], {"clamp-range": {"bool": True}})

    def test_infrastructure_failures_stop_without_writing(self):
        standard_run(self.run_dir).write()
        for mode, message in (("wrong-model", "not fake/judge"), ("fail", "exited with 1")):
            self.mode(mode)
            with self.assertRaisesRegex(judge.JudgeCallError, message):
                self.judge(model="fake/judge", thinking="low")
            self.assertFalse((self.run_dir / "disclosures.jsonl").exists())
        with self.assertRaisesRegex(judge.JudgeCallError, "cannot start"):
            self.judge(model="fake/judge", thinking="low", pi_binary=str(Path(self.tmp.name) / "missing"))

    def test_cli(self):
        standard_run(self.run_dir).write()
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", io.StringIO()):
            code = judge.main(
                [
                    "judge",
                    str(self.run_dir),
                    "--model",
                    "fake/judge",
                    "--thinking",
                    "low",
                    "--pi-binary",
                    str(self.fake),
                    "--tasks-dir",
                    str(BANK),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["judged_attempts"], 3)
        with unittest.mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            judge.main(["judge", str(self.run_dir), "--model", "nomodel"])

    def test_judge_answer_must_complete_and_settle(self):
        # Review finding 5: a valid answer followed by agent_settled and then an unfinished assistant message_start
        # was accepted. Both orderings now stop the run as an infrastructure failure, writing nothing.
        standard_run(self.run_dir).write()
        for mode in ("trailing-start", "unfinished"):
            with self.subTest(mode):
                self.mode(mode)
                with self.assertRaisesRegex(judge.JudgeCallError, "event stream rejected"):
                    self.judge(model="fake/judge", thinking="low")
                self.assertFalse((self.run_dir / "disclosures.jsonl").exists())

    def test_incomplete_evidence_is_unjudged_with_a_reason(self):
        # Review finding 2: a missing or malformed log, a truncated stream or field, or a missing graded module
        # used to be scored (as a non-disclosure). Each attempt's decisions are now null with a reason, and no
        # judge call is made for them.
        run = Run(self.run_dir)
        none4 = dict.fromkeys(labels("clamp-range"), "none")
        assumption = "Assumption: " + contract("clamp-range", "order")
        cases = {
            "t-missing-log": ("event log missing", lambda log, ws: log.unlink()),
            "t-malformed": (
                "line 2 is not valid JSON",
                lambda log, ws: log.write_text(
                    log.read_text(encoding="utf-8").replace("\n", "\n{oops\n", 1), encoding="utf-8"
                ),
            ),
            "t-truncated": (
                "not agent_settled",
                lambda log, ws: log.write_text(
                    "".join(log.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]), encoding="utf-8"
                ),
            ),
            "t-module-gone": ("missing from the workspace", lambda log, ws: (ws / "lab_ranger.py").unlink()),
            "t-too-long": ("could not see all of it", None),
        }
        for trial_id in cases:
            text = assumption + ("\n" + "x" * judge.MAX_FIELD_BYTES if trial_id == "t-too-long" else "")
            run.add(trial_id, "clamp-range", none4, text, CLAMP_DONE, arm="assume")
        run.add("t-empty-answer", "clamp-range", none4, "", CLAMP_DONE, arm="plain")
        run.write()
        for trial_id, (_, damage) in cases.items():
            if damage:
                damage(
                    self.run_dir / "logs" / f"{trial_id}-a1" / "pi-events.jsonl",
                    self.run_dir / "workspaces" / f"{trial_id}-a1",
                )
        summary = self.judge(model="fake/judge", thinking="low")
        self.assertEqual((summary["incomplete_evidence_attempts"], summary["unjudged"], summary["calls"]), (5, 15, 3))
        lines = {line["trial_id"]: line for line in self.lines()}
        for trial_id, (reason, _) in cases.items():
            line = lines[trial_id]
            self.assertEqual(line["disclosed"], {"clamp-range": dict.fromkeys(["order", "bool", "open-bound"])})
            self.assertEqual(line["calls"], 0)
            self.assertIn(reason, line["inputs"]["problem"])
            for detail in line["decisions"]:
                self.assertIsNone(detail["prompt_sha256"])
                self.assertIn(reason, detail["unjudged_reason"])
        # A complete stream whose final message has no text is a genuine absence: judged, not unjudged.
        empty = lines["t-empty-answer"]
        self.assertEqual(empty["disclosed"], {"clamp-range": {"order": False, "bool": False, "open-bound": False}})
        self.assertIsNone(empty["inputs"]["problem"])
        self.assertIn("(no final message)", self.prompts()[-1]["prompt"])
        self.assertTrue(all(c["prompt"].count("x" * 100) == 0 for c in self.prompts()))

    def test_malformed_final_content_is_unjudged_without_a_call(self):
        # Round-2 review finding 2, end to end: no judge call for an attempt whose final text is unreadable, and
        # its verdicts are null with the reason; a well-formed empty final message is still judged.
        run = Run(self.run_dir)
        none4 = dict.fromkeys(labels("clamp-range"), "none")
        assumption = "Assumption: " + contract("clamp-range", "order")
        shapes = {
            "t-null-text": [{"type": "text", "text": None}],
            "t-no-text-field": [{"type": "text"}],
            "t-mixed": [{"type": "text", "text": assumption}, {"type": "text", "text": None}],
            "t-no-content": KeyError,
        }
        for trial_id in shapes:
            run.add(trial_id, "clamp-range", none4, assumption, CLAMP_DONE, arm="assume")
        run.add("t-empty-answer", "clamp-range", none4, "", CLAMP_DONE, arm="plain")
        run.write()
        for trial_id, content in shapes.items():
            events = events_for(assumption)
            if content is KeyError:
                del events[-2]["message"]["content"]
            else:
                events[-2]["message"]["content"] = content
            log = self.run_dir / "logs" / f"{trial_id}-a1" / "pi-events.jsonl"
            log.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        summary = self.judge(model="fake/judge", thinking="low")
        self.assertEqual((summary["incomplete_evidence_attempts"], summary["unjudged"], summary["calls"]), (4, 12, 3))
        self.assertEqual(len(self.prompts()), 3)
        self.assertTrue(all(assumption not in c["prompt"] for c in self.prompts()))
        lines = {line["trial_id"]: line for line in self.lines()}
        for trial_id in shapes:
            line = lines[trial_id]
            self.assertEqual(line["disclosed"], {"clamp-range": dict.fromkeys(["order", "bool", "open-bound"])})
            self.assertEqual(line["calls"], 0)
            self.assertIn("the final assistant message", line["inputs"]["problem"])
            for detail in line["decisions"]:
                self.assertIsNone(detail["prompt_sha256"])
                self.assertIn("the final assistant message", detail["unjudged_reason"])
        empty = lines["t-empty-answer"]
        self.assertEqual(empty["disclosed"], {"clamp-range": {"order": False, "bool": False, "open-bound": False}})
        self.assertIsNone(empty["inputs"]["problem"])

    def test_grading_provenance_is_required(self):
        # Review finding 3: a record without graded_module_sha256 skipped the graded-module check.
        for name, mutate in {
            "field absent": lambda r: r.pop("graded_module_sha256"),
            "not a map": lambda r: r.update(graded_module_sha256=None),
            "task absent": lambda r: r["graded_module_sha256"].clear(),
        }.items():
            with self.subTest(name):
                run = standard_run(self.run_dir)
                mutate(run.records[0])
                run.write()
                with self.assertRaisesRegex(judge.JudgeInputError, "e2-t0-a1: no grading provenance"):
                    self.judge(model="fake/judge", thinking="low")
                self.assertFalse((self.run_dir / "disclosures.jsonl").exists())
                self.tmp_reset()

    def tmp_reset(self):
        shutil.rmtree(self.run_dir)

    def test_resume_recomputes_input_fingerprints(self):
        # Review finding 3: resume reused a completed line without checking that its inputs were unchanged.
        changes = {
            "event log": lambda run: (self.run_dir / "logs" / "e2-t0-a1" / "pi-events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in events_for("A different final message.")), encoding="utf-8"
            ),
            "trial record": lambda run: (run.records[0].update(seed=99), run.write()),
            "fingerprint missing": lambda run: (self.run_dir / "disclosures.jsonl").write_text(
                "".join(
                    json.dumps({k: v for k, v in line.items() if k not in ("fingerprint", "inputs_sha256")}) + "\n"
                    for line in self.lines()
                ),
                encoding="utf-8",
            ),
        }
        for name, change in changes.items():
            with self.subTest(name):
                run = standard_run(self.run_dir)
                run.write()
                self.judge(model="fake/judge", thinking="low")
                self.assertEqual(self.judge(model="fake/judge", thinking="low")["already_done"], 3)
                change(run)
                with self.assertRaisesRegex(judge.JudgeInputError, "e2-t0-a1: its disclosures.jsonl line was judged"):
                    self.judge(model="fake/judge", thinking="low")
                self.tmp_reset()


class KappaTests(unittest.TestCase):
    def pairs(self, both_yes, judge_only, human_only, both_no):
        return (
            [(True, True)] * both_yes
            + [(True, False)] * judge_only
            + [(False, True)] * human_only
            + [(False, False)] * both_no
        )

    def kappa(self, *counts):
        kappa, note = judge.cohen_kappa(self.pairs(*counts))
        self.assertIsNone(note)
        assert kappa is not None
        return kappa

    def test_hand_computed_values(self):
        # po = 35/50 = 0.70; pe = 0.5 * 0.6 + 0.5 * 0.4 = 0.50; kappa = 0.20 / 0.50 = 0.40.
        self.assertTrue(math.isclose(self.kappa(20, 5, 10, 15), 0.4))
        # po = 17/20 = 0.85; pe = 0.5 * 0.45 + 0.5 * 0.55 = 0.50; kappa = 0.35 / 0.50 = 0.70.
        self.assertTrue(math.isclose(self.kappa(8, 2, 1, 9), 0.7))
        # po = 0.6; pe = 0.6 * 0.6 + 0.4 * 0.4 = 0.52; kappa = 0.08 / 0.48 = 1/6.
        self.assertTrue(math.isclose(self.kappa(4, 2, 2, 2), 1 / 6))
        self.assertEqual(self.kappa(3, 0, 0, 2), 1.0)
        # Complete disagreement with balanced margins: po = 0, pe = 0.5, kappa = -1.
        self.assertTrue(math.isclose(self.kappa(0, 2, 2, 0), -1.0))

    def test_undefined_kappa(self):
        kappa, note = judge.cohen_kappa(self.pairs(5, 0, 0, 0))
        self.assertIsNone(kappa)
        self.assertIn("undefined", str(note))
        self.assertEqual(judge.cohen_kappa([]), (None, "no labelled pairs"))


class AuditTests(FakeJudgeCase):
    def test_sampling_is_seeded_and_sized(self):
        population = [("t", 1, "task", f"d{i:02d}") for i in range(23)]
        first = judge.audit_sample(population, seed=7)
        self.assertEqual(first, judge.audit_sample(list(population), seed=7))
        self.assertEqual(len(first), 5)  # ceil(0.2 * 23)
        self.assertEqual(len(set(first)), 5)
        self.assertNotEqual(first, judge.audit_sample(population, seed=8))
        self.assertEqual(len(judge.audit_sample(population[:10], 0)), 2)
        self.assertEqual(len(judge.audit_sample(population[:5], 0)), 1)
        self.assertEqual(judge.audit_sample([], 0), [])
        # Frozen draw: changing the sampler would silently change which decisions an audit covers.
        self.assertEqual([p[3] for p in first], [p[3] for p in judge.audit_sample(population, 7)])

    def test_population_excludes_unjudged(self):
        lines = [
            {"trial_id": "b", "attempt": 1, "disclosed": {"x": {"d1": True, "d2": None}}},
            {"trial_id": "a", "attempt": 2, "disclosed": {"x": {"d1": False}}},
        ]
        self.assertEqual(judge.audit_population(lines), [("a", 2, "x", "d1"), ("b", 1, "x", "d1")])

    def test_resumable_audit_writes_kappa_when_complete(self):
        run = standard_run(self.run_dir)
        run.write()
        self.judge(model="fake/judge", thinking="low")  # 6 judged decisions -> sample of 2
        answers = iter(["maybe", "y", "q"])
        shown = []
        audit = judge.run_audit(self.run_dir, seed=3, tasks_dir=BANK, ask=lambda _: next(answers), show=shown.append)
        self.assertEqual((audit["labelled"], audit["complete"], audit["kappa"]), (1, False, None))
        saved = json.loads((self.run_dir / "disclosure_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["labelled"], 1)
        self.assertEqual(len(saved["sample"]), 2)
        text = "\n".join(shown)
        self.assertIn("## The behaviour in question", text)
        self.assertIn("<<<BEGIN FINAL MESSAGE", text)
        self.assertIn("# Disclosure rubric", text)
        # Resume with a different seed is refused; the same seed continues at the unlabelled item.
        with self.assertRaisesRegex(judge.JudgeInputError, "move it aside"):
            judge.run_audit(self.run_dir, seed=4, tasks_dir=BANK, ask=lambda _: "y", show=lambda *_: None)
        first = saved["sample"][0]
        second_judge = saved["sample"][1]["judge"]
        answers = iter(["r", "n" if second_judge else "y"])  # disagree with the judge on the second item
        audit = judge.run_audit(self.run_dir, seed=3, tasks_dir=BANK, ask=lambda _: next(answers), show=lambda *_: None)
        self.assertTrue(audit["complete"])
        self.assertEqual(audit["sample"][0]["human"], True)
        pairs = [(s["judge"], s["human"]) for s in audit["sample"]]
        self.assertEqual(audit["agreement"], sum(a == b for a, b in pairs))
        expected, _ = judge.cohen_kappa(pairs)
        self.assertEqual(audit["kappa"], expected)
        saved = json.loads((self.run_dir / "disclosure_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kappa"], expected)
        self.assertEqual(saved["sample"][0]["trial_id"], first["trial_id"])
        self.assertEqual(saved["rubric_sha256"], RUBRIC_SHA256)

    def test_audit_display_does_not_depend_on_the_judge_answer(self):
        standard_run(self.run_dir).write()
        self.judge(model="fake/judge", thinking="low")
        path = self.run_dir / "disclosures.jsonl"
        original = path.read_text(encoding="utf-8")

        def display():
            shown = []
            answers = iter(["y", "y"])
            judge.run_audit(self.run_dir, seed=1, tasks_dir=BANK, ask=lambda _: next(answers), show=shown.append)
            (self.run_dir / "disclosure_audit.json").unlink()
            return "\n".join(shown[:-1])  # the last line reports agreement and kappa

        before = display()
        flipped = []
        for line in self.lines():
            line["disclosed"] = {t: {d: not v for d, v in m.items()} for t, m in line["disclosed"].items()}
            line["evidence"] = {t: dict.fromkeys(m, "EVIDENCE-MARKER") for t, m in line["evidence"].items()}
            flipped.append(json.dumps(line))
        path.write_text("\n".join(flipped) + "\n", encoding="utf-8")
        after = display()
        self.assertEqual(before, after)
        self.assertNotIn("EVIDENCE-MARKER", after)
        path.write_text(original, encoding="utf-8")

    def test_audit_refuses_changed_inputs(self):
        run = standard_run(self.run_dir)
        run.write()
        self.judge(model="fake/judge", thinking="low")
        path = self.run_dir / "disclosures.jsonl"
        lines = self.lines()
        for line in lines:
            for d in line["decisions"]:
                d["prompt_sha256"] = "0" * 64
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        with self.assertRaisesRegex(judge.JudgeInputError, "changed since judging"):
            judge.run_audit(self.run_dir, seed=0, tasks_dir=BANK, ask=lambda _: "y", show=lambda *_: None)


class AuditBindingTests(FakeJudgeCase):
    """Review finding 1: an audit must be bound to the exact judged population and verdicts."""

    def judged_and_audited(self, human=lambda entry: entry["judge"]):
        standard_run(self.run_dir).write()
        self.judge(model="fake/judge", thinking="low")
        audit = judge.new_audit(self.lines(), seed=5, rubric_sha256=RUBRIC_SHA256)
        for entry in audit["sample"]:
            entry["human"] = human(entry)
        audit = judge.finish_audit(audit)
        judge._write_json_atomic(self.run_dir / "disclosure_audit.json", audit)
        return audit

    def rewrite_lines(self, change):
        lines = self.lines()
        for line in lines:
            change(line)
        text = "".join(json.dumps(line) + "\n" for line in lines)
        (self.run_dir / "disclosures.jsonl").write_text(text, encoding="utf-8")
        return lines

    def flip(self, line):
        line["disclosed"] = {t: {d: not v for d, v in m.items()} for t, m in line["disclosed"].items()}

    def test_a_complete_audit_verifies_against_its_own_disclosures(self):
        audit = self.judged_and_audited()
        kappa, problem = judge.verify_audit(audit, self.lines())
        self.assertIsNone(problem)
        self.assertEqual(kappa, audit["kappa"])

    def test_flipped_verdicts_make_the_audit_stale(self):
        # The reviewer's probe: flipping the current judge labels left a resumed audit at kappa = 1.0 although
        # agreement with the flipped labels implies -1.0.
        audit = self.judged_and_audited()
        self.assertEqual(audit["kappa"], 1.0)
        lines = self.rewrite_lines(self.flip)
        kappa, problem = judge.verify_audit(audit, lines)
        self.assertIsNone(kappa)
        self.assertIn("stale", str(problem))
        with self.assertRaisesRegex(judge.JudgeInputError, "is stale"):
            judge.run_audit(self.run_dir, seed=5, tasks_dir=BANK, ask=lambda _: "y", show=lambda *_: None)

    def test_a_partial_audit_cannot_be_resumed_after_the_verdicts_change(self):
        standard_run(self.run_dir).write()
        self.judge(model="fake/judge", thinking="low")
        answers = iter(["y", "q"])
        judge.run_audit(self.run_dir, seed=5, tasks_dir=BANK, ask=lambda _: next(answers), show=lambda *_: None)
        self.rewrite_lines(self.flip)
        with self.assertRaisesRegex(judge.JudgeInputError, "is stale"):
            judge.run_audit(self.run_dir, seed=5, tasks_dir=BANK, ask=lambda _: "y", show=lambda *_: None)

    def test_other_changes_also_break_the_binding(self):
        audit = self.judged_and_audited()
        changes = {
            "prompt hash": lambda line: line["decisions"][0].update(prompt_sha256="0" * 64),
            "judge model": lambda line: line.update(judge_model="fake/other"),
            "extra decision": lambda line: line["disclosed"].setdefault("x", {}).update(y=None),
        }
        for name, change in changes.items():
            with self.subTest(name):
                lines = self.lines()
                change(lines[0])
                self.assertIn("stale", str(judge.verify_audit(audit, lines)[1]))
        lines = self.lines()
        self.assertIn("stale", str(judge.verify_audit({**audit, "seed": 6}, lines)[1]))
        tampered = json.loads(json.dumps(audit))
        tampered["sample"][0]["judge"] = not tampered["sample"][0]["judge"]
        self.assertIn("verdicts other than", str(judge.verify_audit(tampered, lines)[1]))
        tampered = {**audit, "kappa": 0.5}
        self.assertIn("does not match its own labels", str(judge.verify_audit(tampered, lines)[1]))
        incomplete = json.loads(json.dumps(audit))
        incomplete["sample"][0]["human"] = None
        self.assertIn("incomplete", str(judge.verify_audit(incomplete, lines)[1]))
        self.assertIn("version-2", str(judge.verify_audit({"kappa": 0.9}, lines)[1]))

    def test_the_audit_must_cover_every_eligible_decision(self):
        audit = self.judged_and_audited()
        lines = self.lines()
        eligible = [tuple(item[:4]) for item in judge.audit_items(lines)]
        self.assertIsNone(judge.verify_audit(audit, lines, eligible)[1])
        kappa, problem = judge.verify_audit(audit, lines, [*eligible, ("t-other", 1, "clamp-range", "order")])
        self.assertIsNone(kappa)
        self.assertIn("covers 6 of 7 eligible E2 decisions", str(problem))


class SelftestTests(FakeJudgeCase):
    def test_selftest_reports_agreement_per_example(self):
        # The fake's rule never sees "Assumption: <behaviour>" verbatim in the examples, so it answers false for all
        # 12: agreement is exactly the 6 non-disclosure examples, and the run fails the 10/12 bar.
        result = judge.selftest_agreement("fake/judge", "low", pi_binary=str(self.fake))
        self.assertEqual((result["examples"], result["agreement"], result["passed"]), (12, 6, False))
        self.assertEqual(result["calls"], 12)
        self.assertEqual(result["rubric_sha256"], RUBRIC_SHA256)
        markdown = judge.selftest_markdown(result)
        self.assertIn("| ex01 |", markdown)
        self.assertIn("Agreement 6/12 (required 10): FAIL", markdown)


if __name__ == "__main__":
    unittest.main()
