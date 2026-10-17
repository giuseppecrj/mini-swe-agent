Power simulation, seed 0: 1000 simulations per cell with 10000 bootstrap resamples; cells within 2 SE of 80% on their target were re-simulated with 1000 simulations and 10000 resamples (marked * in the Sims column). Cells show % supported ± 1 Monte Carlo SE / % falsified ± 1 SE.

E1 scenarios (logit SDs task 0.8, decision 0.5, trial 0.5). Decision mix per task, arbitrary/inferable/preserve, cycled over tasks: draft bank 4/1/1, 3/1/1, 4/1/1, 4/2/1, 3/1/1, 4/1/1 unless stated:

- **strong**: arbitrary settled 0.95 on every channel, unsettled 0.30; inferable settled 0.95, unsettled 0.80; no channel, filler or spillover effect. Correct outcome: P1 supported, P2 supported, P3 supported, P4 supported.
- **strong-legacy**: as strong, with the first simulation's structure: 6 decisions, 2 arbitrary, 3 inferable, 1 preserve. Correct outcome: P1 supported, P2 supported, P3 supported, P4 supported.
- **moderate**: arbitrary settled 0.93, unsettled 0.45 (+48 pp, close to the P1(b)/(c) thresholds). Correct outcome: P1 supported.
- **null**: no coverage effect: every arbitrary channel 0.85 (unsettled decisions are not arbitrary for this agent). Correct outcome: P1 falsified.
- **execution**: arbitrary settled 0.70, unsettled 0.20: a large effect produced by poor execution. Correct outcome: P1 falsified.
- **channel-gap**: arbitrary contract/both 0.95, check-only 0.65, unsettled 0.30 (+30 pp contract minus check). Correct outcome: P2 falsified.
- **channel-gap-inferable**: as strong, but inferable check-only 0.65 (+30 pp contract minus check, inferable only). Correct outcome: P2 falsified.
- **filler**: as strong, plus filler raises the unsettled arbitrary rate by 25 pp (on minus off). Correct outcome: P3 falsified.
- **filler-inferable**: as strong, plus filler raises the unsettled inferable rate by 25 pp (inferable only). Correct outcome: P3 falsified.
- **spillover**: as strong, plus unsettled arbitrary rate 25 pp higher at sibling-check >= 0.6 than at <= 0.4. Correct outcome: P4 falsified.
- **spillover-inferable**: as strong, plus the same 25 pp spillover on unsettled inferable decisions only. Correct outcome: P4 falsified.

