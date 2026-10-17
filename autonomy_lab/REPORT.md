# Delegation succeeds to the extent decisions are resolved and checkable

**Status:** working draft, 2026-10-17. The evidence is one model and two toy tasks, so treat every finding below as a hypothesis with early support, not a result.
**Branch:** `experiment/autonomy-lab`. Raw trajectories stay local and are not committed; run IDs below identify them.
**Companion files:** [LOOP_SPEC.md](LOOP_SPEC.md) (next experiments), [SESSION_HANDOFF.md](SESSION_HANDOFF.md) (full provenance), [STATE.json](STATE.json).

## 1. Question

Suppose an engineer spends a full day writing a specification — contract, decisions, tests, acceptance criteria — and then hands execution to an autonomous coding agent with no further intervention. When does that produce more accepted work per human hour than spending the day coding?

The quantity we care about is leverage:

    L = independently accepted work / (human planning + intervention + review time)

Agent runtime is not the goal. A long unattended run that produces work nobody can accept without re-deriving it has low leverage.

## 2. Claim under test

> An agent's unattended output is acceptable to the extent that every consequential decision in the work was resolved — stated in the contract or encoded in a check the agent can run — before execution began. Unresolved decisions are not escalated; the agent fills them silently with plausible choices.

Two consequences follow if the claim holds:

1. **Planning depth means decision coverage, not prose volume.** The useful part of a planning day is the set of consequential decisions it settles. Extra words that settle nothing should not help.
2. **Silent gap-filling is the main threat to leverage.** If the agent reports success whether or not it guessed, the reviewer cannot tell which parts to check, and review time stays high.

### Relation to the original model

The handoff started from `P(success) ≈ (1 − q)^B`, with a per-decision failure rate `q` that falls with depth `D` and verifiability `V`. The pilot suggests a more operational form: treat each consequential decision `d` separately.

    P(accepted) ≈ Π_d p_d
    p_d ≈ 1 − ε     if d is settled by the contract or a visible check
    p_d ≈ π_d       if d is unsettled

`ε` is the capable agent's residual execution error. `π_d` is the probability that the agent's own default happens to match the hidden choice. That is high for decisions any competent engineer would make the same way (*inferable*) and can be near zero for decisions with several reasonable answers (*arbitrary*). Under this view, a planning day buys acceptance by settling the arbitrary decisions, and coverage `D` is the fraction of them settled. This is a hypothesis, not a fitted law; the `ε`, `π_d` and independence assumptions are exactly what the next experiments test.

## 3. Method

**Harness.** A local Python runner launches one headless Pi CLI process per trial (`--mode json --no-session`), with extensions, skills, prompt templates, context files and MCP disabled and only the `read`, `bash`, `edit` and `write` tools enabled. Each trial gets a fresh workspace containing the starter code, `prompt.txt` and `visible_checks.py`. No human can intervene during a trial.

**Pinned runtime.** `--model provider/id` and `--thinking` are required. A trial counts only if every assistant message reports the requested model and thinking level and the event stream parses cleanly. This was added after we found that an unpinned run silently used a different model than its metadata claimed.

**Acceptance.** A trial is accepted only if all of these hold: held-out checks pass, `prompt.txt` and `visible_checks.py` are byte-identical to the originals, the agent settled without errors or timeout, and the runtime matched the request. Process exit status alone is not evidence: in one trial Pi exited 0 after the provider rejected every request.

**Conditions.** A 2×2×2 factorial:

| Factor | Levels |
|---|---|
| Breadth | 1 task (`ordered-merge`) or 2 tasks (`ordered-merge` + `atomic-ledger`) |
| Depth | *shallow*: a one-line brief; *deep*: an explicit contract with invariants and error behaviour |
| Verification | *basic*: one smoke assertion per task; *rich*: several edge-case assertions per task |

**Model.** `openai-codex/gpt-5.6-sol`, thinking `high`, via Pi 1.3.0, using the user's ChatGPT-account Codex access.

## 4. Results

### 4.1 First trial

Run `run-20261017T130316Z-e15424`, condition breadth 1 / deep / rich: accepted in 8.6 s, 4 turns, 5 tool calls, no corrections. An earlier attempt on `gpt-5.5` was rejected by the provider before any tool call and is recorded as an infrastructure failure, not an agent result.

### 4.2 Pilot

Run `run-20261017T131959Z-5b17ab`, 3 repetitions per cell, fixed condition order, 24 trials. Every trial settled, used the requested runtime, left its spec and checks intact, and had no retries or tool errors.

| Breadth | Depth | Verification | Accepted | Mean seconds |
|---|---|---|---|---|
| 1 | shallow | basic | 3/3 | 16.7 |
| 1 | shallow | rich | 3/3 | 12.2 |
| 1 | deep | basic | 3/3 | 10.3 |
| 1 | deep | rich | 3/3 | 9.7 |
| 2 | shallow | basic | 0/3 | 55.7 |
| 2 | shallow | rich | 0/3 | 35.2 |
| 2 | deep | basic | 3/3 | 26.6 |
| 2 | deep | rich | 3/3 | 27.1 |

`ordered-merge` passed its held-out checks in all 24 trials. Every failure was in `atomic-ledger`. Checking each breadth-2 solution against each hidden ledger requirement separately:

