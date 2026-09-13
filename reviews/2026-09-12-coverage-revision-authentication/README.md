# Exact Source Revision Authentication: 2026-09-12

## Outcome

Audit-only tooling is implemented and deployed to SG. It does not publish data,
change the production scale-authentication threshold, or promote shadow days.

- Frozen failed scope: 5,295 securities, 16 XNYS source sessions, 421 authenticated
  canonical-window caches. Historical identity mapping is the production mapping.
- Classification: 4,844 terminal-date candidates; 450 earlier revisions; one
  blocked date-coverage change (CEPS). This supersedes the earlier diagnostic
  current-ticker bulk join for authentication, without rewriting that evidence.
- BRBS, UNCY and NKE passed two bounded complete-history source requeries.
  The deployed-code run checked 1,932 / 1,297 / 1,932 retained prefix rows exactly.
  All required dates and aliases were preserved; frozen corrections and new rows
  agreed with canonical data; no quarantine or source disagreement was accepted.
- Only these three securities are VERIFIED_LOCAL_REVISION. All certificates are
  `publishable: false`; 4,841 terminal-date candidates remain unverified.
- Coverage and PIT remain published at 2026-09-10. No candidate snapshot or
  production cup history was added, deleted, or re-finalized.
- v3 remains 0/5, delivery disabled. The 2026-09-11 session still fails evaluable
  stock coverage (53/56) and gap stock ratio (3/56). Its 12 unique gaps remain
  unresolved; daily source authentication is independent of minute evidence.

## Evidence

- `audit-summary.json`: extracted deployed-run metadata, exact classifications,
  complete three-sample proofs, CEPS failure, original report path/hash and journal.
  The complete 5,295-plan report and raw canonical artifacts remain on SG.
- `deployment.json`: verified implementation hashes and pre-deployment backup.
- `shadow-state.json`: official CLI, four read-only cup tables, service/timer/journal
  evidence and resource snapshot at 18:11 SGT. Its unauthenticated HTTP 401s are
  expected, not service failures; see the authenticated snapshot below.
- `operations-snapshot.json`: authenticated health and job snapshot at 18:13 SGT,
  preserving the completed 2026-09-11 FAIL despite a 2026-09-14 scheduling target.
- `pit-signals-and-resources.json`: unchanged PIT binding and v3 VTS/NKTR SHADOW
  signals, with no saved posthoc proxy in their payloads; available memory after
  the audit was 1,060,552 KiB and free disk 29,378,887,680 bytes.
- `mdb-replay.json`: the actual custom-path v1 MDB replay, 110 bars, zero signals,
  proxy null. The empty default replay directory does not erase this old report.
- `test-results.json`: staged and deployed SG regression results.
- `docs-sync.json`: all four updated documents synced to SG at 18:20 SGT after
  checking previous hashes and backing up the previous documents.

Deployed final report SHA-256:
`343ac33a37b5c1cf7f937c912604f60e35975644c3ac5b0a59fe26a29dcb4615`.

SG service ran 18:10:13-18:13:06 SGT, exit 0, wall 173.099 seconds,
CPU 157.209 seconds, peak 571.5 MiB, zero swap. Limits: MemoryHigh 650M,
MemoryMax 750M, TasksMax 64, RuntimeMaxSec 900, Nice 10. AUDITED/exit 0 means
the audit finished, not that the production upstream passed or published.

## Verification

71 tests passed against staged modules (10.92 seconds) and actual deployed
modules (9.90 seconds). No usable local project venv was available, so these are
SG tests, not locally executed tests. The existing production publisher behavior
remains the default. New cases cover exact fingerprints, hidden prefix changes,
missing/extra dates, aliases, canonical disagreement, invalid nominal prices,
cache corruption/read-only behavior, and forbidden/unbounded CLI options.

SG command for the deployed-module regression suite:

```bash
PYTHONPATH=/home/projects/quant OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python -m pytest -q \
  /tmp/quant-coverage-revisions-20260912/test_coverage_revisions.py \
  tests/test_broad_history_repair.py tests/test_broad_coverage.py
```

No production services were restarted. The audit code has no publication option.
Changes in the concurrent EP work were neither reverted nor deployed by this task.

## Remaining Work

Complete resumable, resource-bounded per-security evidence gathering without
extrapolating the three pilot passes. Investigate earlier revisions and CEPS date
coverage separately through the existing full-history source and identity rules.
Review an immutable, expected-parent-checked publication path before it is used;
then rebuild/verify PIT and the next candidate snapshot.

Independently resolve minute-feed gap/finality/interval semantics with real source
evidence. Do not fabricate bars, drop affected stocks, reclassify historical FAILs
as passes, borrow old algorithm observations, or turn zero signals into a 0%
false-positive claim. Five future complete consecutive qualifying v3 sessions
are still required after upstream readiness; even 5/5 permits manual review only.
