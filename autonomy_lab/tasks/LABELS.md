# Decision labels (coordinator draft, revised after the T4 audits)

**Approved by the user on 2026-10-18 for the 9 coordinator tasks below, as proposed, without seeing any agent results** (D3, G2). Labels for tasks added later need their own blind approval.

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

## Pending blind approval (tasks 10–12)

Written by the coordinator after the 2026-10-18 G2 decision, without
seeing any trial results. These labels are **not approved yet**; they need
the same cross-family audit and their own blind approval. Each starter
passes exactly its `preserve` checks and fails every other held-out check.

Totals for the 3 tasks and 17 decisions: 12 arbitrary, 2 inferable and 3
preserve, with 24 mutants. config-merge has no `inferable` decision since
`inputs-unchanged` was relabelled (see below).

Changed after the first audit (`tasks-10-12-audit1`):

- byte-size: `short-units` now grades only whether a unit without its B is
  accepted. Its scale is ungraded, so the result no longer depends on the
  `si-prefixes` policy; `short-units--binary` was removed. `neutral.md` no
  longer talks about limits and thresholds.
- retry-delays: an immediate first retry is a new `first-delay`
  alternative. The `cap-clamps` and `multiplier-range` checks no longer
  depend on the first-delay rule. `positive-base` and `multiplier-range`
  cover int and float values in both channels. `base-above-cap--first-uncapped`
  was removed, because `max_delay` names an upper bound.
- config-merge: the `inputs-unchanged` checks contain overlapping lists,
  top-level and nested, and the decision is now `arbitrary`. The
  `nested-merge`, `none-value` and `list-values` contracts are narrowed to
  what their checks grade.
- All three starters state their input domain in the docstring.

Changed after the second audit (`tasks-10-12-audit2`; byte-size and
retry-delays had no findings): config-merge's visible `none-value` check
now includes `None` over a string, its held-out check `None` over a number,
and its visible `nested-merge` check a nested string conflict, so both
channels cover the same value types.

