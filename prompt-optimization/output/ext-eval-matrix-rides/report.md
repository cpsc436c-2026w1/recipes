# Eval matrix

Case `rides`: 20 test cases in 4 groups, table `trips` with 837 rows (trips_hour.csv).

## Eval matrix (3 runs per cell, offline stub)

| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |
|---|---|---:|---|---:|---|---:|---:|
| no-model | templates | 55% | 55% / 55% / 55% | 33/60 | answered an unanswerable case 3, refused an answerable case 24 | 0.000000 | 0.0000 |
| qwen3.6-35b-a3b (stub) | seed | 40% | 40% / 40% / 40% | 24/60 | answered an unanswerable case 12, wrong value 24 | 0.000827 | 0.0138 |
| qwen3.6-35b-a3b (stub) | after | 95% | 95% / 95% / 95% | 57/60 | wrong value 3 | 0.001126 | 0.0188 |
| qwen3.8-27b (stub) | seed | 80% | 80% / 80% / 80% | 48/60 | wrong value 12 | 0.001629 | 0.0271 |
| qwen3.8-27b (stub) | after | 95% | 95% / 95% / 95% | 57/60 | wrong value 3 | 0.002252 | 0.0375 |

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
| one-zone | 3 | 0 | 2 | 0 | 3 of 5 |
| time-window | 1 | 0 | 4 | 0 | 1 of 5 |
| zone-compare | 0 | 0 | 5 | 0 | 0 of 5 |
| fare-sum | 4 | 0 | 1 | 0 | 4 of 5 |
| all | 8 | 0 | 12 | 0 | 8 of 20 |

### qwen3.6-35b-a3b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 3 | 1 | 1 | 0 | 4 of 5 |
| time-window | 4 | 1 | 0 | 0 | 5 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 15 | 4 | 1 | 0 | 19 of 20 |

### qwen3.8-27b/seed

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 3 | 1 | 1 | 0 | 4 of 5 |
| time-window | 1 | 1 | 3 | 0 | 2 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 12 | 4 | 4 | 0 | 16 of 20 |

### qwen3.8-27b/after

| group | correct | correct refusal | wrong | of them refused | cases passed |
|---|---:|---:|---:|---:|---:|
| one-zone | 3 | 1 | 1 | 0 | 4 of 5 |
| time-window | 4 | 1 | 0 | 0 | 5 of 5 |
| zone-compare | 4 | 1 | 0 | 0 | 5 of 5 |
| fare-sum | 4 | 1 | 0 | 0 | 5 of 5 |
| all | 15 | 4 | 1 | 0 | 19 of 20 |

## Cost and time per answer (time: from sending the question to the scored answer; p95 by nearest rank)

| cell | answers | CAD | CAD per answer | median time (s) | p95 time (s) |
|---|---:|---:|---:|---:|---:|
| no-model/templates | 60 | 0.000000 | 0.000000 | 0.00 | 0.00 |
| qwen3.6-35b-a3b/seed | 60 | 0.000827 | 0.000014 | 0.00 | 0.00 |
| qwen3.6-35b-a3b/after | 60 | 0.001126 | 0.000019 | 0.00 | 0.00 |
| qwen3.8-27b/seed | 60 | 0.001629 | 0.000027 | 0.00 | 0.00 |
| qwen3.8-27b/after | 60 | 0.002252 | 0.000038 | 0.00 | 0.00 |

## Eval trip-sql (a cell passes with at least 80% of its runs and of its cases passed, and 60% of the cases of every group)

| cell | runs passed | cases passed | groups under 60% | gate |
|---|---:|---:|---|---|
| no-model/templates | 33 of 60 (need 48) | 11 of 20 (need 16) | zone-compare 1 of 5 | fails |
| qwen3.6-35b-a3b/seed | 24 of 60 (need 48) | 8 of 20 (need 16) | time-window 1 of 5, zone-compare 0 of 5 | fails |
| qwen3.6-35b-a3b/after | 57 of 60 (need 48) | 19 of 20 (need 16) | none | passes |
| qwen3.8-27b/seed | 48 of 60 (need 48) | 16 of 20 (need 16) | time-window 2 of 5 | fails |
| qwen3.8-27b/after | 57 of 60 (need 48) | 19 of 20 (need 16) | none | passes |

## Comparisons (same cases, saved results only, no model call)

### qwen3.6-35b-a3b/seed against qwen3.6-35b-a3b/after

