# Prompt optimization: qwen3.6-35b-a3b

## Test pass rate (6 questions, 3 runs per prompt, task model qwen3.6-35b-a3b, course LLM gateway)

| prompt | median | per run | answers passed | failures | test cost (CAD) |
|---|---:|---|---:|---|---:|
| before | 83% | 83% / 83% / 100% | 16/18 | wrong value 2 | 0.000396 |
| after | 100% | 100% / 100% / 83% | 17/18 | wrong value 1 | 0.000856 |

## Spend

| role | calls | tokens in | tokens out | CAD |
|---|---:|---:|---:|---:|
| reflect | 4 | 6,252 | 13,348 | 0.003295 |
| task | 128 | 99,012 | 3,929 | 0.005344 |
| test-after | 21 | 15,740 | 691 | 0.000856 |
| test-before | 18 | 6,465 | 730 | 0.000396 |
| total | 171 | | | 0.009891 |

## Candidates (validation score, used only to choose)

- #0: 67%
- #1: 100%
- #2: 67%
- #3: 100%
