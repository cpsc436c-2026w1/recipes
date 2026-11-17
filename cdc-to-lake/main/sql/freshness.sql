-- How fresh the lake is: the newest change it holds, and how long changes took to reach the pipeline.
-- last_change is the "data last updated" value a dashboard shows next to its numbers.
-- A retried batch can put the same change into the log twice; a change is one (trip_id, seq), counted once.
SELECT
  count(DISTINCT concat(trip_id, '#', seq))             AS changes,
  count(DISTINCT trip_id)                               AS trips,
  max(change_time)                                      AS last_change,
  max(date_diff('second', change_time, arrival_time))   AS max_change_to_arrival_s
FROM {table}
