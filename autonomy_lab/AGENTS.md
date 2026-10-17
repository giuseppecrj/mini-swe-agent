# AGENTS.md — Autonomy Lab local coordinator

This directory is an experiment, not a product feature. You are continuing an earlier ChatGPT research and implementation session. The canonical context is SESSION_HANDOFF.md, the machine-readable state is STATE.json, and the baseline implementation is runner.py / pi_runner.py.

## On startup

1. Read SESSION_HANDOFF.md, STATE.json, and this file before proposing actions.
2. From the repository root, inspect the Git branch/status, the installed Pi CLI version, and the Python version. Do not assume what is installed.
3. Stay on experiment/autonomy-lab or a new derived experiment branch; never modify main without explicit instruction.
4. Run offline validation before any billed model call:
   - python3 -m unittest discover -s autonomy_lab/tests -v
   - python3 -m autonomy_lab.runner --backend mock --out autonomy_lab_results
5. If the user asked to start real testing, execute one bounded trial with the authenticated Pi CLI and report actual results. Never describe mock pass rates as empirical results.

## Running a real trial

The first real trial has already run (see SESSION_HANDOFF.md, section 8). From the repository root, the same bounded condition is:

    python3 -m autonomy_lab.pi_runner \
      --model openai-codex/gpt-5.6-sol --thinking high \
      --breadths 1 --depths deep --verifications rich \
      --repetitions 1 --timeout 300

--model (exact provider/model-id) and --thinking are required. Pi's configured default is not used, because extension-provided providers are unavailable under --no-extensions. A trial is only accepted when every assistant message reports the requested model and thinking level. Check that a model works with the user's account before a run: ChatGPT-account Codex rejects some catalog models (e.g. gpt-5.5) at request time.

pi_runner.py deliberately launches benchmark children with --no-session --mode json --no-extensions --no-skills --no-prompt-templates --no-context-files --no-mcp --no-approve and a fixed tool allowlist. **Do not inject these handoff instructions into trial prompts.** The interactive coordinator may use Herdr/Pi skills; benchmark children may not.

## Experimental integrity

- Keep planning depth, agent-visible verification, and breadth distinct. A deep plan isn't a longer prompt by definition; it contains more correct consequential decisions.
- Keep harness, provider/model, model settings, starting code, and test suite stable across compared arms.
- Only task modules may be changed by benchmark agents. Do not change visible_checks.py, prompt.txt, held-out checks, or fixture requirements to boost results.
- Report held-out acceptance, wall time, tool calls, model usage/cost (if available), errors, retries, and explicitly allowed human interventions. CLI exit status alone does not establish correct work.
- A Pi process running with the user's OS rights is not sandboxed, and held-out grader source exists in the parent checkout. The v0.2 Pi adapter is for trusted first-party toy tasks, not adversarial agents.
- autonomy_lab/local-results contains raw agent event logs that could include confidential content. Do not commit, publish, or paste raw logs without review.
- Repeat and randomize trials before fitting confidence intervals. State the confounds of the two-task prototype.
- Never commit credentials or inspect environment secrets. Pi may automatically use the user's authorized model authentication.
- Global Pi workflows such as /skill:implementation-workflow, /sweep, /ship and optional Pi Herdr Agents may help DEVELOP the experiment, but may not silently affect benchmark agents. /ship does not confer merge, deployment, release or force-push authority.
- Do not create a new cloud VM or incur recurring cloud charges without explicit instruction.

## Handoff report

Record the exact branch/commit, host runtime and Pi version, model ID (or unknown), whether the run was mock or real, the exact command, number of trials, accepted results, errors, limitations, and next falsifiable hypothesis.

## First task after handoff

Validate the repo and run the smallest real Pi benchmark the user requested. If Pi or its authenticated model is unavailable, stop at that prerequisite and report the actual blocker. Do not substitute a mock result.
