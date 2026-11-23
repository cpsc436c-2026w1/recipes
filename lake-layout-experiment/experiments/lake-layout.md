# Experiment: `lake-layout`

## 1. Question

For the analyst's zone counts, which table layout (Hive folders or Iceberg, without or with day partitions) and which
engine (Athena or DuckDB in a Lambda function) answers in the least time and bills the least, on a table large enough
that the smallest query scans more than Athena's 10 MB minimum?

## 2. Design alternatives

| Alternative | What it is | Why it might win |
|---|---|---|
| `hive-flat` | Parquet files under one prefix, no partitions | fewest files; a whole-table query lists one folder |
| `hive-by-day` | Parquet files in one folder per pickup day (`dt=2024-03-14`) | a one-day query reads one folder (partition pruning, L11) |
| `iceberg-flat` | Iceberg table, unpartitioned | file statistics in the metadata let Athena skip files |
| `iceberg-by-day` | Iceberg table partitioned by `day(pickup_time)` | pruning without folder listing; the table format most project lakes serve |
| DuckDB in Lambda | the same queries on `hive-by-day`, in `<PREFIX>-duckdb-query` | no 10 MB minimum; billed by GB-second |

## 3. Overall evaluation criterion and guardrails

- **Overall evaluation criterion (OEC):** cost of 1,000 queries per query and size, in USD, lower is better.
- **Guardrail metrics:** median time to the last row under 5 s for the one-day query; the same rows, keyed by zone,
  in every layout and engine; the run's Athena spend under `MAX_USD` (2.00 USD).
- **Requirements it informs:** R80, R120 (README).

## 4. Experiment setup

- **Same for both alternatives:** DS01 and its copies, the same three queries (one day, one week, whole table), the
  workgroup `<PREFIX>-wg` with result reuse off, one query at a time.
- **Test cases:** three sizes from the growth rule (1 copy, the copies at which the one-day query passes 10 MB, and
  4 times that), 3 queries, 4 layouts, 10 runs each: 360 Athena runs and 90 DuckDB calls.

## 5. Prediction

The one-day query bills the 10 MB minimum in the `by-day` layouts at every size below about 21 copies (0.49 MB a day
a copy, *assumed*; the first build measures it), so layouts differ in the bill only for the flat layouts and the whole-table query; the
`by-day` layouts read about 1/31 of the flat ones for the one-day query (partition pruning, L11). DuckDB in Lambda
is cheaper than Athena for each one-day query (about 0.04 against 0.05 USD per 1,000, *computed* from the price sheet
at 850 ms billed) and slower on a cold call (L2).

Measured after the build (2026-11-23), the prediction left as written: the probe is 0.16 MB a day a copy, not
0.49, so the one-day query would pass 10 MB at about 64 copies; `MAX_USD` stopped the growth at 8.

## 6. Method

- **Command:** `uv run python experiment.py build && uv run python experiment.py run && uv run python experiment.py summary`
- **Repeats:** 10 runs per cell, in one order shuffled with seed 436
- **Evidence:** `evidence/<run-id>/` for each run (command, commit SHA, `results.duckdb`, `summary.sql`)

## 7. Result

