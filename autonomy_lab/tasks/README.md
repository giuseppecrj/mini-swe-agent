# Autonomy Lab task bank

This directory is the task bank for E1–E3 in [LOOP_SPEC.md](../LOOP_SPEC.md). Each
subdirectory is one task: a small stdlib-only Python module with a weak starter
and 4–8 *consequential decisions*. Each decision has exactly one held-out check.
A trial settles each decision through a contract sentence, a visible check, both
or neither, and then grades every decision separately.

Nine tasks are written by the coordinator (`"author": "coordinator"`). This guide
is for the three you write (`"author": "user"`). Proposed labels, their
alternatives and the post-audit changes are in [LABELS.md](LABELS.md).

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
                   the reference with only that decision changed (every decision)
  mutants/<decision-id>--<alt>.py
                   one more alternative policy (arbitrary decisions only)
  decisions.json   [{"id", "label", "contract", "visible_check", "heldout_check"}, ...]
  neutral.md       filler: one neutral, accurate sentence per line
```

- `id` must equal the directory name. Pick a `module` name that no other task
  uses and that does not shadow an importable module. Tasks share one workspace
  at breadth > 1. Names already taken: `split`, `durations`, `expiring_cache`,
  `binning`, `slots`, `options`, `pages`, `ports`, `tables`.
- `label` is one of `inferable`, `arbitrary` or `preserve` (definitions below).
- `decisions.json` order is prompt order. Keep it stable.
- An `arbitrary` decision has at least two mutants: `<id>.py` and at least one
  `<id>--<alt>.py`. `<alt>` is a short lowercase name for the alternative
  policy, such as `--last-wins` or `--clamped`. Other labels have exactly
  `<id>.py`.

## Rules every task must meet

Mechanical (checked by the bank validator):

1. 4–8 decisions, with at least 2 `arbitrary` (3 is better) and at least 1
   `inferable`.
2. `reference.py` passes every held-out and every visible check.
3. Every mutant of decision `d`, including the `--<alt>` ones, fails `d`'s
   held-out check **and** its visible check, and passes every other
   decision's held-out and visible checks. This is strict for visible checks
   too: a mutant that fails a sibling's visible check is an error, not a
   warning.
4. `starter.py` fails at least half of the held-out checks, including at least
   2 `arbitrary` ones, and passes every `preserve` check (held-out and visible).
5. Each check is a self-contained snippet that imports the module by name and
   passes exactly when it exits 0. It runs as its own `python -I` process, with
   the workspace on `sys.path` as the working directory and a 10 s timeout. All
   visible checks are also concatenated into one `visible_checks.py`, so they
   must not depend on leftover state.
6. `neutral.md` can bring every combination of shown contracts to within ±5%
   of the all-contract prompt length.

By construction (checked by review and the cross-family audit; §4.1
"Alternative mutants and construct rules"):

- **Stdlib only.** Pure functions or small classes, with no file, network,
  clock or randomness side effects. Deterministic on Python 3.11+.
- **One choice per decision.** A decision is a single policy choice. Split a
  bundle such as "sorted and deduplicated", or narrow it. When one choice only
  exists under another (a tie rule under a ranking rule, or page-0 validity
  under a numbering rule), merge them into one decision.
- **Natural alternatives.** Every mutant is what a competent engineer might
  plausibly write for a different policy, not a contrived bug. "No check at
  all" counts only when the result is a coherent behaviour, such as "a zero
  lifetime means entries expire at once" or "Python's own
  `ZeroDivisionError`". Name each alternative in LABELS.md.
- **No settling outside the channels.** The brief, the starter's docstrings,
  names, signature and comments, and `neutral.md` must not state or imply any
  decision's behaviour. The audit found these leaks: a docstring saying
  entries expire "after they are stored" (it settled the lifetime rule), a
  contract defining expiry as `now < t + ttl` (also the lifetime rule), filler
  saying "the second line separates headers from data" (it settled empty
  tables), and filler mentioning `int()`, firewalls or opening listeners.
- **Prerequisites are not decisions.** Behaviour that many checks rely on, and
  that you do not want to measure, goes in the starter's docstring as a fixed
  fact. Examples: "the result ... adds up to total_cents", "a sorted list of
  port numbers", "`default` if it is missing or has expired". A prerequisite
  must not settle any decision you do grade.
- **Matching coverage.** A decision's visible and held-out checks cover the
  same input classes, with different concrete values: the same types (int
  *and* float), error families (zero *and* negative, reversed *and*
  zero-length), and boundary kinds (doubled, leading *and* trailing commas).
  Both stay inside the domain the starter declares; for example, use `[]`,
  not `()`, if the docstring says "list". A class with only one value, such
  as the empty string, may repeat across channels, but each check must also
  contain other values.
- **Contracts.** Each contract sentence states the graded behaviour exactly,
  agrees with both checks, and settles only its own decision. Contract
  examples should not reuse held-out inputs.
- **The brief** is a terse issue title, such as "histogram() counts are
  wrong". It must not hint at any decision.

## The three labels

- **inferable**: a competent engineer who reads only the brief, the starter and
  normal domain conventions would pick the graded behaviour with high
  confidence (over 80%). Examples: a range "8000-8002" includes both ends;
  `page_count(25, 10) == 3`; a value may contain "=" after the first one.
- **arbitrary**: at least two other reasonable behaviours exist, and the
  graded one is just one of them. Examples: raise vs return `None` vs return 0;
  last-wins vs first-wins vs reject; LRU vs FIFO vs oldest-write eviction.
- **preserve**: behaviour the starter already has, correctly, that a rewrite must
  not break. Example: `parse_ports` already raises `ValueError` for `"http"`.

## Writing good arbitrary decisions

- **You need two alternatives besides the graded one.** A purely binary
  choice (inclusive vs exclusive boundary, 0- vs 1-based pages, sorted vs
  input order, trailing spaces kept or not, `page_count(0)` = 0 vs 1) cannot
  be an arbitrary decision on its own. Either merge it with a dependent
  choice into one decision with more options, as `page-numbering` does, or
  leave it ungraded. If you leave it ungraded, keep it out of **every**
  check, and list it under "deliberately ungraded" in LABELS.md.
- **Make the starter's current behaviour one of the alternatives**, or make the
  starter crash on that input. Either way the starter fails the check without
  the check being contrived.
- **Avoid famous katas and well-known specifications** (Roman numerals, semver,
  LRU caches, FizzBuzz, merge intervals). If a convention is a known attractor,
  say so in LABELS.md, as `last-edge` does for NumPy.
- **Prefer edges an engineer meets by accident**: empty input, repeated input,
  ordering, out-of-range values and invalid arguments. Each of these should be
  triggered by an input class that no sibling decision touches.
- **Re-check labels against the starter.** If the starter's code or docstring
  already points at the graded behaviour, the label is probably `inferable`.
  The audit moved `whole-numbers-only` out of the bank for this reason, and
  relabelled `empty-rows` the other way.

## Keeping checks isolated (the part that takes the most iterations)

Rule 3 says every check must be insensitive to every *other* decision's
alternatives, not just to the one mutant you wrote. The audit broke the first
draft with alternatives the author had not tried. These techniques fixed it:

- **Assert only what the decision owns.**
  - `half-open-intervals` asserts that the boundary starts are *in* the
    result (`assert 570 in got`), not the whole list, so the busy-overlap
    rule is not also graded.
  - `header-sets-width` asserts that the column separators line up at the
    header-derived positions, not the exact padding, so cell alignment is
    not also graded.
  - `empty-rows` asserts the structure: two lines, the header names, and a
    separator made only of `-` and `+`.
  - The overlapping-range case of `duplicate-ports` asserts uniqueness and a
    few interior ports, so range-end inclusion is not also graded.
- **Normalize what is ungraded.** `pagination` compares `list(get_page(...))`
  so return-type alternatives cannot leak. `table-render` compares lines after
  `rstrip()` because trailing spaces are not graded.
- **Populate the background.** In `histogram-bins`, every check except
  `one-count-per-bin` has every bin non-empty, so "drop zero bins" cannot
  leak into the boundary checks.
- **Never reach a precondition through another graded behaviour.** The old
  `order-status` reached `delivered` by calling the graded `deliver`
  transition. Call the function directly on the state you need.
- **Pick inputs that separate all alternatives at once.** `leftover-order` uses
  inputs (found by a small brute-force search) where every listed alternative,
  and three more plausible ones, gives a different answer, while the exact and
  float versions of the reference still agree.
- **Assert the decision, not a sibling's exception type.** If an error check
  needs another decision's error, accept any exception there.
- **Avoid inputs that can hang the starter or a mutant**, such as a zero or
  negative loop step.

### Probing, before you hand a task over

Passing your own mutants is necessary but not proof of isolation. For each
decision, write one or two extra natural alternatives in a *different
implementation style*. Examples: an `OrderedDict` LRU, a loop instead of
`bisect`, `str.partition` instead of `split`, or a version that also flips an
ungraded choice. Keep them outside the repository. Each probe must pass every
sibling decision's checks. If it fails a sibling check, either the probe
changes two decisions at once (fix the probe) or the checks are coupled (fix
the checks). During this revision, the alternative mutants and probes found
four couplings and two coverage gaps that the first draft's mutants hid. A probe that also passes its own target's checks shows a coverage
gap: add an input that separates it, unless it implements the graded policy.

## neutral.md

Filler is added in file order until the prompt is within ±5% of the
all-contract prompt length. So:

- Keep every sentence short (about 65 characters or fewer) so one sentence
  cannot jump over the ±5% window.
- Make the total at least 1.5 times the combined length of all contract
  sentences.
- **Purpose and context only:** who calls the function, where the input comes
  from, that it is pure and uses only the standard library. Do not describe
  output format, validation, ordering, boundaries, allocation, error behaviour
  or implementation tools.
  - Removed during the audit: "the first line holds the column headers",
    "amounts are integer cents, never floating-point values", "each entry
    pairs a value with the time it was stored", "a web handler reads the page
    number from the query string", "the built-in int() converts digit
    strings".
  - Removed by the coordinator, for hinting a decision: "storage drivers are
    typical users" (mount options make bare keys flags), and "callers pass the
    returned seconds straight to timer functions" (it implies a result is
    never `None`).

## Minimal filled-in example

This is a condensed version of the `port-spec` task. See the directory for all
six decisions and nine mutants.

`task.json`

```json
{"id": "port-spec", "module": "ports.py",
 "brief": "parse_ports() returns wrong results for some specs",
 "version": 2, "author": "coordinator"}
