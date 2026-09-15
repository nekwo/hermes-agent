# Planned — global-singleton persona instances

**Owner domain:** system architecture ([01-system-architecture.md](../01-system-architecture.md))
**Status:** queued redesign, not implemented.
**Raised:** in-code, cited as "the queued global-singleton redesign" at
`agent_runtime/persona_assignments.py:2518` and
`tests/agent_runtime/test_office_state_patches.py:930`. Re-verified 2026-08-22.

## What is true today

A persona has **two structurally distinct kinds of instance row**, and the
discriminator is the id shape:

- The **canonical operator channel** — `persona_instance_id_for(persona_id)`,
  e.g. `personainst_qa`, `personainst_profile_alice`
  (`persona_assignments.py:2501`). One per persona, runtime plumbing, minted
  implicitly by a bind. `is_canonical_persona_channel` (`:2509`) is the live
  test for it.
- A **placement-backed row** — `persona_instance_id_for_placement(placement_id)`
  (`:2505`), whose tail is the scene `itemId`, so it can never collapse onto the
  canonical id.

The distinction is load-bearing in three places today: the retire verb refuses
to end a canonical row (`:2516-2519`), workspace scoping excludes global
canonicals from a real workspace scope and lets placements shadow them
(`agent_runtime/workspace_scope.py:191` `exclude_global_canonicals` and `:150`
`shadow_canonical_by_placement`), and the identity reconciler folds legacy id
schemes onto the canonical id (`agent_runtime/persona_instance_identity.py`).

## What the redesign would change

The queued idea is that a persona's canonical row becomes a true global
singleton with no scene claim at all, so "instance" means "in-level placement"
without exception (the operator ruling of 2026-07-18 that
`workspace_scope.py`'s exclusion rule already encodes at the *advertising*
layer, but not at the *storage* layer).

No design document for it survives, and no code implements it. What exists is
the discriminator that protects the possibility.

## Gate to open this

1. An operator ruling on whether a canonical row may ever carry a
   `workspace_id` pointer. Today it may, and `workspace_scope.py` follows that
   pointer when present — the singleton redesign has to say whether that stays
   legal or becomes an anomaly.
2. A migration answer for existing canonical rows that already carry scope
   pointers, since `open_chat` stamps them whenever an authoritative scope is
   supplied (`persona_assignments.py:1836-1847`).
3. A read-side answer for the three consumers above, which currently derive
   different behaviour from the same discriminator.
