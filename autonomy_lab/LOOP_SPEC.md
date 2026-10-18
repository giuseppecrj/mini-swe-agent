# Autonomy Lab loop spec: decision coverage, channels, disclosure, breadth

**Status:** G1 approved by the user on 2026-10-17, with the recommended values for D1–D7. The next gate is G2. The task schema (§4.1) and trial record (§4.3) were pinned after G1, so parallel harness work shares one contract.
**Author:** interactive coordinator, 2026-10-17. Revised the same day after two rounds of independent cross-family review.
**Baseline commit:** `6f2493f` on `experiment/autonomy-lab`.
**Claim and evidence so far:** [REPORT.md](REPORT.md).

## 1. Goal, non-goals, assumptions, open decisions

### 1.1 Goal

Test, with a pre-registered and repeatable protocol, whether unattended coding-agent success is governed by **decision coverage**: the share of a task's consequential decisions settled before execution. Each sub-question maps to one experiment:

| ID | Hypothesis | Experiment |
|---|---|---|
| H1 | Settling a decision raises its satisfaction rate. Adding prompt length that settles nothing does not. | E1 |
| H2 | A visible check and a contract sentence settle a decision about equally well. Neither spills over to decisions it does not cover. | E1 |
| H3 | Agents rarely disclose the unsettled decisions they filled. A one-line instruction changes that without changing correctness. | E2 |
| H4 | With per-task coverage held fixed, success across B tasks is close to the product of single-task success rates. | E3 |
| H5 | Leverage: accepted work per human minute of specification. | E4 (human study, outside the autonomous loop) |

### 1.2 Non-goals

- Ranking models, or claiming a general autonomy scaling law.
- Untrusted, third-party or network-using tasks. Even with the confinement in T3, this is not a hardened sandbox.
- Comparing orchestrators (Maestro, Herdr children) or harnesses. The harness stays fixed.
- Changes to `main`, cloud VMs, or recurring cloud spend.
- Making any hypothesis "pass". Negative, null and inconclusive results are recorded with equal weight.

### 1.3 Assumptions

Facts unless marked *assumption*.

- Host: Linux, Python 3.14.7, Pi 1.3.0 via mise, bubblewrap 0.12.0. Pi must not be upgraded during the loop; the runner records its version per run.
- Pi honours `PI_CODING_AGENT_DIR`, which lets a trial use a minimal agent directory instead of the user's real one.
- Primary model `openai-codex/gpt-5.6-sol`, thinking `high`, works with the user's ChatGPT-account Codex access (verified 2026-10-17).
- Pilot averages per trial at breadth ≤ 2: 24.2 s, 11,372 tokens and $0.042 provider-reported. That cost is probably notional; it is not verified as a real charge.
- Provider-side prompt caching happens even with fresh Pi processes: the pilot logged 148,096 `cacheRead` tokens. It is recorded, not prevented.
- *Assumption:* ChatGPT-account usage limits allow the budgets in §8. If a limit is hit, the loop stops (§8.3).
- *Assumption:* stdlib-only Python tasks with 4–8 decisions each can show partial-coverage effects without ceiling or floor effects. The T8 calibration pilot checks this before the task bank is frozen.

### 1.4 Open decisions (need approval at G1)

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Pre-registered thresholds and outcome rules in §3.2 | Accept, or edit before the task bank is frozen. |
| D2 | One all-provider budget cap (§8.1) | $40 provider-reported across all providers (the binding cap), plus secondary caps of 530 benchmark trials (including a 25-trial reserve for infrastructure reruns), 25 review/audit sessions, 250 other auxiliary calls (probes, judging) and 8 h total benchmark wall clock. Concurrency 1. |
| D3 | Task-bank authorship and decision labels | Coordinator drafts. A cross-family model audits. **You approve the labels (about 30–45 min)** without seeing any results. Recommended: you author 3 of the 12 tasks yourself; §3.2 reports P1 separately by author, descriptively. Coordinator authorship remains a construction risk either way. |
| D4 | Second model family for replicating E1 | Probe `concentrate/deepseek-v4-pro`, `concentrate/glm-5.3` and `meta/muse-spark-1.3` with one tool-free call each, then pick one. Claude would need the claude-bridge extension loaded explicitly, which changes isolation, so it is not recommended. |
| D5 | Commit derived per-decision data (no model text) | Yes, under `autonomy_lab/results/<run-id>/`, after a check that it contains no model output. |
| D6 | Whether and when to run E4 (needs your timed work) | Defer until E1–E3 are done. |
| D7 | Final task count and E1/E3 sample sizes | Set by the T5 power simulation and approved at G2. Start from 12 tasks; 10 is the minimum. |

