# Scheduled Live Minute Witness

## Current State: Armed, Not Yet Observed

Deployed and checked on September 13, 2026. No September 14 live requests or live
result exist yet. The production v3 shadow remains 0/5, delivery disabled, with
September 11 FAIL retained in operations. This is a separate evidence collector,
not a new signal source or a promotion observation. All 27 previously deployed
runtime-source hashes and the four shadow-table hashes were unchanged at setup.
Cursor rotation and parallel EP changes were not modified. No commit/push.

## Frozen Schedule

All user-facing times below are Beijing/Singapore (UTC+8). Calendar generation
used the project's existing XNYS session resolver, not weekday arithmetic.

| Time | Action |
| --- | --- |
| September 14 21:29:45 | Start independent live capture, 15 seconds before open |
| September 14 21:30 through September 15 04:00 | Observe regular-session one-minute and native five-minute responses |
| September 15 04:00:15, 04:01:15, 04:05, 04:15, 05:00 | Final-boundary and post-close checks; last request round starts 05:00 |
| September 15 05:15 | Codex follow-up for capture and post-close evidence |
| September 15 20:00 | One further same-session query before the next XNYS open |
| September 15 20:15 | Codex follow-up for next-day revisions and remaining source-contract requirements |

During the session, polls surround five-minute boundaries at -15, +15, +75,
and +180 seconds. Four fixed symbols: UAN, IBTA, WBI, SPY. Each round queries both
intervals sequentially, alternating interval order by round. There are 318 live
and post-close rounds plus one next-day round: 2,552 planned requests total.
The hard request cap is 2,600, with at least one second between requests and no
automatic retries. Requests are not atomic across interfaces; each timestamp and
pair receipt skew is recorded. Missed slots are skipped, never burst-caught-up.

SG units:
- `quant-cup-minute-witness@live.service`
- `quant-cup-minute-witness@recheck.service`
- `quant-cup-minute-witness-live.timer`
- `quant-cup-minute-witness-recheck.timer`

Both timers were verified enabled/active, with the exact next activations above.
Their calendar expressions contain specific dates, not recurring weekdays;
Persistent=false prevents historical catch-up after downtime. Restart=no means
failures remain visible instead of silently restarting an incomplete experiment.
The app uses one heartbeat named `茶杯柄同期取证阶段验收`, id `automation`, with two
scheduled occurrences at 05:15 and 20:15 on September 15. An attempted second
heartbeat was rejected by the app's one-per-task constraint; the existing one
was updated to cover both checkpoints, with no workaround cron task.

## Isolation and Failure Handling

The service uses ProtectSystem=strict and only grants writes to
`/home/projects/quant/outputs/data_audits/cup_minute_live/2026-09-14`.
Production OHLCV, SQLite, configuration and code paths remain read-only inside
the service. It has no signal, outbox or delivery integration. The delivery guard
reloads the actual production settings before each round and aborts if the cup
delivery flag is not false.

CPUQuota=20%, MemoryHigh=192 MiB, MemoryMax=256 MiB, TasksMax=8, Nice=15, idle I/O,
and RuntimeMaxSec=8h. Skip a round if host available memory is below 300 MiB or
disk free space below 1 GiB. Raw-byte accounting has a conservative 256 MiB cap;
individual decompressed responses are limited to 2 MiB. HTTP 401/402/403/429,
three consecutive failed requests, or a detected clock discontinuity trip the
circuit breaker. NTP synchronization is checked at startup and periodically.
No provider key or credential-bearing URL/error text is written to evidence.

All failures and resource/missed-slot skips are explicit. Each phase has a
separate status file; the status index preserves live failure even when the
next-day recheck succeeds. A process interruption is recorded as FAILED when
the process can handle the signal; systemd/journal remains authoritative for
unhandleable termination such as OOM/SIGKILL. A still-RUNNING file with a dead
service must not be interpreted as success.

## Evidence and Interpretation

Frozen plan:
`/home/projects/quant/outputs/deployments/cup-live-witness-20260914/plan.json`.
The plan includes source SHA-256, XNYS open/close, all sampling slots and next-day
recheck time. Changing the source after freezing invalidates startup.