Run 2026-11-23 in ca-central-1, the second of two runs (`output/live.txt`; every cell of `summary.sql` in
`output/summary-live.md`; the first run's flat tables did not grow with their copies and were rebuilt). Growth: 10 MB
not reached, probe 1.24 MB at 8 copies; `MAX_USD` stopped the doubling (the plan of 1, 16 and 64 copies had an upper
bound of 2.29 USD), so the sizes are 1, 8 and 32 copies. Whole-table scan of the four layouts: 4.9, 39 and 153 to 155
MB, so every table grew with its copies. 450 runs, none excluded; DuckDB's rows equal Athena's in every cell. Each
cell: median time to the last row in seconds (IQR, 10 runs), median Data scanned and billed MB (Athena) or billed
GB-seconds and S3 GETs (DuckDB), and the cost of 1,000 queries in USD (the OEC).

| Copies | Query | Athena hive-flat | Athena hive-by-day | Athena iceberg-flat | Athena iceberg-by-day | DuckDB in Lambda hive-by-day |
|---|---|---|---|---|---|---|
| 1 | one-day | 1.44 s (1.34–1.84); 30.46 MB scanned, 31 billed; 0.1626 USD | 0.80 s (0.74–0.88); 0.16 MB scanned, 10 billed; 0.0525 USD | 1.50 s (1.36–1.65); 27.13 MB scanned, 28 billed; 0.1469 USD | 0.74 s (0.74–0.82); 0.16 MB scanned, 10 billed; 0.0525 USD | 0.30 s (0.29–0.31); 0.53 GB-s, 3 GET; 0.014 USD |
| 1 | one-week | 1.39 s (1.32–1.44); 30.46 MB scanned, 31 billed; 0.1626 USD | 0.80 s (0.74–0.86); 1.06 MB scanned, 10 billed; 0.0525 USD | 1.55 s (1.44–1.63); 27.13 MB scanned, 28 billed; 0.1469 USD | 0.86 s (0.85–1.50); 1.06 MB scanned, 10 billed; 0.0525 USD | 0.41 s (0.41–0.42); 0.75 GB-s, 14 GET; 0.0219 USD |
| 1 | whole-table | 0.97 s (0.88–0.99); 4.88 MB scanned, 10 billed; 0.0525 USD | 0.97 s (0.89–1.12); 4.89 MB scanned, 10 billed; 0.0525 USD | 1.10 s (1.08–1.47); 4.87 MB scanned, 10 billed; 0.0525 USD | 0.97 s (0.95–1.11); 4.89 MB scanned, 10 billed; 0.0525 USD | 0.58 s (0.56–0.59); 1.09 GB-s, 58 GET; 0.0457 USD |
| 8 | one-day | 1.89 s (1.87–2.29); 243.7 MB scanned, 244 billed; 1.2798 USD | 0.86 s (0.85–0.89); 1.24 MB scanned, 10 billed; 0.0525 USD | 2.42 s (2.04–3.06); 217.09 MB scanned, 218 billed; 1.1435 USD | 0.96 s (0.96–0.97); 1.24 MB scanned, 10 billed; 0.0525 USD | 0.33 s (0.32–0.34); 0.60 GB-s, 3 GET; 0.015 USD |
| 8 | one-week | 2.09 s (2.00–2.20); 243.7 MB scanned, 244 billed; 1.2798 USD | 1.03 s (0.88–1.08); 8.62 MB scanned, 10 billed; 0.0525 USD | 2.48 s (2.26–2.88); 217.44 MB scanned, 218 billed; 1.1435 USD | 1.18 s (1.07–1.49); 8.64 MB scanned, 10 billed; 0.0525 USD | 0.46 s (0.45–0.46); 0.85 GB-s, 15 GET; 0.0237 USD |
| 8 | whole-table | 1.24 s (1.20–1.32); 38.95 MB scanned, 39 billed; 0.2046 USD | 1.31 s (1.15–1.48); 39.01 MB scanned, 40 billed; 0.2098 USD | 1.67 s (1.50–1.85); 38.97 MB scanned, 39 billed; 0.2046 USD | 1.32 s (1.13–1.64); 38.94 MB scanned, 39 billed; 0.2046 USD | 0.87 s (0.84–0.89); 1.69 GB-s, 62 GET; 0.0555 USD |
| 32 | one-day | 2.10 s (2.01–2.33); 946.92 MB scanned, 947 billed; 4.9672 USD | 1.16 s (1.08–1.30); 4.91 MB scanned, 10 billed; 0.0525 USD | 2.85 s (2.45–2.99); 847.37 MB scanned, 848 billed; 4.4479 USD | 1.37 s (1.21–1.45); 4.94 MB scanned, 10 billed; 0.0525 USD | 0.43 s (0.41–0.45); 0.81 GB-s, 5 GET; 0.0187 USD |
| 32 | one-week | 2.47 s (2.27–2.51); 946.92 MB scanned, 947 billed; 4.9672 USD | 1.21 s (1.16–1.54); 34.5 MB scanned, 35 billed; 0.1836 USD | 2.88 s (2.53–3.26); 849.74 MB scanned, 850 billed; 4.4584 USD | 1.61 s (1.44–2.21); 34.5 MB scanned, 35 billed; 0.1836 USD | 0.83 s (0.80–0.86); 1.60 GB-s, 29 GET; 0.0398 USD |
| 32 | whole-table | 1.54 s (1.35–1.64); 155.02 MB scanned, 156 billed; 0.8183 USD | 1.65 s (1.35–2.44); 153.1 MB scanned, 154 billed; 0.8078 USD | 1.76 s (1.64–2.13); 154.64 MB scanned, 155 billed; 0.813 USD | 1.79 s (1.65–2.33); 152.91 MB scanned, 153 billed; 0.8025 USD | 2.12 s (2.07–2.19); 4.19 GB-s, 124 GET; 0.1161 USD |

## 8. What you conclude

- The one-day query billed Athena's 10 MB minimum in both by-day layouts at every size (0.16 to 4.9 MB scanned), as
  predicted; the flat layouts billed 28 to 947 MB for it, so by-day is about 3 to 95 times cheaper for the one-day
  query. The one-week query at 32 copies: 35 MB by day against 850 to 947 MB flat.
- Every layout met the 5 s guardrail for the one-day query (by-day 0.74 to 1.37 s, flat 1.44 to 2.85 s).
- The whole-table query read the same in all four layouts (4.9, 39 and 153 to 155 MB): partitions neither help nor
  cost it anything here.
- Iceberg against Hive: the same bytes for the by-day layouts, about 10% less for the flat ones (27 against 30, 217
  against 244, 848 against 947 MB), and slower in 15 of the 18 pairs of cells, by up to 0.75 s.
- DuckDB in Lambda on the Hive by-day table had the lowest cost of 1,000 queries in every cell: 0.014 to 0.040 USD for
  the one-day and one-week queries against 0.0525 to 0.18 for Athena by-day, and 0.116 against 0.80 for the whole
  table at 32 copies. It was faster warm for the one-day and one-week queries (0.30 to 0.83 s) and slower for the
  whole table at 32 copies (2.12 s against 1.54 to 1.79). The one cold call (whole table, 32 copies) took 3.20 s
  against a warm median of 2.12 s (init 338 ms).
- Not shown: sizes past 32 copies, where the one-day probe would pass 10 MB (about 64 copies at 0.16 MB a day a copy),
  and copies past 31, which repeat (`layout.rotation_days`); a larger `MAX_USD` or a by-day-only plan would reach them.
