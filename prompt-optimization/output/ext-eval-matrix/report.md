# Eval matrix

## Eval matrix (3 runs per cell, offline stub)

| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |
|---|---|---:|---|---:|---|---:|---:|
| qwen3.6-35b-a3b (stub) | seed | 17% | 17% / 17% / 17% | 3/18 | SQL error 6, wrong unit 6, wrong value 3 | 0.000365 | 0.0203 |
| qwen3.6-35b-a3b (stub) | after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000431 | 0.0239 |
| qwen3.8-27b (stub) | seed | 50% | 50% / 50% / 50% | 9/18 | wrong unit 6, wrong value 3 | 0.000721 | 0.0400 |
| qwen3.8-27b (stub) | after | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000862 | 0.0479 |
