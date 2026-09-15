# Decision requested: preserve unchanged upstream test assertions

Full Python CI completed on `a3d33e95a904c7e981627a0d044a4ac6356a98b4`: 58,707 passed, 106 failed, 539 skipped across 4,785 files (3772.7 seconds). Windows CI passed 212 tests; all ten JavaScript/TypeScript checks passed. Hermes main has not changed.

The source-text assertion gate found 72 new assertions relative to the two closed fork registers. An AST comparison against pinned upstream `110baa095bc7135a0624557a9cc35df0f98ece0f` proves **71 occur in functions unchanged from upstream**. The remaining fork assertion must be repaired and is excluded from this proposal.

The exact inventory and SHA256 of each enclosing function's normalized AST are in `proposed-upstream-source-assertions.json`. This preserves function identity across formatting changes while detecting semantic/code changes anywhere in the function.

## Recommended decision

Authorize a separate closed, pinned upstream-import register for these 71 assertions. Do not add entries to either existing closed register. The gate must reject new assertions, stale entries, and changed enclosing-function hashes. This retains imported upstream tests and avoids rewriting upstream test functions solely for the fork's style policy. The register acknowledges test debt; it does not claim that source-text checks prove runtime behavior.

Alternative: retain the current no-additions policy and rewrite the 71 imported assertions into permitted AST or behavioral tests. That adds a persistent downstream test delta and requires individual semantic verification.

Authority requiring this decision: `tests/test_no_source_grep_assertions.py` says `source_grep_debt.txt` and `source_grep_ruled_exemptions.txt` are both closed to additions. No gate or register has been changed by this proposal. Other regression repairs continue independently.
