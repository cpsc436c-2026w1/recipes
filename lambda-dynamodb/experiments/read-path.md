# Experiment: `read-path`

## 1. Question

For the rider's trip screen, which read (an eventually consistent `get`, a strongly consistent `get`, or the rider's
history `Query`) keeps the answer under 1 s at p99, and how much later is the first answer after a quiet time or on a
fresh copy of the function?

## 2. Design alternatives

| Alternative | What it is | Why it might win |
|---|---|---|
| `get-eventual` | `GetItem` by trip id, eventually consistent | half the read units of a strong read (L12) |
| `get-strong` | the same, strongly consistent | shows a trip just written; same time expected |
| `rider` | `Query` of a rider's history | one call for the whole screen; read units grow with the history |

## 3. Overall evaluation criterion and guardrails

- **Overall evaluation criterion (OEC):** p99 of the caller's time over warm and cold calls, in ms, lower is better.
- **Guardrail metrics:** at most 2 failed calls in 200 for each variant; cost of 1,000 calls under 0.001 USD warm;
  each after-idle and fresh-copy call under 10 s (a limit a team set).
- **Job story it informs:** the rider screen; R220, R290 of ext-read-path.

## 4. Experiment setup

- **Same for both alternatives:** the function `<PREFIX>-api` at 128 MB, the tables of SIM01, one laptop terminal,
  one call at a time.
- **Test cases:** SIM02's 600 calls (200 a variant), SIM03's 200 busy-rider calls with and without the writer, 20
  after-idle calls and 20 fresh-copy calls.

## 5. Prediction

The three variants have the same p50, about 100 ms at the caller (the network and the URL, not DynamoDB, decide it),
and a p99 under 1 s; the busy rider's Query reads 3.0 units against 0.5 and is slower by tens of milliseconds; cold
calls add about half a second of init (493 ms in the captured run, L2), so after-idle calls past about 30 minutes
are slower than the warm p99.

## 6. Method

- **Command**, in this order (ext-read-path/README.md, Run it live): `bash ext-read-path/run.sh --busy-rider --writer 5`;
  `bash ext-read-path/run.sh --idle` until its plan is done (20 calls); `bash ext-read-path/run.sh --no-plan --fresh 20`
  (the fresh-copy round last, so its configuration changes do not make the after-idle calls cold);
  `bash ext-read-path/run.sh --summary`
- **Repeats:** 200 calls a variant; 20 after-idle calls in five rounds; 20 fresh-copy calls
- **Evidence:** `evidence/<run-id>/` for each run (command, commit SHA, `results.duckdb`, `summary.sql`)

## 7. Result

One run's output, for example, from a machine in ca-central-1 (not a laptop); the full summary is in
`../output/ext-read-path.txt`. p50 at the caller 63.3, 63.3 and 63.6 ms (`get-eventual`, `get-strong`,
`rider`); p99 82.7, 83.3 and 84.2 ms; busy rider p50 73.6 ms, p99 332.5 ms with the writer off and 222.6 ms with it
on; 0 failed calls. Read units: 0.5, 1.0, 0.5, and 3.0 for the busy rider (3.5 with the writer on). The writer made
300 updates with 0 throttled. The 20 fresh copies were all cold: init 269 to 497 ms, 624 to 1,042 ms at the caller.
Two after-idle calls ran (5 and 15 minutes), both cold; the first followed `--fresh`'s last configuration change.
The other 18 idle calls were not run.

## 8. What you conclude

The prediction holds for the shape and not for the size: the three reads are the same at the caller and well under 1 s
at p99, but the p50 was about 63 ms, not 100 (the caller sat in the same Region). The busy rider's Query is about 10
ms slower at the median and four times slower at p99. A cold call costs about 700 ms at the caller and one fresh copy
went over 1 s, so a team whose users come after quiet times should measure its own idle round before it promises 1 s
for the first answer.
