# Bounded whole-security isolation

## Implementation-stage state (before rollout)

The later authorized production rollout is tracked separately in
[ROLLOUT.md](ROLLOUT.md). The statements below describe the earlier implementation
and isolated-verification stage, not the latest deployment state.

Implemented on local `main`, including publication, persistence, explicit
restoration, PIT, breakout candidate contracts, daily orchestration and operations
reporting. Tested in the SG isolated checkout, not the production checkout.
This turn does not deploy code, advance formal publication pointers, commit or
push. Existing Cursor sector-rotation and local EP work remain untouched.

The previous review's statement that publication isolation was not implemented
is superseded by this patch. Its statement that production remains unchanged is
still true. Deployment and a complete, current-contract production preparation
remain necessary before the running system benefits from this change.

## Approved policy and implementation

- Maximum isolated count is both 0.1% of the complete selected security scope and
  ten securities. Equality is accepted without rounding up a fractional allowance.
  Configuration may tighten these limits, but this policy version rejects larger
  limits. Missing configuration disables new isolation rather than enabling it.
- Only `MissingAuthenticatedHistory`, raised after numeric, price-source, identity
  and alias validation, can create an isolation record. Generic exceptions,
  foreign identities, corrupt hashes, bad prices, incomplete downloads and
  ambiguous source contracts remain hard failures. Error text is not a whitelist.
- Freeze the complete expected ID set and its hash before isolation. The immutable
  coverage universe keeps every selected ID; the existing 98% target coverage
  denominator is unchanged. Existing bad-bar quarantine limits also remain.
- Remove the entire isolated security from new usable bar partitions, including
  its old prefix. Preserve its last authenticated version/hash as evidence, not as
  a source of current prices. No invented prices, zero-volume bars or scale splices.
- The ledger records missing dates, reason, first-isolated session, last-good
  binding and source evidence. It persists across days. Dropping an isolated ID
  from the expected scope is a hard error rather than a way to shrink the budget.
- Required benchmarks cannot be isolated, and their current bars are mandatory
  even when aggregate coverage is otherwise above 98%.
- `--restore-isolated-security SECURITY_ID` requires complete-scope full-history
  repair. The old authenticated version is verified and the isolation-period XNYS
  dates are required too. A current active security also needs a real target-day
  row. Explicit successful certification is required before removing the ledger
  entry; publication rejects unproved removal.
- `--repair-only` can return PREPARED_DEGRADED without publishing. Cache-only release
  can revalidate hash-bound complete raw responses from a failed preparation;
  it never reuses an old failure classification or performs a provider request.
- Daily orchestration invokes full-history repair when needed, treats successful
  degraded publication as a usable stage, and continues to PIT with the exact
  coverage version. It reports DEGRADED, not all-market SUCCESS.

## Consumer contracts and observability

PIT stores the identical availability contract in its manifest. Isolated IDs
cannot enter membership or positive eligibility; they must remain in the
eligibility audit, with `UPSTREAM_SECURITY_ISOLATED` in the builder output.
Active isolation or restoration invalidates incremental PIT reuse and triggers a
full rebuild, avoiding stale historical membership surviving a data removal.

Breakout candidates carry the upstream expected count and complete isolation
ledger in their data contract. A mismatched ledger or isolated PIT member is
rejected. Removing the PIT contract from broad candidates is not a bypass.
Underlying source and parent manifest hashes remain authenticated.

The operations adapter reports the isolated count, denominator and evidence as
DEGRADED while keeping actual upstream failures visible. The minute monitor's
95% evaluable / 5% gap gates, five consecutive completed-session requirement and
delivery=false are unchanged. Historical failures are not recalculated.

## Tests and real SG evidence

Regression coverage includes the exact ratio boundary, absolute limit, unknown
errors, benchmark gaps, scope/hash tampering, unusable old bars, PIT and candidate
binding, frozen-source publication, cross-day persistence, explicit complete
restoration, daily pipeline continuation and the existing cup/operations/isolation
suites. Final result: **352 passed**, with one existing Starlette/httpx deprecation
warning. The 26 tested source/config/test files match local SHA-256 hashes.

`verify_policy.py` independently re-ran the validator on SG's hash-verified frozen
canonical responses. It made **zero provider requests** and no production writes.
The exact current selected scope is **8,026 securities**. Three identified history
gaps fit the policy: **3 / 8,026 = 0.0373785%**, below 0.1% and ten securities.
The earlier 5,295 number was the repair-affected subset, not the full denominator.

| Security | Existing valid response rows revalidated | Missing authenticated parent dates |
| --- | ---: | ---: |
| TEAD / historical OB | 1,202 | 88 |
| BGMS / historical CYCC | 1,744 | 190 |
| STEX / historical BSGM | 1,621 | 313 |

TEAD's 88 is the frozen canonical whole-history certification result. Earlier
follow-up found 18 of these dates under another query, leaving 70 without that
additional evidence; this patch does not splice those 18 rows into the canonical
history or pretend the certification reported 70.

This probe establishes missing-parent-history classification and budget
eligibility only. It is not a fresh whole-scope certification, coverage publication,
PIT build or successful new daily candidate. XMAX's separate 1,934-date proof from
the previous review also remains an isolated proof until controlled preparation.
Formal coverage is still `76e68448ccea48f5b5e1dbf871c9f6c9`, target September 10.
All four live cup table hashes were unchanged and delivery remained false.

## Controlled rollout

Deploy the scoped writer/reader/PIT/candidate/operations patch together after
review, preserving the parallel sector-rotation and EP releases. Do not deploy
only the error-catching branch. Prepare the entire affected scope under the then
current parent/master/rules contract, review the isolation report and all remaining
quality checks, then publish with the frozen scope hash and expected-parent CAS.
Build PIT from that exact publication and validate the next real candidate.

If any identity, source, hash or non-isolatable failure remains, stop the release;
the code change is not a guarantee that every future batch is publishable.
Minute-source inconsistencies remain independent work, not cured by daily
isolation. Delivery stays false even after five passing days until manual review.

Generated evidence is ignored by Git under `evidence/`. Its nine-file inventory
includes the real policy probe, the test reports (including the initial failing
fixture run), and the tested-source hashes. The SG durable archive is
`/home/projects/quant/outputs/data_audits/security_isolation_20260913/evidence.tar.gz`.
Archive SHA-256:
`b094eddf902f877eaffc68c7f843ab5203d4e2525b134bde758889e6e4eb267a`.