```

`starter.py` (weak: exclusive ranges, keeps duplicates, no upper bound,
crashes on `""`). "Sorted" in the docstring is a prerequisite, not a decision.

```python
def parse_ports(spec):
    """Parse a port specification such as "22,80,8000-8010" into a sorted list of port numbers."""
    ports = []
    for part in spec.split(","):
        if "-" in part:
            low, high = part.split("-")
            ports.extend(range(int(low), int(high)))
        else:
            ports.append(int(part))
    return sorted(ports)
```

One `inferable` and one `arbitrary` entry from `decisions.json`:

```json
[
  {"id": "inclusive-ranges", "label": "inferable",
   "contract": "A range low-high includes both of its ends.",
   "visible_check": "...for spec, want in ((\"20-22\", [20, 21, 22]), (\"7-7\", [7])): ...",
   "heldout_check": "...for spec, want in ((\"8000-8002\", [8000, 8001, 8002]), (\"5-5\", [5])): ..."},
  {"id": "empty-spec", "label": "arbitrary",
   "contract": "An empty or whitespace-only spec returns an empty list.",
   "visible_check": "...for spec, want in ((\"\", []), (\"  \\t \", [])): ...",
   "heldout_check": "...for spec, want in ((\"\", []), (\" \", [])): ..."}
]
```

Mutants for `empty-spec` (graded: `[]`):

- `mutants/empty-spec.py` deletes `if not spec.strip(): return []`, so `int("")`
  raises `ValueError`, as in the starter.
- `mutants/empty-spec--all-ports.py` returns every port for an empty spec,
  reading it as "any port".

`mutants/inclusive-ranges.py` changes `range(low, high + 1)` to
`range(low, high)`.

In LABELS.md, `empty-spec` lists both alternatives with their mutant names, and
`inclusive-ranges` is `inferable` because "8000-8002" means three ports in
every configuration convention.

## Checklist before handing a task over

- [ ] Every file in the schema exists: one `<id>.py` mutant per decision, plus
      `<id>--<alt>.py` until each arbitrary decision has at least two.
- [ ] The bank validator passes for the task: reference, full mutation matrix
      (held-out and visible) and starter rule. The official validator
      (`python3 -m autonomy_lab.taskbank validate autonomy_lab/tasks`) is being
      extended to accept `--<alt>` mutants. Until then it reports them as
      unknown files.
- [ ] Probes of each decision in a different style pass every sibling check.
- [ ] Every contract agrees with both of its checks and leaks no sibling
      decision.
- [ ] Visible and held-out checks cover the same input classes with different
      values.
- [ ] The brief, the starter's docstrings and names, and `neutral.md` hint at
      no decision.
- [ ] Each label has a reason in LABELS.md, and each arbitrary decision names
      every alternative with its mutant file.
