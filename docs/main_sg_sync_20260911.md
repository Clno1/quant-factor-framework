# 2026-09-11 Main / SG Source Synchronization

## Scope

- Source baseline: `50ec3d0fa6d5f2ad1d5924e0b6ab1c77ef2a8f8b`.
- Functional changes: `b7899afb1b38e7148e499fcf000387923d815a92`.
- Clean-checkout test repair: `8af9686911b7b3bba4fbcf98bb8e0b8a72cee359`.
- SG project: `/home/projects/quant`; interpreter: `/home/projects/quant/.venv/bin/python`.
- Existing SG source edits were compared with local files and preserved. Later concurrent changes to four operations documents were also byte-identical and included in the documentation follow-up.
- Secrets, raw research databases, generated EP outputs, and server-local deployment state were not committed.

## Verification

The exact Git archive at `8af9686`, without local ignored outputs, passed 1,264 local tests plus 63 subtests. SG passed 1,248 tests; six PDF tests were skipped because `pypdf` is unavailable and ten browser event-handler tests because Node is unavailable. All skipped tests passed locally.

Deployment compatibility fixes preserve strategy rules: categorical text normalization before missing-value filling, explicit microsecond timestamp precision, the root unit's optional runlog path, and checked-in fixed audit digests replacing an ignored generated-file dependency.

The functional deployment verified 710 Git files and changed 76 server files. Backup:
`/home/projects/quant-backups/main-sync-20260911T034636Z`.

After restarting only the two Web services, authenticated `/research`, `/paper`, `/breakouts`, `/group-analytics`, `/group-analytics/daily`, and operations `/healthz` returned HTTP 200. Operations health was `ok`; unauthenticated access remained HTTP 401.

## Unexpected Timer Execution

Restoring the existing EP timer after deployment unexpectedly triggered a run at 11:46:37 SGT, outside its configured 08:20 / 09:20 / 10:20 New York schedule. The actual timer has `Persistent=false` and no drop-ins. The underlying reason for this immediate trigger has not been established. Do not describe this deployment as producing no external calls or messages.

That run completed successfully and generated one RUM AI commentary with one additional model call and one Discord message (`1547815835089764437`). Total model calls increased from 21 to 22. Reserved budget increased from 2,527,505 to 2,570,256 micro-USD, a delta of 42,751 micro-USD; this is the budget ledger's reservation, not a verified provider invoice. The cumulative limit remains 10,000,000 micro-USD.

The source collection and outbox changes were retained. No message was deleted and no budget or audit history was reset. Private configuration and credential files remained unchanged. The next timer event remained the normal 20:20 SGT event. The documentation-only follow-up must not stop/start the timer, reload units, or restart the worker.

Future deployments should not assume restarting a calendar timer is side-effect-free. Avoid unnecessary timer restarts; verify scheduled execution state and use an explicit execution guard when maintenance requires timer manipulation.

## Remaining Operational Issues

Code synchronization does not resolve the pre-existing security-master / broad-data failures or the resulting cup-handle candidate and minute-monitor failures. See the latest entries in `sg_operations_overview.md`, `cup_handle_monitoring.md`, and `us_broad_factor_research_implementation.md`. No PASS days were fabricated, screening gates relaxed, or minute-signal delivery enabled by this deployment.