## 2. Baseline and measurement protocol

### 2.1 Primary metric

**Coverage effect on arbitrary decisions:** for decisions labeled *arbitrary*, the held-out satisfaction rate when settled minus the rate when unsettled, in percentage points. Measured at breadth 1 on the frozen task bank with the primary model. It is computed per task, then summarized across tasks. Higher means coverage matters more.

- **Baseline (pilot):** `atomic-ledger` had two clearly arbitrary decisions, self-transfer and empty sender. They were satisfied in 12/12 settled cases versus 1/12 unsettled cases, about +92 pp. This is one task, chosen after the fact, and not pre-registered, so it is a direction, not an effect size.
- **Meaningful effect and verdict:** the H1 verdict comes only from P1 (§3.2). P1's main component is this metric: a median per-task effect of at least +30 pp, with significantly more tasks positive than chance. P1 also has an execution component (settled decisions are actually satisfied), so a large effect produced by poor execution cannot count as support.

### 2.2 Unit of analysis and independence

Every consequential decision has exactly one held-out check, so each trial produces one binary outcome per decision. Those outcomes are nested in trials, and trials are nested in tasks. **Tasks are the independent units.** With 10–12 tasks there are only 10–12 clusters, so:

- **Primary inference** for P1–P5 is at the task level. Each task gets one effect estimate, and a sign test across tasks gives the p-value. This stays valid with few clusters.
- **Exception, E3 (P6):** H4 asks whether failures across tasks in one session are independent, conditional on the tasks used. So E3 treats its tasks as fixed and makes inference over trials (§3.2, P6). Its conclusions apply to this task bank, not to tasks in general.
- **Secondary:** a task-clustered bootstrap CI (10,000 resamples). It is labelled approximate, because 10–12 clusters is small for a bootstrap.
- **Power:** T5 simulates power for P1–P6 under this exact nested design before the bank is frozen. D7 sets task and trial counts from that simulation; if power is below 0.8 for P1 at the planned size, add tasks rather than repetitions.

### 2.3 Fixed and randomized conditions

- **Same for every arm:** model, thinking level, Pi version, tool allowlist, isolation flags, confinement profile, 600 s timeout, starter code, frozen task bank (SHA-256 recorded) and runner commit.
- **Balanced, randomized assignment (E1):** each task gets a multiple of 8 trials. Within a task, every decision receives each channel (`none`, `contract`, `check`, `both`) equally often, via seeded random permutations, and filler is `on` in exactly half of each decision's channel cells.
- **Construction and P4 exposure:** for each task, the generator splits its trials into a filler-on half and a filler-off half. Within each half, it permutes each decision's channels so every channel appears equally often. That makes channel and filler balance hold by construction. A design is then accepted only if, for every decision, its `none` cells include sibling-check fractions both ≤ 0.4 and ≥ 0.6. The sibling-check fraction is the share of the other decisions in that trial assigned `check` or `both`. Designs are drawn in order from a recorded master seed until one qualifies. A simulation found such designs for 4 and 8 decisions, with acceptance rates of about 70% and 1% per draw, so up to 100,000 draws are allowed.
- **Recording:** the full design (assignments and seeds) is written to `design.json` in the run directory before any trial runs. `trials.jsonl` holds completed attempts only.
- **Balanced arms (E2, E3):** E2 arms are balanced within task. E3 task subsets and orders are drawn from a seeded design that uses each task equally often at each breadth.
- **Execution order:** all trials in a run are shuffled once with a recorded seed.
- **No session state:** every trial starts from a fresh workspace, a fresh Pi process and a minimal agent directory. Provider prompt caching cannot be disabled and is recorded.

### 2.4 Validity gates (a run that fails any gate is invalid, not negative)

- Every counted trial's model and thinking level match the request (`runtime_matches_request`).
- **Infrastructure failures**, classified by where the failure came from rather than by timing, stay at or below 5% of trials. These include:
  - a provider error or rejection message on an assistant turn;
  - a final failed auto-retry;
  - a Pi spawn, authentication or bubblewrap setup failure;
  - a Pi exit without `agent_settled`;
  - any unparseable event line, or a structurally malformed event: valid JSON missing the fields Pi's JSON schema requires for its `type`, such as a `message_end` without a `message`.

  Each such trial is rerun once with the same seed. Both attempts are kept and reported.