| Scenario | Tasks | Trials/task | Total | Sims | P1 | P1(a) | P2 | P3 | P4 |
|---|---|---|---|---|---|---|---|---|---|
| strong | 12 | 16 | 192 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 97±0.5 / 0±0.0 | 1±0.4 / 0±0.1 | 1±0.2 / 0±0.1 |
| strong | 12 | 24 | 288 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 99±0.3 / 0±0.0 | 9±0.9 / 0±0.1 | 2±0.4 / 0±0.0 |
| strong | 12 | 32 | 384 | 1000 | 99±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.2 / 0±0.0 | 23±1.3 / 0±0.0 | 7±0.8 / 0±0.1 |
| strong-legacy | 12 | 16 | 192 | 1000 | 99±0.3 / 0±0.0 | 100±0.1 / 0±0.0 | 99±0.3 / 0±0.0 | 4±0.7 / 0±0.1 | 2±0.4 / 0±0.1 |
| strong-legacy | 12 | 24 | 288 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.0 / 0±0.0 | 14±1.1 / 0±0.0 | 10±1.0 / 0±0.1 |
| strong-legacy | 12 | 32 | 384 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.0 / 0±0.0 | 37±1.5 / 0±0.0 | 34±1.5 / 0±0.0 |
| moderate | 12 | 16 | 192 | 1000 | 74±1.4 / 0±0.0 | 98±0.4 / 0±0.0 | 96±0.6 / 0±0.0 | 2±0.4 / 0±0.2 | 0±0.2 / 0±0.2 |
| moderate | 12 | 24 | 288 | 1000 | 77±1.3 / 0±0.0 | 100±0.2 / 0±0.0 | 100±0.2 / 0±0.0 | 7±0.8 / 0±0.0 | 2±0.4 / 0±0.1 |
| moderate | 12 | 32 | 384 | 1000 | 78±1.3 / 0±0.0 | 99±0.3 / 0±0.0 | 100±0.0 / 0±0.0 | 20±1.3 / 0±0.0 | 5±0.7 / 0±0.0 |
| null | 12 | 16 | 192 | 1000 | 0±0.0 / 91±0.9 | 0±0.0 / 2±0.4 | 94±0.8 / 0±0.0 | 2±0.5 / 0±0.1 | 1±0.3 / 0±0.1 |
| null | 12 | 24 | 288 | 1000 | 0±0.0 / 93±0.8 | 0±0.0 / 2±0.5 | 99±0.3 / 0±0.0 | 11±1.0 / 0±0.0 | 4±0.6 / 0±0.2 |
| null | 12 | 32 | 384 | 1000 | 0±0.0 / 95±0.7 | 0±0.0 / 2±0.4 | 100±0.1 / 0±0.0 | 27±1.4 / 0±0.0 | 10±0.9 / 0±0.1 |
| execution | 12 | 16 | 192 | 1000 | 0±0.0 / 61±1.5 | 100±0.2 / 0±0.0 | 92±0.9 / 0±0.0 | 3±0.5 / 0±0.1 | 2±0.4 / 0±0.1 |
| execution | 12 | 24 | 288 | 1000 | 0±0.0 / 65±1.5 | 99±0.2 / 0±0.0 | 98±0.4 / 0±0.0 | 9±0.9 / 0±0.0 | 3±0.5 / 0±0.1 |
| execution | 12 | 32 | 384 | 1000 | 0±0.0 / 70±1.5 | 100±0.2 / 0±0.0 | 100±0.2 / 0±0.0 | 23±1.3 / 0±0.0 | 7±0.8 / 0±0.1 |
| channel-gap | 12 | 16 | 192 | 1000 | 3±0.5 / 0±0.0 | 100±0.1 / 0±0.0 | 0±0.0 / 51±1.6 | 1±0.4 / 0±0.2 | 0±0.2 / 0±0.2 |
| channel-gap | 12 | 24 | 288 | 1000 | 2±0.4 / 0±0.0 | 100±0.0 / 0±0.0 | 0±0.0 / 62±1.5 | 8±0.9 / 0±0.0 | 2±0.4 / 0±0.1 |
| channel-gap | 12 | 32 | 384 | 1000 | 1±0.3 / 0±0.1 | 100±0.0 / 0±0.0 | 0±0.0 / 68±1.5 | 21±1.3 / 0±0.0 | 6±0.7 / 0±0.0 |
| channel-gap-inferable | 12 | 16 | 192 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 0±0.1 / 28±1.4 | 1±0.3 / 0±0.1 | 1±0.3 / 0±0.0 |
| channel-gap-inferable | 12 | 24 | 288 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 0±0.0 / 38±1.5 | 7±0.8 / 0±0.0 | 2±0.4 / 0±0.1 |
| channel-gap-inferable | 12 | 32 | 384 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 0±0.0 / 40±1.5 | 20±1.3 / 0±0.0 | 8±0.8 / 0±0.0 |
| filler | 12 | 16 | 192 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 97±0.6 / 0±0.0 | 0±0.0 / 39±1.5 | 0±0.2 / 0±0.2 |
| filler | 12 | 24 | 288 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 99±0.2 / 0±0.0 | 0±0.0 / 53±1.6 | 2±0.4 / 0±0.1 |
| filler | 12 | 32 | 384 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.2 / 0±0.0 | 0±0.0 / 57±1.6 | 8±0.8 / 0±0.0 |
| filler-inferable | 12 | 16 | 192 | 1000 | 99±0.3 / 0±0.0 | 100±0.0 / 0±0.0 | 97±0.5 / 0±0.0 | 0±0.0 / 19±1.2 | 0±0.2 / 0±0.2 |
| filler-inferable | 12 | 24 | 288 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 99±0.3 / 0±0.0 | 0±0.2 / 26±1.4 | 1±0.3 / 0±0.2 |
| filler-inferable | 12 | 32 | 384 | 1000 | 100±0.0 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.2 / 0±0.0 | 0±0.1 / 26±1.4 | 5±0.7 / 0±0.0 |
| spillover | 12 | 16 | 192 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 96±0.6 / 0±0.0 | 1±0.3 / 0±0.0 | 0±0.0 / 32±1.5 |
| spillover | 12 | 24 | 288 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 99±0.3 / 0±0.0 | 8±0.9 / 0±0.0 | 0±0.0 / 39±1.5 |
| spillover | 12 | 32 | 384 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.2 / 0±0.0 | 20±1.3 / 0±0.0 | 0±0.0 / 47±1.6 |
| spillover-inferable | 12 | 16 | 192 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 96±0.6 / 0±0.0 | 1±0.3 / 0±0.0 | 0±0.0 / 19±1.2 |
| spillover-inferable | 12 | 24 | 288 | 1000 | 100±0.1 / 0±0.0 | 100±0.0 / 0±0.0 | 99±0.3 / 0±0.0 | 5±0.7 / 0±0.1 | 0±0.1 / 20±1.3 |
| spillover-inferable | 12 | 32 | 384 | 1000 | 100±0.2 / 0±0.0 | 100±0.0 / 0±0.0 | 100±0.2 / 0±0.0 | 19±1.3 / 0±0.0 | 0±0.2 / 23±1.3 |

