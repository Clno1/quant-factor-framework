# Cup shadow: fault containment and source evidence

## Scope and release state

Work was performed on local `main`, based on
`a14957b121c7c9cd0d31eeff0fdcff4136dabd08`. The new patch is **not committed,
pushed, or deployed to production**. SG execution used an isolated checkout at
`/tmp/quant-cup-resilience-20260913-j4aMBx/project` and the production virtualenv.
No production code, publication pointer, service configuration, delivery setting,
or historical shadow result was changed. The previous scoped production release
remains in place. Do not interpret the isolated test directory as a deployment.

No changed path overlaps `origin/cursor/sector-rotation-v3-99b0` at the time of
inspection. Existing local EP changes and the separate sector-rotation worktree
were left untouched. Shared-module behavior still received regression coverage.

## What the previous three fixes actually did

1. Of 35 failed full-history certifications, 15 required bounded, independently
   evidenced provider query aliases. Whole-security history was fetched again;
   historical tickers and security IDs were preserved. Another 16 had 114 bad
   source rows that exactly matched an already authenticated quarantine ledger.
   These were not newly waived valid rows: zero valid parent dates were removed.
   The resulting full-scope revalidation was 5291/5295, not a published dataset.
2. Delisted Security Master pagination was capped at 100 pages before reaching
   the configured historical boundary. Raising the cap to 200 allowed the real
   run to reach that boundary after 102 pages. Identity and coverage checks were
   retained; this was a real ingestion repair, not an exception to validation.
3. Cup session contract validation changed from `any(...)` to
   `bool(cycles) and all(...)`. One good cycle can no longer hide another cycle's
   incomplete contract, and an empty session cannot pass. All 70 September 11
   cycles already had complete contracts, so this did not cure that day's
   independently failing minute coverage.

## New implemented repairs

- **Immutable bundle reads:** `SecurityMasterStore.load_generation(id)` verifies
  an exact PUBLISHED generation using existing artifact and manifest checks.
  The breakout adapter requires identical coverage/PIT master ID and hash, then
  reads that bound generation instead of the globally latest master. Publishing
  a newer master no longer invalidates an intact historical bundle. Missing,
  tampered, mismatched or stale versions still fail closed; no pointer is moved.
- **Sparse bucket cache:** the rolling cache key now includes the completed
  local interval. A real three-minute sparse bucket can become completed when
  the clock crosses its boundary, even if no new source row arrives. Its source
  count stays three: no missing OHLCV is invented.
- **Volume evidence:** old code only checked aggregate averages, allowing one
  zero-volume bar inside a positive baseline. Reproduced this as a MATCH on the
  actual selected baseline. Now each selected baseline/handle/breakout bar must
  be positive and finite. Invalid raw-minute volume is retained as an explicit
  aggregate evidence count, so positive aggregation cannot hide it. Invalid
  evidence returns `INSUFFICIENT_VOLUME_EVIDENCE`, no signal and no valid ratio.
- **Replay followthrough:** reuse the existing strict raw-minute auditor. A
  continuous, positive-OHLCV horizon and unambiguous post-trigger barrier order
  are required. Missing/duplicate/zero-volume minutes and ambiguous trigger or
  same-minute barrier order remain UNRESOLVED. The all-signal false-positive
  rate is null for no signals or any unresolved signal; resolved-only statistics
  are explicitly labelled. Total evaluations and maximum sequence length are
  separate fields.
- **Gap diagnostics:** group by ticker AND session, retain response evidence,
  compare native five-minute values to the actually observed one-minute subset,
  and report missing slots, invalid values, duplicates and timestamp assumptions.
  Requeries do not rewrite a live gap or its classification.

## Actual isolated SG results

### XMAX

