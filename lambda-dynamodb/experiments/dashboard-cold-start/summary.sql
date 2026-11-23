-- per cell: cold Init Duration, Duration, Init + Duration, caller wall time, billed, warm duration and billed
CREATE OR REPLACE TABLE runs AS SELECT * FROM read_json_auto(getvariable('path'));
SELECT memory_mb, client,
       count(*) FILTER (kind = 'cold') AS n_cold,
       round(median(init_ms) FILTER (kind = 'cold')) AS init_med, round(max(init_ms) FILTER (kind = 'cold')) AS init_max,
       round(median(duration_ms) FILTER (kind = 'cold')) AS dur_med, round(max(duration_ms) FILTER (kind = 'cold')) AS dur_max,
       round(median(init_ms + duration_ms) FILTER (kind = 'cold')) AS sum_med, round(max(init_ms + duration_ms) FILTER (kind = 'cold')) AS sum_max,
       round(median(wall_ms) FILTER (kind = 'cold')) AS wall_med, round(max(wall_ms) FILTER (kind = 'cold')) AS wall_max,
       round(median(billed_ms) FILTER (kind = 'cold')) AS billed_cold_med,
       round(median(duration_ms) FILTER (kind = 'warm'), 1) AS warm_dur_med,
       round(median(billed_ms) FILTER (kind = 'warm')) AS warm_billed_med,
       round(median(wall_ms) FILTER (kind = 'warm')) AS warm_wall_med,
       round(median(import_boto3_ms) FILTER (kind = 'cold')) AS import_med,
       round(median(make_client_ms) FILTER (kind = 'cold')) AS client_med,
       max(max_memory_mb) AS max_mem
FROM runs GROUP BY ALL ORDER BY memory_mb, client;