| Hidden requirement | Stated in shallow brief | Checked by rich visible tests | shallow + basic | shallow + rich | deep (both) |
|---|---|---|---|---|---|
| Input dict not mutated | no | yes | 0/3 | 3/3 | 6/6 |
| Invalid amounts (−2, 0, 2.0, `True`) raise `ValueError` | no | yes | 0/3 | 3/3 | 6/6 |
| Sender = recipient raises `ValueError` | no | no | 0/3 | 0/3 | 6/6 |
| Empty sender raises `ValueError` | no | no | 0/3 | 1/3 | 6/6 |
| Missing sender raises `KeyError` | no | no | 3/3 | 2/3 | 6/6 |

### 4.3 What the agents did with unresolved decisions

Without the contract, agents made reasonable but different choices from the hidden ones. For example, they treated a self-transfer as a no-op instead of an error, kept the starter's in-place mutation when no check forbade it, and raised `ValueError` instead of `KeyError` for an unknown sender. **None of the six failing trials mentioned an ambiguity or an assumption in its final message; all six reported success.** Some final messages did describe the chosen behaviour (for example, "handles self-transfers"), but as implemented features, not as guesses about unspecified requirements. At breadth 2, shallow trials also ran 1.3× (rich) to 2.1× (basic) longer than deep ones and still failed.

## 5. Interpretation

What the pilot supports, for this model and this task:

- **Execution was not the bottleneck when the contract was given.** All 12 contract trials were accepted, including all six two-task trials. The six breadth-1 shallow trials were also accepted, because `ordered-merge`'s hidden requirements coincide with the obvious reading of its brief; nothing settled them explicitly.
- **Visible checks closed the requirements they check, and did not reliably close the others.** Rich checks moved the two ledger requirements they encode from 0/3 to 3/3. The three unchecked requirements moved from 0/3, 0/3 and 3/3 to 0/3, 1/3 and 2/3, which is no consistent improvement. With three samples per cell, this suggests that checks and contract statements are two channels for settling decisions; it does not establish that.
- **Unsettled decisions were filled without disclosure.** That matters more for leverage than the failure itself. A reviewer reading these six reports saw descriptions of behaviour, but no marker saying which behaviours were guesses.

What it does **not** support:

- **A general depth effect.** The 6/6 vs 0/6 ledger contrast (Fisher exact two-sided p ≈ 0.002) is partly by construction: the hidden checks test decisions that only the deep prompt states. The non-trivial findings are the second and third bullets above.
- **Any breadth effect.** Breadth 2 is the only way the ledger task enters the design, so breadth is confounded with task identity.
- **Generality.** One model family, two hand-written toy tasks, three repetitions per cell, fixed order, trusted code only.
- **Cost claims.** The provider-reported total for the pilot was $1.01 for 272,922 tokens. Under ChatGPT-account sign-in this is probably a notional list-price figure, not a charge; that is unverified.

## 6. Threats to validity

1. **Construction.** Hidden requirements absent from the shallow brief measure missing information. The next design must separate decisions an engineer could infer from decisions that are genuinely arbitrary.
2. **Task bank.** Two tasks, both authored alongside the hypothesis. Task authors know what the experiment hopes to show, and the next experiments only partly mitigate this (cross-family audit, label approval by the user, optional user-authored tasks).
3. **Single model.** All agent evidence is from one model at one thinking level.
4. **Order.** Conditions ran in a fixed order; provider-side drift over the 10-minute run is unmeasured.
5. **Isolation.** Pi runs with the user's OS permissions. Held-out checks live in the same repository the trial directories sit inside, so an agent that deliberately looks outside its workspace can read them. A pattern scan of all 165 tool-call arguments in the 25 real trials found no parent-directory, absolute or home-relative paths. That scan is a heuristic: it cannot see shell expansion, `cd ..`, symlinks or code that opens files itself, so it does not prove the checks were not read.
6. **Small n.** Three repetitions per cell. Per-cell confidence intervals are wide (0/3 → 95% Wilson CI 0–0.56).

## 7. Next steps

The experiments in [LOOP_SPEC.md](LOOP_SPEC.md) test the claim directly:

1. **Coverage, not length.** Vary how many consequential decisions are settled, and separately add prompt length that settles nothing.
2. **Channel equivalence.** Settle the same decision by contract only, check only, or both, and measure spillover to unchecked decisions.
3. **Disclosure.** Score whether agents disclose unresolved decisions, and whether a one-line instruction changes that.
4. **Breadth.** With per-task coverage fixed, test whether success across several tasks is the product of single-task success rates.
5. **Leverage.** Time human specification work and compare accepted work per human minute.

## Provenance

| Item | Value |
|---|---|
| Runner commit for the pilot | `69ca419` |
| Pilot run | `run-20261017T131959Z-5b17ab` |
| First trial run | `run-20261017T130316Z-e15424` (at `bc5d061`) |
| Infrastructure-failure run | `run-20261017T130249Z-2d6c5a` (at `bc5d061`) |
| Pi version | 1.3.0 |
| Model | `openai-codex/gpt-5.6-sol`, thinking `high` |
| Host | Linux, Python 3.14.7 |
