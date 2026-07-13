# Snapshot Projection Line-Count Exception

Status: accepted for Stage 45B on 2026-07-06.

`agent_runtime/snapshot.py` is 3,170 lines after Stage 45A. It remains above the
3,000-line fork-owned file bar because it is the single compatibility projection
for Mission Control's legacy full snapshot while 45C moves the Launcher to the
warm stream read model.

The current split trigger is 45C completion: once long-lived stream lifecycle,
watermark-ordered delta apply, identity-map joins, and freshness/drop UI are
landed, split the snapshot projections along these seams before adding new
operator-visible snapshot sections:

- mission state: `_mission_level_state`, `_mission_flow_timeline`, actor joins;
- proof state: `_proof_gate_state`, proof batches, verifier summaries;
- parity envelope: `_parity_envelope`, completeness/drops/warnings;
- persona chat: history and trace projections.

Until that split lands, changes in `snapshot.py` should stay narrow and covered
by `python -m pytest tests/agent_runtime -q`.
