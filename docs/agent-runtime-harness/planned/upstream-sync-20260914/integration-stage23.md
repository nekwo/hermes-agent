# Stage 23 — full CI result and bounded final repairs

Candidate and atomic pushed recovery: `c112a9347a4e14ce573cdbf3d154177a6f521843`.
Tool hint/inventory repair: `1d85b5e41a`; historical audit repair: `c112a9347a`.

## Full run and exact remaining issues

CI 35028303037 on `9caca1a1a257244c6e23542b6e034e96016dac32` completed, not hung: 4,785 files, **59,007 passed, 3 failed, 540 skipped**, 2,838.8 seconds, four workers. Separately, the tombstone audit timed out at 300 seconds twice and had no counted result. Raw log: `python-ci-9caca-complete.log`. All other CI jobs passed (Desktop E2E skipped), including 212 Windows tests with one skip, mutation claims, attribution, history, supply-chain checks, docs, JS/TS and Nix.

The three failures were stale generated inventory descriptions and missing process-tool instruction details. Both are repaired: the brief retains partial-output timeout behavior and the submit/Enter versus raw-write/no-newline distinction while staying below the existing aggregate description budget. Inventory regeneration changed six descriptions only; no tool membership or admission changed.

## Audit performance and coverage

The historical coverage audit performed a Git subprocess per changed test and triggered lazy historical-blob fetching in CI's partial clone. It now prefetches using numstat with rename detection disabled, reads the old tree's object IDs, and reads those blobs in one cat-file batch. Three Git calls replace thousands. The old side of renamed tests is checked as a deletion; new-only tests need no old source. No tombstone row was dropped and no timeout was increased.

A fresh blob:none clone demonstrated one bulk fetch of 5,881 missing objects in 7.88 seconds (`tombstone-bulk-prefetch-trace.jsonl`). A real temporary-Git regression covers modification, deletion, rename, addition, spaces/Unicode, unchanged history and missing base. Object IDs preserve compatibility with older Git without cat-file's newer NUL-input option. Its files explicitly use LF on Windows.

Focused proof through `scripts/run_tests.sh`:

- Linux Python 3.11: **1,177 passed**, four affected files, 96.4 seconds (`linux-final-ci-repair-v2.log`). The final LF-only fixture adjustment is byte-identical on Linux.
- Windows Python 3.12.14: inventory 10, process semantics 3 and brief-description 7 passed (`final-ci-repair-focused-v2.log`). Final complete tombstone audit: **1,157 passed**, 113.6 seconds (`final-tombstone-windows.log`). The preceding new-fixture CRLF mismatch was corrected; it was not ignored.
- Ruff on both changed Python files passed. No production deadlines, audit scopes or source-policy exceptions were loosened.

## Delivery decision

Use the completed broad run plus focused proof of every remaining failure and the previously uncompleted audit for delivery. Do not wait another roughly 48 minutes for an automatic full rerun of these narrow repairs. Do not claim exact-final-head full CI is green: fresh CI may still be pending. The operator's already-approved Hermes-only stop/FF/dependency refresh/start is next; Launcher remains untouched. Main has not moved at this pre-maintenance checkpoint.
