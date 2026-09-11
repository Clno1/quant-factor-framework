# EP SG Deployment And Channel Migration

## Deployment

- Server: `root@43.156.89.232`, Python: `/home/projects/quant/.venv/bin/python`.
- Active EP link: `/home/projects/quant/ep-event-current`.
- New target: `/home/projects/quant/releases/ep-peer-7f3579f170f989bd`.
- Previous target: `/home/projects/quant/releases/ep-stage1-9b04a5312a414b43`.
- Release archive SHA-256: `9ada5260971807a0ba0cd3df792592c6a2e03bae3855caadc0793727ac63d2f3`.
- 584 manifest entries verified. Differences are confined to EP code, tools and tests; other running services still use their original code paths.
- This is an isolated EP release, not a whole-repository synchronization or Git commit.

## Service State

| Service | Result |
| --- | --- |
| `quant-momentum-alerts.timer` | Disabled and stopped |
| `quant-momentum-alerts.service` | Stopped |
| `quant-intraday-momentum-monitor.service` | Left active; cup/handle monitor unchanged |
| `quant-premarket-digest.timer` | Left enabled and active |
| Sector rotation digest | `PREMARKET_SECTOR_ROTATION_ENABLED=true`, unchanged |
| `quant-ep-event-review.timer` | Existing SG timer restored; enabled and active |
| EP Discord delivery | Disabled pending new-channel configuration |

No local Mac scheduler was created or changed. Disabling the hourly scan does not disable the daily digest or minute cup/handle monitor.

### Channel Configuration Confirmed

After the user ran the configurator, Discord webhook metadata was checked again: the configured channel is `1547985728670015528`, it differs from the old cup/handle channel, and EP `delivery_enabled=true`. The existing SG timer remains enabled and active; the hourly momentum timer remains disabled. No synthetic test message was sent. This supersedes the pending-channel state in the deployment snapshot above. Actual report delivery still depends on an eligible report being produced.

## Acceptance

- Local: 549 EP and isolation tests passed.
- SG: the same 549 tests passed, in 48.27 seconds, under 512 MB MemoryMax and 50% CPUQuota.
- SG JUnit: `/home/projects/quant/data/ep/peer-7f3579f170f989bd-tests.xml`.
- Real queue copy: 1,083 ready article SOURCE jobs migrated to 1,066 event SOURCE jobs.
- Queue-copy acceptance: 29.249 seconds; peak RSS 149,732 KiB (about 146 MiB).
- This proves structural migration, not live candidate recall or original-document-to-notification success. The analysis preflight had no ready input (`EMPTY_INPUT`).
- Acceptance made zero model requests and sent zero Discord messages.
- Budget journal at deployment: 29 recorded calls; reserved USD 2.964555 against the existing USD 10 cumulative cap. Reserved amount is not a provider billing statement.
- Normal scheduled collection and analysis can resume after the timer is restored, under the existing budget. Delivery remains disabled until channel configuration succeeds.

### First Production Cycle

The first post-switch cycle completed successfully on 2026-09-11 at 15:25 UTC. Total elapsed time was 80.887 seconds; systemd reported peak memory 250,118,144 bytes. Notifications were disabled and model request count was zero; the budget journal was unchanged.

Collection was `PARTIAL`: 3,064 discovery candidates, 1,843 registered candidates, and 975 unmapped symbols. These are pipeline discovery counters, not proof of that many unique stocks scanned in real time. The eight source outcomes were six `NO_SUPPORTED_RECENT_FILING_IN_WINDOW` and two `NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE`. Analysis reported `EMPTY_INPUT`. Therefore a successful service exit does not establish source coverage or successful EP report generation. These source/identity gaps remain visible and need subsequent work.

## Configure The New Channel

Create/use the dedicated webhook in the Discord channel named `事件驱动型量化`. In Discord, enable Developer Mode and copy that channel's ID. It is not the webhook ID.

Run on the Mac terminal:

```sh
ssh -t root@43.156.89.232 '/home/projects/quant/.venv/bin/python /home/projects/quant/ep-event-current/scripts/configure_ep_event_discord.py --config /etc/quant/ep-event-worker.json --replace'
```

The command prompts for the webhook URL with hidden input, then the new channel ID. Do not put the webhook in chat, a command argument or a repository file.

The configurator retrieves Discord webhook metadata and requires the returned channel ID to match the supplied ID and differ from the old EP channel. It creates a new private 0600 key file and atomically updates only EP configuration while holding the worker lock. The old key is preserved for any existing cup/handle consumers. It sends no test message and does not start another timer. Successful configuration enables delivery for subsequent scheduled runs.

Old-route outbox history is retained. Pending messages addressed to an old route are held by the existing route check rather than blindly resent to the new channel.

## Remaining Gaps

- ORCL regression covers prior-session filing selection; complete source-to-notification acceptance is still outstanding.
- CPRT and RH archived-source regressions passed; this does not establish full-market live recall.
- WATCH accepts validated observations but is not connected to a production whole-market premarket scanner or automatic WATCH Discord delivery.
- Verified extended-hours volume semantics, same-time relative-volume baselines and live opening confirmation remain separate acceptance work.
- Financial comparison validation and incremental revisions are deployed; production comparable consensus/previous-guidance inputs are not configured.
- Historical `explain --as-of` is available, but real-time discovery latency and evidence-completion latency still need end-to-end measurement.

## Recovery

Backup directory: `/home/projects/quant/data/ep/peer-deploy-ep-peer-7f3579f170f989bd`.

It contains the previous private worker configuration, consistent SQLite snapshots of the queue, outbox and model ledger, and acceptance/deployment receipts. Keep these on SG; do not commit them.

Queue schema is now v2. Do not simply repoint to the old release, which expects the old queue. Stop the EP timer and wait for any active worker before preparing a rollback. If new jobs have run, a rollback needs a current snapshot and reconciliation rather than restoring the old queue blindly. Never rewind the model budget ledger or sent-message history. The deployment helper restores only the queue and old link if switching fails before any new worker starts.
