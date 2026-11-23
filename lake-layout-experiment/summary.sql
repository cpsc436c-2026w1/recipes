-- The summary of the experiment, from results.duckdb only (R80, R90, R110, R120). Every number in summary.md is a
-- value of the last SELECT below. Quantiles are quantile_cont: interpolated between neighbouring runs.
-- Times: to_last_row_ms is the caller's clock from sending the query to reading the last row, for both engines;
-- engine time is Athena's EngineExecutionTimeInMillis or the function's Duration.
-- Only the latest run (run_id, a UTC time) is summarised: a rerun of experiment.py run does not add to it.

CREATE OR REPLACE TEMP VIEW a_runs AS
SELECT * FROM athena_runs WHERE run_id = (SELECT max(run_id) FROM athena_runs);
CREATE OR REPLACE TEMP VIEW d_runs AS
SELECT * FROM duckdb_runs WHERE run_id = (SELECT max(run_id) FROM duckdb_runs);
CREATE OR REPLACE TEMP VIEW r_rows AS
SELECT * FROM result_rows WHERE run_id = (SELECT max(run_id) FROM result_rows);

CREATE OR REPLACE TEMP VIEW price AS
SELECT max(usd) FILTER (WHERE name = 'athena_per_tb') AS athena_per_tb,
       max(usd) FILTER (WHERE name = 'lambda_arm64_gb_s') AS gb_s,
       max(usd) FILTER (WHERE name = 'lambda_request') AS request,
       max(usd) FILTER (WHERE name = 's3_get_per_1000') AS get_1000,
       max(usd) FILTER (WHERE name = 's3_put_per_1000') AS list_1000,
       max(price_date) AS price_date
FROM prices;

CREATE OR REPLACE TEMP VIEW athena_cells AS
SELECT engine, layout, copies, query,
       count(*) FILTER (WHERE NOT excluded) AS runs_kept,
       count(*) AS runs,
       quantile_cont(to_last_row_ms, 0.25) FILTER (WHERE NOT excluded) AS q25_ms,
       quantile_cont(to_last_row_ms, 0.5) FILTER (WHERE NOT excluded) AS median_ms,
       quantile_cont(to_last_row_ms, 0.75) FILTER (WHERE NOT excluded) AS q75_ms,
       quantile_cont(to_last_row_ms, 0.9) FILTER (WHERE NOT excluded) AS p90_ms,
       quantile_cont(engine_ms, 0.25) FILTER (WHERE NOT excluded) AS engine_q25_ms,
       quantile_cont(engine_ms, 0.5) FILTER (WHERE NOT excluded) AS engine_median_ms,
       quantile_cont(engine_ms, 0.75) FILTER (WHERE NOT excluded) AS engine_q75_ms,
       quantile_cont(engine_ms, 0.9) FILTER (WHERE NOT excluded) AS engine_p90_ms,
       quantile_cont(data_scanned_bytes, 0.5) FILTER (WHERE NOT excluded) / 1048576.0 AS median_scanned_mb,
       quantile_cont(billed_mb, 0.5) FILTER (WHERE NOT excluded) AS median_billed_mb,
       sum(spend_mb) AS spend_mb,
       NULL::DOUBLE AS median_billed_gb_s,
       NULL::DOUBLE AS median_s3_gets,
       NULL::DOUBLE AS median_s3_lists
FROM a_runs GROUP BY ALL;

CREATE OR REPLACE TEMP VIEW duckdb_cells AS
SELECT engine, layout, copies, query,
       count(*) FILTER (WHERE NOT excluded) AS runs_kept,
       count(*) AS runs,
       quantile_cont(to_last_row_ms, 0.25) FILTER (WHERE NOT excluded) AS q25_ms,
       quantile_cont(to_last_row_ms, 0.5) FILTER (WHERE NOT excluded) AS median_ms,
       quantile_cont(to_last_row_ms, 0.75) FILTER (WHERE NOT excluded) AS q75_ms,
       quantile_cont(to_last_row_ms, 0.9) FILTER (WHERE NOT excluded) AS p90_ms,
       quantile_cont(duration_ms, 0.25) FILTER (WHERE NOT excluded) AS engine_q25_ms,
       quantile_cont(duration_ms, 0.5) FILTER (WHERE NOT excluded) AS engine_median_ms,
       quantile_cont(duration_ms, 0.75) FILTER (WHERE NOT excluded) AS engine_q75_ms,
       quantile_cont(duration_ms, 0.9) FILTER (WHERE NOT excluded) AS engine_p90_ms,
       NULL::DOUBLE AS median_scanned_mb,
       NULL::DOUBLE AS median_billed_mb,
       0::BIGINT AS spend_mb,
       quantile_cont(billed_gb_s, 0.5) FILTER (WHERE NOT excluded) AS median_billed_gb_s,
       quantile_cont(s3_gets, 0.5) FILTER (WHERE NOT excluded) AS median_s3_gets,
       quantile_cont(s3_lists, 0.5) FILTER (WHERE NOT excluded) AS median_s3_lists