The [SEC filing](https://www.sec.gov/Archives/edgar/data/1473334/000149315225020756/form8-k.htm)
establishes corporate identity/name-change context, not the November 10 trading
boundary. The official broker's
[November 10 executed corporate-actions post](https://community.trading212.com/t/corporate-events-in-november-2025/88702/4)
corroborates the authenticated Master's November 10 NVFY-to-XWIN boundary.
This is broker corroboration, not a Nasdaq exchange notice.

A bounded query rule fetches November 6-7, 2025 through XWIN while retaining NVFY
historical identity. The fresh whole-security certification passed **1934/1934**
dates from 2019-01-02 through 2026-09-11, with zero missing dates or quarantined
rows. Three canonical price endpoints and a separate recent overlap were checked.
Validated artifact SHA-256:
`84cf3bdc15928d703c943c060a531bf8ca68559435dea3ffa33ee7af0aa0cb7d`.
The proof is isolated and `publishable=false`; no new whole-5295 PASS is claimed.
TEAD still lacks 70 required dates, BGMS/CYCC 190 and STEX/BSGM 313.

### MDB and candidate contracts

At 03:26 SGT September 13, the fixed adapter successfully ran the current v3 MDB
replay with formal coverage/PIT and their exact bound master. Two sessions,
110 total evaluations, **maximum sequence 78**, P95 **0.574459 ms**, zero signals,
false-positive proxy **null**. This is offline evidence, not a live passing day.

The September 14 candidate check still correctly fails: formal coverage is
September 10, but September 11 is required. All four problematic securities are
absent from the September 10 PIT membership, illustrating the over-wide impact
of the global publication dependency, not permission to delete them from scope.

### Minute evidence, September 11

Six real FMP requests obtained both intervals for UAN, IBTA and WBI. For the 12
saved gap events: UAN's nine and IBTA's one remain absent from both intervals.
For WBI, under the explicitly recorded start-labelled exchange-local assumption:

| Window | One-minute response | Native five-minute response |
| --- | --- | --- |
| 09:35-09:40 | Only 09:35; volume 120; high/close 32.77 | Volume 453; high/close 33.12 |
| 09:40-09:45 | No rows | Volume 497; open 33.09, close 33.08 |

The response disagreement is established; interval semantics and actual live
availability are not independently established. All twelve historical events
therefore remain UNRESOLVED_SOURCE_GAP, with zero NO_TRADE_CONFIRMED and zero
PROVIDER_GAP_CONFIRMED. No native-five-minute substitution or synthetic bars.

## Fault-tolerance boundary and remaining implementation

The user's concern is valid: a few isolated security defects should not stop
unrelated work. Existing batch preparation already continues collecting other
securities; live evaluation also isolates ticker errors. This patch repairs the
unnecessary latest-master coupling on **reads**, but the **publication** path
still requires complete whole-scope repair. It has not been made fault tolerant
by silently ignoring failures.

The next publication change must introduce an explicit per-security availability
contract, not a `--ignore-errors` flag:

1. Preserve the full expected security scope and quality denominator. Record
   failed security ID, reason, affected dates, last authenticated version and
   source evidence. Never label a degraded release as complete-market PASS.
2. Retain an entire last-good security version for explicitly historical uses;
   never splice old pre-adjustment prices into a new corrected series or present
   stale bars as current. Unavailable current securities cannot generate signals.
3. Make publication, PIT, candidate selection and every affected consumer verify
   the same availability/version contract before enabling degraded publication.
   Systemic identity, hash, price-semantics or scope corruption still blocks all.
4. Keep unavailable securities in the appropriate expected-scope quality
   accounting. Retain the existing 95%/5% minute gates, daily freshness and five
   real consecutive sessions. Audit a degraded release separately from acceptance.

This cross-consumer publication change is **not implemented in this patch**.
It requires broader integration than a reader fix and must account for the
parallel sector-rotation consumer. Missing historical prices cannot be created
by deployment or by waiting until Monday. No supplier response is assumed.

## Production evidence and verification

At 03:28 SGT September 13, status still reports **0/5**, no passing dates;
September 8/11 failed, September 4/9/10 missing. Delivery is false. September 11
has 600 candidates, 70/78 cycles, 2800 evaluations, 0 matches, 2243 rejections,
480 waits, 77 unevaluable and 0 errors. Evaluable coverage 53/56 (94.6429%), gap
coverage 3/56 (5.3571%), P95 0.588805 ms, maximum sequence 77. Historical counts,
contracts and all four v3 table hashes are unchanged. At least five future
consecutive complete passing sessions remain after recovery; then manual review
only, never automatic delivery activation.

Operations remains DEGRADED/completed_session with September 11 FAIL and v3,
despite target September 14. Candidate/monitor services exited successfully and
their timers are active. Watchdog completed at 03:28:30, exit 0, CPU 3.289 seconds,
peak 117039104 bytes. Web is running, current 43687936 bytes, peak 54796288 bytes;
no timer by design. No services were restarted. Historical failures remain.

Final tests: **232 related regressions + 65 shared/isolation regressions = 297
passed**, one existing Starlette/httpx deprecation warning. The extra 35 replay
checks are a repeated subset, not added to that total. Baseline failing tests and
final JUnit reports are retained. Local dependency limitations required SG's
isolated environment; this is not a test of the unmerged Cursor branch.

Generated evidence is ignored by Git in this directory's `evidence/` subdirectory.
SG durable copy:
`/home/projects/quant/outputs/data_audits/cup_resilience_20260913/isolated-verification/`.
The 29-file inventory records each artifact hash. Archive SHA-256:
`379096a2b094e20dd4d1e6b255151b26a0d14b8c4a46e88cee6046e20343852d`.
`evidence-01/report.json` holds XMAX proof; `evidence-02/report.json` and
`mdb-v3-replay.json` hold final replay results; `runtime-final.json` and
`watchdog-completed.json` hold production service/status evidence.
