# Eval matrix

Case `rides`: 20 test cases in 4 groups, table `trips` with 837 rows (trips_hour.csv).

## Eval matrix (3 runs per cell, course LLM gateway)

| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |
|---|---|---:|---|---:|---|---:|---:|
| no-model | templates | 55% | 55% / 55% / 55% | 33/60 | answered an unanswerable case 3, refused an answerable case 24 | 0.000000 | 0.0000 |
| qwen3.6-35b-a3b | seed | 90% | 80% / 90% / 90% | 52/60 | SQL error 1, answered an unanswerable case 5, wrong value 2 | 0.001014 | 0.0169 |
| qwen3.6-35b-a3b | after | 100% | 100% / 100% / 95% | 59/60 | answered an unanswerable case 1 | 0.001252 | 0.0209 |
| qwen3.8-27b | seed | 90% | 90% / 85% / 95% | 54/60 | wrong value 6 | 0.007390 | 0.1232 |
| qwen3.8-27b | after | 100% | 100% / 95% / 100% | 59/60 | SQL error 1 | 0.004245 | 0.0707 |

## Outcomes per group (each case counted once: it passes when it passes in at least 2 of its 3 runs; a refusal of an answerable case is counted in wrong and shown as refused)

### no-model/templates

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 2 | 1 | 2 | 2 | 3 of 5 |
| time-window | 4 | 0 | 1 | 0 | 4 of 5 |
| zone-compare | 0 | 1 | 4 | 4 | 1 of 5 |
| fare-sum | 2 | 1 | 2 | 2 | 3 of 5 |
| all | 8 | 3 | 9 | 8 | 11 of 20 |

### qwen3.6-35b-a3b/seed

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 4 | 1 | 0 | 0 | 5 of 5 |
| time-window | 4 | 0 | 1 | 0 | 4 of 5 |
| zone-compare | 4 | 0 | 1 | 0 | 4 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 16 | 2 | 2 | 0 | 18 of 20 |

### qwen3.6-35b-a3b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 4 | 1 | 0 | 0 | 5 of 5 |
| time-window | 4 | 1 | 0 | 0 | 5 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 16 | 4 | 0 | 0 | 20 of 20 |

### qwen3.8-27b/seed

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 4 | 1 | 0 | 0 | 5 of 5 |
| time-window | 2 | 1 | 2 | 0 | 3 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 14 | 4 | 2 | 0 | 18 of 20 |

### qwen3.8-27b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 4 | 1 | 0 | 0 | 5 of 5 |
| time-window | 4 | 1 | 0 | 0 | 5 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 16 | 4 | 0 | 0 | 20 of 20 |

## Cost and time per answer (time: from sending the question to the scored answer; p95 by nearest rank)

| cell | answers | CAD | CAD per answer | median time (s) | p95 time (s) |
|---|---:|---:|---:|---:|---:|
| no-model/templates | 60 | 0.000000 | 0.000000 | 0.00 | 0.00 |
| qwen3.6-35b-a3b/seed | 60 | 0.001014 | 0.000017 | 0.64 | 0.99 |
| qwen3.6-35b-a3b/after | 60 | 0.001252 | 0.000021 | 0.63 | 0.78 |
| qwen3.8-27b/seed | 60 | 0.007390 | 0.000123 | 2.20 | 18.80 |
| qwen3.8-27b/after | 60 | 0.004245 | 0.000071 | 1.94 | 3.75 |

## Eval trip-sql (a cell passes with at least 80% of its runs and of its cases passed, and 60% of the cases of every group)

| cell | runs passed | cases passed | groups under 60% | gate |
|---|---:|---:|---|---|
| no-model/templates | 33 of 60 (need 48) | 11 of 20 (need 16) | zone-compare 1 of 5 | fails |
| qwen3.6-35b-a3b/seed | 52 of 60 (need 48) | 18 of 20 (need 16) | none | passes |
| qwen3.6-35b-a3b/after | 59 of 60 (need 48) | 20 of 20 (need 16) | none | passes |
| qwen3.8-27b/seed | 54 of 60 (need 48) | 18 of 20 (need 16) | none | passes |
| qwen3.8-27b/after | 59 of 60 (need 48) | 20 of 20 (need 16) | none | passes |

## Comparisons (same cases, saved results only, no model call)

### qwen3.6-35b-a3b/seed against qwen3.6-35b-a3b/after

B is `qwen3.6-35b-a3b/after`, A is `qwen3.6-35b-a3b/seed`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | qwen3.6-35b-a3b/seed passed | qwen3.6-35b-a3b/after passed | qwen3.6-35b-a3b/after only | qwen3.6-35b-a3b/seed only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| time-window | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| zone-compare | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| fare-sum | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| all | 18 of 20 | 20 of 20 | 2 | 0 | +0.10 | 0.5000 | too few cases |

All cases, difference in the share of runs passed (B minus A): +0.12, descriptive 95% interval +0.03 to +0.22 (bootstrap stratified by group, 10,000 resamples, seed 0).

### qwen3.6-35b-a3b/after against qwen3.8-27b/after

B is `qwen3.8-27b/after`, A is `qwen3.6-35b-a3b/after`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | qwen3.6-35b-a3b/after passed | qwen3.8-27b/after passed | qwen3.8-27b/after only | qwen3.6-35b-a3b/after only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| time-window | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| zone-compare | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| fare-sum | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| all | 20 of 20 | 20 of 20 | 0 | 0 | +0.00 | 1.0000 | too few cases |

All cases, difference in the share of runs passed (B minus A): +0.00, descriptive 95% interval -0.03 to +0.03 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.6-35b-a3b/seed

B is `qwen3.6-35b-a3b/seed`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.6-35b-a3b/seed passed | qwen3.6-35b-a3b/seed only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| time-window | 4 of 5 | 4 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 4 of 5 | 4 | 1 | +0.60 | 0.3750 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 18 of 20 | 8 | 1 | +0.35 | 0.0391 |  |

All cases, difference in the share of runs passed (B minus A): +0.32, descriptive 95% interval +0.07 to +0.55 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.6-35b-a3b/after

B is `qwen3.6-35b-a3b/after`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.6-35b-a3b/after passed | qwen3.6-35b-a3b/after only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| time-window | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 20 of 20 | 9 | 0 | +0.45 | 0.0039 |  |

All cases, difference in the share of runs passed (B minus A): +0.43, descriptive 95% interval +0.25 to +0.62 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.8-27b/seed

B is `qwen3.8-27b/seed`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.8-27b/seed passed | qwen3.8-27b/seed only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| time-window | 4 of 5 | 3 of 5 | 1 | 2 | -0.20 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 18 of 20 | 9 | 2 | +0.35 | 0.0654 |  |

All cases, difference in the share of runs passed (B minus A): +0.35, descriptive 95% interval +0.12 to +0.58 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.8-27b/after

B is `qwen3.8-27b/after`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.8-27b/after passed | qwen3.8-27b/after only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| time-window | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 20 of 20 | 9 | 0 | +0.45 | 0.0039 |  |

All cases, difference in the share of runs passed (B minus A): +0.43, descriptive 95% interval +0.25 to +0.63 (bootstrap stratified by group, 10,000 resamples, seed 0).
