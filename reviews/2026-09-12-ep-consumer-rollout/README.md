# EP consumer production grey rollout

## Result

SG production cutover completed on 2026-09-12. This deploys independent price,
news and official-source consumers. It does **not** establish screenshot-equivalent
EP recall, financial interpretation, volume confirmation or Strong/Moderate ratings.

- Host: SG, project `/home/projects/quant`, interpreter `.venv/bin/python`.
- Active link: `/home/projects/quant/ep-event-current`.
- Active release: `/home/projects/quant/releases/ep-consumers-0329a78620e7-sessionguard`.
- Base commit: `0329a78620e771a00fd6e46d80d6881210b80b38`.
- Overlay: reviewed session guard in `scripts/run_ep_event_worker.py` and its test.
  The overlay is deployed but not committed by this task. The manifest records
  per-file hashes; it must not be described as an unchanged checkout of the commit.
- No local Mac scheduler or Codex automation was created.
- No new model calls or Discord messages occurred during this rollout.

The production checkout and local working tree contain concurrent cup-handle and
other changes. Those were left untouched; this release uses the pinned EP baseline
and the explicit two-file guard overlay, not an indiscriminate working-tree copy.

## Scheduling and capacity

| Lane | Schedule | Grey capacity |
| --- | --- | --- |
| Price | Each minute, second 00 | Up to 5 batches / roughly 500 symbols, 40-second deadline |
| News | Each minute, second 15 | Up to 32 jobs, 2 request threads, 40-second deadline |
| Official source | Each minute, second 35 | Up to 8 jobs, 45-second deadline |
| Existing analysis/delivery | Unchanged: every 10 minutes, 08:00-11:59 ET weekdays | Existing call limits, model, budget and delivery routing preserved |

Consumer timers cover 04:00-15:59 ET weekdays. The Python exchange-calendar guard
rejects holidays and times outside the session window, including early closes.
Separate lane locks prevent overlapping runs of the same consumer. Persisted work
survives bounded batches, with retry capacity and lease recovery from the prior release.

The 500-symbol capacity is **not** a claim of full-market coverage every minute.
Actual sweep latency also depends on provider latency, retries and queue pressure.
The consumer slice caps CPU at 75% of one core and memory at 640 MiB (high: 512 MiB);
this does not cap the entire application or its separate model worker. Each consumer
has a 120-second systemd timeout. SG was observed with about 2 GiB RAM, not assumed 4 GiB.

Final timer checks (Beijing/SG time):

- Price/news/source: enabled, waiting for Monday 2026-09-14 at 16:00:00/15/35.
- Existing EP review: enabled, waiting for Monday at 20:00:03, including random delay.
- Hourly momentum scan: remains disabled and inactive.
- Cup-handle and sector-rotation service definitions were not changed.

Minute data processing does not mean minute confirmed EP alerts. The analysis and
notification cadence remains unchanged, and production volume-confirmed WATCH
delivery has not been accepted.

## Verification and recovery fix

1. Verified 260 runtime source files against the previously tested SG code
   (previous EP suite: 759 passed). This was a baseline check, not a new full-suite run.
2. Tested the actual base release: 75 targeted tests passed.
3. Tested the guarded release: **83 passed**, 0 failed, 0 skipped; XML is stored here.
4. Backed up config, affected units, and existing queue/evidence/outbox databases;
   checked SQLite backups and preflighted additive queue initialization on a copy.
5. Started all three consumer units on Saturday. Each exited successfully with
   `OUTSIDE_MARKET_WINDOW`, without external requests.
6. Restoring the old review timer unexpectedly triggered a Saturday run despite
   `Persistent=false`. It collected news for about 40 seconds, but made no model
   calls and sent no messages. This exposed a missing guard at the production CLI.
7. Added the guard **before opening the store** for executing collection-enabled
   runs. Weekend, holiday, premarket-start, normal-close and early-close cases are
   tested. Read-only commands and collection-disabled archival trials retain their paths.
8. Activated the guarded release and explicitly smoke-tested the real review unit:
   `OUTSIDE_MARKET_WINDOW`, external requests 0, model requests 0, messages 0.
   Then restored the old timer and rechecked all timers and counters.

Model ledger stayed at 29 entries / 2,964,555 reserved micro-USD; this is a reservation
counter, not an assertion about actual billed cost. The cumulative cap remains $10.
Discord outbox stayed at 8 SENT. Existing secrets and delivery configuration were
preserved, including the EP-specific channel. No test message was sent.

## What remains before screenshot-equivalent acceptance

- Live discovery timing: use real discovered candidates, not a manually supplied ticker list.
- Source coverage: original-document success rate, queue age and issuer-specific failures.
- Comparable earnings feed: `financial_input_path` remains unconfigured. An extraction
  validator cannot supply missing pre-announcement consensus estimates.
- Extended-hours volume: `watch_input_path` remains unconfigured. Price discovery
  does not establish absolute-volume or same-time RVOL semantics; missing volume stays unknown.
- Integrated rating and opening confirmation: candidate alerts must not be represented
  as volume-confirmed EP or cup-handle entry signals.

The next real session must measure discovery, evidence and analysis timestamps,
pending-job ages, throughput, source match rate, failures and resource use. Compare
against the reference alerts at their actual timestamps, not later information.
Saturday deployment checks cannot provide these live-session results.

## Artifacts and rollback

- `manage.py`: base off-market deploy/preflight helper; rejects an already active rollout.
- `activate_guard.py`: one-time guarded-release activation and side-effect checks.
- `deployment.json`, `guard-deployment.json`: sanitized receipts (no credentials).
- `ep-consumer-rollout-20260912-guard-tests.xml`: actual guarded-release test result.
- SG private backup: `/home/projects/quant/data/ep/consumer-rollout-ep-consumers-0329a78620e7`.
- Original release: `/home/projects/quant/releases/main-b5fe44afc68c`.

Rollback must stop the new consumer timers and old review timer, wait for workers
to exit, restore the prior config and affected units from the private backup, then
atomically restore the release link and reload systemd. Restore only the previously
enabled schedules. Do **not** rewind the model ledger, outbox or newly recorded
observations. The original entrypoint lacks the new guard, so do not blindly restart
its timer off-market; retain a verified session guard or resume in the intended window.