| Task | Decision | Label | Reason, then the graded behaviour and each alternative with its mutant |
|---|---|---|---|
| byte-size | decimal-numbers | preserve | The starter's `float()` already accepts "1.5KB". A rewrite with a digits-only pattern might reject it (`decimal-numbers`). The checks compare a decimal size with a whole one (`"1.5KB"` twice is `"3KB"`), so the meaning of KB is not also graded. |
| byte-size | si-prefixes | arbitrary | Binary. Graded: KB, MB, GB and TB are powers of 1000, as in SI and IEC 80000-13. Alternative: powers of 1024, the JEDEC and Windows reading, as in the starter (`si-prefixes`). |
| byte-size | binary-prefixes | inferable | KiB, MiB, GiB and TiB are defined as powers of 1024 by IEC 80000-13, so no other reading is reasonable. The starter does not know them and raises (`binary-prefixes`). |
| byte-size | short-units | arbitrary | Binary. Graded: a bare K, M, G or T is accepted as a unit. Alternative: the B is required, so `ValueError` (`short-units`); the starter raises `KeyError`. The scale of a bare unit is ungraded: the checks accept anything from the 1000-based to the 1024-based value and never compare with KB, so a parser that keeps K binary (as `ls -h`, JVM and Docker memory flags do) passes under either `si-prefixes` policy. |
| byte-size | fractional-bytes | arbitrary | Graded: a size that is not a whole number of bytes ("2.5B", "1.0005KB") raises `ValueError`. Alternatives: truncate, as the starter's `int()` does (`fractional-bytes`); round to the nearest byte (`fractional-bytes--nearest`); round up (`fractional-bytes--round-up`). The inputs are fractional under both readings of KB. |
| byte-size | negative-size | arbitrary | **Borderline**: rejecting a negative size may be the expected behaviour for most engineers. Binary. Graded: `ValueError`. Alternative: return the negative byte count, as the starter does, for size deltas (`negative-size`). The filler no longer mentions limits or thresholds, so it does not favour sizes that cannot be negative. |
| retry-delays | first-delay | arbitrary | **Borderline**: the name `base` may lean toward the graded behaviour. Graded: the first delay is `base`, and the multiplier first applies to the second delay (AWS's `base * 2 ** attempt` with attempt 0). Alternatives: the multiplier already applies to the first delay, as with tenacity's attempt numbering and the starter (`first-delay`); the first retry happens at once, then `base`, then growth (`first-delay--immediate`). |
| retry-delays | cap-clamps | preserve | The starter's `min()` already makes a delay that would grow past `max_delay` equal `max_delay`. A rewrite might stop growing at the last delay below the cap instead (`cap-clamps`). The checks use enough retries that the delays reach the cap under every first-delay alternative, and assert only that the largest and the last delay equal `max_delay`, so the first-delay rule is not also graded. |
| retry-delays | long-schedule | inferable | Nobody wants a schedule of a few thousand retries to crash. The starter's closed form `base * multiplier ** retry` overflows floats (`long-schedule`, the starter's formula). The checks assert the length, the cap and a constant tail, not the cap-clamping policy. |
| retry-delays | base-above-cap | arbitrary | Binary. Graded: a base larger than `max_delay` raises `ValueError`. Alternative: clamp every delay to `max_delay`, as the starter's `min()` does (`base-above-cap`). Returning an uncapped base as the first delay was dropped as an alternative: the name `max_delay` states an upper bound. A base equal to `max_delay` is not graded. |
| retry-delays | multiplier-range | arbitrary | Graded: a multiplier below 1 (fractions, 0 and negatives, as ints and floats) raises `ValueError`, and exactly 1 (int or float) is allowed, with no delay above `base`. The check asserts no exact schedule, so the first-delay rule is not also graded. Alternatives: a multiplier between 0 and 1 is allowed and the delays shrink, as in the starter (`multiplier-range`); the delays must grow, so 1 also raises (`multiplier-range--strict`). |
| retry-delays | positive-base | arbitrary | Binary. Graded: a base of zero or less raises `ValueError`. Alternative: a zero base is allowed and means retrying at once, while a negative one still raises (`positive-base`). The starter allows both and fails the check. Both channels use int and float zeros and negatives. |
| config-merge | type-change | preserve | The starter's `dict.update` already lets a string or number in `override` replace a dictionary in `base`, and the other way round. A deep-merge rewrite might recurse whenever the base value is a dictionary and crash (`type-change`). |
| config-merge | inputs-unchanged | arbitrary | Relabelled after the first audit: merging into `base` in place is a reasonable API, and the starter does it, so neither the brief nor the signature settles immutability. Graded: neither argument, nor anything inside it, is modified. Alternatives: an in-place deep update that writes into `base` and returns it, like the starter (`inputs-unchanged`); a variant of the in-place policy that copies dictionaries but extends `base`'s lists in place (`inputs-unchanged--lists-in-place`). The checks contain overlapping lists, top-level and nested, and assert only that the inputs are unchanged, not the merged result. |
| config-merge | nested-merge | arbitrary | **Borderline**: deep merging may be the expected behaviour for layered configuration. Binary. Graded: two dictionaries under the same key are merged recursively. The contract and checks cover only nested strings and numbers, so `None` and lists below the top level are not graded. Alternative: a shallow merge, where the override dictionary replaces the base one, as `dict.update` and `{**a, **b}` do and as the starter does (`nested-merge`). |
| config-merge | none-value | arbitrary | Graded (top level only): a `None` in `override` keeps the base value, or leaves the key absent, as with unset options. Alternatives: `None` is stored like any value, as in the starter (`none-value`); `None` deletes the key, as in JSON Merge Patch, RFC 7386 (`none-value--deletes`). Falsy values such as 0 and `False` still override in both checks, so a truthiness test fails this decision only. |
| config-merge | list-values | arbitrary | **Borderline**: the parameter name `override` may lean toward replacement. Graded (top level only): two lists under the same key are concatenated, base first. Alternatives: the override list replaces the base list, as in the starter (`list-values`); append only the override items that the base list lacks (`list-values--union`). |

Deliberately ungraded for tasks 10–12 (no check depends on these; probes that
flip them pass every check):

- byte-size: letter case and lowercase "b" as bits (every check uses
  canonical case), spaces, bare numbers without a unit, unknown units,
  whether a bare K, M, G or T is decimal or binary, and float precision
  (every decimal in a whole-size check is exact in binary).
- retry-delays: zero or negative `retries`, the validity of `max_delay`,
  a base equal to `max_delay`, int versus float results, and the order of
  validation.
- config-merge: how `None` and lists below the top level appear in the
  returned result (`inputs-unchanged` still grades that nested containers in
  the inputs are left unmodified), key order, whether the result shares
  nested objects with the inputs, and `None` values in `base`.