FROM d_runs GROUP BY ALL;

CREATE OR REPLACE TEMP VIEW cells AS
SELECT * FROM athena_cells UNION ALL SELECT * FROM duckdb_cells;

-- R110: each DuckDB cell's rows against Athena's for the same table and query, keyed by zone (first run of each).
CREATE OR REPLACE TEMP VIEW first_rows AS
SELECT r.* FROM r_rows r
JOIN (SELECT engine, layout, copies, query, min(run_no) AS run_no FROM r_rows GROUP BY ALL) f
  USING (engine, layout, copies, query, run_no);

CREATE OR REPLACE TEMP VIEW differ AS
SELECT d.layout, d.copies, d.query,
       bool_or(a.trips IS DISTINCT FROM d.trips) AS results_differ
FROM (SELECT * FROM first_rows WHERE engine = 'duckdb') d
FULL OUTER JOIN (SELECT * FROM first_rows WHERE engine = 'athena' AND layout = 'hive-by-day') a
  ON a.copies = d.copies AND a.query = d.query AND a.pickup_zone_id = d.pickup_zone_id
WHERE d.layout IS NOT NULL OR EXISTS (SELECT 1 FROM first_rows x WHERE x.engine = 'duckdb' AND x.copies = a.copies
                                      AND x.query = a.query)
GROUP BY ALL;

-- R90: pairs that differ only in layout (same engine, size and query) or only in engine (same table and query).
CREATE OR REPLACE TEMP VIEW overlap_pairs AS
SELECT x.engine, x.layout, x.copies, x.query,
       string_agg(y.engine || ' ' || y.layout, '; ' ORDER BY y.engine, y.layout) AS ranges_overlap_with
FROM cells x JOIN cells y
  ON x.copies = y.copies AND x.query = y.query
 AND ((x.engine = y.engine AND x.layout <> y.layout) OR (x.layout = y.layout AND x.engine <> y.engine))
WHERE x.q25_ms <= y.q75_ms AND y.q25_ms <= x.q75_ms
GROUP BY ALL;

SELECT c.engine, c.layout, c.copies, c.query, c.runs_kept, c.runs,
       round(c.median_ms, 1) AS median_ms, round(c.q25_ms, 1) AS q25_ms, round(c.q75_ms, 1) AS q75_ms,
       round(c.p90_ms, 1) AS p90_ms,
       round(c.engine_median_ms, 1) AS engine_median_ms, round(c.engine_q25_ms, 1) AS engine_q25_ms,
       round(c.engine_q75_ms, 1) AS engine_q75_ms, round(c.engine_p90_ms, 1) AS engine_p90_ms,
       round(c.median_scanned_mb, 2) AS median_scanned_mb, c.median_billed_mb,
       c.spend_mb, round(c.spend_mb * 1048576.0 / 1099511627776.0 * p.athena_per_tb, 6) AS spend_usd,
       c.median_billed_gb_s, c.median_s3_gets, c.median_s3_lists,
       CASE WHEN c.engine = 'duckdb' AND coalesce(d.results_differ, false) THEN 'results differ' END AS mark,
       CASE
         WHEN c.engine = 'athena' THEN round(1000 * c.median_billed_mb * 1048576.0 / 1099511627776.0 * p.athena_per_tb, 4)
         WHEN coalesce(d.results_differ, false) THEN NULL
         ELSE round(1000 * (c.median_billed_gb_s * p.gb_s + p.request + coalesce(c.median_s3_gets, 0) * p.get_1000 / 1000
                          + coalesce(c.median_s3_lists, 0) * p.list_1000 / 1000), 4)
       END AS cost_per_1000_usd,
       o.ranges_overlap_with,
       p.price_date
FROM cells c CROSS JOIN price p
LEFT JOIN differ d ON c.engine = 'duckdb' AND d.layout = c.layout AND d.copies = c.copies AND d.query = c.query
LEFT JOIN overlap_pairs o
  ON o.engine = c.engine AND o.layout = c.layout AND o.copies = c.copies AND o.query = c.query
ORDER BY c.copies, c.query, c.engine, c.layout;
