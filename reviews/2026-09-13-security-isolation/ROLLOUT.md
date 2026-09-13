# Controlled SG rollout, 2026-09-13

## Scope and deployment

User approved controlled deployment, formal coverage publication and exact
PIT/candidate validation. Cup delivery remains false. This does not authorize
retrospective passing sessions, synthetic bars or automatic delivery promotion.

At 11:04 SGT, 26 tested runtime/configuration/test/audit files were installed on
SG using preimage compare-and-swap checks and SHA-256 verification. Every existing
preimage matched local main HEAD a14957b121c7c9cd0d31eeff0fdcff4136dabd08. New source
is a reviewed uncommitted overlay, not a claim that SG Git HEAD advanced. Local
EP edits and Cursor sector rotation files are outside the release.

Backup, source archive, file manifest and deployed test evidence are retained at
`/home/projects/quant/outputs/deployments/quant-isolation-release-20260913/`.
The coverage writer lock protected installation. The watchdog timer/service were
briefly stopped and the timer restored; operations-web restarted successfully.
Business monitor, candidate, EP and sector rotation services were not started or
restarted. No systemd unit definitions or delivery settings changed.

The first remote orchestration command failed shell quoting before execution;
the prematurely requested test run found the not-yet-deployed new test file and
ran no tests. After successful deployment, the actual production checkout passed
**202 tests in 90.37 seconds** (`deployed-tests-final.xml`). The earlier isolated
352-test verification remains separate evidence.

## Complete preparation

The bounded `quant-isolation-prepare-20260913.service` exited 0 after 1148.635
seconds. It used the broad production lock, one repair worker, 100% CPU quota,
700 MiB MemoryHigh and 900 MiB MemoryMax. Peak cgroup memory was 736112640 bytes.
No formal pointer was advanced during preparation.

Result: **PREPARED_DEGRADED**, 5295/5295 examined, 5292 validated, three structured
`AUTHENTICATED_HISTORY_MISSING` failures. TEAD has 88 missing authenticated dates,
BGMS 190, STEX 313. All three fit the approved policy: 3/8026 = 0.0373785%; complete
expected scope retained. No other failure was accepted. XMAX passed 1934/1934
dates under the current whole-scope contract, using freshly acquired complete
canonical alias responses and the reviewed query-boundary rule.

