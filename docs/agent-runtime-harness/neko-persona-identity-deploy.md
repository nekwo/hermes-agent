# Neko persona identity — deploy runbook (`fix/neko-persona-identity`)

Fixes the "Neko Mission Lead messages itself" incident. The code changes on this
branch (identity block on the mission-chat lane + `include_profile_memory`-gated
memory + honest prompt observability + relay-guard regression) ship with the
merge. The steps below are the **operator deploy** that completes the fix on the
live host; they change live profile/runtime state and are intentionally NOT run
by the merge.

## Why (root cause, one paragraph)

The canonical Mission Control chat lane (`mission_chat_reply`) runs isolated
(`skip_context_files=True`), so the bound profile's `SOUL.md` is not loaded as
the identity — the model got the generic `DEFAULT_AGENT_IDENTITY` and, before
this branch, **no "you are Neko" hat at all**. Meanwhile it loaded the bound
profile's memory unconditionally. `neko_supervisor` is bound to the **alice**
profile (for capabilities), so every Neko turn inherited Alice's
`MEMORY.md`/`USER.md` — in which "Neko" is a downstream pipeline node
("goal→Neko→Dev→QA"). Generic identity + Alice's "Neko is someone I dispatch to"
worldview → the model relayed the operator's question to `neko_supervisor` (i.e.
itself). Code fix = inject a first-person identity block + stop borrowing a
profile's memory unless opted in. Deploy fix = give Neko her **own** profile so
the memory she does load is hers.

## Step 1 — create the `neko` profile with the OFFICIAL command

Run on the live host (bash reaching the `hermes` shim), NOT hand-built — the
official path wires the wrapper alias, schema tracking, `.env`, and skill sync:

```bash
hermes profile create neko --clone --clone-from alice \
  --description "Neko Mission Lead - Mission Control supervisor persona; coordinates Dev/Backend Dev/QA mission slices."
```

`--clone` copies `config.yaml`, `.env`, `SOUL.md`, skills, and
`memories/MEMORY.md` + `memories/USER.md` from `alice` (see
`hermes_cli/profiles.py` `_CLONE_CONFIG_FILES` / `_CLONE_SUBDIR_FILES`). Auth is
still borrowed from the head profile at runtime via `HERMES_AUTH_HOME`
(`agent_runtime/profile_context.py`), so no credential/OAuth work is needed.

## Step 2 — curate identity (required; the clone carries Alice's identity)

1. **Overwrite** `X:\Eternia\.hermes\profiles\neko\SOUL.md` with the draft in
   `neko_SOUL_draft.md` (next to this file). Tony tunes voice before it goes
   live. It is catgirl-family (nekomimi) but a distinct character — Neko the
   mission lead, not Alice — with no "deploys Neko" third-person framing and no
   Alice/Tony relationship text.
2. **Reset** `X:\Eternia\.hermes\profiles\neko\memories\MEMORY.md` and
   `...\memories\USER.md` to empty. The clone carries Alice's memories; Neko
   starts fresh and accumulates her own from her own turns.

## Step 3 — rebind + preserve qa memory in alice's live config

Edit `X:\Eternia\.hermes\profiles\alice\config.yaml` under `agent_runtime.personas`:

```yaml
    neko_supervisor:
      hermes_profile: neko        # was: alice
      # display_name / include_profile_memory unchanged; include_profile_memory:
      # true is now CORRECT — it loads Neko's own memories, not Alice's.
    qa:
      hermes_profile: launcher-qa
      include_profile_memory: true  # ADD: preserve qa chat memory now that the
                                    # chat lane honors the flag (was implicitly on
                                    # via the old hardcoded skip_memory=False).
```

The explicit `hermes_profile: neko` sticks (`agent_runtime/config.py`
`_explicit_supervisor_profile_override`); the legacy-alice fallback only rewrites
the literal value `alice` when that profile is missing, which it is not.

Back up first: `cp config.yaml config.yaml.bak-neko-profile-$(date +%Y%m%d)`.

## Step 4 — restart serve + live proof

1. Restart `hermes harness serve` (kill the running serve; it re-execs from the
   editable checkout).
2. In Mission Control, message the **Neko Mission Lead** channel:
   - "what do you see in your hud rn" → trace shows **no** `agent_chat_send →
     neko_supervisor`; Neko answers directly.
   - "use agent_chat_send to relay this to neko_supervisor: ping" → trace shows a
     typed `relay_cycle` refusal (defense-in-depth; already in the running
     build).
   - CONTEXT peek → `prompt_layers` shows the `Persona identity` layer, `Profile
     memory` = loaded (Neko's), and the `final_model_input` contains no Alice
     sentinel line ("goal→Neko→Dev", "catgirl").

## Blast radius

- Launcher roster palette is profile-keyed → Neko's Mission Control accent color
  changes (cosmetic).
- Persona-instance rows keep `persona_id=neko_supervisor`; `profile_id` refreshes
  to `neko` on the next `derive_from_workers`. Existing chat sessions (keyed on
  persona_instance_id) survive.
- Model/provider resolution unchanged (top-level `model.default` authority for
  harness turns; the cloned config carries the same provider settings).
