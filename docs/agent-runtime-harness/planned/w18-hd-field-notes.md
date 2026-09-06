# w18/hd — field notes (2026-09-06)

Two small defects, both found by w17/hb while writing something else and rowed
rather than fixed there. Short notes, one section per row.

## 1. `realm sync revert --item` could not address a removed persona instance

**The read.** `_persona_instance_store_drift_items` emits `container=""` for a
baselined instance with no live record — the `workspace_id` is read off the
record, and the record is the thing that is gone. `parse_item_spec` then refused
the row's own `spec` (`persona_instance::<id>`) because it required every one of
the three parts to be non-empty. So the row was counted, named in
`store_drift.items`, and revertable only through `--all` — on the one drift kind
an operator most wants to take one at a time.

**Which fix, and why.** The row offered two: derive the container from the item
key the way the canvas family does (`owner_instance_id_of`), or let
`parse_item_spec` accept an empty container. The first is not available here and
would not be honest if it were: a graph id literally CONTAINS its owner instance
id, which is why the canvas family can derive one, while an instance id says
nothing about the workspace that held it — any derived container would be a
guess printed as a fact. And this family needs no container at all: `_Upstream.
lookup` reads the persona-instance projection (ONE realm-wide document) by key
and never touches the container. So the blank is the accurate value, not a
missing one, and the fix belongs in the parser.

The relaxation is one field wide. Family and key stay required — a blank family
has no transition table to dispatch on, a blank key names nothing — and the
guard now says exactly that (`parts[0]` and `parts[2]`), rather than "all three".

**The property that makes it safe.** A blank container is a SPELLING, never a
wildcard. Selection is `by_spec.get(f"{family}:{container}:{item_key}")`, an
exact match against the derived drift set, so `FAMILY::KEY` reaches only a row
that itself reported a blank; a live agent is still addressed by its workspace.
That is its own test (`test_a_blank_container_is_not_a_wildcard`), because it is
the property the original refusal was protecting and the one a future
"convenience" lookup would quietly break.

**Red → green.** Red: 3 failed, 67 passed
(`test_a_removed_agent_is_addressable_by_item_and_restored`,
`test_a_blank_container_is_not_a_wildcard`,
`test_a_blank_container_parses_to_the_empty_container`). Green after the fix:
70 passed.

**Killing mutations** (each applied on its own, both registered in
`tests/mutation_claims.json` so they keep running):

| mutation | reds |
| --- | --- |
| the guard back to `not all(part.strip() for part in parts)` | all three new tests |
| `by_spec` miss on a blank container falls back to a family+key scan | `test_a_blank_container_is_not_a_wildcard` |
| drop the `parts[2].strip()` half of the guard | both empty-key cases of `test_the_family_and_the_key_are_still_required` |

**Two stale claims corrected in passing.** The canvas family's docstring and
`docs/agent-runtime-harness/01-system-architecture.md` both said a blank
container makes `FAMILY:CONTAINER:KEY` *unparseable*. That was true when it was
written and is the reason the canvas family derives its container; it is no
longer true, and the canvas family's reason is now stated as the naming property
it actually is.

## 2. `draft_lock._claim` answered two different things on two hosts

(filled in with the second commit)
