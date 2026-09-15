# Local llama installer handoff after upstream integration

Status: **proposed, not fixture-frozen**. Candidate backend `567770b10dddcc5ff48019a932432f71bbaf156d` combines original `34ad8ba33f2508ab10bb24a26f0377ddb62660cb` and upstream `110baa095bc7135a0624557a9cc35df0f98ece0f`. Final delivery status belongs to the latest integration checkpoint. No installer implementation, Launcher integration, live restart or runtime configuration change.

## Updated source findings

Upstream DOES have an installer/runtime in `hermes_cli/local_runtime/`: binaries/bootstrap, release assets, hardware detection, GGUF/catalog, context policy, process supervision, recovery and desktop local-model surfaces. Earlier absence claims in this audit are withdrawn. Upstream's `local_runtime` configuration is disabled by default, pinned to b10964 with backend auto, four resident models, dynamic port and no additional detection ports. Disabled means detection-only in upstream; it does not grant the fork permission to adopt or control an external process.

The fork's `agent_runtime/local_llama/` remains the authoritative owner for `local-llama-hermes`: nine existing v1 RPC methods, root-bound manager, owned router/workers, epoch/global/config revisions, durable request fingerprints, same-route inference lease, active-turn exclusion and no fallback. Upstream's `llamacpp` aliases and supervisor are separate owners. Do not route the Launcher API into upstream bootstrap/supervisor merely because names overlap.

## Required design changes before implementation

1. Inventory reusable upstream primitives: asset selection/download/extraction, hardware facts, GGUF reading and binary qualification. Document each reuse boundary and adapt results to the producer contract. Do not introduce a second lifecycle owner or copy the entire upstream installer.
2. Preserve distinct install, validate and activate operations. Neither install nor activation starts a server or loads weights. Activation requires the fork server off and no active turns; upstream automatic context growth/multi-residency policies do not apply implicitly to the fork's pinned context and single-model ownership.
3. Status still reports `configured` from an executable string, not qualification. Add explicit installation inventory and qualification facts. Keep read-tier summaries sanitized; console tier owns paths and detailed receipts.
4. Separate `active_operation` from `requested_operation`: current receipt lookup replaces the operation field and may hide another running mutation. Historical success must not unlock the UI around current work.
5. Keep original draft revisions, epoch and request fingerprints. Add inventory revision and explicit validation precedence. Journal multi-file config/inventory/receipt publication and prove interrupted/cancelled recovery; current config and receipt saves are not one transaction.
6. Reuse catalog UUID/shard/link rules. Do not invent a second model importer. Preserve explicit replacement semantics: failure after replacement does not promise restoration of old weights.
7. Revalidate official llama.cpp release/asset APIs at implementation time; this source review did not refresh release metadata. Pin asset identities, download/extraction trust checks and qualification. No driver installation or PATH/package-manager mutation.
8. Freeze backend method/schema/error fixtures only after served-wire authorization and real isolated proof. Launcher must wait for that producer packet; mocked UI fixtures alone do not establish the contract.

## Candidate proof

All 48 fork Local llama tests pass under the candidate's pinned dependency environment through `scripts/run_tests.sh`. Existing isolated real probe completed: configure, scan, start, load at 8192, text inference, full agent/terminal tool roundtrip, same-model/same-endpoint compression with no fallback, active-turn closure, unload, reload at 4096, owned stop and shutdown. Receipt hash and exact invocation are in `candidate-test-evidence.json`.

No installer endpoints, second-host acceptance, full Launcher/Stage C pass, or installer crash journal proof exists. The installation contract remains proposed. Pin the final delivered SHA and rerun producer proof before enabling Launcher installer integration.