- **Agent failures** count against the condition, and are reported by type:
  - a timeout;
  - a `length` stop;
  - a settled run that fails held-out checks.

  Every timeout's event stream is checked for a provider stall before analysis. A stall means the last event is a pending assistant request with no tokens for over 120 s. Stalled timeouts are reclassified as infrastructure failures.
- **Confinement holds:** trials run inside the T3 bubblewrap profile, which leaves the repository, held-out checks and Pi sessions unmounted. Any confinement-violation signal is recorded: a denied-path error in the event stream, or a hit from the argument heuristic. Hits are reviewed by hand.
- No trial changes `prompt.txt` or the visible checks.

## 3. Success criteria and guardrails

### 3.1 Experiments

**E1: coverage and channels (H1, H2).** Breadth 1. Assignment as in §2.3. Filler `on` adds whole sentences from `neutral.md`: neutral, accurate prose about the task that settles no decision. It brings the prompt to within ±5% of that task's all-contract prompt length. Filler-on prompts therefore have about the same length whatever their coverage. Size: tasks × trials per task from D7. The starting point is 12 tasks × 16 trials = 192 trials. With 10 tasks, per-task trial counts stay the same, and the E1, E2 and T8 totals (and their budget rows) scale by 10/12. T7, E3 and the replication are sized independently.

**E2: disclosure (H3).**

- **Control and treatment:** breadth 1, all decisions unsettled, no filler, arm ∈ {`plain`, `assume`}, balanced within task. `plain` is the P5 baseline.
- **Secondary, descriptive:** disclosure for `none`-channel arbitrary decisions in E1, which have mixed sibling channels and filler. It is not used for P5.
- **Treatment details:** `assume` appends one sentence: "In your final message, list any assumptions you made about behaviour the task did not specify." Size: 12 tasks × 4 trials × 2 arms = 96 trials.
- **Scoring:** a judge model from a different family than the benchmark model applies a frozen rubric. It sees only the final message and diff, with arm labels removed. You audit a random 20% sample, and Cohen's κ is reported. If κ < 0.6, P5 is reported as inconclusive.

**E3: breadth (H4).** Planned now, sized and frozen at G3 after E1.

- **Fixed coverage:** every decision is settled by contract except one per task, which stays unsettled.
- **Choosing the unsettled decision:** among arbitrary decisions with at least 4 E1 `none` observations, take the one whose E1 `none`-channel rate is closest to 0.75, restricted to rates between 0.5 and 0.9.
- **Eligibility gate:** if fewer than 6 tasks qualify, E3 is not run and is reported as not testable with this bank.
- **Single-task rates:** they are re-estimated in E3's own B=1 arm, independent of the E1 data used for selection, to avoid selection bias.
- **Breadth and test:** breadth ∈ {1, 2, 4}. The prediction for each B>1 trial is the product of the B=1 rates for its sampled tasks. P6 is tested separately at B=2 and B=4 (§3.2), and sample sizes come from the T5 power simulation for that test.

**E4: leverage (H5).** A human study. You write contracts for N new tasks while timed, and they run through the E1 pipeline. Specified separately when D6 is approved.

**Replication.** E1's `none` vs `contract` contrast is repeated on the D4 model, with sample size from T5 and within the D2 cap.

### 3.2 Pre-registered predictions and outcome rules (D1)

Each prediction is made of one or more **components**, listed in the table. Each component is a single estimate with a 95% task-clustered bootstrap CI (§2.2), or, for P1's first component, a task-level sign test.

- **A component is supported** when its point estimate is in the prediction region and its CI does not intersect the falsification region. For the sign-test component: median per-task effect ≥ +30 pp and sign test p < 0.05 in the positive direction.
- **A component is falsified** when its CI lies entirely within the falsification region. For the sign-test component: sign test p < 0.05 in the negative direction.
- **A prediction is supported** when all of its components are supported, **falsified** when any component is falsified, and **inconclusive** otherwise.

P6 uses its own rule, given below.

