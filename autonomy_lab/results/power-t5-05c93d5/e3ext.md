Power simulation, seed 0: 400 simulations per cell with 1000 bootstrap resamples; cells within 2 SE of 80% on their target were re-simulated with 1000 simulations and 10000 resamples (marked * in the Sims column). Cells show % supported ± 1 Monte Carlo SE / % falsified ± 1 SE.

E3 scenarios (single-task success mean 0.60, logit SD 0.6; true mean observed - predicted):

- **independent**: tasks fail independently: observed equals the product in expectation (B=2 +0.0 pp, B=4 +0.0 pp). Correct outcome: P6 supported at B=2 and B=4 (H4 supported).
- **interference**: per-task success drops from 0.60 to 0.40 inside multi-task sessions (B=2 -20.0 pp, B=4 -10.4 pp). Correct outcome: P6 falsified at B=2; the B=4 gap is inside the margin, because products near 0.13 cannot fall 15 pp.
- **shared-trial**: a shared per-trial logit effect (SD 2.5) makes successes positively dependent (B=2 +10.8 pp, B=4 +21.9 pp). Correct outcome: P6 falsified at B=4; the B=2 gap is inside the margin.

| Scenario | Tasks | B=1 per task | Trials per breadth | Total | Sims | P6 B=2 | P6 B=4 | H4 (joint) |
|---|---|---|---|---|---|---|---|---|
| independent | 12 | 4 | 24 | 96 | 400 | 0±0.0 / 6±1.2 | 14±1.7 / 4±0.9 | 0±0.0 / 9±1.4 |
| independent | 12 | 4 | 192 | 432 | 400 | 16±1.8 / 6±1.2 | 64±2.4 / 2±0.7 | 15±1.8 / 7±1.3 |
| independent | 12 | 4 | 288 | 624 | 400 | 18±1.9 / 8±1.4 | 62±2.4 / 2±0.6 | 17±1.9 / 8±1.4 |
| independent | 12 | 4 | 384 | 816 | 400 | 21±2.0 / 5±1.1 | 66±2.4 / 2±0.6 | 20±2.0 / 6±1.2 |
| independent | 12 | 12 | 24 | 192 | 400 | 0±0.4 / 5±1.1 | 33±2.4 / 4±0.9 | 0±0.4 / 8±1.3 |
| independent | 12 | 12 | 192 | 528 | 400 | 67±2.4 / 2±0.7 | 90±1.5 / 0±0.4 | 65±2.4 / 2±0.7 |
| independent | 12 | 12 | 288 | 720 | 400 | 74±2.2 / 0±0.2 | 93±1.3 / 0±0.0 | 74±2.2 / 0±0.2 |
| independent | 12 | 12 | 384 | 912 | 1000* | 80±1.3 / 1±0.3 | 96±0.6 / 0±0.0 | 79±1.3 / 1±0.3 |
| independent | 12 | 16 | 24 | 240 | 400 | 0±0.2 / 7±1.3 | 35±2.4 / 4±0.9 | 0±0.0 / 10±1.5 |
| independent | 12 | 16 | 192 | 576 | 400 | 73±2.2 / 1±0.5 | 95±1.1 / 0±0.2 | 72±2.3 / 1±0.6 |
| independent | 12 | 16 | 288 | 768 | 400 | 85±1.8 / 1±0.4 | 96±1.0 / 0±0.0 | 84±1.8 / 1±0.4 |
| independent | 12 | 16 | 384 | 960 | 1000* | 87±1.1 / 0±0.1 | 97±0.5 / 0±0.0 | 86±1.1 / 0±0.1 |
| independent | 12 | 24 | 24 | 336 | 400 | 0±0.0 / 8±1.4 | 46±2.5 / 2±0.7 | 0±0.0 / 10±1.5 |
| independent | 12 | 24 | 192 | 672 | 400 | 90±1.5 / 0±0.0 | 98±0.7 / 0±0.0 | 90±1.5 / 0±0.0 |
| independent | 12 | 24 | 288 | 864 | 400 | 94±1.2 / 0±0.0 | 99±0.4 / 0±0.0 | 94±1.2 / 0±0.0 |
| independent | 12 | 24 | 384 | 1056 | 400 | 95±1.1 / 0±0.0 | 98±0.6 / 0±0.0 | 95±1.1 / 0±0.0 |
| interference | 12 | 4 | 24 | 96 | 400 | 0±0.0 / 41±2.5 | 24±2.1 / 20±2.0 | 0±0.0 / 48±2.5 |
| interference | 12 | 4 | 192 | 432 | 400 | 2±0.8 / 68±2.3 | 35±2.4 / 21±2.0 | 2±0.8 / 68±2.3 |
| interference | 12 | 4 | 288 | 624 | 400 | 3±0.8 / 70±2.3 | 34±2.4 / 15±1.8 | 3±0.8 / 70±2.3 |
| interference | 12 | 4 | 384 | 816 | 400 | 2±0.7 / 64±2.4 | 38±2.4 / 21±2.0 | 2±0.7 / 64±2.4 |
| interference | 12 | 12 | 24 | 192 | 400 | 0±0.2 / 56±2.5 | 26±2.2 / 18±1.9 | 0±0.2 / 60±2.4 |
| interference | 12 | 12 | 192 | 528 | 400 | 1±0.4 / 76±2.1 | 45±2.5 / 11±1.5 | 1±0.4 / 76±2.1 |
| interference | 12 | 12 | 288 | 720 | 400 | 1±0.5 / 83±1.9 | 43±2.5 / 12±1.6 | 1±0.5 / 83±1.9 |
| interference | 12 | 12 | 384 | 912 | 400 | 1±0.4 / 83±1.9 | 44±2.5 / 14±1.7 | 1±0.4 / 83±1.9 |
| interference | 12 | 16 | 24 | 240 | 400 | 0±0.0 / 58±2.5 | 36±2.4 / 18±1.9 | 0±0.0 / 62±2.4 |
| interference | 12 | 16 | 192 | 576 | 400 | 1±0.4 / 82±1.9 | 47±2.5 / 12±1.6 | 1±0.4 / 82±1.9 |
| interference | 12 | 16 | 288 | 768 | 400 | 0±0.2 / 83±1.9 | 46±2.5 / 12±1.6 | 0±0.2 / 83±1.9 |
| interference | 12 | 16 | 384 | 960 | 400 | 0±0.2 / 85±1.8 | 50±2.5 / 13±1.7 | 0±0.2 / 85±1.8 |
| interference | 12 | 24 | 24 | 336 | 400 | 0±0.0 / 64±2.4 | 40±2.4 / 16±1.8 | 0±0.0 / 68±2.3 |
| interference | 12 | 24 | 192 | 672 | 400 | 0±0.4 / 87±1.7 | 55±2.5 / 12±1.6 | 0±0.4 / 87±1.7 |
| interference | 12 | 24 | 288 | 864 | 400 | 0±0.2 / 87±1.7 | 52±2.5 / 10±1.5 | 0±0.2 / 87±1.7 |
| interference | 12 | 24 | 384 | 1056 | 400 | 1±0.4 / 86±1.7 | 52±2.5 / 11±1.6 | 1±0.4 / 86±1.7 |
| shared-trial | 12 | 4 | 24 | 96 | 400 | 0±0.0 / 10±1.5 | 1±0.4 / 46±2.5 | 0±0.0 / 48±2.5 |
| shared-trial | 12 | 4 | 192 | 432 | 400 | 2±0.7 / 29±2.3 | 0±0.0 / 78±2.1 | 0±0.0 / 78±2.1 |
| shared-trial | 12 | 4 | 288 | 624 | 400 | 4±1.0 / 32±2.3 | 0±0.0 / 84±1.8 | 0±0.0 / 84±1.8 |
| shared-trial | 12 | 4 | 384 | 816 | 400 | 3±0.9 / 32±2.3 | 0±0.0 / 83±1.9 | 0±0.0 / 83±1.9 |
| shared-trial | 12 | 12 | 24 | 192 | 400 | 0±0.0 / 15±1.8 | 0±0.2 / 58±2.5 | 0±0.0 / 63±2.4 |
| shared-trial | 12 | 12 | 192 | 528 | 400 | 14±1.8 / 27±2.2 | 0±0.0 / 94±1.2 | 0±0.0 / 94±1.1 |
| shared-trial | 12 | 12 | 288 | 720 | 400 | 15±1.8 / 21±2.0 | 0±0.0 / 94±1.1 | 0±0.0 / 94±1.1 |
| shared-trial | 12 | 12 | 384 | 912 | 400 | 23±2.1 / 20±2.0 | 0±0.0 / 93±1.3 | 0±0.0 / 93±1.3 |
| shared-trial | 12 | 16 | 24 | 240 | 400 | 0±0.0 / 14±1.8 | 1±0.5 / 58±2.5 | 0±0.0 / 63±2.4 |
| shared-trial | 12 | 16 | 192 | 576 | 400 | 19±2.0 / 27±2.2 | 0±0.2 / 93±1.3 | 0±0.2 / 93±1.3 |
| shared-trial | 12 | 16 | 288 | 768 | 400 | 24±2.1 / 18±1.9 | 0±0.0 / 94±1.1 | 0±0.0 / 94±1.1 |
| shared-trial | 12 | 16 | 384 | 960 | 400 | 26±2.2 / 17±1.9 | 0±0.0 / 94±1.2 | 0±0.0 / 94±1.2 |
| shared-trial | 12 | 24 | 24 | 336 | 400 | 0±0.0 / 18±1.9 | 1±0.5 / 62±2.4 | 0±0.0 / 67±2.4 |
| shared-trial | 12 | 24 | 192 | 672 | 400 | 19±2.0 / 21±2.0 | 0±0.0 / 94±1.2 | 0±0.0 / 94±1.2 |
| shared-trial | 12 | 24 | 288 | 864 | 400 | 25±2.2 / 18±1.9 | 0±0.2 / 96±1.0 | 0±0.2 / 96±1.0 |
| shared-trial | 12 | 24 | 384 | 1056 | 400 | 26±2.2 / 15±1.8 | 0±0.0 / 97±0.9 | 0±0.0 / 97±0.9 |

Recommendation (target power 80%, power ± 1 MC SE):

- E1, P1 under strong: no grid cell reaches the target; add tasks.
- E1, P2 under strong: no grid cell reaches the target.
- E1, P3 under strong: no grid cell reaches the target.
- E1, P4 under strong: no grid cell reaches the target.
- E2, P5 under effective: no grid cell reaches the target.
- E3, joint H4 under independence, within the 90-trial E3 budget: no cell reaches the target.
- E3, joint H4 under independence, in the grid: smallest qualifying cell 12 tasks x 24 B=1 per task x 192 per breadth (672 trials): 90±1.5%.

Elapsed: 1105.5 s