Per-stratum support (% supported ± SE) for the strong scenario; a component needs both strata:

| Tasks | Trials/task | P2[arbitrary] | P2[inferable] | P3[arbitrary] | P3[inferable] | P4[arbitrary] | P4[inferable] |
|---|---|---|---|---|---|---|---|
| 12 | 16 | 100±0.0 | 97±0.5 | 36±1.5 | 4±0.6 | 20±1.3 | 3±0.5 |
| 12 | 24 | 100±0.0 | 99±0.3 | 62±1.5 | 13±1.1 | 33±1.5 | 6±0.8 |
| 12 | 32 | 100±0.0 | 100±0.2 | 80±1.3 | 28±1.4 | 52±1.6 | 12±1.0 |

E2 scenarios (P5; every decision unsettled, arms balanced within task, draft-bank mix; logit SDs task 0.8, decision 0.5, trial 0.5, satisfaction task-by-arm 0.25; 20% audit, auditor assumed error-free):

- **effective**: plain discloses 10%, assume 75%; satisfaction unchanged (arbitrary 0.30, inferable 0.80); judge 95% accurate. Correct outcome: P5 supported.
- **effective-noisy-judge**: as effective, judge 85% accurate (judged plain rate about 0.22, audit kappa about 0.7). Correct outcome: P5 supported in truth; judge error biases P5(a) and lowers kappa.
- **weak-instruction**: plain 10%, assume 45%: the instruction helps, but less than the prediction. Correct outcome: P5 inconclusive.
- **no-effect**: plain and assume both disclose 10%. Correct outcome: P5 falsified (P5(b)).
- **satisfaction-shift**: as effective, plus assume raises arbitrary satisfaction by 20 pp. Correct outcome: P5 falsified (P5(c)).
- **satisfaction-shift-inferable**: as effective, plus assume lowers inferable satisfaction by 20 pp (inferable only). Correct outcome: P5 falsified (P5(c)).