| ID | Prediction region | Falsification region |
|---|---|---|
| P1 (arbitrary decisions) | (a) sign-test component as above; (b) pooled settled rate ≥ 0.90; (c) pooled unsettled rate ≤ 0.50 | (a) as above; (b) pooled settled < 0.80 (execution, not coverage, is the bottleneck); (c) pooled unsettled ≥ 0.70 (not arbitrary for this agent) |
| P2 | One component: \|contract-only − check-only\| < 10 pp | \|difference\| > 20 pp |
| P3 | Among unsettled decisions, \|filler on − off\| < 10 pp | \|difference\| > 15 pp in either direction |
| P4 | Among `none` decisions, \|rate at sibling-check fraction ≥ 0.6 − rate at ≤ 0.4\| < 10 pp, computed per decision and then averaged per task | \|difference\| > 15 pp in either direction |
| P5 (in E2) | (a) `plain` disclosure ≤ 20% of unsettled arbitrary decisions; (b) `assume` disclosure ≥ 60%; (c) \|satisfaction(`assume`) − satisfaction(`plain`)\| < 10 pp | (a) > 40%; (b) < 30%; (c) > 15 pp |
| P6 (each of B=2, B=4) | Equivalence: the 90% CI of the mean (observed − predicted) success lies within ±15 pp (two one-sided tests) | The 95% CI excludes 0 and the point estimate is beyond ±15 pp |

**P6 inference** treats tasks as fixed (§2.2). For each bootstrap replicate:

1. Resample each task's E3 B=1 trials within that task, and recompute every per-task rate.
2. Resample the B>1 trials at that breadth.
3. Recompute each resampled trial's predicted product from the step-1 rates, and take the mean of (observed − predicted).

H4 is supported only if P6 is supported at both breadths. It is falsified if P6 is falsified at either breadth.

**Pre-registration clarifications** (added before freeze, after the T5 review; approved at G2):

- **Invalid runs.** If any §2.4 validity gate fails, every prediction from that run is reported as `invalid`, never supported or falsified. Descriptive statistics may still be shown, clearly labelled. A record with missing `runtime_matches_request` counts as a mismatch. The infrastructure-failure gate is failed attempts ÷ distinct trials.
- **Decision populations.**
  - P1 and P5(a)/(b) use `arbitrary` decisions, as written.
  - P2, P3, P4 and P5(c) are each evaluated in two strata: `arbitrary` and `inferable`. A component is supported only if it is supported in both strata, and falsified if it is falsified in either. A stratum with fewer than 2 tasks contributing is inconclusive.
  - `preserve` decisions measure non-regression only and are reported descriptively.
- **Sign test (P1(a)).** Ties (a per-task effect of exactly 0) count against whichever direction is being tested. Support needs at least as many positive tasks as an exact binomial test over all tasks requires (one-sided p < 0.025, so two-sided p < 0.05, with ties counted as non-positive). Falsification uses the mirror rule.
- **P4 identification.** Each decision missing either exposure stratum after exclusions is counted and reported. If any decision is unidentified, P4 cannot be supported; it can only be falsified or inconclusive.
- **P5 completeness.** Every eligible decision must be judged. If any are unjudged, or κ is missing, non-finite or below 0.6, P5 is inconclusive.
- **Power targets (T5).** Every power figure is reported with its Monte Carlo standard error. Figures that a sample-size recommendation rests on (the declared recommendation basis: E1 strong-scenario P1–P4, E2 P5, E3 joint H4 under independence) are re-simulated with at least 1,000 simulations and the production bootstrap count whenever they fall within 2 standard errors of 80%. Other scenarios are descriptive, labelled unrefined, and never used for sizing. E3 is sized on joint H4 power, not on each breadth separately. P5 power is simulated too.

**User decisions at G2 (2026-10-18):**

- **P3 and P4 are descriptive in E1.** Estimates, strata and CIs are reported without a verdict, because the T5 power simulation puts their support probability near 1% at any affordable size.
- **The T6 judge uses minimal redaction.** Only a verbatim quotation of the E2 `assume` instruction is removed. Instruction-origin residue such as "you asked me to list assumptions" stays visible to the judge, and that is a stated limitation of P5.
- **The post-round-3 task-bank patch `8e08636` is kept.**

**Authorship (descriptive only):** if you author any tasks (D3), P1's per-task effects are also reported separately for your tasks and the coordinator's. With so few tasks, no test is run.

