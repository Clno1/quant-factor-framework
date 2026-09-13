# Controlled Coverage Recovery: 2026-09-12

Terminal at 23:36 SGT: all 5295 securities revalidated, 5291 passed, four remain
blocked (TEAD, XMAX, BGMS, STEX). Thirty-one original failures were repaired.
Reviewed repair policy is deployed; formal coverage/PIT publication is blocked,
not completed. Cup v3 is still 0/5, delivery=false, historical observations intact.

## Current Work

User authorized completing per-security authentication, controlled publication,
PIT/candidate validation, and independent minute-gap investigation. This work
continues the audit-only pilot; no shadow acceptance gate may be relaxed.

The production whole-security canonical replacement path is retained. It now
supports bounded parent reads (25 securities), one or two provider workers,
preparation-only scope slices, exact expected-scope SHA checking, and cache-only
publication. Partial scope cannot publish. Cache-only recovery refuses new reads
from all provider entry points, including identity delta, bulk, canonical refresh,
replacement history and nominal month-end prices; missing evidence blocks it.

## Frozen Binding

- Target: 2026-09-11; parent coverage: `76e68448ccea48f5b5e1dbf871c9f6c9`.
- Current master: `5c738854ad504f1c863c47cf15bb4a63`.
- Failed scope SHA-256:
  `0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a`.
- Scope: 5,295 continuing securities. The prior 4,844/450/1 classification is a
  triage, not proof of a successful publication. Full-history replacement is
  authenticated per security and replaces the complete canonical history; it
  does not splice revised prices into an unverified old prefix.

## Preparation

Pilot: 50/50 validated, zero errors, no publication. Runtime 208.489 seconds,
CPU 102.033 seconds, peak 701.9 MiB, zero swap.

