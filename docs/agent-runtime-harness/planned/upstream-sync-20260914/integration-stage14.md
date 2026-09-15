# Checkpoint 14: focused proof complete; delivery gates pending

Candidate parent: `f538d7c91668d761e6532455e642ca3ba5e45486`. This checkpoint is documentation only. Product/test repair tip was `00ab2400b1`; subsequent changes add fork CI time allowance and merge audit notes. All published main ancestry is retained.

## Closing the checkpoint 13 gaps

- Linux `linux-gateway-loader-final.log`: gateway process ownership **8 passed**, relay lifecycle **64 passed**, live-system guard **42 passed**. The remaining provider identity fixture expected the old unqualified name; upstream intentionally qualifies external modules by source path. Updated assertion preserves parent binding while honoring that isolation. `provider-loader-source-identity-final.log`: **4 passed** on Windows.
- Actual Linux mutation execution against original main: **21 selected, 21 killed, exit 0** (`linux-mutation-final.log`). Includes both shared-loader claims. The Linux checkout was clean after mutation restoration. The log contains expected failing test output from deliberately broken code; the gate's final result is success.
- Launcher `check_producer_contracts.py --hermes-root <candidate>` regenerated BOTH stream frames and response envelopes, and byte-compared them to the companion: **exit 0**, exact match (`launcher-producer-contract-crosscheck.log`). No tracked fixture changes remained after regeneration.
- Original review series tree and original main tree were re-read: both **5f5a34ad214d695f5c889a0798e220d175cc91ad**. Direct diff exit 0. Candidate ancestry checks for original main and pinned upstream both exit 0.
- Successful isolated llama receipt remains complete with two API calls and terminal tool events, owned runtime stopped. The first inspection timeout is retained as a qualification limitation: cold executable startup can exceed the current 10-second probe bound. Installer design should distinguish inspection timeout from actual incompatible flags; no installer or runtime config change was made here.

## CI and maintenance decisions

Full GitHub CI is still required before main delivery and is not claimed green. The standard fork runner reached only 52% in its original 30-minute window; commit `93d3eb681c` gives forks 90 minutes while preserving upstream's 30-minute limit. This changes no tests or acceptance conditions. Workflow syntax was parsed locally; GitHub must validate execution.

The `ci-reviewed` label remains a human maintainer gate from `.github/workflows/review-labels.yml`; it has not been self-applied. Review includes the imported upstream workflow/dependency changes plus the fork runner fallback, timeout allowance, and case-safe contributor reader.

Hermes primary and origin/main remain **34ad8ba33f2508ab10bb24a26f0377ddb62660cb**. Launcher primary and origin/main remain **084a169fd802b80e2a0789a398f500f010bba44b**; the Launcher updater safety fix is delivered there. Launcher contract companion is **241391dd66a55d4375fee81609c9195d8240c1b7**, pushed but not landed.

Read-only process inventory now sees the managed Alice gateway importing the editable Hermes checkout. Do not rely on historical process IDs; rediscover exact managed processes at maintenance time. No stop/start command was issued for the operator's runtime or Launcher. A Windows whole guard-file test run ended without a result and is explicitly not proof; the complete Linux guard file passed. The final isolated model probe was stopped through its own manager.

A live editable checkout can mix old imports with new source after fast-forward. The user's no-restart/runtime-change constraint therefore still blocks delivery into Hermes primary. Required decision after CI is green: authorize a brief, explicitly scoped Hermes maintenance stop/fast-forward/start, or keep the pushed candidates for later. Do not restart Launcher or touch unrelated runtime configuration. Do not force-push main, squash the integration merge, or fabricate ancestry for archived pre-fold installs.

Installer handoff: consume the verified paired fixtures, preserve the Local llama manager's nine RPC methods and exact access tiers, source-isolated provider discovery, profile-owned MCP connections, revision/idempotency receipts, active-turn lease exclusion, and exact local inference routing. Reuse upstream bootstrap primitives without treating a configured executable/model string as successful qualification. The 15-theme review index and 8-to-3 exact-tree review series remain separate from published history delivery.
