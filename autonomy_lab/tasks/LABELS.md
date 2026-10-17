# Proposed decision labels (coordinator draft, for G2 approval)

Approve, change or reject each label **before seeing any agent results**
(D3, §3.3). The labels mean:

- **inferable**: a competent engineer with only the brief, the starter and
  normal conventions would pick the graded behaviour with more than 80%
  confidence.
- **arbitrary**: at least two other reasonable behaviours exist. Each line
  below lists them.
- **preserve**: the starter already does this correctly, and it must not
  break.

"Starter" in a reason is the task's `starter.py`. Every coordinator starter
passes exactly its `preserve` checks and fails every other check. Lines marked
**borderline** are the ones most worth a second look.

Totals for the 9 tasks and 51 decisions: 33 arbitrary, 10 inferable and
8 preserve.

| Task | Decision | Label | Reason (alternatives for arbitrary) |
|---|---|---|---|
| bill-split | exact-total | inferable | Money splits that do not add back up to the bill are wrong by any convention. The starter's per-share `round()` gains or loses cents. |
| bill-split | largest-remainder | arbitrary | Graded: leftover cents go to the largest fractional parts. Alternatives: the first parties in the list, the last parties, the largest weights, or one party takes the whole remainder. |
| bill-split | ties-to-earlier | arbitrary | Graded: tied fractional parts favour earlier parties. Alternatives: later parties, the larger weight, or a rotating order. |
| bill-split | zero-weight-share | preserve | The starter already gives weight-0 parties 0. A rewrite might reject zero weights instead. |
| bill-split | invalid-weights | arbitrary | Graded: `ValueError` for an empty list, a negative weight, or all-zero weights. Alternatives: `ZeroDivisionError` (the starter's behaviour), return `[]` for an empty list, or accept negative weights as offsets. |
| bill-split | negative-total | arbitrary | Graded: `ValueError`. Alternatives: split a refund symmetrically (negate the positive split), or apply floor arithmetic to negatives as the starter does. |
| duration-parse | combined-units | preserve | The starter's `findall` already sums multi-unit strings such as "1h30m". |
| duration-parse | invalid-text | inferable | A parser that returns 0 for "soon" or "" hides errors. `ValueError` is the Python convention for unparseable text. |
| duration-parse | bare-number-seconds | arbitrary | Graded: "90" means 90 seconds. Alternatives: `ValueError` because the unit is required (Go's rule), or minutes. |
| duration-parse | case-insensitive-units | arbitrary | Graded: "2H" equals "2h". Alternatives: reject uppercase. "M" could also mean months, so rejecting is equally defensible. |
| duration-parse | canonical-order | arbitrary | Graded: units must appear in d, h, m, s order, each at most once; anything else raises `ValueError`. Alternatives: accept any order and sum it (the starter's behaviour), or let a repeated unit add up or override. |
| duration-parse | whole-numbers-only | arbitrary | Graded: "1.5h" raises `ValueError`. Alternatives: accept it as 5400 s, or round or truncate fractional seconds. |
| expiring-cache | missing-key-default | preserve | The starter's `get` already returns `default` for an unknown key. A rewrite might raise `KeyError` or ignore `default`. |
| expiring-cache | expiry-boundary | arbitrary | Graded: expired once `now >= t + ttl`. Alternative: still valid at exactly `t + ttl`, as the starter's `>` comparison does. |
| expiring-cache | fixed-lifetime | arbitrary | Graded: a read does not extend lifetime. Alternative: sliding expiry, where a read refreshes the entry, as the starter does. Both are standard cache designs. |
| expiring-cache | eviction-order | arbitrary | Graded: evict the entry whose most recent put is oldest; reads do not protect an entry. Alternatives: LRU, where reads count; FIFO by first insertion (the starter's dict order); evict the soonest-expiring entry; or refuse the new key. |
| expiring-cache | update-does-not-evict | inferable | Replacing an existing key in a full cache should not throw out an unrelated key. The starter's capacity check runs before its existence check, which is a plain bug. |
| expiring-cache | invalid-settings | arbitrary | Graded: `ValueError` for capacity < 1 or ttl <= 0. Alternatives: capacity 0 means caching is disabled (as with `functools.lru_cache(maxsize=0)`), or accept and misbehave as the starter does. |
| histogram-bins | one-count-per-bin | preserve | The starter already returns `len(edges) - 1` counts, including zeros and the empty-input case. |
| histogram-bins | uneven-bins | inferable | The caller passes explicit edges, so bins must follow them. The starter's assumption of equal widths is clearly a bug. |
| histogram-bins | lower-edge-inclusive | arbitrary | Graded: bins are `[low, high)`, so an edge value goes to the right-hand bin. Alternatives: `(low, high]` bins, or exclude boundary values as the starter's strict comparison does. |
| histogram-bins | last-edge-included | arbitrary | Graded: a value equal to the final edge is counted in the last bin (numpy's convention). Alternatives: treat it as out of range so every bin is half-open, or drop it as the starter does. |
| histogram-bins | out-of-range-raises | arbitrary | Graded: `ValueError`. Alternatives: silently ignore the value (the starter's behaviour), clamp it into the end bins, or count it in underflow and overflow totals. |
| meeting-starts | avoids-busy | preserve | The starter already lists every fitting start that overlaps no busy interval, in ascending order, for any order of busy intervals. |
| meeting-starts | clock-aligned-starts | arbitrary | Graded: starts are multiples of `step` from midnight, e.g. 9:05 rounds up to 9:15. Alternative: `day_start` plus multiples of `step` (the starter's behaviour). |
| meeting-starts | half-open-intervals | inferable | Back-to-back meetings, and meetings that end exactly at the end of the working day, are normal calendar behaviour. The starter's strict comparisons are a bug. |
| meeting-starts | invalid-busy-interval | arbitrary | Graded: `ValueError` when end <= start. Alternatives: ignore the interval (the starter's behaviour), swap its ends, or treat a zero-length interval as harmless. |
| meeting-starts | positive-length | arbitrary | Graded: `ValueError` for length <= 0. Alternatives: return `[]`, or treat zero length as a valid instant (the starter returns every step). |
| order-status | forward-path | preserve | The starter's table already handles pay, ship and deliver along the main path. |
| order-status | illegal-event-raises | arbitrary | Graded: `ValueError`. Alternatives: `KeyError` (the starter's behaviour), return the status unchanged, return `None`, or raise a custom exception. |
| order-status | repeat-is-noop | arbitrary | Graded: repeating the event that produced the current status (e.g. pay on paid) returns the status unchanged. Alternative: treat it as an illegal transition and raise. |
| order-status | cancel-until-shipped | arbitrary | Graded: cancel is allowed while paid but not once shipped. Alternatives: only while pending (the starter's table), or until delivered. |
| order-status | refund-after-delivery | inferable | **Borderline.** The starter lists `refund` and `refunded` with no transition into them, and delivered → refunded is the obvious missing edge. An engineer might add other refund sources too; the check does not penalise that. The risk is that an engineer does not notice the gap at all, which would make the confidence below 80%. |
| pagination | past-end-is-empty | preserve | The starter's slicing already returns `[]` past the last page. A rewrite might raise or clamp instead. |
| pagination | one-based-pages | arbitrary | Graded: page 1 is the first page. Alternative: 0-based, which is the starter's behaviour and Python's indexing convention. Both are common in APIs. |
| pagination | page-below-one | arbitrary | Graded: `ValueError` for a page below 1. Alternatives: return `[]`, or clamp to page 1. |
| pagination | count-rounds-up | inferable | A final, partly filled page is still a page. Ceiling division is the universal convention. |
| pagination | empty-is-one-page | arbitrary | Graded: `page_count(0, n) == 1`. Alternative: 0 pages. Both are widespread in UI libraries. |
| pagination | per-page-positive | arbitrary | Graded: `ValueError` from both functions. Alternatives: `ZeroDivisionError` (the starter's behaviour), treat it as "everything on one page", or fall back to a default page size. |
| port-spec | malformed-raises | preserve | The starter's `int()` and tuple unpacking already raise `ValueError` for "http", "80-" and "1-2-3". |
| port-spec | inclusive-ranges | inferable | "8000-8002" means three ports in firewall and configuration conventions. The starter's `range(low, high)` is an off-by-one bug. |
| port-spec | sorted-unique | arbitrary | Graded: ascending order with duplicates removed. Alternatives: keep input order, keep duplicates, or raise on duplicates. |
| port-spec | backwards-range | arbitrary | Graded: `ValueError` for "10-5". Alternatives: swap the ends, or return no ports (the starter's behaviour). |
| port-spec | port-bounds | arbitrary | Graded: only 1–65535 are valid. Alternatives: allow 0 (often "any port"), or no range check (the starter's behaviour). The check includes 0, so it is not only the obvious 65536 case. |
| port-spec | empty-spec | arbitrary | Graded: "" or whitespace gives `[]`. Alternative: `ValueError` (the starter crashes with one). |
| table-render | header-sets-width | inferable | A table whose long header pushes the separators out of line is visibly broken. The starter sizes columns from the cells only. |
| table-render | empty-rows | inferable | **Borderline.** With no rows, the table is the header and separator lines (the starter crashes). Returning `""` or raising is possible but less likely. |
| table-render | numbers-right-aligned | arbitrary | Graded: int and float cells are right-aligned. Alternative: left-align everything, as the starter does. |
| table-render | none-is-blank | arbitrary | Graded: `None` renders as an empty cell. Alternatives: "None" (the starter, via `str()`), "-", or "null". |
| table-render | no-trailing-spaces | arbitrary | Graded: strip trailing padding from every line. Alternative: keep every line padded to the full width (the starter's rectangular output). |
| table-render | ragged-rows | arbitrary | Graded: `ValueError` for rows with too many or too few cells. Alternatives: pad short rows, drop extra cells (the starter's `zip`), or `IndexError`. |