### 3.3 Guardrails

- **Correctness:** the existing runner tests and the fake-Pi end-to-end tests pass at every commit.
- **Integrity:** the task bank, labels, checks, rubric, analysis script and the pre-registration for each experiment are frozen by hash before that experiment runs. Any change creates a new version, and the affected experiment reruns from scratch. Re-scoring under a changed rule reports both versions.
- **Security:** trusted, first-party, stdlib-only tasks; no secrets in prompts. Trials run under the T3 confinement profile. Network access remains, because the model provider needs it.
- **Privacy:** raw event logs stay local and are not committed. Model text is never pasted into committed files.
- **Cost:** stop at the D2 cap. The runner's timeout is the only hard per-trial limit, so spend is tracked across all providers from provider-reported usage.

## 4. Technical sub-spec

### 4.1 Task schema (new; `autonomy_lab/tasks/<task-id>/`)

```text
task.json        {"id", "module": "<name>.py", "brief": str, "version": int,
                  "author": "coordinator" | "user"}
starter.py       weak starting implementation, copied into the workspace as <module>
reference.py     a solution that satisfies every decision
mutants/<decision-id>.py
                 the reference with only that decision violated
decisions.json   [{"id": str, "label": "inferable" | "arbitrary" | "preserve",
                   "contract": str, "visible_check": str, "heldout_check": str}]
neutral.md       filler: one neutral, accurate sentence per line that settles no decision
```

**Checks:**

- Every `visible_check` and `heldout_check` is a self-contained Python snippet. It runs as its own process with the workspace as working directory, imports the module by name, and passes exactly when it exits 0.
- Every check gets the same per-check timeout of 10 s, and runs with `python -I` plus the workspace on `sys.path`.

**Alternative mutants and construct rules** (added before freeze, after the T4 audit):

- **Alternatives for arbitrary decisions.** Every `arbitrary` decision has one mutant per reasonable alternative named in `LABELS.md`: `mutants/<decision-id>.py` for the first, plus `mutants/<decision-id>--<alt>.py` for each further one. A binary choice (graded behaviour plus one alternative) is a valid arbitrary decision with a single mutant. Do not invent contrived alternatives to reach a count. (Corrected after the T4 rework: an earlier "at least 2 mutants" wording wrongly excluded binary decisions, and the current draft bank dropped some for that reason.) Each one implements a natural alternative policy, not a contrived bug. Every mutant must fail its own decision's held-out and visible checks and pass all other decisions' held-out and visible checks.
- **One choice per decision.** A decision is a single policy choice. A bundle such as "sorted and deduplicated" or "empty, negative or all-zero inputs raise" is split into separate decisions, or narrowed to one.
- **No settling outside the channels.** The brief, the starter's docstrings, names, signature and comments, and `neutral.md` must not state or imply any decision's behaviour. `neutral.md` describes purpose and context only: no output format, validation, ordering, boundary, allocation or error behaviour.
- **Matching coverage.** A decision's visible and held-out checks cover the same input classes (types, error families, boundary kinds), with different concrete values. Both stay within the declared input domain. **Singleton exception:** where an input class has exactly one member (for example the empty string `""`), both checks may use it, provided each also includes different inputs for the rest of the class (such as distinct whitespace-only strings). For that sub-case, held-out grading measures the visible behaviour, not generalization.

**Mechanical bank validation** (T1 provides it; T4 must pass it):

- `reference.py` passes every held-out and visible check.
- Every mutant of decision `d`, including the `--<alt>` variants, fails exactly `d`'s held-out check, fails `d`'s visible check, and passes every other visible check.
- The starter fails at least half of all held-out checks, including at least 2 `arbitrary` decisions.
- Each task has 4–8 decisions, at least 2 of them `arbitrary`.

**Other rules:**

- `preserve` marks behaviour the starter already has that must not break.
- For every decision, the contract sentence and the visible check must agree. The mechanical validation checks the check side; the cross-family audit checks the wording.
- Labels are fixed at freeze time.
- The bank hash (`task_bank_sha256`) is a SHA-256 over all files under `autonomy_lab/tasks/`, sorted by relative path, hashing each path and its content.

**Prompt and check generation** (from `decisions.json`, per trial):