Preparation report:
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260913T030705Z_5696a304/full_history_repair.json`

Report SHA-256:
`ac81eb810817e6f0a2cf63b5c595077f85d296f3186a67fd72e28d4119973097`

Frozen affected-scope SHA-256:
`0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`

The source repair report retains FAIL for the three raw-source failures; the
writer's PREPARED_DEGRADED result explicitly distinguishes permitted isolation
from successful full-history certification of those stocks.

## Publication and downstream verification

The first cache-only publication revalidated all 5295 securities, with the same
5292 valid and three isolatable failures. Its first monthly reconstruction then
made no checkpoint progress for over four minutes. The monthly query expanded
5292 Parquet inputs at once; this was an unbounded query-planning bottleneck,
not permission to bypass source or coverage checks. It was stopped with SIGTERM
before producing any monthly partition or advancing the formal pointer. The
systemd wrapper's exit 0/result success is not publication success: the main
process was terminated. The original RUNNING checkpoint, logs and explicit
`publish-interruption.json` are retained, not rewritten as a successful run.

The writer now verifies each certified source's manifest/path/hash, loads at most
25 files per batch into a run-local DuckDB table, verifies total rows, and queries
that bounded staged dataset by month. It retains the exact reconstructed-row
comparison, stage database hash check, all publication checks and parent CAS.
No data certification, price formula or quality threshold changed.

The performance fix passed 61 isolated regression tests. A real whole-scope probe
loaded 5292 files / 7868781 rows in 41.454 seconds; its first monthly query took
0.471 seconds. All 65082 January 2019 rows matched an independent bounded read
of the certified inputs exactly, across every column. The probe published nothing
and made no provider requests. Evidence: `staging-probe/report.json`.

The two-file amendment was preimage-checked and deployed with backups under
`outputs/deployments/quant-isolation-staging-fix-20260913/`. The source writer SHA
is `eb9f1130dfa7c7de7b795159374227c9e4196ee07e9c89fee8e1147f74045c78`.
The original 26-file manifest remains immutable; the amendment records the new
writer and added regression test rather than falsely reusing the old source hash.

The resumed cache-only publication completed successfully as
`quant-isolation-publish-v2-20260913.service`, main process exit 0. Both attempts
retain separate stdout/stderr and the interrupted attempt is not counted as
success. A second resource issue was observed in the real combined process:
700 MiB MemoryHigh caused repeated reclaim despite zero OOM events. Only this
transient unit's soft limit was raised to 800 then 850 MiB after checking host
headroom; MemoryMax stayed 900 MiB and regular unit definitions were unchanged.
The first adjustment yielded partitions; the second allowed sustained rebuilding.
The root issue therefore included both unbounded file expansion and insufficient
soft-limit headroom, not just query structure. `resource-adjustment.json` records
the measured working set, available host memory and changes.

At 12:30:51 SGT, coverage published as **PUBLISHED_DEGRADED**, target September 11:

| Field | Verified value |
| --- | --- |
| Coverage version | `a5ea8408daa04e4da15735d6790154c5` |
| Manifest SHA-256 | `cf9054db44cabb4c0673fda61d41bc2c9825013966e010e693c0891203772992` |
| Master generation | `5c738854ad504f1c863c47cf15bb4a63` |
| Availability SHA-256 | `273fc3687a07bd17c38d31e65801ae8c7b331d0331de6eefb63f638bda5e045f` |
| Monthly partitions / rows | 93 / 10514109 |
| Complete expected / usable securities | 8026 / 8023 |
| Current target coverage | 5713 / 5742 = 99.494949% |
| Isolated | TEAD, BGMS, STEX; 3/8026 = 0.0373785% |
| Failed publication checks | None |
| Duplicates, invalid keys/numerics/OHLC, future/off-session rows | All zero in published bars |
| Retained bad-bar quarantine | 121 rows, separately audited; not synthetic repairs |
| New bulk provider requests / cached sessions | 0 / 16 |
| Successful publish elapsed / process peak RSS | 1860.115 seconds / 884.398 MiB |

An independent reader verified the current manifest and found zero usable bars
for the isolated IDs across the entire published history. The universe and
availability ledger still retain all 8026 expected IDs. Publication audit SHA:
`253589496b7e96efe90390f12150139e27a5e10f18473f1de3652774117e1c93`.

At 12:33:47 SGT, full PIT rebuild published **PUBLISHED_DEGRADED**:
`5c6090a2d5464a429af75147cfd3d992`, exact parent coverage above; manifest SHA
`b9f0be493e56965a66a7545d03db384cf33e26a402c545e6bde6c18c28479412`.
It contains 92 snapshots, 224786 membership rows, 5516 historical members and
2846 current members. The full-history daily bar-coverage gate passed. Runtime
112.217 seconds; process peak RSS 787.391 MiB. This transient run used 850/900 MiB
soft/hard limits; normal service definitions were not changed.

The initial extra validation program unnecessarily retained all 993362 eligibility
rows beside the candidate build and hit its soft limit. It was stopped, its code
and logs preserved, then changed to hash-authenticate the complete artifacts and
predicate-read only the isolated IDs. This is a bounded verification read, not a
change to PIT membership, candidate screening or the quality denominator.

At 12:39:56 SGT, the September 14 candidate was generated from September 11 data
and saved to the production candidate snapshot table. The final verifier ran in
60.409 seconds within the normal candidate service's 620/700 MiB limits. It
verified exact coverage/PIT/availability bindings, no isolated membership, all
276 isolated eligibility records false with `UPSTREAM_SECURITY_ISOLATED`, and no
isolated candidate. Of 2846 eligible stocks, 2844 had exact-date daily bars
(99.929726%); v3 screened 2844, qualified 1240 and selected 600.

The shared snapshot retains the existing legacy momentum envelope. Acceptance
uses only its cup sub-contract `daily-cup-5m-handle-shadow-v3` / `2026-09-01.1`;
the envelope is not a v1 cup observation or a promotion credit. Candidate artifact
SHA: `3d96fcb2e8297ac20abd43a3e18ecbca7c9f60806e405a4cd3a90cbac4c7d0a3`.
This is explicit advance preparation, not evidence that Monday's timer or any
live five-minute evaluation already ran.

## Final runtime acceptance

All four complete live cup table content hashes equal the pre-deployment hashes,
including historical versions. No failure was backfilled or reclassified. Current
v3 acceptance is **0/5**, passed dates empty, failed September 8/11, missing
September 4/9/10, over the consecutive completed XNYS window September 4/8/9/10/11.
Delivery remains false. At least five future consecutive complete passing days
are still required; 5/5 only authorizes a manual acceptance report, not sending.

September 11 remains FAIL: 600 candidates, 70/78 cycles (89.7436%), 2800 evaluations,
0 matches, 2243 rejected, 480 waiting, 77 unevaluable, 0 errors. Evaluable ticker
coverage is 53/56 (94.6429%), gap ticker ratio 3/56 (5.3571%); P95 0.588805 ms,
maximum bars 77. The 12 unique events remain UNRESOLVED_SOURCE_GAP, with zero
NO_TRADE_CONFIRMED and zero PROVIDER_GAP_CONFIRMED. The two IBTA zero breakout-volume
evaluations remain INSUFFICIENT_VOLUME_EVIDENCE, without a signal or valid ratio.
The deployed fix also rejects nonpositive constituent-minute volume evidence.

Top eight rejection reasons: HANDLE_TOO_SHALLOW 1793;
INSUFFICIENT_COMPLETED_5M_BARS 302; HANDLE_VOLUME_NOT_CONTRACTING 289;
RIM_NOT_BROKEN 156; STALE_QUOTE 92; UNRESOLVED_5M_SOURCE_GAP 77;
HANDLE_NOT_FORMED 49; NO_COMPLETED_5M_BARS 36.

Minute evidence is not repaired by daily isolation. UAN/IBTA lack enough source
evidence; WBI's native one-minute/five-minute responses disagree. Existing NKTR
STOP_FIRST and VTS UNRESOLVED/NONPOSITIVE_OHLCV followthrough evidence is retained:
one resolved failure proxy and one unresolved case, not a 0% false-positive claim.

Watchdog refresh succeeded. The operations API shows candidate preparation
SUCCESS, while intraday monitoring remains DEGRADED/completed_session with
September 11 FAIL and v3. The broad whole-pipeline job retains its older failed
run, and the formal research gate remains blocked. This manual recovery did not
run eight-factor production or declare the entire broad pipeline successful;
new coverage/PIT availability is independently published and visible.

Candidate and monitor services are inactive after exit 0, with enabled/active
timers next scheduled September 14 18:30:25 and 21:20:04 SGT. Their recorded CPU
usage is 2.588 and 381.173 seconds; monitor peak memory 461377536 bytes, candidate
peak unavailable. Watchdog last exit 0, CPU 3.269 seconds, peak 134148096 bytes,
timer enabled/active. Operations-web is running, 7 tasks, memory 42962944 bytes,
peak 55377920 bytes. All four report NRestarts=0; the web has no timer by design.

Effective 27-file source manifest SHA:
`0db79496826d563b53de614b6009488f1f7ece496d662d7e5f3e5558e54878bd`.
No commit/push was performed in this rollout. The parallel EP worker diff remained
six added lines and Cursor sector rotation files were not touched.

Final operations health check returned `ok`, live=true and snapshot freshness
SUCCESS (age 35.763 seconds, 180-second limit). Generated evidence is kept outside
Git. The 55-file inventory and artifacts are archived on SG at
`/home/projects/quant/outputs/data_audits/security_isolation_20260913/rollout/evidence.tar.gz`,
with a local copy under `evidence/rollout/`. Archive SHA-256:
`384106d1b6d1c39bcfdc2b2885beb1f859de7c11b5558ec94f41a8f9f5767994`.

## Deployed MDB verification

The deployed v3 replay used the existing authenticated historical bundle
`76e68448ccea48f5b5e1dbf871c9f6c9` and saved MDB one-minute data, not future coverage.
Result: two sessions, 110 evaluations, maximum 78 bars, P95 0.875848 ms, zero
signals, false-positive proxy rate null. This is not 0% false positives and does
not count toward promotion. Evidence: `mdb-replay.json` in the release directory.