| Scenario | Tasks | Trials/arm | Total | Sims | P5 | P5(a) | P5(b) | P5(c) | P5(c) arb | P5(c) inf | kappa >= 0.6 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| effective | 12 | 4 | 96 | 1000 | 18±1.2 / 0±0.0 | 96±0.6 / 0±0.0 | 100±0.2 / 0±0.0 | 18±1.2 / 0±0.0 | 72±1.4 | 25±1.4 | 100±0.0 |
| effective | 12 | 12 | 288 | 1000* | 81±1.2 / 0±0.0 | 99±0.4 / 0±0.0 | 100±0.1 / 0±0.0 | 82±1.2 / 0±0.0 | 98±0.4 | 84±1.2 | 100±0.0 |
| effective-noisy-judge | 12 | 4 | 96 | 1000 | 6±0.7 / 0±0.0 | 29±1.4 / 0±0.0 | 96±0.6 / 0±0.0 | 19±1.2 / 0±0.0 | 69±1.5 | 28±1.4 | 84±1.1 |
| effective-noisy-judge | 12 | 12 | 288 | 1000 | 16±1.2 / 0±0.0 | 20±1.3 / 0±0.0 | 99±0.3 / 0±0.0 | 82±1.2 / 0±0.0 | 99±0.3 | 82±1.2 | 97±0.6 |
| weak-instruction | 12 | 4 | 96 | 1000 | 0±0.1 / 0±0.0 | 95±0.7 / 0±0.0 | 0±0.2 / 0±0.0 | 16±1.2 / 0±0.0 | 68±1.5 | 25±1.4 | 100±0.0 |
| weak-instruction | 12 | 12 | 288 | 1000 | 0±0.0 / 0±0.0 | 98±0.4 / 0±0.0 | 0±0.1 / 0±0.0 | 82±1.2 / 0±0.0 | 98±0.5 | 83±1.2 | 100±0.0 |
| no-effect | 12 | 4 | 96 | 1000 | 0±0.0 / 84±1.2 | 96±0.6 / 0±0.0 | 0±0.0 / 98±0.5 | 19±1.2 / 0±0.1 | 69±1.5 | 26±1.4 | 87±1.1 |
| no-effect | 12 | 12 | 288 | 1000 | 0±0.0 / 96±0.6 | 98±0.4 / 0±0.0 | 0±0.0 / 100±0.2 | 81±1.3 / 0±0.0 | 98±0.5 | 82±1.2 | 96±0.6 |
| satisfaction-shift | 12 | 4 | 96 | 1000 | 0±0.1 / 21±1.3 | 96±0.6 / 0±0.0 | 99±0.3 / 0±0.0 | 0±0.1 / 21±1.3 | 1±0.3 | 25±1.4 | 100±0.0 |
| satisfaction-shift | 12 | 12 | 288 | 1000 | 0±0.1 / 30±1.5 | 99±0.3 / 0±0.0 | 100±0.2 / 0±0.0 | 0±0.1 / 30±1.5 | 0±0.1 | 84±1.2 | 100±0.0 |
| satisfaction-shift-inferable | 12 | 4 | 96 | 1000 | 1±0.4 / 12±1.0 | 96±0.6 / 0±0.0 | 99±0.3 / 0±0.0 | 1±0.4 / 12±1.0 | 67±1.5 | 2±0.4 | 100±0.0 |
| satisfaction-shift-inferable | 12 | 12 | 288 | 1000 | 1±0.2 / 22±1.3 | 99±0.4 / 0±0.0 | 100±0.1 / 0±0.0 | 1±0.2 / 22±1.3 | 98±0.5 | 1±0.2 | 100±0.0 |

Recommendation (target power 80%, power ± 1 MC SE):

- E1, P1 under strong: smallest qualifying cell 12 tasks x 16 trials per task (192 trials): 100±0.2%.
- E1, P2 under strong: smallest qualifying cell 12 tasks x 16 trials per task (192 trials): 97±0.5%.
- E1, P3 under strong: no grid cell reaches the target.
- E1, P4 under strong: no grid cell reaches the target.
  - At the planned 12 x 16, supported under strong: P1 100±0.2%, P2 97±0.5%, P3 1±0.4%, P4 1±0.2%.
- E2, P5 under effective: smallest qualifying cell 12 tasks x 12 trials per arm (288 trials): 81±1.2%.
  - At the planned 12 x 4 x 2, supported under effective: P5 18±1.2%, P5a 96±0.6%, P5b 100±0.2%, P5c 18±1.2%, P5c[arbitrary] 72±1.4%, P5c[inferable] 25±1.4%.
- E3, joint H4 under independence, within the 90-trial E3 budget: no cell reaches the target.
- E3, joint H4 under independence, in the grid: no cell reaches the target.

Elapsed: 740.7 s
