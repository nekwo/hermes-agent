#!/usr/bin/env python3
"""Re-key Mission Office actors from their persona CLASS key to their canonical
persona-instance key.

Why this exists
---------------
``OfficeStore`` has always keyed instance-bound actors by
``canonical_persona_instance_id`` (``office_store.py`` header, plan §4.3;
``_canonical_actor_key``). Every placement on disk today was nonetheless written
WITHOUT a ``persona_instance_id``, so all live actors fell through to the bare
persona id. The schema was never the problem — the DATA is. This script moves
the data.

The binding is taken from the actor's own ``agent``-kind item id, which for
every live actor IS a real ``persona_instances/*.json`` file name. That is a
happy coincidence of how the placements were authored, NOT an invariant, so
every step of the derivation is verified and a step that does not verify
REFUSES rather than guesses:

- no ``agent``-kind item, or more than one  -> SKIP (reported, never guessed)
- item id does not canonicalize             -> REFUSE
- canonical id has no ``persona_instances/<id>.json`` -> REFUSE
- canonical id equals the current actor key -> already keyed, nothing to do

What ``--apply`` would do, per actor, and in this order
------------------------------------------------------
1. ``upsert_actor`` under the NEW key, carrying ``persona_instance_id`` so the
   store mints the instance key itself (never a hand-written filename — the
   store is the only key authority).
2. ``remove_actor`` on the OLD class key, which ARCHIVES it (archive-never-
   delete) and appends the old key to the surface's ``archived_actor_keys``
   resurrection guard.

New-key-first is deliberate: it is the order that never leaves the workspace
with zero placements for an agent if the process dies between the two writes.
The cost of that choice is a transient duplicate, not a transient gap.

Read ``RESURRECTION GUARD`` below before running with ``--apply``.

RESURRECTION GUARD — how step 2 interacts with ``archived_actor_keys``
----------------------------------------------------------------------
Archiving ``backend_dev`` and then upserting
``personainst_backend_dev_agent_29fdd71a`` does NOT collide: the guard is keyed
by actor key, the two keys are different strings, and ``upsert_actor``'s
ledger-clearing branch (``office_store.py:347``) only fires when the key being
upserted is itself in the ledger. The new key never is. So the local sequence is
clean.

The interaction that is NOT clean is realm sync, in two directions:

(a) A PEER that still publishes an active ``backend_dev``. On the next pull,
    ``classify_three_way_pull`` sees ``locally_archived=True`` and returns
    ``NOOP`` if the remote copy is absent/unchanged — the guard doing its job —
    but ``CONFLICT("archive_vs_edit")`` if the peer EDITED it. That writes a
    conflict sidecar for the old key, which then shows up as an
    ``office_actor_conflict`` parity warning and blocks further writes to the
    OLD key (``_guard_no_conflict``). It does not block the new key. Resolve
    with ``--take local`` (keeps the archive). ``--take remote`` would write
    ``backend_dev`` back as ACTIVE beside the instance-keyed actor: the same
    agent placed twice, with duplicate ``item_id`` values on the canvas.

    FENCED as of ``OfficeStore._guard_class_keyed_adoption``. That branch calls
    ``_write_actor`` directly, past ``upsert_actor`` and past every caller-side
    guard below, so the fence sits in the STORE: a class-keyed adoption that
    resurrects an archived key or duplicates an item id is refused with
    ``duplicate_conflict``, naming ``--take local`` as the exit.
    ``--allow-class-key`` on ``harness office resolve-conflict`` is the
    operator's on-the-record override.

(b) Any later class-keyed WRITE re-creates the old actor and CLEARS the guard.
    ``upsert_actor`` treats an explicit local upsert of an archived key as
    operator intent to re-add (``office_store.py:344-351``). So a launcher save,
    a ``harness office actor-upsert`` without ``persona_instance_id``, or a
    ``workspace_template`` apply that copies class-keyed payloads would undo the
    migration for that actor AND leave the instance-keyed actor in place — again
    a double placement.

    FENCED IN THE STORE as of EG-6.6: ``OfficeStore.upsert_actor`` refuses the
    write itself (``_guard_class_keyed_write``), so NO caller has to remember to
    ask. Each writer keeps only its own translation of that one refusal:

    - ``workspace_template._copy_office`` reports it per-actor
      (``office_actor_class_key_refused`` warning on the create envelope). It
      never passes the override: a template apply holds no operator intent about
      the destination.
    - ``harness office actor-upsert`` (also the launcher's save path — the
      Flutter bridge shells out to this verb) exits ``duplicate_conflict``.
      ``--persona-instance-id`` threads the binding through;
      ``--allow-class-key`` re-issues the write with the store's
      ``allow_class_key`` parameter and records the override.
    - ``runtime.office.upsert`` answers 4090 / ``class_key_collision`` and has NO
      override, deliberately: a wire parameter is not consent.
    - ``agent_create``'s placement leg compensates the reservation and refuses.

    The guard is CONDITIONAL, not a blanket ban — a class-keyed write only
    fails when the class key is archived or its item ids already belong to an
    active instance-keyed sibling. Class-keyed placements remain legal
    (``archive_actors_for_instance``: they survive instance churn by design).

    The remaining door is ``_write_actor`` reached WITHOUT ``upsert_actor`` or
    ``resolve_conflict``, and it is now enumerated rather than merely warned
    about: ``tests/agent_runtime/test_office_class_key_one_fence.py`` pins every
    production writer of a live actor file and its fence disposition, so a fourth
    writer reds by enumeration instead of shipping unfenced.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _canonical(item_id: str, *, persona_id: str) -> str | None:
    from agent_runtime.persona_assignments import canonical_persona_instance_id

    return canonical_persona_instance_id(item_id, persona_id=persona_id)


def _plan_actor(actor: Any) -> dict[str, Any]:
    """One actor's verdict. Pure — reads the store, decides, writes nothing."""

    from agent_runtime import paths

    row: dict[str, Any] = {
        "workspace_id": actor.workspace_id,
        "old_key": actor.actor_key,
        "persona_id": actor.persona_id,
        "items": len(actor.items),
        "new_key": None,
        "instance_file": None,
        "verdict": "",
        "detail": "",
    }

    if actor.persona_instance_id:
        row["verdict"] = "already-keyed"
        row["new_key"] = actor.actor_key
        row["detail"] = f"actor already bound to {actor.persona_instance_id}"
        return row

    agents = [item for item in actor.items if item.kind == "agent"]
    if len(agents) != 1:
        row["verdict"] = "SKIP"
        row["detail"] = (
            f"{len(agents)} agent-kind items (need exactly 1); "
            f"kinds={[i.kind for i in actor.items]}"
        )
        return row

    item_id = agents[0].item_id
    canonical = _canonical(item_id, persona_id=actor.persona_id)
    if not canonical:
        row["verdict"] = "REFUSE"
        row["detail"] = f"agent item_id {item_id!r} does not canonicalize to an instance id"
        return row

    row["new_key"] = canonical
    path = paths.persona_instance_path(canonical)
    exists = path.exists()
    row["instance_file"] = f"{'present' if exists else 'MISSING'}: {path}"
    if not exists:
        row["verdict"] = "REFUSE"
        row["detail"] = f"no persona_instances/{canonical}.json — refusing to invent an instance"
        return row

    if canonical == actor.actor_key:
        row["verdict"] = "already-keyed"
        row["detail"] = "class key already equals the canonical instance id"
        return row

    row["verdict"] = "REKEY"
    row["detail"] = f"from agent item {item_id!r}"
    return row


