# Proposed decision labels (coordinator draft, revised after two T4 audits, for G2 approval)

Approve, change or reject each label **before seeing any agent results**
(D3, §3.3). The labels mean:

- **inferable**: a competent engineer with only the brief, the starter and
  normal conventions would pick the graded behaviour with more than 80%
  confidence.
- **arbitrary**: at least one other reasonable behaviour exists. Each
  alternative named below is implemented as a mutant: `<decision>` is
  `mutants/<decision>.py`, and `<decision>--<alt>` is
  `mutants/<decision>--<alt>.py` (§4.1). A binary choice, the graded
  behaviour plus one natural alternative, is a valid arbitrary decision with
  a single mutant. No alternative is invented to reach a count.
- **preserve**: the starter already does this correctly, and it must not
  break.

"Starter" in a reason is the task's `starter.py`. Each coordinator starter
passes exactly its `preserve` checks and fails every other held-out check.
Lines marked **borderline** are the ones most worth a second look.

Totals for the 9 coordinator tasks and 50 decisions: 30 arbitrary, 11
inferable and 9 preserve, with 83 mutants.

## What changed after the first audit

- `order-status` was replaced by `option-string`. A finite transition table
  leaves one meaningful input per decision, so visible and held-out checks
  could not use different inputs.
- That revision required at least two natural alternatives per arbitrary
  decision, so choices with only one alternative were merged, narrowed or
  dropped. The governing spec has since been corrected: binary choices are
  valid (see the second-audit changes below). No check depends on a choice
  that is still dropped; those are listed at the end of this file.
- Behaviour that every check relies on, but that is no decision, is stated in
  the starter's docstring as a fixed prerequisite. For example, split shares
  add up to the total, ports come back sorted, and an expired cache entry
  reads like a missing one.

## What changed after the second audit

- **Binary decisions.** The definition above follows the corrected §4.1
  rule. `expiring-cache` regains the dropped binary choice
  `expiry-boundary`, now isolated: no other check reads an entry at exactly
  its expiry time. Weak "no check" alternatives that the audit judged
  incoherent were removed, not replaced: negative meeting lengths
  (`positive-length`), negative per_page (`per-page-positive`) and negative
  page counts (`negative-total`). Their remaining mutant is a coherent
  policy.
- **Singleton inputs.** The governing spec now allows an input class with
  exactly one member to appear in both channels when the other inputs of
  the class differ. `duration-parse` `blank-text` and `port-spec`
  `empty-spec` use `""` in both checks, with distinct whitespace-only
  strings. For that sub-case held-out grading measures the visible
  behaviour, not generalization.
- **Couplings and coverage fixed.** `pagination` numbering checks use only
  full pages. `meeting-starts` `avoids-busy` excludes endpoint contact from
  its contract, and visible `invalid-busy-interval` adds an invalid interval
  outside the day. `option-string` covers spaces on both sides of keys and
  values in both channels. `port-spec` visible `duplicate-ports` adds
  partly overlapping ranges. `expiring-cache` lifetime checks call the cache
  in time order.
- **Starter and filler cues removed.** The `table-render` starter states
  its format only for tables with at least one row. The `duration-parse`
  starter no longer says "Return the number of seconds", its filler no
  longer describes the result, and `invalid-text` lists its classes instead
  of claiming all non-duration text. Remaining output and domain facts were
  removed from the bill-split, duration-parse, option-string, port-spec and
  histogram-bins filler.

## Labels

