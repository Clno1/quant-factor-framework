# SG EP Capability Acceptance

This is an isolated capability audit, not a production release or a performance backtest.
The complete findings are in `docs/ep_sg_capability_20260911.md`.

## Evidence

- SG acceptance directory: `/home/projects/quant/tmp/ep-sg-capability-20260911-0700`.
- Local raw evidence copy: `tmp/ep-sg-capability-evidence` (ignored, not a runtime dependency).
- `replay_report.json`: compact captured-request summary, report hashes, current-code diagnostics,
  financial packet checks and cross-interval comparisons.
- Source receipts preserve original HTML/PDF and identity/version metadata. They do not certify
  point-in-time historical availability, financial semantics or model accuracy.
- 42 public-source requests and 11 FMP requests. No model calls or Discord messages.
- The audit input dates/URLs are explicitly seeded known cases, not discovered live signals.

## Reproduce Offline

With the archived evidence copied locally and project dependencies installed:

```sh
python reviews/2026-09-11-ep-sg-capability/replay.py \
  --evidence tmp/ep-sg-capability-evidence \
  --output tmp/ep-sg-capability-evidence/replayed.json
```

On SG use `.venv/bin/python` and prepend the isolated `dependencies` and checkout directories
to `PYTHONPATH`. This replays captured data only; it does not read model keys or send API requests.

`probe.py` without `--execute` only displays the maximum request count and does not write files.
Execution requires a new output directory under the isolated checkout's `tmp` directory.
Only `market` reads the existing FMP environment file. `sec`/attachment inspections use the
existing SEC contact setting for SEC requests. No credentials are copied into evidence.

Do not use these seeded historical dates to drive live alerts. Do not retry 403 endpoints by
changing identity, host or transport. The ANF JSON endpoint returned 403 with the observed page
parameters; it is not an accepted production source.

## Packaging

Include supporting `scripts`, example configs and the review Python modules imported by tests.
Exclude keys, environment files, live databases, `__pycache__`, `.pyc`, and AppleDouble `._*` files.
When using macOS tar, use `COPYFILE_DISABLE=1` and `--no-xattrs` and verify the archive before upload.
Never unpack into the production checkout or overwrite its concurrent changes for this audit.

Initial verification: local 646 passed plus 43 subtests; SG 646 passed. Production services,
timers, model budget and message channels were not changed by this acceptance.

## Source / Volume Follow-Up

See `docs/ep_source_volume_followup_20260911.md` and `docs/ep_fmp_volume_support_questions.md`.
The latter is a technical support draft, not an already-sent request.
`volume_probe.py` performs at most nine calls only with `--execute`; it never certifies volume semantics.
`followup_replay.py` summarizes the additional receipts, including failed attempts, without network calls.
`followup_report.json` is separate from the initial audit so request counts are not silently merged.
Follow-up regression: local 656 passed plus 43 subtests; SG 656 passed.
Follow-up SG requests: 30 public source requests and 18 FMP requests, zero model/delivery calls.
Seven key implementation/configuration/probe file hashes match local and SG acceptance copies.