B is `qwen3.6-35b-a3b/after`, A is `qwen3.6-35b-a3b/seed`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | qwen3.6-35b-a3b/seed passed | qwen3.6-35b-a3b/after passed | qwen3.6-35b-a3b/after only | qwen3.6-35b-a3b/seed only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 4 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| time-window | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| zone-compare | 0 of 5 | 5 of 5 | 5 | 0 | +1.00 | 0.0625 | too few cases |
| fare-sum | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| all | 8 of 20 | 19 of 20 | 11 | 0 | +0.55 | 0.0010 |  |

All cases, difference in the share of runs passed (B minus A): +0.55, descriptive 95% interval +0.40 to +0.70 (bootstrap stratified by group, 10,000 resamples, seed 0).

### qwen3.6-35b-a3b/after against qwen3.8-27b/after

B is `qwen3.8-27b/after`, A is `qwen3.6-35b-a3b/after`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | qwen3.6-35b-a3b/after passed | qwen3.8-27b/after passed | qwen3.8-27b/after only | qwen3.6-35b-a3b/after only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 4 of 5 | 4 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| time-window | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| zone-compare | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| fare-sum | 5 of 5 | 5 of 5 | 0 | 0 | +0.00 | 1.0000 | too few cases |
| all | 19 of 20 | 19 of 20 | 0 | 0 | +0.00 | 1.0000 | too few cases |

All cases, difference in the share of runs passed (B minus A): +0.00, descriptive 95% interval +0.00 to +0.00 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.6-35b-a3b/seed

B is `qwen3.6-35b-a3b/seed`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.6-35b-a3b/seed passed | qwen3.6-35b-a3b/seed only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 3 of 5 | 1 | 1 | +0.00 | 1.0000 | too few cases |
| time-window | 4 of 5 | 1 of 5 | 0 | 3 | -0.60 | 0.2500 | too few cases |
| zone-compare | 1 of 5 | 0 of 5 | 0 | 1 | -0.20 | 1.0000 | too few cases |
| fare-sum | 3 of 5 | 4 of 5 | 2 | 1 | +0.20 | 1.0000 | too few cases |
| all | 11 of 20 | 8 of 20 | 3 | 6 | -0.15 | 0.5078 |  |

All cases, difference in the share of runs passed (B minus A): -0.15, descriptive 95% interval -0.40 to +0.10 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.6-35b-a3b/after

B is `qwen3.6-35b-a3b/after`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.6-35b-a3b/after passed | qwen3.6-35b-a3b/after only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 4 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| time-window | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 19 of 20 | 8 | 0 | +0.40 | 0.0078 |  |

All cases, difference in the share of runs passed (B minus A): +0.40, descriptive 95% interval +0.20 to +0.60 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.8-27b/seed

B is `qwen3.8-27b/seed`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.8-27b/seed passed | qwen3.8-27b/seed only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 4 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| time-window | 4 of 5 | 2 of 5 | 1 | 3 | -0.40 | 0.6250 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 16 of 20 | 8 | 3 | +0.25 | 0.2266 |  |

All cases, difference in the share of runs passed (B minus A): +0.25, descriptive 95% interval +0.00 to +0.50 (bootstrap stratified by group, 10,000 resamples, seed 0).

### no-model/templates against qwen3.8-27b/after

B is `qwen3.8-27b/after`, A is `no-model/templates`; the difference is the share of cases passed, B minus A; p is the exact McNemar p.

| group | no-model/templates passed | qwen3.8-27b/after passed | qwen3.8-27b/after only | no-model/templates only | difference | p | mark |
|---|---:|---:|---:|---:|---:|---:|---|
| one-zone | 3 of 5 | 4 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| time-window | 4 of 5 | 5 of 5 | 1 | 0 | +0.20 | 1.0000 | too few cases |
| zone-compare | 1 of 5 | 5 of 5 | 4 | 0 | +0.80 | 0.1250 | too few cases |
| fare-sum | 3 of 5 | 5 of 5 | 2 | 0 | +0.40 | 0.5000 | too few cases |
| all | 11 of 20 | 19 of 20 | 8 | 0 | +0.40 | 0.0078 |  |

All cases, difference in the share of runs passed (B minus A): +0.40, descriptive 95% interval +0.20 to +0.60 (bootstrap stratified by group, 10,000 resamples, seed 0).
