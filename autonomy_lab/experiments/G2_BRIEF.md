# Gate G2 decision brief

**Status:** prepared 2026-10-17 by the coordinator, for the user's decisions at G2 (and G4 for the budget). Nothing here is decided until the user approves it.
**Evidence:**
- power simulations: `autonomy_lab/results/power-t5-05c93d5/`;
- T7 plumbing run `run-20261017T162631Z-97ecc5` (local; per-decision results withheld until blind label approval);
- budget usage: STATE.json `loop.budget_used`.

## What G2 must settle

G2 freezes the task bank, its labels and the E1 sample size (LOOP_SPEC §8.4). Seven items are open.

### 1. The bank and its labels (blocked on you)

- **Labels:** approve the labels of the 9 coordinator tasks blind, without looking at T7 results. Review `autonomy_lab/tasks/LABELS.md` and the borderline labels flagged there.
- **Your tasks:** add your 3 tasks (`autonomy_lab/tasks/README.md`), which brings the bank to 12. Alternatively, ask the coordinator to write 3 more; the authorship comparison is then lost.
- **The late fix:** keep or revert the post-round-3 bank patch (`8e08636`). It contains the round-3 auditor's own pre-tested check additions, applied after the three-round review limit.

### 2. E1 size

**Recommendation: 12 tasks × 16 trials = 192 trials.** At production strength (1,000 simulations × 10,000 bootstrap resamples), with the draft bank's decision mix:

| Prediction | Strong scenario, % supported (± Monte Carlo SE) | Note |
|---|---|---|
| P1 (coverage effect) | 100 ± 0.2 | Also 91% falsified under the null scenario, so the test can fail. |
| P2 (contract ≈ check) | 97 ± 0.5 | |
| P3 (filler has no effect) | 1 ± 0.4 | Effectively untestable. |
| P4 (no spillover) | 1 ± 0.2 | Effectively untestable. |

### 3. P3 and P4: keep as tests, or make them descriptive

They cannot reach "supported" at any size up to 16 × 32. The binding constraint is the inferable stratum: only 1–2 inferable decisions per task. **Recommendation:** make P3 and P4 *descriptive* in the E1 pre-registration. Report their estimates and CIs, but give no verdict. A pre-registered test that cannot succeed adds nothing but the appearance of rigour. The alternative is to keep them and expect "inconclusive".

### 4. E2 size (P5, disclosure)

| E2 design | Trials | P5 supported (effective scenario) | Approx. cost |
|---|---|---|---|
| 12 × 4 × 2 (planned) | 96 | 18 ± 1.2% | $6.4 + about $1.3 judging |
| 12 × 12 × 2 | 288 | 81 ± 1.2% | $19.3 + about $3.8 judging |

**Recommendation:** run E2 at 288 trials if the budget allows; otherwise defer E2 rather than run an 18%-power version. A P5 that is almost certain to be inconclusive costs money and settles nothing.

**Known limitations of the P5 measurement:**

- **Heuristic redaction.** Hiding the arm from the judge relies on rewriting instruction-origin wording (for example, "as you asked" becomes a neutral "Assumption:"). The human audit sees the same redacted text, so κ measures the judge's reliability, not redaction errors. The redaction tests cover 28 realistic phrasings, but this stays a residual risk.
- **Fragility to unjudged decisions.** One timed-out or cut-off E2 trial leaves its decisions unjudged, and under the clarified rule that makes the whole P5 verdict inconclusive. **Decide before freeze** whether to keep that rule or tolerate a small pre-registered unjudged fraction.

**Escalated: T6 arm redaction (after three review rounds, two medium findings open).** The judge must not learn which arm a trial came from. The current design rewrites instruction-origin wording in the agent's text: "You asked me to list assumptions, so X" becomes "Assumption: X". Each review round closed one direction of bias and exposed the other:

- **Inventing a disclosure:** "You asked me to list changes, *not* assumptions: X" becomes "Assumption: X".
- **Dropping one:** a list heading's scope is lost when the first item carries its own flag; multi-line docstring framing is removed.

The human audit sees the same rewritten text, so κ cannot detect either error. The options:

| Option | What changes | Cost |
|---|---|---|
| **(a) Minimal redaction** (recommended) | Delete only a verbatim quotation of the arm sentence. Leave every other agent word unchanged, and accept that the judge may see "you asked me to list assumptions". | Small code change and a re-run of the agreement test (12 calls). |
| (b) Keep the rewriting | Continue fixing it. | More review rounds, no convergence in sight. |
| (c) Structural measure | Ask each arm for a machine-readable section. | Changes H3 itself, because plain-arm agents would then be prompted. |

Why (a): the judge's question is per decision ("was *this* behaviour flagged?"), and the agent's own words answer it. An instruction residue changes what a sentence means only marginally, whereas the rewriting changes it outright. Leftover cues to the arm then become a stated limitation of P5. They are not hidden.

### 5. E3 (breadth, H4)

Joint H4 power is about 0% within the roughly 90 budgeted trials, and reaching 80% would take about 670–960 trials. **Recommendation:** drop E3 from this round and record it as "not testable at this budget". The alternative is to re-pre-register it as falsification-only: it could still detect large interference, but its false-falsification rate (6–16%) runs above nominal at that size.

### 6. Replication on a second model family (`meta/muse-spark-1.3`)

40 trials of E1's `none` vs `contract` contrast cost about $2.7 at the primary model's rate; meta's own rate is unmeasured. **Recommendation:** defer it until E1 shows an effect worth replicating.

### 7. Budget (G4)

**Spent since G1:** about $21.3 provider-reported. Reviews account for $20.8 of it, against an estimate of $5; this went unnoticed because spend wasn't tracked as it accrued. Two further gaps:

- **Unpriced:** about 92M Claude writer tokens (claude-bridge reports $0).
- **Notional:** the OpenAI figures are list-price estimates under ChatGPT sign-in.

**Measured benchmark rate (T7):** $0.067 and about 34 s per trial, against the $0.042 pilot estimate.

| Plan | Trials | Added cost | Loop total | Fits the $40 cap? |
|---|---|---|---|---|
| T8 + E1 | 228 | about $16 | about $37 | yes |
| T8 + E1 + E2 at 96 | 324 | about $23 | about $45 | no |
| T8 + E1 + E2 at 288 + replication + reserve | 581 | about $43 | about $65 | no |

**Recommendation:**

- **If the provider-reported figures are real charges:** approve "T8 + E1" now. Decide E2 at G3, once E1 results exist.
- **If they are notional** (subscription usage): raise the cap to about $70 and approve the full plan, with E2 at 288 and E3 dropped.

Either way, review-session spend is now tracked as it accrues.

**The cap on auxiliary calls also needs raising.** The T6 judge makes one call per unsettled arbitrary decision, which keeps each judgement independent:

| E2 size | Judge calls | Approx. cost |
|---|---|---|
| 96 trials | 240 or more | about $1.30 |
| 288 trials | about 950 | about $5 |

Both exceed the 250-call cap on auxiliary calls in §8.1; 19 calls have been used so far. **Recommendation:** replace the call-count cap with the dollar cap. The count was only a proxy for cost.

## Not decided here

- E1's design seed and frozen hashes are fixed when the bank is frozen; see `experiments/E1.md`.
- The E4 leverage study stays deferred (D6).