- **Prompt:** the fixed preamble, then each task's brief, then the `contract` sentences of decisions whose channel is `contract` or `both`, in `decisions.json` order. When filler is on, whole `neutral.md` sentences come next, in file order, until the prompt is within ±5% of that task's all-contract prompt length. The preamble is the current `build_prompt` preamble without the visible-check strength sentence.
- **Visible checks:** `visible_checks.py` contains the `visible_check` of decisions whose channel is `check` or `both`, plus one import smoke line per task.
- **E2 `assume` arm:** appends the sentence given in §3.1.

### 4.2 Confinement profile (T3)

Each Pi process runs under `bwrap` with:

- the host filesystem read-only;
- `$HOME`, the repository, and every results directory except the trial's own workspace replaced by empty tmpfs mounts;
- the trial workspace bound read-write at a neutral path;
- the Pi installation bound read-only;
- `PI_CODING_AGENT_DIR` pointing to a minimal agent directory holding only `auth.json`, bound **read-only**. Before each trial the runner calls `prepare_host_auth` on the host, which refreshes the token if it expires within the trial timeout plus a margin. A refresh attempted inside the sandbox therefore fails and is classified as an infrastructure `auth` failure; it can never modify the host file.
- a seccomp filter blocking `socket(AF_UNIX)` and io_uring, so host abstract Unix sockets are unreachable despite the shared network. A shim closes every inherited file descriptor before bubblewrap starts.

The user's real `~/.pi/agent/sessions` is never mounted, because coordinator sessions contain the held-out checks. The agent can still read its own provider credentials. That is a known, accepted limitation for trusted tasks, as it is today.

### 4.2a Grading isolation (added after the T1/T2 review)

Grading runs agent-written code, so it gets its own confinement. Each held-out or visible check:

- **Source:** only the designated module file is graded. It must be a regular file, not a symlink, and a package or other file with the same import name in the workspace is ignored. Task module names may not collide with harness files (`visible_checks`, `prompt`) or standard-library modules.
- **Fresh state:** each check runs in its own fresh temporary directory containing only a copy of that module. It has its own bytecode cache, and no state is shared between checks.
- **Sandbox:** each check runs inside a bubblewrap grading profile with no network, a read-only root, an empty `$HOME`, and nothing of the repository, sessions or results mounted. Its only writable location is its own temporary directory, which is deleted afterwards.
- **Completion:** a check passes only if it exits 0 **and** prints a per-check random completion token after its last assertion. That defeats accidental `sys.exit`/`os._exit` during import. It is not proof against an adversarial agent.
- **Time limit:** the 10 s timeout covers the whole process group, including cleanup.
- **Integrity:** workspace spec files are hashed after the agent finishes and before grading. Grading never reads them from the workspace.

**Bank integrity:** directory or file symlinks inside the bank are rejected, so the bank hash always covers exactly what is loaded.

### 4.3 Trial record schema (`trials.jsonl`, one JSON object per line)

The existing fields from `pi_runner.py` stay, including the runtime, event and acceptance fields. New fields:

```text
experiment            "E1" | "E2" | "E3" | "T7" | "T8" | "replication"
design_id, seed       design identifier and per-trial seed (written before any trial runs)
task_ids              ordered list of task ids in this trial (order = prompt order)
breadth               len(task_ids)
channels              {task_id: {decision_id: "none"|"contract"|"check"|"both"}}
filler                bool
arm                   null | "plain" | "assume"
decision_results      {task_id: {decision_id: true|false}}   held-out, per decision
labels                {task_id: {decision_id: label}}         copied from the frozen bank
task_bank_sha256      str
infrastructure_failure  null | "provider_error" | "retry_exhausted" | "spawn" | "auth" |
                        "sandbox" | "no_settle" | "malformed_events" | "provider_stall"
agent_failure         null | "timeout" | "length" | "heldout_failed"
confinement_signals   list of {"kind", "detail"}  (empty when none)
attempt               1 | 2   (2 = infrastructure rerun with the same seed)
```

The design for a run is also written to `design.json` in the run directory before the first trial starts.

## 5. Tasks

All harness work uses one sequential writer on `experiment/autonomy-lab`. A task is accepted only when its acceptance test passes and a cross-family review has no unresolved high or medium findings.