def _apply(store: Any, actor: Any, new_key: str, *, updated_by: str) -> None:
    """New key first, then archive the old — see the module docstring."""

    from agent_runtime.serde import to_jsonable

    payload = {
        "persona_id": actor.persona_id,
        "persona_instance_id": new_key,
        "backing_profile": actor.backing_profile,
        "items": [to_jsonable(item) for item in actor.items],
    }
    minted = store.upsert_actor(actor.workspace_id, payload, updated_by=updated_by)
    if minted.actor_key != new_key:
        # The store is the key authority; if it disagrees with the plan, stop
        # rather than archive the old actor against an unexpected new key.
        raise RuntimeError(f"store minted {minted.actor_key!r}, plan expected {new_key!r}")
    store.remove_actor(
        actor.workspace_id, actor.actor_key, reason="rekey_to_instance", updated_by=updated_by
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. WITHOUT this flag the script is a pure read (the default).",
    )
    parser.add_argument("--workspace", action="append", help="limit to this workspace id (repeatable)")
    parser.add_argument("--updated-by", default="office_rekey_migration")
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON instead of a table")
    args = parser.parse_args(argv)

    from agent_runtime import paths
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    workspaces = args.workspace or store.list_workspaces()

    print(f"office root : {paths.office_root()}")
    print(f"instances   : {paths.persona_instances_dir()}")
    print(f"mode        : {'APPLY (writing)' if args.apply else 'DRY RUN (no writes)'}")
    print()

    rows: list[dict[str, Any]] = []
    for workspace_id in workspaces:
        if not store.surface_exists(workspace_id):
            print(f"[{workspace_id}] no office surface — skipped")
            continue
        actors = store.list_actors(workspace_id)
        surface = store.get_surface(workspace_id)
        print(f"[{workspace_id}] {len(actors)} active actor(s); archived_actor_keys={surface.archived_actor_keys}")
        for actor in actors:
            row = _plan_actor(actor)
            rows.append(row)
            print(f"  {row['old_key']} -> {row['new_key'] or '(none)'}")
            print(f"      verdict   : {row['verdict']}")
            print(f"      items     : {row['items']}")
            print(f"      instance  : {row['instance_file'] or '(not resolved)'}")
            print(f"      detail    : {row['detail']}")
            if row["verdict"] == "REKEY":
                print(f"      would     : upsert {row['new_key']!r}, then archive {row['old_key']!r}")
                print(f"      guard     : {row['old_key']!r} joins archived_actor_keys (see module docstring)")
            if args.apply and row["verdict"] == "REKEY":
                _apply(store, actor, row["new_key"], updated_by=args.updated_by)
                print("      APPLIED")
        print()

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    print("summary: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no actors"))
    if not args.apply:
        print("DRY RUN — nothing was written. Re-run with --apply to commit.")
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 1 if counts.get("REFUSE") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
