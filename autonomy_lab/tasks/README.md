# Autonomy Lab task bank

This directory is the task bank for E1–E3 in [LOOP_SPEC.md](../LOOP_SPEC.md). Each
subdirectory is one task: a small stdlib-only Python module with a weak starter
and 4–8 *consequential decisions*. Each decision has exactly one held-out check.
A trial settles each decision through a contract sentence, a visible check, both
or neither, and then grades every decision separately.

Nine tasks are written by the coordinator (`"author": "coordinator"`). This guide
is for the three you write (`"author": "user"`). Proposed labels and their
reasons are in [LABELS.md](LABELS.md).

> Everything under `autonomy_lab/tasks/` is hashed into `task_bank_sha256`
> (§4.1). After the bank is frozen, any edit, even to this README, creates a new
> bank version.

## Files in a task (§4.1, exact schema)

```text
autonomy_lab/tasks/<task-id>/
  task.json        {"id": "<task-id>", "module": "<name>.py", "brief": str,
                    "version": 1, "author": "user"}
  starter.py       weak starting implementation, copied into the workspace as <module>
  reference.py     a solution that satisfies every decision
  mutants/<decision-id>.py
                   the reference with only that decision violated (one file per decision)
  decisions.json   [{"id", "label", "contract", "visible_check", "heldout_check"}, ...]
  neutral.md       filler: one neutral, accurate sentence per line
```

- `id` must equal the directory name. Pick a `module` name that no other task
  uses and that does not shadow a standard-library module. Tasks share one
  workspace at breadth > 1. Names already taken: `durations`, `slots`,
  `expiring_cache`, `split`, `tables`, `order_status`, `ports`, `binning`,
  `pages`.
- `label` is one of `inferable`, `arbitrary` or `preserve` (definitions below).
- `decisions.json` order is prompt order. Keep it stable.

## Rules every task must meet

Mechanical (checked by the bank validator):

1. 4–8 decisions, with at least 2 `arbitrary` (3 is better) and at least 1
   `inferable`.
2. `reference.py` passes every held-out and every visible check.
3. `mutants/<d>.py` fails decision `d`'s held-out check **and** its visible check,
   and passes every other decision's held-out and visible checks.
4. `starter.py` fails at least half of the held-out checks, including at least 2
   `arbitrary` ones.
5. Each check is a self-contained snippet that imports the module by name and
   passes exactly when it exits 0. It runs as its own `python -I` process, with
   the workspace on `sys.path` as the working directory and a 10 s timeout.

By construction (checked by review and the cross-family audit):

- Stdlib only. Pure functions or small classes. No file, network, clock or
  randomness side effects. Deterministic on Python 3.11+.
- Each held-out check tests exactly one decision. Each visible check tests the
  same decision with *different* concrete inputs, so it is not a copy of the
  grader.
- Each contract sentence states the graded behaviour precisely. It must agree
  with both checks and settle only its own decision. Watch the examples inside
  a contract: an example that involves another decision leaks that decision.
- The brief is a terse issue title, such as "histogram() counts are wrong". It
  must not state or hint at any decision.
- `neutral.md` sentences are accurate and about the task's domain or purpose,
  and they settle or hint at no decision (see below).
- The coordinator tasks also keep this convention: the starter passes exactly
  the `preserve` decisions and fails every other one. It is not a spec rule,
  but it makes the labels easy to read.

## The three labels

- **inferable**: a competent engineer who reads only the brief, the starter and
  normal domain conventions would pick the graded behaviour with high
  confidence (over 80%). Examples: a range "8000-8002" includes both ends; split
  shares add up to the total; `page_count(25, 10) == 3`.
- **arbitrary**: several reasonable behaviours exist, and the graded one is just
  one of them. Examples: raise vs return `None` vs no-op; which exception type;
  inclusive vs exclusive boundaries; tie-breaking order; 0- vs 1-based numbering.
- **preserve**: behaviour the starter already has, correctly, that a rewrite must
  not break. Example: `parse_ports` already raises `ValueError` for `"http"`.

## Writing good arbitrary decisions

- **Name the alternatives.** If you cannot list at least two other behaviours a
  careful engineer might pick, the decision is inferable, not arbitrary. Put
  the alternatives in LABELS.md.
- **Make the starter's current behaviour one of the alternatives**, or make the
  starter crash on that input. Either way the starter fails the check without
  the check being contrived. The starter's choice is itself a plausible
  alternative: 0-based pages, sliding expiry and `KeyError` on illegal
  transitions are all reasonable to keep.
- **Avoid famous katas and well-known specifications** (Roman numerals, semver,
  LRU caches, FizzBuzz). A model that recognises the task brings in a
  convention, and the decision stops being arbitrary.
- **Prefer edges an engineer meets by accident**: boundary equality, empty
  input, repeated input, ordering, out-of-range values and invalid arguments.
