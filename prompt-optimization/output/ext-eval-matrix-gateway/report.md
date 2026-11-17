# Eval matrix

## Eval matrix (3 runs per cell, course LLM gateway)

| model | prompt | median | per run | answers passed | failures | CAD | CAD per 1,000 answers |
|---|---|---:|---|---:|---|---:|---:|
| qwen3.6-35b-a3b | seed | 83% | 83% / 83% / 83% | 15/18 | wrong value 3 | 0.000419 | 0.0221 |
| qwen3.6-35b-a3b | after | 100% | 100% / 100% / 100% | 18/18 | none | 0.000775 | 0.0408 |
| qwen3.8-27b | seed | 100% | 100% / 100% / 100% | 18/18 | none | 0.002594 | 0.1441 |
| qwen3.8-27b | after | 100% | 83% / 100% / 100% | 17/18 | SQL error 1 | 0.002475 | 0.1375 |
