# Planned — writer-less goal-lane residue on live surfaces

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md))
**Status:** not done. Two entity-surface leftovers of the removed mission lane
still project or describe state nothing can produce.
**Raised / verified:** 2026-08-22 against HEAD.

## 1. `GoalRuntimeInstance` lanes: a read lane with no writers

`GoalRuntimeInstanceStore` (`agent_runtime/runtime_instances.py:10`) lost its
entire write lane at S53 — `create_lane`, `transition`, `park_lane`,
`resume_lane`, `park_open_task`, `mark_terminal_for_task`, and the single
`save` chokepoint (`:14-31`). The read half was deliberately kept because
`status.py` projects it.

It still does, twice, under two names:
`"foreground_runtime": runtime_instances_summary(...)` and
`"runtime_instances": runtime_instances_summary(...)`
(`agent_runtime/status.py:110`, `:113`). A grep for `runtime_instance_path` /
`runtime_instances_dir` outside tests finds only the read in
`runtime_instances.py:34,37`, the path helpers, and a checkpoint `EntityClass`
registration (`agent_runtime/checkpoint.py:94`) — **nothing writes the
directory**. So both status keys can only ever report whatever the 2026-07-30
data migration left on disk, frozen, and the 22-key
`runtime_instance_summary` row (`runtime_instances.py:58-81`) carries
`daemon_lease_id`, `latest_proof_ids`, `open_incident_ids` and
`current_stage_id` — four fields naming subsystems that no longer exist.

The precedent for what to do is in the same file: S21 removed
`foreground` / `foreground_active_count` / `background_parked_count` because
they were "a foreground lane that no code could ever elect, reported in the
shape of a live reading" (`:86-88`). The remaining projection is the same
defect one layer up.

## 2. Docstrings naming deleted execution lanes

`agent_runtime/chat_lane_toolsets.py:26-30` tells the reader that "Worker / dev
task lanes never go through here — they resolve toolsets via
`effective_toolsets(persona)` directly (`persona_runtime.run_persona`,
`root_node_engine`, `node_tools`)". All three names are gone:
`agent_runtime.root_node_engine` and `agent_runtime.node_tools` are `MODULE`
tombstone rows (`tests/agent_runtime/test_tombstone_registry.py`, s5 rows), and
`persona_runtime.run_persona` does not exist — `GPTPersonaRuntime` has exactly
one public method, `mission_chat_reply` (`agent_runtime/persona_runtime.py:71`).
The same docstring also says "Role gating … runs first and upstream", which R-1
removed.

`agent_runtime/runtime_hud.py:5-6` similarly describes the launcher HUD strip as
showing "the daemon pulse (state · loop · beat · next-wake)". There is no daemon
module in `agent_runtime/`, and the declared HUD field roster
(`runtime_hud.py:151` `HUD_FIELDS`) has no daemon row.

These are prose, not behaviour — but they are the prose a future session reads
before deciding what a lane is, and they currently assert a second execution
lane exists.

## Gate to open this

1. Operator ruling on the two `status.py` keys: drop both, or keep one with an
   honest "frozen, no writers" marker. A Launcher-side reader check is required
   first — the S21 precedent removed a top-level duplicate only after
   confirming no number became unreachable.
2. The docstring corrections need no ruling, only a pass that fixes them
   *against the code* rather than by deleting the sentences — the S48/S57
   lesson recorded in the tombstone registry header is that prose fixed by
   grep is how vacuous claims get written in the first place.