| Task | Decision | Label | Reason, then the graded behaviour and each alternative with its mutant |
|---|---|---|---|
| bill-split | leftover-order | arbitrary | Graded: leftover cents go to the largest fractional parts, ties to the earlier party. Alternatives: the first parties in list order (`leftover-order`); the largest weights (`leftover-order--largest-weight`); largest fractional parts with ties to the later party (`leftover-order--ties-to-later`). The earlier "largest-remainder" and "ties-to-earlier" decisions were merged because the tie rule only exists under the largest-remainder policy. |
| bill-split | exact-arithmetic | inferable | **Borderline.** Integer cents with float arithmetic lose cents above 2**53, and money code conventionally avoids floats. Alternative: float division (`exact-arithmetic`, the starter's approach). |
| bill-split | zero-weight-share | preserve | The starter already gives weight-0 parties 0. A rewrite might reject zero weights (`zero-weight-share`). |
| bill-split | all-zero-weights | arbitrary | Graded: `ValueError`. Alternatives: let `ZeroDivisionError` escape, as the starter does (`all-zero-weights`); treat all-zero weights as equal weights (`all-zero-weights--equal-split`). |
| bill-split | negative-total | arbitrary | Graded: `ValueError`. Alternatives: split a refund symmetrically by negating the split of its absolute value (`negative-total`); apply the same floor arithmetic to negative totals (`negative-total--floor`). |
| duration-parse | combined-units | preserve | The starter's `findall` already sums multi-unit strings such as "1h30m". A rewrite might keep only the first component (`combined-units`). |
| duration-parse | invalid-text | inferable | A parser that returns 0 for "soon" or ignores trailing junk hides errors. `ValueError` is the Python convention. Alternative: sum whatever components are found (`invalid-text`, the starter's behaviour). |
| duration-parse | bare-number-seconds | arbitrary | Graded: "90" means 90 seconds. Alternatives: the unit is required, so `ValueError` (`bare-number-seconds`, Go's rule); a bare number means minutes (`bare-number-seconds--minutes`). |
| duration-parse | repeated-unit | arbitrary | Graded: "1h1h" raises `ValueError`. Alternatives: repeated units add up, as in the starter (`repeated-unit`); the last one wins (`repeated-unit--last-wins`). |
| duration-parse | blank-text | arbitrary | Graded: "" or whitespace returns `None`, meaning no duration. Alternatives: 0 seconds, as in the starter (`blank-text`); `ValueError` (`blank-text--raises`). |
| expiring-cache | lifetime-origin | arbitrary | Graded: lifetime counts from the latest put, and reads do not refresh it. Alternatives: sliding expiry, where reads refresh it, as in the starter (`lifetime-origin`); lifetime counts from the first put and re-puts keep it, like Redis `KEEPTTL` (`lifetime-origin--first-put`). The starter docstring no longer says "after they are stored". |
| expiring-cache | expiry-boundary | arbitrary | Restored binary choice. Graded: an entry is gone at exactly its lifetime (`now - put_time >= lifetime`). Alternative: still live at that moment and gone only after it, as in the starter (`expiry-boundary`). Both are common in caching libraries. No other check reads an entry at exactly its expiry time. |
| expiring-cache | eviction-order | arbitrary | Graded: evict the entry whose latest put is oldest, so reads do not protect it. Alternatives: LRU, where reads count (`eviction-order`); FIFO by first insertion, where re-puts keep their place, as in the starter (`eviction-order--first-insertion`). |
| expiring-cache | update-does-not-evict | inferable | Replacing an existing key in a full cache should not evict an unrelated key. The starter checks capacity before existence, which is a plain bug (`update-does-not-evict`). |
| expiring-cache | capacity-at-least-one | arbitrary | Graded: `ValueError` for capacity < 1. Alternatives: capacity 0 disables caching, like `functools.lru_cache(maxsize=0)` (`capacity-at-least-one`); capacity 0 means unlimited (`capacity-at-least-one--unlimited`). |
| expiring-cache | lifetime-positive | arbitrary | Graded: `ValueError` for lifetime <= 0. Alternatives: no check, so entries expire immediately (`lifetime-positive`); lifetime <= 0 means entries never expire (`lifetime-positive--never-expires`). |
| histogram-bins | one-count-per-bin | preserve | The starter already returns one count per bin, zeros included. A rewrite might return only non-empty bins (`one-count-per-bin`). |
| histogram-bins | uneven-bins | inferable | The caller passes explicit edges, so bins must follow them. The starter's equal-width assumption is a bug (`uneven-bins`). |
| histogram-bins | internal-edges | preserve | The starter already counts a value on an internal edge in the bin that starts there (equal-width bins). A rewrite might use right-closed bins (`internal-edges`). |
| histogram-bins | first-edge | inferable | **Borderline.** Once internal edges go right, bins are `[low, high)` and the first edge belongs to the first bin. The starter's strict `low <` is the bug. Alternatives: drop it (`first-edge`, the starter's behaviour) or raise. |
| histogram-bins | last-edge | arbitrary | Graded: a value on the last edge counts in the last bin (NumPy's convention, a known attractor). Alternatives: drop it, as the starter does (`last-edge`); treat it as outside a uniformly half-open range and raise (`last-edge--raises`). |
| histogram-bins | out-of-range | arbitrary | Graded: `ValueError`. Alternatives: ignore the value, as the starter does (`out-of-range`); clamp it into the end bins (`out-of-range--clamped`). |
| meeting-starts | avoids-busy | preserve | Away from endpoint contact, the starter already returns exactly the starts whose meeting avoids every busy interval, for any busy order. A rewrite might test only whether the start lies inside a busy interval (`avoids-busy`). Boundary contact belongs to `half-open-intervals`, so this contract and its checks exclude it. |
| meeting-starts | half-open-intervals | inferable | Back-to-back meetings and meetings ending at the end of the day are normal calendar behaviour. The starter's strict comparisons are a bug (`half-open-intervals`). |
| meeting-starts | invalid-busy-interval | arbitrary | Graded: `ValueError` when end <= start. Alternatives: ignore the interval (`invalid-busy-interval`); swap reversed ends (`invalid-busy-interval--swapped`); reject only reversed intervals, and drop zero-length ones because they block nothing (`invalid-busy-interval--zero-allowed`). |
| meeting-starts | positive-length | arbitrary | Binary. Graded: `ValueError` for length <= 0. Alternative: return `[]`, since no real meeting fits (`positive-length`). The earlier "no check" mutant was dropped: negative lengths produce candidate starts, which is not a coherent policy. |
| meeting-starts | empty-day | arbitrary | Graded: `ValueError` when day_end <= day_start. Alternatives: no check, so the result is `[]`, as in the starter (`empty-day`); reject only a reversed day and return `[]` for an empty one (`empty-day--reversed-only`). |
| option-string | basic-pairs | preserve | The starter already turns "a=b,c=d" into a dict and keeps letter case. A rewrite might lower-case keys (`basic-pairs`). |
| option-string | value-contains-equals | inferable | Splitting at the first "=" is the near-universal convention (`urllib.parse.parse_qsl` does it), and values such as base64 tokens contain "=". Alternatives: split at the last "=" (`value-contains-equals`) or raise, as the starter does. |
| option-string | duplicate-keys | arbitrary | Graded: `ValueError`. Alternatives: last wins, as in the starter (`duplicate-keys`); first wins (`duplicate-keys--first-wins`). |
| option-string | bare-key | arbitrary | Graded: "verbose" sets the key to `True`. Alternatives: an empty-string value, as `str.partition` gives (`bare-key`); `None` (`bare-key--none`); `ValueError`, as the starter does (`bare-key--raises`). |
| option-string | empty-items | arbitrary | Graded: empty items from doubled, leading or trailing commas are skipped. Alternatives: treat an empty item as a bare empty key (`empty-items`); `ValueError`, as the starter does (`empty-items--raises`). |
| option-string | surrounding-spaces | arbitrary | Graded: spaces around keys and values are ignored. Alternatives: keep them, as the starter does (`surrounding-spaces`); strip only the ends of each item, so spaces around "=" stay (`surrounding-spaces--item-ends`). |
| pagination | returns-list | preserve | The starter's `list(...)` already returns a list for tuples and strings. A rewrite might return the sliced sequence's own type (`returns-list`). |
| pagination | past-end-is-empty | preserve | The starter's slicing already returns `[]` past the last page. A rewrite might raise instead (`past-end-is-empty`). |
| pagination | page-numbering | arbitrary | Graded: pages start at 1, and a page below 1 raises `ValueError`. The previous "one-based" and "page-below-one" decisions were merged, because rejecting page 0 revealed the numbering. Alternatives: 0-based pages, as in the starter (`page-numbering`); 1-based with pages below 1 clamped to 1 (`page-numbering--clamped`); 1-based with `[]` below 1 (`page-numbering--empty`). |
| pagination | count-rounds-up | inferable | A final, partly filled page is still a page. Ceiling division is the universal convention; the starter floors (`count-rounds-up`). |
| pagination | per-page-positive | arbitrary | Graded: `ValueError` from both functions. Alternatives: per_page < 1 means everything on one page (`per-page-positive`); treat it as 1 (`per-page-positive--clamped`). The earlier "no check" mutant was dropped, because negative sizes give no coherent result. The starter still fails the checks. |
| pagination | negative-total | arbitrary | Binary. Graded: `ValueError`. Alternative: clamp to 0 pages (`negative-total`). The earlier "no check" mutant was dropped, because a negative page count is not a coherent policy. The starter still fails the checks. |
| port-spec | malformed-raises | preserve | The starter's `int()` and unpacking already raise `ValueError` for "http", "80-", "1-2-3" and "8o". A rewrite might skip bad items (`malformed-raises`). |
| port-spec | inclusive-ranges | inferable | "8000-8002" means three ports in every configuration convention. The starter's `range(low, high)` is an off-by-one bug (`inclusive-ranges`). |
| port-spec | port-max | inferable | 65535 is the largest TCP/UDP port, so higher numbers are errors. Alternatives: accept them (`port-max`, the starter's behaviour) or clamp. Port 0 is not graded. |
| port-spec | duplicate-ports | arbitrary | Graded: each port listed once. Alternatives: keep duplicates, as the starter does (`duplicate-ports`); `ValueError` for a repeated port (`duplicate-ports--raises`). Sorting is now a stated prerequisite ("a sorted list"), not part of this decision. |
| port-spec | backwards-range | arbitrary | Graded: `ValueError` for "10-5". Alternatives: swap the ends (`backwards-range`); return no ports for that range, as the starter does (`backwards-range--empty`). |
| port-spec | empty-spec | arbitrary | Graded: "" or whitespace gives `[]`. Alternatives: `ValueError`, as the starter does (`empty-spec`); an empty spec means every port (`empty-spec--all-ports`). |
| table-render | header-sets-width | inferable | A long header that pushes the separators out of line is visibly broken. The starter sizes columns from cells only (`header-sets-width`). |
| table-render | empty-rows | arbitrary | Relabelled from inferable after the first audit. The starter's format sentence now applies only when there is at least one row, so empty-table output is left open. Graded: the header line and the separator line. Alternatives: `""` (`empty-rows`); `ValueError`, as the starter does (`empty-rows--raises`); the header line alone (`empty-rows--header-only`). |
| table-render | numbers-right-aligned | arbitrary | Graded: int and float cells right-aligned, everything else (digit strings too) left-aligned. Alternatives: left-align everything, as the starter does (`numbers-right-aligned`); right-align every body cell (`numbers-right-aligned--all-right`); right-align ints only (`numbers-right-aligned--ints-only`). |
| table-render | none-is-blank | arbitrary | Graded: `None` renders as an empty cell. Alternatives: "None" via `str()`, as the starter does (`none-is-blank`); a "-" placeholder (`none-is-blank--dash`). |
| table-render | ragged-rows | arbitrary | Graded: `ValueError` for too many or too few cells. Alternatives: render rows as given, so missing cells are left out and extra cells are dropped (`ragged-rows`); pad short rows with blanks and drop extra cells (`ragged-rows--padded`). |

## Choices that are deliberately ungraded

No check depends on these choices, so an agent may decide them either way.
The throwaway validator's probes confirm that flipping each one changes no
check.

- bill-split: empty or negative weights, which are outside the declared
  domain.
- duration-parse: uppercase units, unit order, decimals and outer whitespace.
- expiring-cache: whether expired entries are evicted first, and whether
  time may run backwards (every check calls the cache in time order).
- histogram-bins: invalid edge lists.
- meeting-starts: the candidate grid (every check uses a day_start aligned
  to step), and the order of validation.
- option-string: conversion of numeric values, and the empty string as input.
- pagination: `page_count(0, n)`.
- port-spec: port 0, spaces in specs, and empty items in a nonblank spec
  (the `malformed-raises` contract lists its classes and excludes them).
- table-render: trailing spaces (every check compares lines after
  `rstrip`), and bool cells, which the starter docstring excludes.
