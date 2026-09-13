# Minute-source mechanism investigation, September 13

## Decision

The September 11 failures are present in FMP's raw HTTP records, not introduced
by our parser. Tested timestamp shifts, same-day retries, and wider date queries
do not restore those missing buckets or reconcile the interval datasets.
No trustworthy in-place recovery for the twelve live gap events was established.
The vendor's internal trade filters, venue coverage, and revision rules remain
unknown; an interval disagreement alone does not prove which series is correct.

One separate recoverable defect was demonstrated: a multi-day one-minute query
silently omitted older requested sessions. Individual-day requests recovered
September 8 records. This is a history/preload completeness issue, not a repair
for September 11's intraday holes. No production reader or request policy changed
in this investigation; any adoption needs a bounded request budget and session
coverage checks, not an unlimited retry loop.

## Experiment and Results

SG acquisition: 2026-09-13 13:37:04 through 13:40:46 SGT. Four symbols: UAN,
IBTA, WBI, and SPY as a liquid control. Twenty-four stable requests compared
September 11 alone, September 4-11, and a repeated September 11 query, each for
one-minute and five-minute intervals. All returned HTTP 200. Four additional
single-day requests checked September 8. One documented legacy one-minute request
for SPY returned HTTP 403; all further legacy attempts were stopped. Total 29
requests, no retries and no alternate-account or permission workaround.

Raw successful JSON bodies, acquisition dates, safe response headers, checksums,
and normalized Parquet are saved independently of market-data storage. The exact
deployed `get_intraday_ohlcv` parser was exercised against each captured payload
without another network call: zero dropped rows and zero duplicate timestamps.
Across all eight symbol/interval pairs, September 11 timestamps and OHLCV values
are identical for all three query variants. The six affected-symbol responses
also equal the earlier same-day audit saved around 03:02 SGT. This demonstrates
no observed correction between those snapshots, not a guarantee against future
revision. Some wide-query hashes differ only because integer volume becomes a
float column; exact numeric comparison prevents a false revision finding.

### Complete Positive Buckets

Only buckets with exactly five unique, minute-aligned, finite positive OHLCV
records and a valid native five-minute counterpart enter this comparison.
Agreement means all five OHLCV fields agree within 1e-9 relative/absolute tolerance.

| Symbol | One-minute rows | Native five-minute rows | Complete positive pairs | All OHLCV equal | Differing pairs |
| --- | ---: | ---: | ---: | ---: | ---: |
| UAN | 38 | 36 | 0 | 0 | Not evaluable |
| IBTA | 278 | 77 | 8 | 0 | 8 |
| WBI | 327 | 77 | 40 | 7 | 33 |
| SPY | 390 | 78 | 78 | 49 | 29 |

SPY has no missing minutes or nonpositive volume, yet ten of its complete buckets
have different volume and twenty-nine differ in at least one OHLCV field. WBI
has volume differences in sixteen of forty complete positive buckets. Thus
missing minute rows alone cannot explain the cross-interval disagreements.
This is not a market-wide quality estimate: four symbols were a diagnostic sample.

Thirteen relative timestamp-shift hypotheses were tested: -300, -240, -60, -5,
-4, -1, 0, 1, 4, 5, 60, 240, and 300 minutes. These cover common timezone and
minute/five-minute start/end-label offsets. Zero shift yields SPY 49/78 exact
pairs and WBI 7/40; every nonzero tested shift yields zero exact complete pairs
for both. IBTA has zero matches under every tested shift; UAN has no complete
positive pairs. No tested simple offset reconciles the datasets. This supports
the current relative alignment, not independent proof of exchange event times
or all possible interval conventions.

### Original Live Gap Events

The twelve saved v3 events were separately compared to raw-derived frames:
UAN nine and IBTA one remain absent from both intervals. WBI 09:35 has one
one-minute row (volume 120, high/close 32.77), versus native five-minute volume
453 and high/close 33.12. WBI 09:40 has no one-minute row but native volume 497.
No missing rows are invented, native bars are not spliced into the live series,
and none of the twelve events is retrospectively reclassified.

### Recoverable History Truncation

The September 4-11 one-minute responses start September 9 for all four symbols,
whereas five-minute responses extend to September 4. A separate September 8
one-minute request returns UAN 55, IBTA 311, WBI 271, and SPY 390 rows. Thus
the omitted older session is available through the same stable endpoint.
Daily slicing can recover this history at additional request cost. These samples
do not establish a universal three-session limit. The September 11 subset is
unchanged by daily slicing, so this is not the cause of its twelve live events.
An empty daily response still cannot certify no trading occurred.

## Recovery Boundaries

1. For historical preload/replay: fetch bounded single-session slices when a
   requested session is absent, retain provenance, validate timestamps and
   duplicate/revision conflicts, and report unresolved sessions explicitly.
   Do not equate an HTTP 200 response with complete requested history.
2. For the live holes: retries and query-window changes tested here provide no
   recovery. Native five-minute is not a drop-in equivalent of one-minute
   aggregation, even on complete positive windows.
3. A possible future same-provider path is a separately versioned native-five-
   minute feed, but it first needs live paired capture to establish completion
   labels, revision lag, price/volume semantics and missing/zero-volume handling.
   Its historical availability does not prove real-time availability. No such
   replacement was certified or enabled here.
4. If FMP cannot provide that contract or sufficiently complete minute/trade
   evidence, an independently authenticated source is required for unresolved
   symbols. We do not make vendor support responsiveness the recovery plan.

## Public Documentation Limits

FMP's [FAQ](https://site.financialmodelingprep.com/faqs) describes intraday data as
split-adjusted and exchange-local, and warns of per-query record limits without
an exact bound. Its [one-minute](https://site.financialmodelingprep.com/developer/docs/stable/intraday-1-min)
and [five-minute](https://site.financialmodelingprep.com/developer/docs/stable/intraday-5-min)
pages describe interval OHLCV endpoints. The reviewed pages did not establish
start/end labels, trade-condition/venue equivalence, no-trade watermarks or
revision finality. General timezone wording is not a DST/event-time certificate.
The legacy path is documented in the vendor's
[API repository](https://github.com/FinancialModelingPrep/stocks-api), but was
not accessible under the current credential. No support response was assumed.

## Verification and Evidence

Eleven isolated SG tests passed, including zero/negative/NaN/infinite volume,
missing minutes, duplicates, off-grid times, immutable shift hypotheses and
integer/float serialization. The initial test fixture used an integer column
that could not hold infinity; this fixture was corrected to float before the
final passing run. No production patch or service/timer restart was performed.
Twenty-seven deployed source hashes and the four live table hashes are unchanged.
Final status remains v3 0/5, delivery false. Operations preserves September 11
FAIL/completed_session; the prepared September 14 candidate remains SUCCESS.
Latest-day evaluable coverage 53/56 (94.6429%), gaps 3/56 (5.3571%), twelve unique
UNRESOLVED_SOURCE_GAP events; both confirmed categories remain zero. All existing
thresholds, failure dates, zero-volume rejections and followthrough records remain.

SG evidence: `/home/projects/quant/outputs/data_audits/cup_minute_mechanism/20260913/`.
Local evidence: `evidence/minute-mechanism/`. Inventory: 72 files. Archive SHA-256:
`b9f47060fda8dadbb29e49907d28c19e4986c1b77aeaacae440f6d8b11bdd2a3`.
Raw source responses are ignored by Git. This round has no commit/push and does
not touch Cursor rotation or parallel EP changes. The formal daily/PIT versions
did not change; the broad-factor implementation document needs no new upstream
publication entry for this minute-only audit.
