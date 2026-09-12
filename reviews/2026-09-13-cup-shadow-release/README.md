# Cup Shadow Source Release: 2026-09-13

## Scope

This release commits and verifies the cup-shadow and shared-coverage repairs
already reviewed on September 12. It includes bounded whole-security repair,
reviewed alias/quarantine rules, revision and followthrough audit tools, the
all-cycles contract gate, regression tests, and the four operations documents.
Concurrent uncommitted EP changes are outside this release.

Code deployment does not publish coverage/PIT, enable delivery, amend historical
observations, or claim the unresolved source failures are fixed. Production cup
delivery must remain false and the existing v3 acceptance result remains 0/5.

## Evidence Packaging

Raw provider responses, candidate payloads, Parquet files, and generated run
reports remain in their original local/SG audit locations, outside Git. Their
repository-relative paths, sizes, and SHA-256 checksums are recorded in
`evidence-manifest.json`; this manifest contains no raw market data or credentials.
The reviewed mapping source, immutable policy candidates, reproducer scripts,
and human-readable findings are versioned. Restoring raw evidence is required
before rerunning a reproducer that reads those original reports.

## Deployment Verification

SG uses a selectively deployed working directory with older Git/deployment
markers and independent EP runtime content. This release verifies only its
explicit file scope; it must not relabel the entire checkout as a clean clone
of the new commit or overwrite another task's runtime files.

All 34 scoped files were SHA-256 verified on SG. The nine runtime/configuration
files already matched this release. Eight files required synchronization:
the ignore rules, this release's two documentation/index files, and five test
files. Existing files were backed up before replacement under the production
coverage lock. No business service was restarted and no systemd unit changed.

The initial archive contained macOS AppleDouble metadata. Exact-membership
validation rejected it before any production write. The archive was rebuilt
without macOS metadata and passed the same strict validation.

The following focused tests ran against the deployed SG modules:

```text
tests/test_broad_history_repair.py
tests/test_coverage_repair_batches.py
tests/test_broad_coverage.py
tests/test_coverage_revisions.py
tests/test_data_foundation.py
tests/test_broad_pit_universe.py
tests/test_broad_breakout_adapter.py
tests/test_operations_watchdog.py
tests/test_cup_handle.py
tests/test_cup_handle_followthrough.py
```

Result: 176 passed in 26.56 seconds; systemd test unit exited successfully.
This is focused regression coverage, not a full repository test run or approval
of the remaining production market-data failures.

At 00:51 SGT the status check remained v3/shadow, delivery=false and 0/5.
All four v3 table content hashes matched the pre-deployment snapshot. The
operations API was healthy and retained September 11 FAIL/completed_session.

Deployment backup, JUnit report, and post-deployment verification are retained
on SG under `outputs/deploy_backups/cup_source_release_20260913/`.
The commit-bound final file manifest is retained under
`outputs/deployments/cup_shadow_source/` after the Git commit is created.
