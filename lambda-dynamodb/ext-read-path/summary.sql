-- The read-path summary: one query over results.duckdb (README, Summary). Parameters: $1 Lambda requests per million
-- (USD), $2 Lambda arm64 GB-second, $3 DynamoDB on-demand read units per million: the rows of ../prices.toml.
-- One row per variant, read, writer state and class (warm, cold), and one with both classes ("all"), which is the
-- row the SLO is read from. Planned calls only: after-idle and fresh-copy calls are listed one by one below it.
-- A percentile is given only from enough calls: p95 from 20, p99 from 100. These are reporting cutoffs, not
-- accuracy guarantees: quantile_cont interpolates between ordered calls at any count. Calls that did not answer 200 are left out of the percentiles and counted in `failed`.
WITH ok AS (
  SELECT variant, read, writer, CASE WHEN class = 'unknown' THEN 'no REPORT line' ELSE class END AS class,
         caller_ms, duration_ms, billed_ms, memory_mb, read_units
  FROM calls WHERE kind = 'planned' AND status = 200
), failed AS (
  SELECT variant, read, writer, count(*) AS failed
  FROM calls WHERE kind = 'planned' AND status IS DISTINCT FROM 200 GROUP BY variant, read, writer
), grouped AS (
  SELECT variant, read, writer, CASE WHEN GROUPING(class) = 1 THEN 'all' ELSE class END AS class,
         count(*) AS calls,
         quantile_cont(caller_ms, 0.5) AS p50_ms,
         CASE WHEN count(*) >= 20 THEN quantile_cont(caller_ms, 0.95) END AS p95_ms,
         CASE WHEN count(*) >= 100 THEN quantile_cont(caller_ms, 0.99) END AS p99_ms,
         quantile_cont(duration_ms, 0.5) AS p50_lambda_ms,
         CASE WHEN count(duration_ms) >= 20 THEN quantile_cont(duration_ms, 0.95) END AS p95_lambda_ms,
         CASE WHEN count(duration_ms) >= 100 THEN quantile_cont(duration_ms, 0.99) END AS p99_lambda_ms,
         median(read_units) AS median_read_units,
         median(billed_ms) AS median_billed_ms,
         median(memory_mb) AS median_memory_mb
  FROM ok
  GROUP BY GROUPING SETS ((variant, read, writer, class), (variant, read, writer))
)
SELECT coalesce(g.variant, f.variant) AS variant, coalesce(g.read, f.read) AS read,
       coalesce(g.writer, f.writer) AS writer, coalesce(g.class, 'all') AS class, coalesce(g.calls, 0) AS calls,
       CASE WHEN coalesce(g.class, 'all') = 'all' THEN coalesce(f.failed, 0) ELSE 0 END AS failed,
       p50_ms, p95_ms, p99_ms, p50_lambda_ms, p95_lambda_ms, p99_lambda_ms, median_read_units,
       1000 * ($1 / 1e6 + median_billed_ms / 1000.0 * median_memory_mb / 1024.0 * $2
               + coalesce(median_read_units, 0) * $3 / 1e6) AS cost_1000_usd
FROM grouped g FULL JOIN failed f
  ON g.variant = f.variant AND g.read = f.read AND g.writer = f.writer AND g.class = 'all'
WHERE g.variant IS NOT NULL OR f.variant IS NOT NULL
ORDER BY variant, read, writer, CASE coalesce(g.class, 'all') WHEN 'all' THEN 0 WHEN 'warm' THEN 1 WHEN 'cold' THEN 2 ELSE 3 END
