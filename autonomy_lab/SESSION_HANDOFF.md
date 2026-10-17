# Session handoff: Agentic Engineering Autonomy Lab

**Created:** 2026-10-17  
**Status:** Migrating from a ChatGPT research/build session to the user's real local Pi coding-agent workflow.  
**Source of truth:** https://github.com/giuseppecrj/mini-swe-agent/tree/experiment/autonomy-lab/autonomy_lab  
**Companion machine-readable file:** STATE.json  
**This file is a CURATED transfer, not an exact transcript or a claim that the local computer was contacted.**

## 1. What the human is trying to test

The starting idea (user's words, paraphrased): Spend an entire eight-hour engineering day preparing an end-to-end specification instead of writing the implementation. Write the prototype plan, implementation plan, tests, failure conditions, and acceptance contract. Then delegate execution to an autonomous coding agent. The breadth and depth of planning should determine how much work the agent can complete without requiring intervention.

Our refinements:

- **Breadth (B):** the number/size of consequential engineering decisions or independent work units delegated.
- **Effective depth (D):** the proportion of important design and implementation decisions resolved CORRECTLY, not the volume of prose.
- **Verifiability (V):** the extent to which an agent can detect, diagnose and resolve incorrect work using autonomous tests and feedback.
- **Coupling (K):** dependencies and interlocking choices that can multiply downstream failures.
- **Agent capability (M):** the underlying model and harness, held constant when testing plan characteristics.
- **Autonomy:** independently accepted progress without human intervention. Elapsed model runtime is NOT equivalent to the difficulty of tasks it can solve.
- **Leverage:** independently accepted engineering work per human hour spent planning, intervening and reviewing.

The conceptual mathematical starting point:

    P(autonomous success) ~= (1 - q)^B

where q is the chance that each consequential decision causes an unrecovered failure. A proposed, deliberately UNCALIBRATED risk model is:

    q = q_min + (K/M) * [alpha*(1-D) + beta*(1-V) + gamma*(1-D)*(1-V)]

The original illustrative dashboard used hand-chosen coefficients. **No claim is made that these formulas or coefficients are empirical laws.** Decision failures are not necessarily independent or identically distributed.

At an 80% target reliability, the illustrative horizon in decisions is:

    H_80 = floor(log(0.8) / log(1 - q))

The more important optimization target is:

    L = independently accepted work /
        (human planning time + human intervention time + final review time)

The research question: Does a deep plan increase autonomous execution capacity? Do rich tests have a larger effect? Does a hierarchy of progressively refined plans beat a single extremely detailed upfront plan? Can a human spend a day designing a framework within which an agent autonomously resolves uncertainty instead of prescribing every step?

**Interpretation rule:** More planning tokens are not necessarily more depth. Evaluate coverage and correctness of consequential decisions. A high-level design plus short verified milestones may outperform a long rigid plan.

## 2. Work already implemented, as of migration

The repository is an existing user-owned GitHub fork of mini-SWE-agent:

    https://github.com/giuseppecrj/mini-swe-agent
    branch: experiment/autonomy-lab

The branch was intentionally kept separate from main. No new always-on cloud VM was provisioned.

Files initially implemented on the branch:

- autonomy_lab/runner.py — original mock runner and Docker mini-SWE-agent backend.
- autonomy_lab/Dockerfile — isolated image for mini-SWE-agent.
- autonomy_lab/tests/test_runner.py — offline scaffold tests.
- autonomy_lab/README.md — initial experiment design.
- .github/workflows/autonomy-lab.yml — temporary Ubuntu GitHub Actions runner; builds image, runs mock trials, verifies outputs, uploads artifacts.

The initial design is 2 x 2 x 2:

    Breadth:  1 task vs 2 tasks
    Depth:    shallow issue description vs explicit contract/invariants
    Checks:   basic visible tests vs rich visible edge-case tests

Starter tasks:

- ordered-merge: return unique IDs while preserving first-appearance order.
- atomic-ledger: pure, validated, conservative monetary transfer in a balances dictionary.

The grader includes held-out checks which should never be shown in the trial instructions. Each trial gets a fresh workspace with identical starter code per task. The basic tests are intentionally weak; rich tests expose more requirements. A successful trial must pass held-out acceptance without corrupting its specification.

**Confirmed completed cloud smoke run:**

    GitHub Actions run: 39026174523
    https://github.com/giuseppecrj/mini-swe-agent/actions/runs/39026174523
    conclusion: success

The confirmed cloud run included passing unit tests, a completed 8-configuration MOCK reporting cycle, building the coding-agent Docker image, starting its CLI in the container, and uploading a results artifact. A previous smoke run 39026016811 also succeeded.

**No actual agent-model benchmark trials had been run in that cloud run.** All eight mock results are instrumentation checks, not performance evidence.

## 3. What this migration adds

New source files:

- autonomy_lab/pi_runner.py — invokes the user's real local Pi CLI in headless JSON-event mode, uses the existing authenticated Pi model, measures turns/tools/retries/elapsed time/provider-reported tokens and cost, and grades held-out correctness.
- autonomy_lab/tests/test_pi_runner.py — offline tests of Pi event accounting, flags, and altered public test rejection.
- autonomy_lab/STATE.json — machine-readable working state and unresolved assumptions.
- autonomy_lab/AGENTS.md — operating rules for the local coordinator.
- autonomy_lab/SESSION_HANDOFF.md — this complete research + workflow handoff.
- autonomy_lab/.gitignore — excludes raw local experiment output.

The real Pi adapter **launches local Pi processes**. It does not require a new model API key if the user's Pi is already authenticated. It may still use a REMOTE model provider; locally running the Pi harness is not the same as locally running model inference.

It forces distinct benchmark sessions and disables extensions, skills, prompt templates, context files and MCP during benchmark execution to avoid ambient assistance. The human-operated parent Pi session may load personal skills.

**Security limitation:** the v0.2 Pi runner is a local OS process with the user's own permissions, not a Docker sandbox. Benchmarks use trusted first-party toy code only. Held-out grading is outside the trial working directory but is not protected from a fully privileged agent reading the parent repository. Do NOT expand to untrusted tasks, secrets, or adversarial agents before adding a stronger execution boundary.

## 4. Local Pi / Herdr workflow

Relevant existing user-owned projects:

- Pi Herdr Agents: https://github.com/giuseppecrj/pi-herdr-agents — a Pi extension that coordinates subagents inside Herdr panes and managed worktrees.
- Maestro: https://github.com/giuseppecrj/maestro — harness-agnostic orchestration for Pi, OpenCode and additional adapters.

Their presence in GitHub does NOT establish that particular versions are installed on the user's current machine. The working preference is local-first, CLI-first, explicit operational conventions and a durable Pi conversation. Their global Pi context may be under ~/.pi/agent with skills like /skill:implementation-workflow, /sweep and /ship.

**Initial isolation decision:** Use a persistent interactive Pi coordinator (inside Herdr if available) and a headless Pi subprocess per benchmark trial. Do not introduce Maestro or parallel Herdr child agents for the first controlled experiment; this would change the harness, tool availability or supervision and confound results. Later, compare orchestration approaches as their own independent experimental factor.

### Step A — obtain the exact branch on the local computer

From a terminal:

    git clone https://github.com/giuseppecrj/mini-swe-agent.git
    cd mini-swe-agent
    git fetch origin experiment/autonomy-lab
    git switch --track origin/experiment/autonomy-lab

If the repo is already cloned:

    cd path/to/mini-swe-agent
    git fetch origin experiment/autonomy-lab
    git switch experiment/autonomy-lab

Do not discard uncommitted work; inspect git status first.

### Step B — establish the baseline

From the repository root:

    python3 --version
    pi --version
    pi --help
    python3 -m unittest discover -s autonomy_lab/tests -v
    python3 -m autonomy_lab.runner --backend mock --out autonomy_lab_results

Check Pi authentication using its normal interactive account/model workflow. Avoid placing credentials on the command line or in Git files. If the currently installed Pi lacks documented command flags, inspect the exact local --help and update the adapter explicitly rather than silently weakening the treatment isolation.

### Step C — make this conversation a local interactive agent session

Start Herdr, if installed/configured, then in its shell:

    cd path/to/mini-swe-agent/autonomy_lab
    pi @SESSION_HANDOFF.md "Continue the transferred Autonomy Lab session. Read AGENTS.md and STATE.json. Check the working tree and installed toolchain. Validate local offline tests, then perform the single bounded real Pi trial from AGENTS.md if the CLI and authentication work. Report exact measured outcomes; do not change the experiment's design or graders to make tests pass."

Pi automatically persists its INTERACTIVE session unless configured not to. The benchmark's child Pi CLI calls use --no-session intentionally so repeated trial conditions are fresh. Do not confuse the coordinator's persistent session with the benchmark trials.

Pi's documented CLI supports @file attachment, print/JSON/RPC execution modes, and session management. Reference:
https://pi.dev/docs/latest/cli
https://pi.dev/docs/latest/cli-integration
https://pi.dev/docs/latest/json

### Step D — the FIRST real test (completed 2026-10-17; see section 8)

From the repository root in a local shell (or ask the interactive Pi coordinator to run it):

    python3 -m autonomy_lab.pi_runner \
      --model openai-codex/gpt-5.6-sol \
      --thinking high \
      --breadths 1 \
      --depths deep \
      --verifications rich \
      --repetitions 1 \
      --timeout 300

This makes ONE authenticated Pi agent call. --model (exact provider/model-id) and --thinking are required: Pi's configured default was an extension-provided provider that is unavailable under --no-extensions, so an unpinned run would have silently used a different model. A trial is accepted only if every assistant message reports the requested model and thinking level. Model/usage details, when emitted by Pi, are recorded in the trial metadata. Allow for provider account usage/cost; the adapter enforces a wall-clock limit, NOT a hard spending limit.

Every execution creates an independent, timestamped results directory:

    autonomy_lab/local-results/run-*/run-metadata.json
    autonomy_lab/local-results/run-*/trials.jsonl
    autonomy_lab/local-results/run-*/summary.json
    autonomy_lab/local-results/run-*/summary.csv
    autonomy_lab/local-results/run-*/workspaces/<trial-id>/   (agent working directory)
    autonomy_lab/local-results/run-*/logs/<trial-id>/pi-events.jsonl
    autonomy_lab/local-results/run-*/logs/<trial-id>/pi-stderr.log

Harness logs live outside the agent's working directory, so they no longer show up in its file listings. This prevents accidental exposure only: an agent that deliberately reads ../../logs can still reach them, just as it can reach the held-out grader source. (In the two runs before this change, the logs were inside the workspace.)

Do not commit local event logs. They may contain private model output.

### Step E — only after evaluating the first test, run a pilot

The pinned pilot model is openai-codex/gpt-5.6-sol at --thinking high (user-approved 2026-10-17):

    python3 -m autonomy_lab.pi_runner \
      --model openai-codex/gpt-5.6-sol \
      --thinking high \
      --breadths 1,2 \
      --depths shallow,deep \
      --verifications basic,rich \
      --repetitions 3 \
      --timeout 600

This runs 24 real Pi agent trials and may incur substantial usage. First ensure the model, provider access and budget make sense. Keep this exploratory: two toy tasks and fixed ordering do not yet justify sweeping conclusions.

The older Docker backend stays available as a separate comparison, but comparing its results directly to Pi changes BOTH the harness and potentially the model; document those differences.

## 5. Acceptance protocol and research cautions

Every trial has a stable baseline, a prompt generated from depth+verification+breadth, visible tests, and external acceptance checks.

**Required outcomes:** accepted yes/no, agent completion status, wall seconds, tool calls, tool errors, assistant turns, retries, observed provider/model, token usage/cost (where supported), test integrity, exact repository commit and Pi version.

Human interventions are **disallowed** in current trial code. Therefore zero measured interventions means "the trial was not interactive," not that a general engineering workflow is independently autonomous over days.

Potential improvements after first live run:

1. Replace toy tasks with many independently sampled tasks and rotate task order; record estimated human baseline difficulty.
2. Keep a separate examiner process/host for held-out tests so the agent cannot trivially read the grader.
3. Add task-specific hard budgets on time, tokens, and external side effects; continue to avoid untrusted tasks without isolation.
4. Analyze trajectories for cycles of plan -> action -> observation -> independent correction, not only final success.
5. Run repeated trials with different random seeds when supported; fit an interaction model and uncertainty intervals.
6. Compare monolithic upfront planning against progressively refined milestone contracts.
7. Define an intervention protocol for a later experiment that explicitly permits bounded questions, and measure human minutes.

## 6. Do not claim

- That ChatGPT established a connection to the user's local computer.
- That the single accepted real trial (section 8) measures a success rate or supports any depth/verification effect.
- That passing the cloud mock smoke suite proves any autonomy-performance improvement.
- That the proposed q-model is an empirical fact.
- That a locally running Pi CLI guarantees local inference or zero API costs.
- That the local Pi runner or ordinary git worktrees offer a security sandbox.
- That any connected plugin or orchestration project is installed on the user's device unless the device confirms it.

## 7. Immediate mandate to the next agent

Continue from this exact state without restarting research from scratch.

1. Run offline verification and inspect the actual Pi CLI installation.
2. Run the smallest actual Pi benchmark when model authentication is available.
3. Examine held-out acceptance, process completion and Pi event trajectory; fix instrumentation bugs if found without changing the conceptual conditions.
4. Present a baseline result, not a speculative estimate.
5. Only then recommend how to scale the breadth/depth/verifiability experiment.

The end goal is not to optimize agent runtime for its own sake; it is to maximize accepted engineering progress per hour of human engineering attention.

**Status 2026-10-17:** items 1–4 are done (section 8). Item 5 is the open next step.

## 8. First real local Pi results (2026-10-17)

Host: Linux, Python 3.14.7, Pi 1.3.0. Code: experiment/autonomy-lab at bc5d061, i.e. before the runner changes described below. Offline validation passed: 8/8 unit tests, and the mock run rejected all 8 unmodified starters.

Model selection: Pi's configured default (claude-bridge/claude-opus-5-5) is provided by an extension, so it does not exist under the benchmark's --no-extensions isolation. Built-in providers openai-codex, openai, concentrate and meta reported auth ready. Short tool-free probes showed that the ChatGPT-account Codex provider accepts gpt-5.6-sol and gpt-5.6-luna and rejects gpt-5.5 and gpt-5.3-codex-spark.

Condition: breadth=1 (ordered-merge), depth=deep, verification=rich, 1 repetition, timeout 300 s, no human interventions.

| Run ID | Model (requested → observed) | Outcome |
|---|---|---|
| run-20261017T130249Z-2d6c5a | openai-codex/gpt-5.5:high → openai-codex/gpt-5.5 | Infrastructure failure, not a capability result. The provider rejected the model on the first request ("not supported when using Codex with a ChatGPT account"); 0 tool calls. Pi exited 0, but the runner correctly marked agent_completed=false. |
| run-20261017T130316Z-e15424 | openai-codex/gpt-5.6-sol:high → openai-codex/gpt-5.6-sol, thinkingLevel high | **Accepted.** Held-out passed, scope intact, settled, 8.59 s, 4 turns, 5 tool calls, 0 tool errors, 0 retries, 7,251 tokens, provider-reported $0.0188 (probably notional under ChatGPT OAuth; not verified). |

Trajectory of the accepted trial: list the workspace → read the module and visible checks → one edit (order-preserving seen-set loop) → run visible checks once (pass) → stop. No error-correction cycle was needed.

Interpretation: n=1 on the easiest condition. This shows the real pipeline works end to end, not a success rate. The task is near trivial for this model, so a ceiling effect is likely at breadth 1.

Runner problems found and fixed after these runs:
1. An unpinned run silently used a non-default model while the metadata said "Pi configured default". --model provider/id and --thinking are now required. Acceptance now requires every assistant message to report a provider, model and thinking level that match the request, with no unparseable event lines (tightened after an independent cross-family review).
2. pi-events.jsonl and pi-stderr.log were written inside the agent's workspace, and the agent listed its own live event log. They now go to run-*/logs/<trial-id>/. This prevents accidental exposure, not deliberate access.
3. The thinking level was recorded only inside the --model string. It is now requested and observed as separate fields.

Confound noted for the pilot: in the shallow condition, atomic-ledger's held-out checks require behaviour the brief never states (e.g. KeyError for a missing sender, rejecting sender == recipient). Shallow failures there partly measure missing information, not planning depth.

Next falsifiable hypothesis: with gpt-5.6-sol at thinking high, breadth-1 deep conditions accept ≥90% of trials, and any between-condition difference appears only in shallow conditions involving atomic-ledger. If that holds, harder or more numerous tasks are a better investment than more repetitions of these two.

## 9. First 2x2x2 pilot (2026-10-17)

Run run-20261017T131959Z-5b17ab at commit 69ca419. Pi 1.3.0, openai-codex/gpt-5.6-sol at thinking high, 3 repetitions per cell, timeout 600 s, fixed (non-randomized) condition order, no human interventions. 24 trials in 9m42s, 272,922 tokens, provider-reported $1.01 (probably notional under ChatGPT OAuth). Every trial settled, used the requested model and thinking level, left its spec and visible checks intact, and had 0 retries and 0 tool errors.

| Breadth | Depth | Verification | Accepted | Mean s | Mean turns |
|---|---|---|---|---|---|
| 1 | shallow | basic | 3/3 | 16.7 | 4.0 |
| 1 | shallow | rich | 3/3 | 12.2 | 4.0 |
| 1 | deep | basic | 3/3 | 10.3 | 4.0 |
| 1 | deep | rich | 3/3 | 9.7 | 4.0 |
| 2 | shallow | basic | 0/3 | 55.7 | 5.7 |
| 2 | shallow | rich | 0/3 | 35.2 | 5.3 |
| 2 | deep | basic | 3/3 | 26.6 | 4.3 |
| 2 | deep | rich | 3/3 | 27.1 | 6.3 |

ordered-merge passed held-out checks in 24/24 trials. Every failure is in atomic-ledger. Per-requirement probe of each breadth-2 ledger.py:

| Hidden requirement | In shallow brief | In rich visible checks | shallow+basic | shallow+rich | deep (either) |
|---|---|---|---|---|---|
| input not mutated | no | yes | 0/3 | 3/3 | 6/6 |
| invalid amounts (-2, 0, 2.0, True) raise ValueError | no | yes | 0/3 | 3/3 | 6/6 |
| sender == recipient raises ValueError | no | no | 0/3 | 0/3 | 6/6 |
| empty sender raises ValueError | no | no | 0/3 | 2/3 | 6/6 |
| missing sender raises KeyError | no | no | 3/3 | 2/3 | 6/6 |

Observations:
- With the contract, ledger acceptance was 6/6 (Wilson 95% CI 0.61\u20131.00). With the brief it was 0/6 (CI 0.00\u20130.39). Fisher exact two-sided p \u2248 0.002 for this single task.
- Rich visible checks closed exactly the requirements they encode and none beyond them. Without the contract, rich checks did not rescue acceptance.
- Shallow agents made plausible but different consequential decisions: a self-transfer as a no-op, keeping the starter's in-place mutation, ValueError for an unknown sender. None of their final messages flagged an ambiguity or stated an assumption; all reported success.
- Breadth is confounded with task identity: breadth 2 is the only way atomic-ledger enters the design. This pilot shows no breadth effect separable from "contains the underspecified task".
- Shallow trials took about 1.3\u20132x longer than deep at breadth 2 and still failed.

What this does and does not show: for one model and one underspecified task, outcome was determined by whether each consequential decision was settled in the contract or a visible check. It does not establish an effect of planning depth in general, a q-model fit, or generality across tasks or models. Held-out requirements that the brief does not mention measure missing information, which the deep condition supplies directly.
