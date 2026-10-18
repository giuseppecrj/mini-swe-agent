# Autonomy Lab — cloud + local Pi experiment v0.2

Test the hypothesis that deeper engineering specifications and richer *agent-visible* verification increase the reliable autonomous task horizon as breadth rises.

This lab lives on an isolated branch of `giuseppecrj/mini-swe-agent`; it does not change upstream agent code. The GitHub Actions workflow provisions an **ephemeral GitHub-hosted Ubuntu runner** to execute the smoke suite. No paid VM or API key is required for the smoke run.

## Local Pi / Herdr handoff (2026-10-17)

**Start here:** [REPORT.md](REPORT.md) for the claim and results so far, [LOOP_SPEC.md](LOOP_SPEC.md) for the next experiments, then [SESSION_HANDOFF.md](SESSION_HANDOFF.md), [AGENTS.md](AGENTS.md) and [STATE.json](STATE.json) for provenance and operating rules.

The branch includes a **local Pi backend** (distinct from Docker mini-SWE-agent). It uses the user's own Pi model authentication, captures JSONL events and reports held-out acceptance. The model and thinking level must be pinned. A single bounded trial, from the repository root:

    python3 -m unittest discover -s autonomy_lab/tests -v
    python3 -m autonomy_lab.pi_runner --model openai-codex/gpt-5.6-sol --thinking high \
      --breadths 1 --depths deep --verifications rich --repetitions 1 --timeout 300

Or open a persistent local Pi coordinator inside Herdr from the autonomy_lab directory:

    pi @SESSION_HANDOFF.md "Continue the migrated Autonomy Lab session, run offline checks, then run one bounded real Pi trial and report observed results."

**Be precise:** the cloud workflow used a mock agent for its eight treatment cells and built/starter-checked the Docker image; it did not measure real-model performance. The original 2×2×2 Pi mode runs with the user's host privileges, NOT in a sandbox; use only the trusted built-in toy tasks. Local trajectories are ignored by git and must not be committed. A Pi CLI running on a local computer may still call a remote model provider.

## Task-bank experiments (LOOP_SPEC.md)

The pre-registered E1–E3 experiments use the task bank in `tasks/` and a separate runner mode. Each trial gets a seeded, balanced design: every decision is settled by a contract sentence, a visible check, both, or neither. Each decision is graded by its own held-out check.

    python3 -m autonomy_lab.taskbank validate autonomy_lab/tasks      # mechanical bank validation (sandboxed grading)
    python3 -m autonomy_lab.confine selftest                         # confinement probes
    python3 -m autonomy_lab.pi_runner --model openai-codex/gpt-5.6-sol --thinking high \
      --task-bank autonomy_lab/tasks --experiment T7 --design-seed 1 --timeout 600
    python3 -m autonomy_lab.analysis <run_dir> --tasks-dir autonomy_lab/tasks

Safety defaults in this mode:

- **Confined Pi:** Pi runs under a bubblewrap profile that hides `$HOME`, the repository, Pi sessions and other runs. A seccomp filter blocks Unix and vsock sockets and io_uring, and the credentials file is mounted read-only.
- **Token refresh:** the host refreshes the model token before every attempt.
- **Grading:** each check runs in its own network-less sandbox, holding only a copy of the designated module, and passes only with a per-check completion token.
- **Results location:** results go to `$XDG_STATE_HOME/autonomy-lab` (or `~/.local/state/autonomy-lab`), outside the checkout.
- **Test-only flags:** `--unconfined-pi-for-tests` and `--unconfined-grading-for-tests` exist for tests only.

This is leakage prevention for trusted tasks, not a hardened multi-tenant sandbox. The agent can still read its own model credentials.

Other modules: `design.py` (designs, prompts, visible checks), `analysis.py` (P1–P6 with the §3.2 verdict rules), `power.py` (power simulation). Pre-registrations and the G2 brief are in `experiments/`.

---

## Design

This is a small 2×2×2 factorial experiment:

| Dimension | Treatment |
| --- | --- |
| Planning depth | Shallow issue description vs explicit invariants/contract |
| Verification | Basic smoke checks vs richer agent-visible checks |
| Breadth | One task vs two tasks in the same agent session |

The two toy tasks are an order-preserving ID merge and an atomic ledger transfer. Both have intentionally weak starter implementations. Agents may inspect `visible_checks.py` and change only the task modules. Held-out acceptance tests are executed by the runner outside the agent's Docker mount.

Each trial starts from a fresh workspace with the same initial code. Trials are not allowed to request human intervention. The current runner reports **end-to-end accepted success**, duration, exit status and timeouts, with prompt/workspace artifacts. Human interventions = 0 is a *constraint*, not a measured superiority claim.

**Interpretation:** this is an instrumentation prototype, not a scientifically convincing benchmark. Task breadth is confounded with task mix in the initial 1-vs-2 comparison. Future runs should rotate task order, diversify projects, pre-score plan depth independently, randomize trial order, and repeat each cell.

## Run the cloud smoke test

Push to `experiment/autonomy-lab` and open the repository's **Actions → Autonomy Lab cloud smoke** run. The push-triggered job installs Python 3.11 and runs:

```bash
python -m unittest discover -s autonomy_lab/tests -v
python -m autonomy_lab.runner --backend mock --breadths 1,2 --repetitions 1 --out autonomy_lab_results
```

It uploads `summary.csv`, `summary.json`, `trials.jsonl` and each isolated trial workspace. **Mock means no LLM executed; all mock outcomes are plumbing checks and MUST NOT be interpreted as evidence for the theory.**

## Run real agent trials (Docker required)

In a trusted environment with Docker, a dedicated model API credential and a cost budget:

```bash
docker build -t autonomy-lab-agent -f autonomy_lab/Dockerfile .
export OPENAI_API_KEY=... # put in environment or cloud secret manager, never commit
python -m autonomy_lab.runner --backend docker-mini --model openai/gpt-5 \
  --breadths 1,2 --repetitions 5 --timeout 900 --cost-limit 1.0 \
  --out autonomy_lab_results
```

The model ID is an example; use a model your API account can access. The Docker agent has only the trial workspace mounted, drops Linux capabilities, has resource limits, and runs with a read-only root filesystem. Acceptance code stays outside this mount. Model API keys are forwarded via environment variable; the agent can technically inspect its own runtime environment. Do not execute adversarial or third-party tasks with this credential design. A proxy with scoped credentials and enforced outbound policy is required before using untrusted issue text.

The grader runs separately in a Docker container without network access. Each cell's model run is bounded by `--timeout` and mini-swe-agent's `--cost-limit`, although upstream billing and wall-clock accounting should still be monitored. GitHub-hosted runner minutes and model tokens may incur cost. **Do not treat the per-run token limit as a hard billing guarantee.**

## Files

- `runner.py` — fixtures, prompts, Docker execution, acceptance tests, trial metrics, CSV/JSONL outputs
- `tests/test_runner.py` — plumbing assertions and control-condition checks
- `Dockerfile` — reproducible mini-SWE-agent runtime without the held-out grader
- `.github/workflows/autonomy-lab.yml` — cloud smoke pipeline

## Next experiments

1. Parameterize multiple independently rated real issues; rotate breadth order.
2. Pin model, temperature, tool permissions, time budget, seed (when supported) and repo commit SHA.
3. Add trajectory-derived agent steps, recoveries, model cost/tokens and failed verification cycles.
4. Fit a reliability surface as a function of breadth, depth, verification and coupling with confidence intervals.
5. Add unattended checkpoints/resumption for horizons beyond a single CI runner's lifetime.
