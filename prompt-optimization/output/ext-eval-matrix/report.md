# Eval matrix

## Eval matrix (3 runs per cell, offline stub)

| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |
|---|---|---:|---|---:|---|---:|---:|
| qwen3.6-35b-a3b (stub) | seed | 17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3 | 0.000365 | 0.0203 |
| qwen3.6-35b-a3b (stub) | after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000431 | 0.0239 |
| qwen3.8-27b (stub) | seed | 50% | 50% / 50% / 50% | 9/18 | wrong unit 6, wrong value 3 | 0.000721 | 0.0400 |
| qwen3.8-27b (stub) | after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000862 | 0.0479 |

## Outcomes per group (each case counted once: it passes when it passes in at least 2 of its 3 runs; a refusal of an answerable case is counted in wrong and shown as refused)

### qwen3.6-35b-a3b/seed

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| count | 0 | 0 | 1 | 0 | 0 of 1 |
| percentage | 0 | 0 | 2 | 0 | 0 of 2 |
| wait | 0 | 0 | 1 | 0 | 0 of 1 |
| money | 1 | 0 | 1 | 0 | 1 of 2 |
| all | 1 | 0 | 5 | 0 | 1 of 6 |

### qwen3.6-35b-a3b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| count | 1 | 0 | 0 | 0 | 1 of 1 |
| percentage | 2 | 0 | 0 | 0 | 2 of 2 |
| wait | 1 | 0 | 0 | 0 | 1 of 1 |
| money | 1 | 0 | 1 | 0 | 1 of 2 |
| all | 5 | 0 | 1 | 0 | 5 of 6 |

### qwen3.8-27b/seed

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| count | 1 | 0 | 0 | 0 | 1 of 1 |
| percentage | 0 | 0 | 2 | 0 | 0 of 2 |
| wait | 1 | 0 | 0 | 0 | 1 of 1 |
| money | 1 | 0 | 1 | 0 | 1 of 2 |
| all | 3 | 0 | 3 | 0 | 3 of 6 |

### qwen3.8-27b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| count | 1 | 0 | 0 | 0 | 1 of 1 |
| percentage | 2 | 0 | 0 | 0 | 2 of 2 |
| wait | 1 | 0 | 0 | 0 | 1 of 1 |
| money | 1 | 0 | 1 | 0 | 1 of 2 |
| all | 5 | 0 | 1 | 0 | 5 of 6 |

## Cost and time per answer (time: from sending the question to the scored answer; p95 by nearest rank)

| cell | answers | CAD | CAD per answer | median time (s) | p95 time (s) |
|---|---:|---:|---:|---:|---:|
| qwen3.6-35b-a3b/seed | 18 | 0.000365 | 0.000020 | 0.00 | 0.00 |
| qwen3.6-35b-a3b/after | 18 | 0.000431 | 0.000024 | 0.00 | 0.00 |
| qwen3.8-27b/seed | 18 | 0.000721 | 0.000040 | 0.00 | 0.00 |
| qwen3.8-27b/after | 18 | 0.000862 | 0.000048 | 0.00 | 0.00 |
