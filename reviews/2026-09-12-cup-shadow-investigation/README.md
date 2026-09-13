# 2026-09-12 cup-handle shadow investigation

Status at 2026-09-12 17:42 SGT: investigation complete; data blockers remain.

Scope: daily-cup-5m-handle-shadow-v3 only. Historical failures and delivery settings
are unchanged. The 2026-09-11 session ran to completion but failed the evaluable
ticker coverage and minute-gap ticker ratio gates (53/56 and 3/56).

Confirmed findings:

- FMP requery at 2026-09-12 16:47 SGT still has no 1min or 5min rows for nine UAN
  buckets and one IBTA bucket. WBI's two gaps now have native 5min rows, but only
  one has a 1min row; price and volume differ between intervals.
- Today's upstream publication failed because the delisted-history page budget
  ended at 100 pages, short of the required 2019-01-01 history boundary.
- A bounded probe found older records on page index 100. The page cap was raised
  to 200; the history boundary and all publication quality gates are unchanged.
- SG backup: outputs/deploy_backups/cup_shadow_20260912_pagination/default.yaml.before.
- Recovery ended FAILED at 17:30:56 SGT. Security Master passed and published;
  coverage failed overlap authentication for 5295 securities, so PIT did not run.
  The production lock and 900 MiB memory cap were preserved. Runtime was 34m28s,
  peak cgroup memory 701.9 MiB, swap zero; this was not an OOM or timeout.
- Frozen comparisons matched 79262 of 79274 parent rows. Of the 5295 affected
  securities, 4680 changed only on the parent target date in matched rows; 615
  also changed on earlier dates. The 12 unmatched rows are not certified equal.
  BRBS, UNCY and NKE requery samples agree with the new bulk values, but do not
  prove the remaining securities or source finality.
- Formal coverage/PIT remain at 2026-09-10. An isolated candidate check for
  2026-09-14/source=2026-09-11 returned BLOCKED (stale coverage), without changing
  candidate or cup table row counts. No failed trading day was backfilled.
- A separate gate defect was reproduced: any complete cycle could hide an
  incomplete cycle. The cup gate now requires a nonempty set of entirely complete
  contracts. Only the cup method changed; legacy momentum and past observations
  were not rewritten. Deployed source and backup hashes are recorded separately.
- Relevant tests passed both in isolation and against the deployed SG module:
  62 passed (post-deployment: 5.79s), including six nonpositive-volume scenarios
  and four complete/incomplete contract combinations. No project venv was
  available locally, so this is SG test evidence, not local execution.
- Final status remains v3 0/5 with delivery disabled. The operations API retains
  the 2026-09-11 failure even when its target advances to 2026-09-14.

## Evidence

- `runtime-evidence.json`: complete-session counts, outcomes and per-cycle contract
  checks before the repair. Only v3 observations are used for acceptance.
- `provider-requery.json`, `requery-summary.json`, `interval-comparison.json`:
  query-time minute evidence, distinct from historical live classifications.
- `provider-questions.md`: unsent provider investigation draft.
- `upstream-recovery.json`: final pipeline result, authenticated/failed scope,
  official versions and isolated next-session candidate failure.
- `daily-source-samples.json`, `daily-revision-scope.json`: source revision samples
  and frozen comparisons. These diagnostics do not authorize publication.
- `gate-deployment.json`: exact deployed contract-gate change and backup.
- `closeout-status.json`: final CLI gate, service/timer/resource/health evidence.
- `recovery-runtime.conf`: the one-hour temporary recovery limit. Its SG /run
  drop-in was removed after completion; no recurring unit was modified.

## Remaining Work

Separate localized daily revisions from earlier-history or identity discrepancies
and design an explicitly audited revision path without weakening tolerances or
overwriting immutable parent versions. Revalidate real source data before any new
coverage/PIT publication; do not blindly launch 5295 full-history repairs.
Resolve minute trade/absence/finality evidence independently. Confirmed absence
does not automatically remove a historical gap from the current v3 daily gate.
Five future complete, consecutive, qualified XNYS sessions are still required;
even after 5/5, delivery requires manual acceptance.