Pilot report:
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260912T114207Z_b0506f0b/full_history_repair.json`

Pilot report SHA-256:
`227bb699260d0707914e25e82b5e3deae3f8c6dac3950fd95727b5792999ee94`.

Full preparation unit: `quant-coverage-repair-all-20260912.service`.
It is preparation-only, with no OnSuccess publication or notification action.
Limits: MemoryHigh 700M, MemoryMax 900M, TasksMax 64, RuntimeMaxSec 14400,
Nice 10; external production flock `data/lake/.broad-production.lock`.

Full preparation checkpoint (mutable while running):
`data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260912T114630Z_0da7917a/full_history_repair.json`.

At 19:49:59 SGT the checkpoint was RUNNING, 90/5295 validated, no errors,
including 50 revalidated cache hits. Later final evidence must supersede this
progress snapshot without describing RUNNING or PREPARED as a publication PASS.

At 20:09 SGT, 600/5295 had completed: 599 validated, one failure (TEAD).
This checkpoint does not authorize publication.

### TEAD Source Failure

The frozen approved aliases are OB through 2025-06-09 and TEAD from 2025-06-10.
The full replacement loses 88 authenticated dates in 2025-02-03..2025-06-09.
Independent requests to each of FMP full, dividend-adjusted and non-split-adjusted
return zero rows for OB in that window and only 18 rows for TEAD (2025-05-14..06-09).
Even a reviewed query-key mapping would leave 70 dates without new-source proof.
No mapping was added, no old prefix was spliced in, and no dates were removed.

The [SEC name-change filing](https://www.sec.gov/Archives/edgar/data/1454938/000095015725000478/form8-k.htm)
and [subsequent quarterly filing](https://www.sec.gov/Archives/edgar/data/1454938/000145493825000132/outbrain-20250630.htm)
support the June 10 symbol change and February 3 acquisition, respectively. They
establish identity/lifecycle, not the missing OHLCV values. All six raw responses
and the 18-row canonical response are preserved in `tead-alias-evidence/`.
The underlying provider history must be restored or separately sourced and
authenticated before this security, and therefore the full publication, can pass.

### SNYR Prior-Quarantine Review

The second failure is INVALID_OHLC_BOUNDS on 2020-03-11, 2020-04-08 and
2021-03-17. At 20:19 SGT, the existing `inherit_quarantine` validator confirmed
that every OHLCV/adj_close/quality field exactly matches the immutable reviewed
source ledger (`ad5de5cfd10d47e2ae21364f1808248d`, quarantine SHA
`081f7e715620f7e71a52102a96451d25843b91113b65fe2ed2a6f24b7b719255`).
All three dates are already absent from valid parent coverage. Therefore zero
previously valid dates would be removed by inheriting these existing exclusions.
Certificate: `snyr_quarantine_certificate.json`, VERIFIED_PRIOR_QUARANTINE_ONLY,
publishable=false. This is not yet a complete SNYR replacement certification.
Production rules remain unchanged while the full failure inventory is collected.
Review these exact existing rows with any later failures before changing policy;
do not add genuinely new quarantine or waive the aggregate quality gates.

### Later Cumulative Ledger and Alias Evidence

The initial QVCG probe found no row in the August 14 ledger and correctly did
not approve a new exclusion. Historical operations documentation then identified
the August 21 cumulative ledger. At 20:35 SGT its version
`5ed0bc1f4b104e4f8b85256f15efba45`, manifest SHA
`6b791bfc95f8199d4909c114e4aa3cde570f71b980965646dc135cc2826a33ad`
and quarantine SHA
`c5048e83f49dc14c12c2e657a8a54ab4adf22cbd638fe5d3f074e0c5a35d5d21`
were verified. SNYR's three rows and QVCG's 2026-08-09 NON_XNYS_SESSION row
exactly match it and remove zero valid parent dates. The original six selected
rows are also identical across both ledgers. `later_quarantine_certificate.json`
supersedes the earlier absence finding, not the original historical failure.
A future reviewed policy can bind the cumulative ledger and these exact keys;
the full process is still running under the original policy.

At 20:38 SGT: 1380 checked, 1375 validated, five failures. New failures:
INEO/SAG lacks 2025-04-25; XMAX/NVFY lacks 2025-11-06 and 07. Real canonical
responses under INEO and XWIN respectively provide those dates. These are
isolated evidence, not an approved publication or an alteration of historical
tickers. The [INEO SEC filing](https://www.sec.gov/Archives/edgar/data/1933951/000164117225008113/form6-k.htm)
explicitly establishes announcement April 25 and symbol change April 28.
The [Nova/XMax name-change filing](https://www.sec.gov/Archives/edgar/data/1473334/000149315225020756/form8-k.htm)
establishes issuer continuity, but does not independently certify the frozen
master's November 10 trading-symbol boundary. Preserve that distinction in any
query mapping review; never assert that this filing supplies the exact date.

TEAD's legacy FMP endpoint was also checked once using the existing subscription.
It returned 403 (legacy eligibility), preserved in `legacy_endpoint_probe.json`.
No retry, entitlement bypass, alternate account or production fallback was used.

At 20:54 SGT the first review candidate policy (not production) passed the
existing ledger-hash and formal-alias contract loader. It has ten exact prior
exclusion keys and only the additional INEO mapping. Its SHA is
`71a6e5aca5131710c35cabd0e359e128dc75a176c3469ff8c7f95c076ef8c5f2`;
keep this validated candidate immutable and create a new revision for later keys.

At 20:58 SGT the failure inventory covered 1890 completed securities. It strictly
verified original failure contracts/raw hashes and each parent history. Besides
SNYR/QVCG, ZDAI (historical PGHL, 2024-11-26), BSEM (2021-08-31) and YALA
(2023-02-08) each exactly match one existing cumulative-ledger exclusion and
remove zero valid parent dates. `failure-inventory-2058.json` records these proofs.
`audit_failed_histories.py` is a fixed-scope read-only reproducer; it cannot
change policy, publish a version or amend any shadow day.

BCIC's old PTMN query omits 29 sessions, 2025-07-15..2025-08-22. The real BCIC
canonical query supplies all 29 (`BCIC_alias_boundary.parquet`, SHA
`b4ee96900e7b595e47b77a2fb8c7a9da1317f774e1923d7f58b6b867a0d6288a`).
The [issuer's SEC name-change filing](https://www.sec.gov/Archives/edgar/data/1372807/000119312525185297/d937608d8k.htm)
explicitly places the trading-symbol change on August 25. A bounded provider
query mapping must retain PTMN for the preceding sessions and revalidate the
complete security, not merely insert the recovered 29 rows. No BCIC policy has
yet been activated in the ongoing original-policy preparation.

The second immutable review candidate has 13 prior-exclusion keys and adds the
bounded BCIC mapping to INEO/HSON. The existing loader and formal-master alias
checks passed; `candidate2_policy_validation.json` binds rules SHA
`ef8769ba348c5f0d2ea77bcf2221470d9410e1cd17b45d0654d0a9e8eb1b29cb`.
Neither candidate has replaced production policy or certified a full history.

At the next checkpoint (2491 checked, ten failures), GXAI's old NFTG query lacked
six dates, 2024-01-10..18. The real GXAI query supplies all six, SHA
`a275ca944c77b4b977bd07b90663c2db66612bf09ba187864e9e558064f8ed99`.
The [issuer's 2023 Form 10-K](https://www.sec.gov/Archives/edgar/data/1895618/000121390024026681/ea0202356-10k_gaxosai.htm)
explicitly dates the symbol change to January 19, 2024. A later policy revision
may therefore query only January 10..18 under GXAI, retaining NFTG as the formal
historical ticker; entire-security revalidation is still required.
`failure-inventory-2120.json` preserves the original failure and raw hashes.

XMAX's November 10 boundary has corroborating broker-calendar search text, but
the retrieved file was an HTML notice rather than a readable PDF. This does not
close the exact-date evidence gap; no XMAX mapping has been approved on that basis.

The subsequent inventory adds HYPD and IMDX, each missing the last session before
the formal ticker switch. Both real canonical boundary queries returned one row:

- EYEN/HYPD, 2025-07-02; SHA
  `39f09ad5f641c8a5042a00892c0680a212918a8a2c97eca66635a8a3518a067e`.
  The [SEC-filed issuer announcement](https://www.sec.gov/Archives/edgar/data/1682639/000110465925065294/tm2519676d1_ex99-1.htm)
  explicitly places trading under HYPD on July 3.
- OCX/IMDX, 2025-06-17; SHA
  `1726fb1e7e8b76dab18e9cae9eb264f2b845bc92c981262da207f48e4a48cb8c`.
  The [issuer's Form 8-K](https://www.sec.gov/Archives/edgar/data/1642380/000164117225015338/form8-k.htm)
  specifies the June 18 pre-open symbol change and unchanged CUSIP.

`failure-inventory-2129.json` preserves both formal alias intervals and original
failed-source hashes. These are evidence-backed query-mapping candidates, not
completed whole-security certifications or permission to publish a subset.

CEPS passed at original-scope item 2759; it was not skipped. By item 2879 there
were 14 failures. `failure-inventory-2138.json` adds NXH and TONX:

- NXH's first failure is an empty BYON response, after OSTK succeeded. Later
  BBBY/NXH aliases were not attempted by that failed fetch. Its 713 missing dates
  therefore describe downloaded input, not 713 independently confirmed source
  absences. The revised read-only auditor explicitly marks partial raw queries.
  A bounded BBBY query supplies all 454 BYON-era sessions, 2023-11-06..2025-08-28,
  SHA `02c8cce7e7067f1f3c89036afb27a57662f6b37031136951198615fa60b14a94`.
  Subsequent formal BBBY and NXH intervals independently returned 241 and 19 rows,
  SHA `8c158e59989c0a2dd9a7eb39d4dd934b1cc41e24c03640da9f71bfbce39150d9`
  and `1390f67eaae95e4d8044eada108c0558825a07914ea28ff8cfa29fbdfeae4d9c`.
  The issuer's SEC filings explicitly establish [OSTK to BYON on November 6, 2023](https://www.sec.gov/Archives/edgar/data/1130713/000113071323000074/nameandlistingchangepressr.htm),
  [BYON to BBBY on August 29, 2025](https://www.sec.gov/Archives/edgar/data/1130713/000113071325000078/byon-20250930.htm),
  and [BBBY to NXH on August 17, 2026](https://www.sec.gov/Archives/edgar/data/1130713/000114036126033194/ef20080088_8k.htm).
  This is CIK 1130713/Overstock's identity chain, not the bankrupt former issuer
  that previously used BBBY. Never map its earlier unrelated BBBY history.
- TONX's complete old VERB query lacks 215 sessions, 2024-10-21..2025-08-29.
  A real TONX canonical query supplies all 215, SHA
  `5a841506da7e96179b98104ca56ae13d38611cbcc67707fadd49e2dec6a83666`.
  The [SEC-filed issuer announcement](https://www.sec.gov/Archives/edgar/data/1566610/000164117225026325/ex99-1.htm)
  confirms trading under TONX began September 2, 2025. Any provider mapping must
  retain VERB for this entire pre-change window and validate the whole history.

These independent downloads are still isolated evidence. Production rules and
formal coverage/PIT pointers remain unchanged during the original full run.

At item 3121 the 15th failure was GDYN: the first CTAC alias returned no history,
so no raw alias was completed and the missing-date count is unknown, not zero.
The reviewed auditor now makes that distinction explicit. GDYN's independent
canonical query returned 296 rows for 2019-01-02..2020-03-05, SHA
`8f8363376d459342efa82a6bef3c583b7442d27916e73ad7c0ea753d0c027483`.
The [post-close SEC report](https://www.sec.gov/Archives/edgar/data/1743725/000121390020005742/ea119399-8k_griddynamics.htm)
and [Nasdaq corporate action notice](https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2020-44)
bind ChaSerg CTAC to GDYN effective March 6, 2020, including the issue's CUSIP
transition. A query-only mapping must preserve the historical ticker CTAC and
must not use any later issuer that reused the symbol.

At item 3308 the 16th failure was CHAI: SYTA's response lacks October 3 and 6,
2025. Both dates exist in the independent CHAI canonical response, SHA
`698fe7a50861ae711884a5bd58bb51c9926a98e5f39d66e258d2b2897e59f3eb`.
The [issuer's October 6 Form 6-K](https://www.sec.gov/Archives/edgar/data/1649009/000149315225017099/form6-k.htm)
explicitly separates merger completion October 3 from trading under CHAI and
the 1-for-4 reverse split on October 7. Preserve SYTA as the historical ticker
for October 3/6 and retain both canonical and nominal prices; no manual split
adjustment or rounding is authorized. `failure-inventory-2158.json` records the
original contracts and failures.

At item 3503, 19 failures had been collected. VRXA lacks the two VACH-era dates
June 9/10, 2026; its real VRXA canonical query supplies both, SHA
`85521a7ecf3b835bc8752e59aced823d89697642d9685c375770fec3329d8651`.
The [amended SEC registration](https://www.sec.gov/Archives/edgar/data/2079109/000182912626006302/veraxabiotech_8-a12ba.htm)
explicitly places VRXA trading on June 11. Preserve VACH on June 9/10.

IMA's invalid July 25, 2025 bar exactly matches the old quarantine ledger.
WATR's Sunday August 16, 2026 bar was absent from the August 21 ledger, but a
read-only discovery across 17 published versions found an exact existing
exclusion in the September 1 and September 4 versions. The September 4 source
was then fully authenticated, and all 13 prior candidate keys were compared
exactly and found unchanged. WATR and IMA remove zero valid parent dates.
`september_quarantine_certificate.json` records the proof at 22:03:56 SGT:

- Ledger version: `562967c01bb54e2ab39454804cc4ac73`.
- Manifest: `7388933abb12306b88ebe05bf51c2eb9fa15b29e1ac51c3af273e29af1326579`.
- Quarantine: `e5ea49cc797694e27cc5c02c21f1e54449cf917a8f1a30cde6cca6dc3b2cba4f`.

The read-only failure auditor now references this authenticated later ledger;
older inventory files are immutable. This supersedes WATR's earlier absence
finding without creating a new exclusion or changing production policy.

### Later Full-Scope Failures

At 22:42 SGT, independent canonical queries established two further recoverable
alias windows: SMRT returns 139 sessions for the approved FWAA interval
2021-02-04..2021-08-24 (first returned date February 5; no February 4 bar is
invented), and FGNX returns all 362 missing FGF sessions 2024-02-29..2025-08-08.
The [SmartRent closing filing](https://www.sec.gov/Archives/edgar/data/1837014/000119312521260632/d166902d8k.htm)
explicitly bounds FWAA through August 24 and SMRT from August 25, 2021.
The [FG Nexus SEC exhibit](https://www.sec.gov/Archives/edgar/data/1591890/000164117225023074/ex99-1.htm)
explicitly dates the FGF/FGNX switch to August 11, 2025. LUXE also supplies all
eight missing MYTE dates 2025-04-21..30; the [issuer annual filing](https://www.sec.gov/Archives/edgar/data/1831907/000110465925104454/luxe-20250630x20f.htm)
establishes the May 1 change. These proofs only support bounded query mappings;
they do not authorize partial history insertion or publication.

BGMS and STEX are different: the original CYCC and BSGM queries respectively
lose 190 and 313 authenticated dates. Independent successor-symbol queries to
all three current FMP endpoints return empty for the complete missing windows.
The [BGMS filing](https://www.sec.gov/Archives/edgar/data/1130166/000149315225013123/form8-k.htm)
and [STEX filing](https://www.sec.gov/Archives/edgar/data/1530766/000164117225027127/form8-k.htm)
both establish September 12, 2025 symbol changes, but provide no missing OHLCV.
No mapping, splice, new exclusion, or calendar reclassification can close these
source failures. The six empty raw responses are retained alongside the
successful isolated aliases in `tead-alias-evidence/tead_alias_20260912/`.

The September 4 ledger also exactly authenticates NUR (one Sunday bar), CIIT
(one row), JDZG (88 rows), MWG (one row), ZXZZT (ten rows) and OCAC (one row).
Every selected row matches the existing immutable ledger and removes zero
valid parent dates. These are inherited exclusions, not 102 newly quarantined
rows or permission to discard additional data. The reviewed generator only
selects explicit keys from authenticated original-policy failure records; it
requires a terminal 5295/5295 inventory, matching original report/scope hashes,
and then verifies the production policy loader and formal alias boundaries.

The five added whole-alias regression cases pass: historical ticker/security
identity and all parent dates remain required; invalid start/end, successor
boundary and unrelated query symbols fail closed. The deployed focused suite
now passes 135 tests in 23.29 seconds. No production rules have been changed by
building the review mapping list.

## Implementation and Tests

Deployment backup: `outputs/deploy_backups/coverage_repair_batches_20260912/`.

- Writer SHA: `c8af7bf4b192648b938cc9086c8ff4397a2f93d44b2c577f4ecf233ca7c3b42b`.
- Full preparation loaded helper SHA:
  `d4167879746fc265989a39292c9cf1d45f88d32eec3de4ef3bd75322500b3c63`.
- A synthetic missing-parent-identity counterexample failed on that helper.
  The guard was tightened to require exactly all batch identities in parent reads.
- The helper deployed for future processes at 19:51:17 SGT has SHA:
  `09c2256ba03a95ebdffd2f3f818344f2042b375977068793b68d296f58407f1b`.
  Running preparation was not restarted. Publication must use this stricter
  helper and revalidate every cache, not trust the earlier preparation status.
- Initial staged suite: 85 passed (10.26 seconds).
- Expanded suite after the guard: 130 passed in staging (22.22 seconds) and
  against actual deployed code (21.52 seconds), covering coverage, full-history
  recovery, data foundation, PIT and breakout adapters. No local project venv
  execution is claimed.

## Minute Evidence

Fresh FMP requery still finds UAN's nine and IBTA's one missing bucket absent from
both 1min and 5min responses. WBI 09:35 has 1/1 rows and 09:40 has 0/1; these are
later responses, not evidence that those bars existed during the live session.
All 12 historical events remain UNRESOLVED_SOURCE_GAP. No stock or bar is dropped
to make the 95%/5% gates pass, and no zero-volume OHLCV is fabricated.

Requery report:
`outputs/data_audits/cup_handle_gaps/2026-09-11_20c2d3e2b240478b8a02728e9ef3d417.json`.

The two relevant service env files contain an FMP credential, but none of the
checked independent-provider credential names. Only presence booleans were read,
never credential values. The user was asked for an existing independent provider
name, not a secret. No new subscription or provider ticket was purchased/sent.

A current v3 MDB replay attempt using the saved real minute file was blocked by
`[US_LIQUID_5M] PIT and Security Master generations differ`. No substitute report
or acceptance record was generated. Retry only after genuine upstream readiness.

FMP's [1min endpoint documentation](https://site.financialmodelingprep.com/developer/docs/stable/intraday-1-min)
describes OHLCV and date-range retrieval but does not provide the missing-bucket
finality/absence proof needed here. This documentation is not a no-trade certificate.

### Current v3 Signal Followthrough

A new standalone read-only audit binds the saved signal and pre-signal candidate
payloads and captures raw FMP minute responses. It requires all 30 consecutive
minutes for six five-minute bars; invalid/nonpositive OHLCV, missing or duplicate
minutes, trigger-minute barrier events and same-minute barrier ambiguity remain
UNRESOLVED. It never modifies signals, cycles, gap classifications or acceptance.
The snapshot's outer legacy label is retained; the selected cup sub-contract is
strictly v3/2026-09-01.1. Archived source artifacts are not newly certified by this
posthoc audit. The 17 boundary tests pass against deployed code.

2026-09-08 has two unique saved v3 signals:

- NKTR: 30 valid minutes; 11:29 ET touches handle low 76.2 before the +2% target
  79.3101. STOP_FIRST, one false-positive proxy. This is not execution P&L.
- VTS: the 10:48 ET row has volume 0 and flat OHLC 18.115. UNRESOLVED with
  NONPOSITIVE_OHLCV, not a valid ratio, confirmed no-trade event or successful signal.

Resolved-only proxy: 1/1 (100%); unresolved: 1/2. All-signal proxy percentage is
null, not 0%. This tiny posthoc sample is not a stable false-positive estimate.
The 2026-09-11 zero-signal day also has no defined false-positive percentage.
Evidence: `followthrough-20260908/audit.json`, SHA-256
`ff5ccbb82b4e48f9f7016ec860b90b4d75db164c20312152585eed190c1a641d`.

At 20:15 SGT authenticated operations health is OK (snapshot age 13.7 seconds).
The API target is 2026-09-14 but still shows DEGRADED/completed_session and the
2026-09-11 v3 FAIL, 0/5. Delivery is false. `operations_checkpoint.json` freezes
resources and v3-only hashes for cycles (140), evaluations (5560), observations
(2) and gaps (25); these all-time v3 totals are not five-day acceptance counts.
Both September 8 and 11 have all 70 cycle contract flags complete.
`upstream_preflight.json` preserves actual PIT and next-candidate blocked results.

## Original-Policy Terminal Result and Reviewed Deployment

The original full preparation ended at 23:08:04 SGT: all 5295 securities checked,
5260 validated, 35 failed. Exit 1 is an actual data-validation failure, not an
interruption or infrastructure success masquerading as a pass. Runtime was
3h21m36.450s, CPU 1489.744s, peak 735870976 bytes, zero swap, zero restarts.
`full-history-original-policy-final.json` SHA-256:
`7cee92f0f7051ed95c2d456926de2ef8fd65f734d3d1298be3db5bcd6f752d3a`.
`failure-inventory-final.json` SHA-256:
`c0a7317280e63de081e2c5c6debf2a145ad99d4fe5d52c1e2c6fb7ec8c8b9b3b`.
Both remain immutable. CEPS was actually certified (151/151 required dates),
not skipped; MDB's daily history also certified (1934/1934). Neither substitutes
for formal coverage publication, a valid PIT binding or the MDB v3 replay.

The final failures added WLY and PAMT. Their isolated responses provide 819
and two sessions respectively. [PAMT's SEC exhibit](https://www.sec.gov/Archives/edgar/data/798287/000168316824007944/pam_ex9901.htm)
establishes the November 12, 2024 symbol switch, separate from redomestication.
For WLY, the [issuer announcement](https://newsroom.wiley.com/press-releases/press-release-details/2022/Wiley-Announces-NYSE-Ticker-Symbol-Change-to-WLY-and-WLYB/default.aspx)
and [OCC notice](https://infomemo.theocc.com/infomemos?number=50195) establish the
April 1, 2022 JW-A/WLY boundary and unchanged Class A identity. Its linked SEC
annual filing establishes identity only, not that exact date. The policy retains
both kinds of evidence explicitly and does not change the existing loader.
This is stronger than XMAX's unreadable calendar/search snippet, which remains
unapproved. Class B WLYB must never be substituted for Class A WLY.

IVDA (2019-03-07), BEP (2025-11-08) and SKYA/historical STSS (2025-04-28)
each also exactly match one prior exclusion, with zero valid dates removed.
The reviewed candidate3 selects 120 exact prior ledger keys: original six plus
114 across 16 originally failed securities. It contains 16 query mappings,
including the original HSON mapping and 15 newly reviewed security mappings.
No new invalid rows are authorized and no original failure report is overwritten.

At 23:13:50 SGT, the policy passed the existing source/ledger/alias contract
validator and was atomically deployed under the production lock after checking
and backing up the original hash. Policy SHA-256:
`050371ca4001ad8c6e050fc5db118b055e8728cd704d8348fb8e0b92d6c1057f`.
Evidence: `full_history_repair_rules.candidate3.validation.json` and
`candidate3-deployment.json`. This deploys repair rules only; coverage and PIT
are still unpublished. The stricter parent-identity helper was used when full
revalidation started at 23:14:12 SGT under
`quant-coverage-revalidate-all-20260912.service`, with the same 5295-item scope,
two workers and resource bounds. `--reuse-frozen-repair-inputs` revalidates raw
bytes under the new contract, not previous PASS/FAIL labels; changed mappings
fetch and validate the whole security. No automatic publish hook is installed.

At 23:17 the status command and operations API still show cup v3 0/5,
delivery=false and the September 11 completed-session FAIL. All four v3 table
hashes equal the 20:15 checkpoint. PIT and next-candidate checks actually return
BLOCKED for master/coverage mismatch and stale September 10 coverage. The extra
operations/cup/followthrough regression suite passes 41 tests in 10.15 seconds
against deployed code. An initial invocation used a missing production test path
and ran zero tests; the successful invocation used the isolated copied tests.

## Revalidation Terminal Result

The second full run ended at 23:35:35 SGT, exit 1 for four explicit validation
failures, not interruption: 5295 completed, 5291 validated, 31 original failures
fixed. Runtime 21m23.206s, CPU 1056.403s, peak 736043008 bytes (701.9 MiB),
zero swap and restarts. All 5276 unchanged-query successes reused authenticated
raw bytes and underwent new-policy validation; 15 changed mappings fetched and
validated complete histories. The original and second reports are retained.

`full-history-revalidation-final.json` SHA-256:
`8f65155994b3f813c888745d01840cc4de81af88ef13cda3edc97de36b7e9042`.
`revalidation-summary.json` contains every repaired security's required-date
count, manifest hash, selected source and original-policy comparison. No fixed
security loses a required parent date. TEAD, XMAX, BGMS and STEX remain failed
under the unchanged completeness and identity gates. Full publication was not
attempted with these failures, and neither coverage nor PIT was switched.

The final actual status/PIT/candidate/MDB checks are in `closeout-final.json`,
SHA-256 `7aa1f02662dc14e764aac9d9079f05eb89f17f20414f7e8a313642c11ea17705`.
At 23:36 health is OK, snapshot age 10.2 seconds. Operations still shows the
September 11 completed-session FAIL despite its September 14 scheduling target.
All v3 table hashes are unchanged; promotion is 0/5 for September 4/8/9/10/11,
September 8/11 failed and the other three are missing. Delivery remains false.
PIT, candidate and actual MDB replay respectively fail on master-generation
mismatch, stale September 10 coverage and PIT-generation mismatch. No replay
success or zero-percent false-positive claim was manufactured.

Both recovery services have ended. Remaining work requires real provider data
and independent identity evidence, followed by the publication sequence below.
Minute gaps remain a separate blocker. `provider-remediation.md` is an unsent
draft with precise source and finality questions, not a completed support ticket.

## Completion Requirements

After every security passes, run the existing writer with `--publish`,
`--repair-full-history`, `--repair-cache-only` and the same expected scope SHA,
under the production lock and resource limits. Preserve the expected-parent CAS
and all aggregate quarantine/coverage checks. Any failure stops publication.

Then build and verify PIT against the exact new coverage version, validate the
2026-09-14 candidate against source 2026-09-11, and retry the MDB v3 replay.
Do not rebuild the master mid-recovery or let a scheduled broad factor job use
an unverified PIT. Keep the latest full-session FAIL visible in operations.

Finally update the four operations/monitoring documents with the real terminal
outcome and verified hashes. Cup v3 remains 0/5 and delivery disabled unless
real future full sessions prove otherwise; even 5/5 only permits manual review.
