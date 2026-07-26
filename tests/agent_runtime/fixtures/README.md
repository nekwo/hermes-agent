# Vendored cross-repo fixtures

Snapshots of files that live in ANOTHER repo and that a hermes test must be able
to check itself against **without that repo existing at runtime**. Each snapshot
is hash-pinned by the test that consumes it, so refreshing one is a deliberate,
reviewable act rather than something a `cp` can do silently.

## `launcher_qa_profile_allowlists.yaml`

| | |
| --- | --- |
| Source repo | `EterniaLauncher` |
| Source path | `docs/stages/qa-reboot/launcher_qa_profile_allowlists.yaml` |
| Snapshot sha256 | `58976ba37df70e5c79fba06735252d077355d21f69c608d9faa9c5ce1a01586a` |
| Snapshot taken | 2026-07-26, launcher `a856f2b0` (`feat(voice-qa): expose voice state + semantic controls on Stage C MCP`) |
| Consumed by | `tests/agent_runtime/test_mcp_admission_r2.py` (parity of `agent_runtime.mcp_admission.READ_ONLY_INCLUDED_TOOLS` / `READ_ONLY_EXCLUDED_TOOLS` against the YAML's `reviewer` row) |

Hermes **owns** the admission policy — design open question 6. This YAML is
documentation plus a CI parity fixture; it is never read at admission time, so a
missing launcher checkout or a deploy skew can never change what an agent may
call.

### Refreshing it

1. Copy the current source file over this one.
2. Re-run `sha256sum` on it and update **both** the table above and
   `_LAUNCHER_ALLOWLIST_SHA256` in `tests/agent_runtime/test_mcp_admission_r2.py`.
3. Re-run `pytest tests/agent_runtime/test_mcp_admission_r2.py`. If the parity
   assertions now fail, the launcher changed which tools a restricted profile may
   call: decide deliberately whether hermes adopts the change, then update
   `READ_ONLY_INCLUDED_TOOLS` / `READ_ONLY_EXCLUDED_TOOLS` in
   `agent_runtime/mcp_admission.py` **with a written security note**, or record
   the divergence in
   `docs/agent-runtime-harness/mission-chat-mcp-admission.md`. Never silently
   widen the include list to make a test pass.
