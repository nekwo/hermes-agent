# Planned — mission-chat admission latency (the 4–6 s before the provider)

**Status:** measured, convicted, not remediated. **Owner doc:**
[`../05-chat-turn-lane.md`](../05-chat-turn-lane.md).
**Instrumentation that made this measurable is SHIPPED** — this plan is about what to do
with what it says.

---

## 1. The number

Live phase-joined turns, 2026-08-22, read through the launcher's chat-latency audit
against hermes turn records carrying the v3 `phases` block:

| turn | TTFT | admit | `agent_ready` | provider first byte |
| --- | --- | --- | --- | --- |
| alice | 17.8 s | 1.9 s | 2.4 s | 13.5 s |
| qa | 9.2 s | 2.0 s | 4.1 s | 3.1 s |

Warm turns are mostly provider time; the hermes share is roughly **4–6 s** — admission
~1.8–2.0 s plus agent bootstrap. On the alice lane the provider half is separately
explained (a free-tier model default) and is not this plan's subject.

These are carry-forward numbers: they come from one session's live receipts and are not
reproducible from this repo. What IS in the repo is the mechanism that produced them —
`agent_runtime/mission_chat_phases.py` (`PHASE_ORDER`, `:76-89`) and the mark sites in
`hermes_cli/harness_parts/persona_commands.py` (`:2003` anchor through `:3800`).

---

## 2. What is already convicted, and by what

`apply_chat_lane_tool_scope` (`agent_runtime/persona_runtime.py:834-885`) owns the cold
cost, and the conviction is documented in
`tests/agent_runtime/test_agent_create_subphases.py:1-34`: an unwarmed create bills
`instance_ms` 2,781 ms of which `chat_lane_scope_ms` is **2,421**, while
`resolve_tool_visibility` measures **0** because the scope application has already filled
every shared cache it would have reached. The same create after `warm_persona_memos`
(`agent_runtime/persona_prewarm.py:181`) bills 281 ms with `chat_lane_scope_ms` at 15 and
zero probe rounds.

The millisecond magnitudes are process-lifetime state a shared pytest session has already
paid for and cannot safely un-pay. The **enforceable** gate is therefore the counted
mechanism: `tools/registry.py:probe_rounds_this_thread` (`:286`), sampled as a per-turn
DELTA (`persona_commands.py:1930-1944`, baseline at `:2008`, delta at `:3203`) and
recorded as the `registry_probe_rounds` counter. The rounds are billed to
`apply_chat_lane_tool_scope` and to nothing else, and a warm takes them to zero.

The suspect the stage opened with — that `warm_persona_memos` resolves with
`session_id=None` while the create resolves with the freshly minted session id, so the
warm might fill a neighbouring memo key — is **ACQUITTED at HEAD**
(`agent_runtime/agent_create_phases.py:1-12` states the suspicion;
`test_agent_create_subphases.py:20-24` disposes of it).

---

## 3. The prewarm is fired-but-too-slow, not unfired

Live receipts, 2026-08-22 14:48Z boot: the prewarm warmed the whole persona catalog in
~1.2 s of its own accounting (`persona_prewarm done` lines,
`PREWARM_DONE_RECEIPT`, `persona_prewarm.py:127`) — backend_dev 438 ms, base 109, dev
202, neko_supervisor 250, qa 157. The first drop of that session STILL paid
`rpc_instance_ms=2030` against 78 ms for the second drop eleven seconds later.

The pacing finding: the warm worker walks the persona catalog sequentially
(`_drain`, `:243`), and under a saturated boot serve the `check_fn` churn is still
running minutes after mount — so a drop within roughly the first two minutes of boot
still pays cold. The verb and the trigger work; the schedule does not.

**Carry-forward caveat:** the ~1.2 s catalog figure and the 2030/78 ms pair are that
session's receipts, not re-measured here.

---

## 4. Candidate remedies, none taken

1. **Order or parallelise the prewarm drain** so the personas an operator is likely to
   touch first are warm before the first drop, instead of last in a sequential walk.
2. **Serve-side warm-at-boot** rather than warm-on-request, accepting the boot cost the
   operator is already waiting through.
3. **Make `apply_chat_lane_tool_scope` cheap rather than warm.** It exists for display
   parity — it threads the real chat-lane resolution onto an operator preview
   (`persona_runtime.py:840-856`) — and it pays a full registry populate plus a toolset
   sweep to do it. A cheaper parity answer would remove the cost rather than move it.
4. **Prologue diet** (the `run_conversation` prologue measured at 1,338 ms of turn
   `c59ab99e`'s hermes share) — explicitly gated below.

## 5. Gates

- **Do not act on remedy 4 yet.** The `request_assembled` split (commit `785a35beae`)
  is days old; the prologue diet is gated on a week of split data, so the "before" is a
  distribution rather than one turn.
- **Do not treat the operator's felt latency as one number.** The 2026-08-22 drop
  investigation found the operator felt ~10 s on a cold drop where the drop log line
  spans layout+rpc only — roughly 7 s (sprite resolution, readiness-to-visible) is an
  acknowledged observability gap with no instrument at all. Remedies aimed at the
  measured 2.4 s cannot close a felt 10 s.
- **Any fix must keep the honesty contract.** Absent-never-zero, monotonic-only,
  first-mark-wins, release-visible (`mission_chat_phases.py:18-50`). An optimisation that
  makes a phase unmeasurable has made the problem worse.
- **Operator ruling still owed** on the alice-lane provider half (moving that lane off
  the free-tier default). That is a config decision, not an engineering one, and it is
  the larger single win on that lane.

## 6. Related

The launcher-side staged plan for the provider half and the audit-tool rendering lives in
the launcher repo under `Launcher_Brain/20 — Active Initiatives/` (committed
`fd17201f7`, gap-closed `1c7b0ed5c`). Not tracked here; this file owns the hermes half.