| Task | Depends on | Acceptance test |
|---|---|---|
| T1 Per-decision grading from the task schema | — | A fake Pi that satisfies exactly k of n decisions yields exactly those k passes. Existing tests still pass. |
| T2 Balanced seeded design generator (E1/E2/E3), filler, shuffled order; prompts and visible checks generated from `decisions.json` | T1 | Same seed gives byte-identical design, prompts and checks. Channel and filler counts are balanced per decision exactly as in §2.3. Every decision has P4 exposure on both sides. Filler-on prompts are within ±5% of the all-contract length. A `check` channel adds nothing to the prompt, and a `contract` channel adds nothing to the checks. |
| T3 Bubblewrap confinement and minimal agent dir; infrastructure-failure classifier; confinement-signal recorder | — | Inside the profile, a fake agent attempting to read the repository's `runner.py` (by relative and absolute paths, `cd ..` and a Python `open`) and to list `~/.pi/agent/sessions` fails each time. One real tool-free Pi call still authenticates, and the real auth file is unchanged or correctly refreshed. Synthetic event streams are classified correctly. |
| T4 Task bank: 12 tasks (10 minimum), 4–8 decisions each, at least 2 arbitrary decisions that the starter fails | T1 | The reference passes every check. Each mutant fails exactly its own decision's check (a mutation matrix that proves each check isolates one decision). The starter fails at least half of all held-out checks. The cross-family audit confirms that contract and check agree, labels are defensible, and `neutral.md` gives no behavioural cue. You approve the labels (G2). |
| T5 Analysis script (stdlib only) and power simulation | T1, T2 | It recovers a known effect from synthetic `trials.jsonl` and is deterministic given a seed. The power simulation under the nested design produces the sample sizes for D7 and E3. |
| T6 Disclosure judge: rubric, blinded inputs, κ computation | T5 | On 12 hand-labelled synthetic examples, agreement is at least 10/12. The rubric is frozen before E2. |
| T7 Plumbing run: 8 trials under the full pipeline | T1–T3, T5 | All validity gates pass. This validates plumbing only, not difficulty. |
| T8 Calibration pilot on the draft bank: 2 `none`-only and 1 all-`contract` trial per task | T4 draft, T7 | **Bank-level gate (pooled):** unsettled-arbitrary satisfaction is between 0.05 and 0.80, and all-contract satisfaction is at least 0.80.<br>**No per-task selection:** no task is revised, relabelled or dropped because of its T8 outcomes. With 2–3 trials per task, that would select the bank on noise. Defects found by inspection are fixed only when they are objective, such as a contract that contradicts its check or its reference solution, and each fix is logged. If the bank-level gate fails, the whole bank is redesigned as a new version and T8 runs once more.<br>**Calibration data are excluded from all analyses.** |

## 6. Trigger, environment, durable state, evidence

- **Trigger:** manual, by the coordinator after each sign-off gate (§8.4). No cron and no CI-triggered billed runs; CI stays mock-only.
- **Environment:** the user's Linux host, trials sequential (concurrency 1). Benchmark children run under the existing isolation flags plus the T3 confinement profile. The interactive coordinator may use Pi skills; benchmark children may not.
- **Durable work record (committed):**
  - this spec;
  - `autonomy_lab/experiments/E<n>.md`: the pre-registration, committed before its run, with its commit hash recorded;
  - `STATE.json`: current task, experiment status, budget used;
  - `REPORT.md`: results, including negative and inconclusive ones.
- **Evidence (local, ignored):** run directories outside the repository, containing `run-metadata.json`, `trials.jsonl` and per-trial logs and workspaces.
- **Evidence (committed after the D5 check):** `autonomy_lab/results/<run-id>/` with `summary.json` and a per-decision CSV containing no model text.
- **Links:** every reported number cites a run ID, runner commit, task-bank hash, the exact command and the analysis-script commit.

## 7. Build–test–review cycle and change selection

**Harness iteration (T1–T6):** one falsifiable change at a time.

1. State what the change fixes and how a test would show it.
2. Make the smallest coherent change.
3. Run the unit and fake-Pi tests.
4. Get a cross-family review of the diff.
5. Keep the change only if all of these hold: tests pass, the review has no open high or medium findings, and, once T7 exists, a T7 plumbing run meets every validity gate.
6. Otherwise revert it and record why.

**Experiment iteration (E1–E3):**

1. Commit the pre-registration.
2. Run T7 plumbing.
3. Run the full experiment.
4. Run the frozen analysis script unchanged.
5. Record supported, falsified or inconclusive against P1–P6 in REPORT.md.
6. Wait for sign-off before the next experiment.