Capture directory:
`/home/projects/quant/outputs/data_audits/cup_minute_live/2026-09-14/`.

- `raw/<sha256>.json.gz`: successful original HTTP bodies, losslessly compressed
  and content-deduplicated. Empty lists are preserved, not proof of no trading.
- `requests.jsonl`: request start, response headers received, body received,
  monotonic elapsed time, safe headers, status, row count and raw checksum.
- `rounds.jsonl`: actual completed, missed or resource-skipped rounds.
- `status-live.json` and `status-recheck.json`: separate phase outcomes.
- `analysis.json`: first observed and first valid observed bars; value changes
  and disappearances; pair timing skew; four minute/five-minute start/end-label
  hypotheses and completed-positive-window OHLCV comparisons.

An arrival is known only at sample resolution; first receipt is not exact vendor
publication time. HTTP Date is not an exchange freshness watermark. Negative
delay under a start-label hypothesis may indicate a still-forming bar, not a
completed bar arriving early. Unchanged values across samples do not prove final
immutability. A late-found bar has not been proved available to an earlier live
cycle. Start/end-label hypotheses remain explicitly unverified.

Complete comparisons require five unique aligned minute records with finite
positive OHLCV and a valid native bar; they exclude windows not complete before
the earlier request start. Nonpositive, nonfinite, malformed, duplicate and
off-grid evidence cannot certify a completed positive bucket. Different source
trade filters may still yield differences; this experiment does not declare
either interval the ground truth. Missing or failed samples remain uncertainty.

`feed_approved=false` and `counts_for_shadow_promotion=false` are unconditional
in this witness. Raw capture and stability alone never authorize production
replacement, 5/5 credit, gap reclassification, or delivery. A native-five-minute
source would need an explicit, reviewed data-version/semantics contract and
separate downstream validation. The original 95% evaluable/5% gap thresholds and
all other v3 acceptance requirements remain unchanged.

For interrupted collection, preserve all files and inspect the service journal.
The existing evidence can be analyzed without any HTTP requests:

```sh
cd /home/projects/quant
.venv/bin/python reviews/2026-09-13-cup-resilience/live_witness.py analyze \
  --root /home/projects/quant \
  --plan outputs/deployments/cup-live-witness-20260914/plan.json \
  --output outputs/data_audits/cup_minute_live/2026-09-14
```

This analysis does not resume capture. Do not run `live` against past sessions,
substitute the historical smoke data, or enable a recurring catch-up timer.

## Setup Verification

Fifteen SG tests passed, including boundary/early-close plans, invalid volumes,
duplicate/off-grid data, no unfinished-bar comparison, secret redaction, revision
and disappearance tracking, preserving live failure after recheck, resource
skips, and refusal to backfill past sessions as live.

An independent historical smoke ran in the same strict filesystem sandbox and
CPU/memory caps against September 11: eight of eight requests succeeded, zero
duplicate or clock-discontinuity observations, 77 invalid row observations were
retained, and feed approval remained false. Runtime 17.901 seconds; CPU 572 ms.
This is only a collector test, not evidence of September 14 real-time availability.
Smoke files live under `outputs/data_audits/cup_minute_live/smoke-20260913/`.

Production preflight verified credentials present (not disclosed), delivery
false, synchronized host clock, 318 slots and 2,552 planned requests. Existing
services were not restarted; only systemd daemon configuration was reloaded and
the two new timers enabled. The systemd verifier emitted an unrelated existing
tat_agent legacy PIDFile warning, with a successful exit; that unit was untouched.

Setup evidence archive:
`/home/projects/quant/outputs/deployments/cup-live-witness-20260914/evidence.tar.gz`.
Local archive contents: `evidence/live-witness-setup/`, 37 checked artifacts.
SHA-256: `5fc1089b994a0a63917b03d87d76e43eee39c0278ac7f038daa54454dc8b805b`.
This archive contains setup/smoke evidence only. Real capture evidence will be
created by the scheduled services, and must be independently checked afterwards.
