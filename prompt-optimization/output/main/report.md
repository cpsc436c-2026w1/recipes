# Prompt optimization: qwen3.6-35b-a3b (stub)

## Test pass rate (6 questions, 3 runs per prompt, task model qwen3.6-35b-a3b, offline stub)

| prompt | median | per run | answers passed | failures | test cost (CAD) |
|---|---:|---|---:|---|---:|
| before | 17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3 | 0.000365 |
| after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000431 |

## Spend

| role | calls | tokens in | tokens out | CAD |
|---|---:|---:|---:|---:|
| reflect | 8 | 10,395 | 946 | 0.001229 |
| task | 105 | 43,166 | 2,822 | 0.002440 |
| test-after | 18 | 7,602 | 510 | 0.000431 |
| test-before | 18 | 6,198 | 549 | 0.000365 |
| total | 149 | | | 0.004465 |

## Per question (runs passed, out of 3 for before and 3 for after)

| id | question | before | after |
|---|---|---:|---:|
| q01 | How many orders were paid by card? | 0 | 3 |
| q02 | What percentage of orders were for tea? | 0 | 3 |
| q03 | What is the shortest wait, in minutes, for any order? | 0 | 3 |
| q04 | What is the average tip, in dollars, on cash orders? | 3 | 3 |
| q05 | What percentage of all tips came from latte orders? | 0 | 3 |
| q06 | What is the average amount paid per card order, in dollars, counting the quantity and leaving out tips? | 0 | 0 |

With the prompt `after`, 4 of 6 questions pass more often than with `before`, 0 less often, 2 the same.

## Candidates (validation score, used only to choose)

- #0: 33%
- #1: 67%
- #2: 100%

The search ended at: reflection cap (8 calls)