No task, check, label, threshold, rubric or analysis rule changes after a run starts. Changes become a new version, and that experiment reruns in full.

**Next-step rule:** E1 → E2 treatment → E3, in that order. E2 has its own `plain` control; E1 disclosure data are descriptive only. If P1 is falsified because unsettled-arbitrary decisions are satisfied at a high rate, stop and revisit labels and tasks before E3, since E3 depends on arbitrary decisions really being unsettled.

## 8. Budgets, stop conditions, rollback, sign-off

### 8.1 Proposed budget (D2): one cap across all providers

| Item | Benchmark trials | Review/audit sessions | Other auxiliary calls | Estimated provider-reported cost |
|---|---|---|---|---|
| T3 auth check, D4 model probes | 0 | 0 | 5 | < $0.10 |
| Cross-family reviews and audits (T1–T6, T4 audit, ≤ 3 rounds each) | 0 | ≤ 25 | 0 | $5 (rough; review sessions are long) |
| T7 plumbing (≤ 6 runs × 8: harness acceptance plus one before each experiment and the replication) | ≤ 48 | 0 | 0 | $2 |
| T8 calibration (12 × 3) | 36 | 0 | 0 | $1.50 |
| E1 (12 × 16) | 192 | 0 | 0 | $8 |
| E2 (12 × 4 × 2) | 96 | 0 | 0 | $4 |
| Disclosure judging | 0 | 0 | ~200 | $2 |
| E3 (sized at G3) | ≤ 90 | 0 | 0 | ≤ $6 (B=4 trials are longer) |
| E1 replication (D4 model) | ≤ 40 | 0 | 0 | ≤ $5 (unknown pricing) |
| Infrastructure-rerun reserve (5% of the above) | ≤ 25 | 0 | 0 | $1 |
| **Cap** | **≤ 530** (items sum to 527) | **≤ 25** | **≤ 250** (items sum to about 205) | **≤ $40 total, binding** (items sum to about $35) |

Estimated benchmark wall clock is about 5.5 h at pilot rates. The cap is 8 h. Cost estimates scale pilot averages; review-session cost is a guess and is tracked from actual usage. Reaching the cost cap stops the loop even if count caps remain.

**Gate G4 decision (user, 2026-10-18).** The dollar figures are list-price estimates. The model calls run on the user's subscriptions (OAuth sign-ins), not on billed API keys, so dollars are no longer a cap: they are recorded for information only. The binding limits are now:

- **Trial counts:** T8 (36) and E1 (192) are approved now. E2 at 288 trials is approved to run after E1's results have been reviewed at G3.
- **Provider usage limits:** a usage-limit error stops the loop (§8.3).

The 250-call cap on auxiliary calls is dropped.

### 8.2 Iteration budget

Each harness task gets at most 3 review–fix rounds. If a task still has open high or medium findings after that, stop and escalate.

### 8.3 Stop conditions

- **Success:** E1–E3 complete, with each prediction resolved as supported, falsified or inconclusive (or E3 recorded as not testable under its eligibility gate).
- **Budget exhausted:** any cap in §8.1.
- **Invalid measurement:** a validity gate in §2.4 fails twice in a row after a fix attempt.
- **Calibration failure:** T8's bank-level gate still fails after one whole-bank redesign. Stop and redesign the tasks rather than adding repetitions.
- **Provider constraint:** a usage limit or model deprecation.
- **Pi version change:** stop; never mix versions within an experiment.
- **Safety:** any confirmed confinement violation, or any change to `prompt.txt` or the visible checks. Stop and investigate before continuing.
- **No progress:** two consecutive harness iterations fail review on the same issue.

On stopping, report the best verified result, the predictions resolved, and the remaining uncertainty.

### 8.4 Rollback and sign-off

- **Rollback:** every harness change is one commit. Reverting it restores the previous verified runner, and the last verified runner commit is recorded in STATE.json. The loop never deletes raw results.
- **Human sign-off gates:**
  - G1: approve this spec and decisions D1–D7;
  - G2: approve the frozen task bank, labels, and the sample sizes from T5;
  - G3: review E1 results, then approve E2 and the E3 design;
  - G4: any budget increase or scope change.
- **Separate authorization:** commits to this branch are authorized for the loop's work record. Pushes, PRs, merges and any change to `main` each need separate authorization.
