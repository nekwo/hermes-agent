# Planned — the chat-swap `--kill-active` guard is a no-op

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md))
**Status:** known-broken. The flag is accepted, reported as applied, and does
nothing.
**Raised / verified:** 2026-08-22 against HEAD.

## What the invariant used to be

Archived doc [01 — Architecture](../archive/2026-08-22-pre-consolidation/01-architecture.md)
("Chat-swap safety") specified: rebinding a placement's active chat over a live
run required an explicit kill flag, which would `run.cancel` and close the
worker first; without it the harness returned `chat_busy`.

## What is true today

`PersonaInstanceStore.open_chat` still takes `kill_active: bool = False`
(`agent_runtime/persona_assignments.py:1755`), and **the parameter is never
read again anywhere in the method body** (verified by scanning lines 1746-1960
for the name: the signature line is the only hit). The branch where it would
have acted is now an explicit no-op with the comment "Worker/run ownership is
orthogonal to operator chat ownership. Opening another chat root must not
cancel or rebind live work." (`:1807-1810`).

The CLI surfaces the flag on both `harness persona instance create` and
`… open-chat` (`hermes_cli/harness.py:933`, `:949`) and reports
`"killed_previous": bool(kill_active)` in its JSON result
(`hermes_cli/harness_parts/persona_commands.py:667`, `:982`, `:1137`) — an echo
of the operator's own flag, not an observation.

The refusals that *are* live on the bind are different ones, and they live in
`assert_bindable` (`persona_assignments.py:1654`): sibling-steal of another
instance's chat session, and binding a retired instance. `chat_busy` still
exists but now means something else entirely — the per-turn chat-root lease is
held (`agent_runtime/mission_chat_outcome.py:147`, `dispatch_store.py:639`).

This is coherent with the mission-lane removal (there are no worker runs to
cancel), which is why it is a documentation-and-surface defect rather than a
runtime hazard. But a flag that reports success without acting is a lie the
Launcher and every agent skill can read.

## Gate to open this

Pick one, with an operator ruling:

- **Drop it.** Remove `kill_active` from `open_chat`, both CLI parsers, and the
  `killed_previous` result key. Requires a Launcher-side check that nothing
  sends it, since the key is on the wire today.
- **Reimplement it.** Define what "active" means now that runs are gone — the
  only live concurrency object is the chat-root lease — and make the flag break
  that lease, with `chat_busy` as the refusal when it is absent. This changes
  `chat_busy` from a transient-retry signal into a two-meaning code, so it needs
  a separate error kind.
