# Provider Remediation Requests (Draft, Not Sent)

These requests concern real source evidence. They do not authorize synthetic
OHLCV, account/entitlement bypass, parent-prefix splicing, exclusions of valid
dates, looser acceptance thresholds, or retrospective shadow PASS records.

## Daily History

| Security | Formal historical symbol | Missing authenticated dates | Independent successor query |
| --- | --- | --- | --- |
| TEAD | OB through 2025-06-09 | 88, 2025-02-03..2025-06-09 | TEAD returns only 18, 2025-05-14..06-09; 70 remain absent |
| BGMS | CYCC through 2025-09-11 | 190, 2024-12-06..2025-09-11 | BGMS returns zero in all three endpoints |
| STEX | BSGM through 2025-09-11 | 313, 2024-06-12..2025-09-11 | STEX returns zero in all three endpoints |

Ask FMP to restore the full split-adjusted executable OHLCV, dividend-adjusted
close, and independently observed non-split-adjusted nominal close. Request the
exact query symbol mapping, date coverage, issuer identifier, corporate-action
semantics, correction timestamp and revision identifiers. The existing canonical
loader must receive all three mutually date-consistent series for the entire
security. Restored missing dates alone do not certify the untouched prefix.

BGMS and STEX September 12, 2025 symbol changes are established by SEC filings
linked in the review README. The filings cannot establish missing OHLCV values.
TEAD's legacy endpoint returned an entitlement-related 403; do not retry around
that access boundary. Use only an authorized current endpoint or a separately
licensed source with an explicit, reviewed canonical contract.

XMAX: two NVFY dates (2025-11-06 and 07) exist in the XWIN query. Obtain a readable
issuer/exchange record of the precise November 10 trading-symbol transition,
distinct from the November 3 corporate name change. Do not treat a search
snippet, current symbol table or price series as independent lifecycle proof.
Then review a bounded two-session query-key mapping while preserving NVFY as
the historical ticker. No such mapping is currently approved.

## Minute Evidence

The September 11 cup v3 gaps are 12 unique events across UAN (9), IBTA (1),
and WBI (2). Request complete consolidated minute/trade evidence for the exact
event timestamps in `minute-gap-requery.json`, including the venue coverage,
timestamp timezone and bucket convention, revision time and finality semantics.

A source must explicitly distinguish no qualifying trades from omitted data.
An empty FMP response does not prove no trade. WBI native five-minute bars differ
from the available one-minute aggregation; explain and authenticate that
difference before allowing any alternate-source execution path. Do not merge
them silently or synthesize zero-volume flat bars.

All 12 original events stay UNRESOLVED_SOURCE_GAP until adequate evidence exists.
Even confirmed posthoc data restoration does not rewrite the failed historical
acceptance day. A future source/algorithm contract change needs its own review.

No independent provider is configured in the inspected monitor/market-data
environment. The user has been asked for a provider name, not credentials.
No subscription was purchased, support ticket sent, or external data shared.