- **Do not stack two choices in one decision** unless one implies the other.
  If a check needs two independent choices to be right, split it, or remove the
  second choice from the check.

## Keeping checks isolated (the part that takes the most iterations)

Rule 3 means every check must be insensitive to every *other* decision. These
techniques worked for the coordinator tasks:

- **Pick inputs that are neutral for sibling decisions.** In `bill-split`,
  every check except `exact-total` uses inputs that leave at most one cent over
  and have no tied remainders. The `ties-to-earlier` checks use only tied
  inputs.
- **Watch shared output formats.** In `table-render`, each visible and held-out
  check whose last column is left-aligned uses cells exactly as wide as that
  column's header, so `no-trailing-spaces` cannot interfere. Headers are never
  wider than their cells except in the `header-sets-width` and `empty-rows`
  checks.
- **Assert the decision, not a sibling's exception type.** In `order-status`,
  "a shipped order cannot be cancelled" is checked as `result != "cancelled"`,
  with any exception accepted, so it does not depend on `illegal-event-raises`.
- **Write each mutant as a small, realistic bug in the reference**, such as a
  wrong comparison, a missing branch or a wrong sort key. Do not write a
  rewrite. If a natural mutant breaks a sibling check, change the sibling
  check's inputs first. Change the mutant only as a last resort.
- **Avoid inputs that can hang the starter or a mutant**, such as a zero or
  negative loop step. A 10 s timeout counts as a failure, but it slows every
  run.

## neutral.md

Filler is added in file order until the prompt is within ±5% of the
all-contract prompt length. So:

- Keep every sentence short (about 65 characters or fewer) so one sentence
  cannot jump over the ±5% window.
- Make the total at least 1.5 times the combined length of all contract
  sentences.
- Each sentence must be true and about the domain or purpose: who calls the
  function, its units, its inputs, that it is pure, that it uses only the
  standard library. Do not write sentences that nudge toward a choice. Examples
  the coordinator removed: "the default step matches the calendar grid"
  (hints at clock alignment), "events arrive from webhooks" (hints at
  idempotency), "port numbers are 16-bit" (hints at the bounds), "str() turns
  cells into text" (hints at rendering None).

## Minimal filled-in example

This is a condensed version of the `port-spec` task. See the directory for all
six decisions.

`task.json`

```json
{"id": "port-spec", "module": "ports.py",
 "brief": "parse_ports() returns wrong results for some specs",
 "version": 1, "author": "coordinator"}
```

`starter.py` (weak: exclusive ranges, no sorting, no bounds, crashes on `""`)

```python
def parse_ports(spec):
    """Parse a port specification such as "22,80,8000-8010" into a list of port numbers."""
    ports = []
    for part in spec.split(","):
        if "-" in part:
            low, high = part.split("-")
            ports.extend(range(int(low), int(high)))
        else:
            ports.append(int(part))
    return ports
```

One `inferable` and one `arbitrary` entry from `decisions.json`:

```json
[
  {"id": "inclusive-ranges", "label": "inferable",
   "contract": "A range low-high includes both of its ends.",
   "visible_check": "from ports import parse_ports\nassert parse_ports(\"20-22\") == [20, 21, 22], parse_ports(\"20-22\")\n",
   "heldout_check": "from ports import parse_ports\nassert parse_ports(\"8000-8002\") == [8000, 8001, 8002]\nassert parse_ports(\"5-5\") == [5]\n"},
  {"id": "empty-spec", "label": "arbitrary",
   "contract": "An empty or whitespace-only spec returns an empty list.",
   "visible_check": "from ports import parse_ports\nassert parse_ports(\"  \\t \") == [], parse_ports(\"  \\t \")\n",
   "heldout_check": "from ports import parse_ports\nassert parse_ports(\"\") == []\nassert parse_ports(\" \") == []\n"}
]
```

`mutants/empty-spec.py` is `reference.py` with these two lines deleted:

```python
    if not spec.strip():
        return []
```

`mutants/inclusive-ranges.py` is `reference.py` with
`range(low, high + 1)` changed to `range(low, high)`.

In LABELS.md, `empty-spec` is `arbitrary` because raising `ValueError` for an
empty spec is just as defensible as returning `[]`. `inclusive-ranges` is
`inferable` because "8000-8002" means three ports in every firewall and
configuration convention.

## Checklist before handing a task over

- [ ] Every file in the schema exists, with one mutant per decision id.
- [ ] The bank validator passes for the task: reference, mutation matrix and starter rule.
- [ ] Every contract agrees with both of its checks and leaks no sibling decision.
- [ ] The brief and `neutral.md` hint at no decision.
- [ ] Each label has a one-line reason in LABELS.md, with the alternatives listed for arbitrary decisions.
