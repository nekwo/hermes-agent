"""ONE fence: the class-key guard at the store's write chokepoint (Plan EG-6.6).

``tests/agent_runtime/test_office_class_key_guard.py`` proves the fence REFUSES
the right writes and leaves the legitimate ones alone. This file proves the
different property EG-6.6 buys: that there is exactly ONE fence and every lane
rides it.

The historical shape was four callers of ``office_class_key_guard`` around one
store — ``serve_rpc._runtime_office_upsert``, ``agent_create``'s placement leg,
``harness office actor-upsert``, ``workspace_template._copy_office`` — each
holding its own copy of the decision. Nothing was wrong with any of the four.
What was wrong is that the store's contract said nothing about the fence, so
``scripts/office_actor_rekey_to_instance.py`` had to WARN, in prose, that a new
writer reaching ``upsert_actor`` was unguarded by default. Prose does not fail a
build. The fifth writer would have shipped unfenced with every reply-shape test
green, and its symptom is the defect class this stage names: **an identical
sibling nobody can find** — the same agent placed twice under two actor keys,
with no conflict warning anywhere, because every guard in the store keys on the
actor key and the two keys are different strings.

Four witnesses, each aimed at a different way "one fence" can quietly become
none:

1. **Refusal parity** — every lane refuses the SAME seeded collision and names
   the same fault, and none of them writes or consumes anything.
2. **The historical-shape mutation, executed** — delete the store's fence and
   every lane goes through at once. That is what makes the fence's location the
   load-bearing fact rather than a stylistic one, and its companion (no lane
   holds a second copy of the predicate) covers the one lane a monkeypatch
   cannot reach.
3. **Enumeration** — every production path that writes a live actor file, with
   its fence disposition, so a FOURTH writer reds by enumeration instead of
   shipping.
4. **The fence cannot be blinded** — its ``duplicate_item_placement`` half
   consulted ``list_actors``, which drops files that will not decode, so one
   unreadable instance-keyed sibling turned "unknown" into "no conflict" for
   every writer at once. It now refuses typed instead of guessing.

No ``harness serve`` is spawned anywhere here: the RPC lane is exercised by
calling the registered handler function in-process, which is the same code the
socket reaches and cheaper than a child by two orders of magnitude.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from unittest import mock

import pytest

from agent_runtime.office_store import OfficeStore

# The fixtures come from the guard suite on purpose: they run the REAL migration
# script, and a hand-built imitation of post-migration state is exactly what that
# suite exists not to trust.
from tests.agent_runtime.test_office_class_key_guard import (
    INSTANCE,
    _keys,
    _migrated_workspace,
    _payload,
    _run_harness,
    _seed_class_keyed,
    _workspace,
)

#: The fault every lane must name, on the shared fixture. Both of the guard's
#: reasons fire there — real post-migration state, and the peer payload carries
#: the item ids the surviving instance-keyed actor holds.
EXPECTED_REASONS = ["duplicate_item_placement", "resurrects_archived_class_key"]


def _colliding_payload() -> dict:
    """Exactly what the launcher used to send after the re-key: the class key,
    with the items that now belong to the instance-keyed actor."""

    return _payload("backend_dev", agent_item_id=INSTANCE)


def _backend_dev_persona():
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="backend_dev",
        display_name="Backend Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


def _rpc_upsert(rid: str, workspace_id: str, payload: dict) -> dict:
    """``runtime.office.upsert``, in-process.

    The handler function IS the wire behaviour — the transport only frames it —
    and calling it directly keeps this file inside the no-child-process fence
    while still exercising the real translation arm.
    """

    from agent_runtime import serve_rpc

    return serve_rpc._runtime_office_upsert(rid, {"workspace_id": workspace_id, "actor": payload})


def _run_harness_utf8(*args: str):
    """``_run_harness``, but with the child's stdout pinned to UTF-8.

    The shared helper decodes with the platform's locale encoding, which is fine
    for the substring checks every other test makes and NOT fine for a
    byte-for-byte comparison: the refusal sentence contains ``class→instance``,
    and a Windows console re-encodes U+2192 on the way out, so the two lanes
    would differ by a transport artifact and nothing else.
    """

    import os
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=90,
    )


def _template_copy_into(dest_workspace_id: str) -> dict:
    """The template lane: a source holding the stale class-keyed placement,
    copied onto ``dest``."""

    from agent_runtime.workspace_template import copy_workspace_content

    source = _workspace("One Fence Template Source")
    _seed_class_keyed(source, "backend_dev")
    return copy_workspace_content(source, dest_workspace_id, scopes=("office",))


def _agent_create_into(workspace_id: str, monkeypatch) -> object:
    """The create lane, with the payload shape a refactor would produce.

    ``placement_actor_payload`` is instance-keyed BY CONSTRUCTION, so this lane
    cannot collide as it stands — which is precisely why its translation arm has
    to be tested rather than reasoned about. "Correct by construction" is one
    refactor away from absent, and the refactor that drops the binding is the one
    this injection performs. The store's fence is what catches it either way;
    what is under test here is that the create lane COMPENSATES its reservation
    and names the fault instead of stranding a roster row.
    """

    from agent_runtime import agent_create

    _backend_dev_persona()
    monkeypatch.setattr(
        agent_create,
        "placement_actor_payload",
        # ``position`` joined the real signature in S2 (the resolved slot is
        # passed in rather than re-read off the request). Accepted and ignored:
        # this injection is about the payload's BINDING, and swallowing the
        # kwarg keeps the substitution a stand-in for the refactor rather than
        # a TypeError one call earlier.
        lambda request, *, display_name, position=None: _colliding_payload(),
    )
    return agent_create.perform_agent_create(
        {
            "persona_id": "backend_dev",
            "workspace_id": workspace_id,
            "position": [3.5, -1.25],
            "idempotency_key": "one-fence-create",
            "placement_id": "backend_dev_one_fence_agent_2",
        }
    )


# ── witness 1: every lane refuses the same fault, and writes nothing ────────


def test_every_lane_refuses_the_same_class_keyed_write_and_names_the_same_fault(monkeypatch):
    """Refusal PARITY across all four lanes, on ONE fixture.

    All four run against the same migrated workspace, one after another, which is
    only possible because no refusal writes or consumes anything — that is half
    the assertion, re-checked after every lane rather than once at the end.

    The other half is the mapping, and it is deliberately NOT "the same string
    everywhere". EG-4.5 already ruled on that for the resolve verb and the ruling
    holds here: the lanes answer in DIFFERENT taxonomies by design (JSON-RPC's
    ``data.reason``, the stage-42 exit codes, a copy-warning code, a compensated
    placement failure), and asserting they spend one string would either be false
    or force a wrong translation. What must be identical is the FAULT — the
    collision's reasons, the class key, and the sibling it collided with, all
    from the one authority — so the fault is compared for EQUALITY across the
    three lanes that carry it structurally, and the fourth (the CLI, whose
    stage-42 envelope carries evidence in its message) must name every element of
    it.

    The transport codes are pinned as a SET beside that, so a rename on any one
    lane reds here rather than silently splitting the lanes' stories.
    """

    workspace, _items = _migrated_workspace("One Fence Parity Office")
    assert _keys(workspace) == {INSTANCE}
    intact = _keys(workspace)

    def _still_intact(lane: str) -> None:
        assert _keys(workspace) == intact, f"the {lane} lane's refusal wrote something"
        assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys, (
            f"the {lane} lane's refusal cleared the resurrection ledger"
        )

    faults: dict[str, tuple] = {}
    codes: dict[str, str] = {}

    # Lane 1 — the wire.
    rpc = _rpc_upsert("p-rpc", workspace, _colliding_payload())
    data = rpc["error"]["data"]
    assert rpc["error"]["code"] == 4090
    codes["rpc"] = data["reason"]
    faults["rpc"] = (data["persona_id"], data["class_actor_key"], tuple(sorted(data["reasons"])), tuple(data["conflicting_actor_keys"]))
    _still_intact("rpc")

    # Lane 2 — the template copy.
    outcome = _template_copy_into(workspace)
    assert outcome["copied"]["office_actors"] == 0
    [warning] = outcome["warnings"]
    codes["template"] = warning["code"]
    faults["template"] = ("backend_dev", warning["actor_key"], tuple(sorted(warning["reasons"])), tuple(warning["conflicting_actor_keys"]))
    _still_intact("template")

    # Lane 3 — the create sequence, with the binding refactored away.
    created = _agent_create_into(workspace, monkeypatch)
    assert created.refusal is not None, "the create lane placed a class-keyed actor"
    refusal = created.refusal.data
    codes["agent_create"] = refusal["placement_reason"]
    faults["agent_create"] = ("backend_dev", refusal["class_actor_key"], tuple(sorted(refusal["reasons"])), tuple(refusal["conflicting_actor_keys"]))
    assert refusal["rolled_back"] is True, "a refused placement must not strand its roster row"
    _still_intact("agent_create")

    # The fault itself, identical across the three structured lanes.
    assert len(set(faults.values())) == 1, faults
    assert faults["rpc"] == ("backend_dev", "backend_dev", tuple(EXPECTED_REASONS), (INSTANCE,))

    # Lane 4 — the CLI (also the launcher's save path). Its stage-42 envelope
    # carries the evidence in the message, so parity is "names every element".
    cli = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_colliding_payload()), "--json",
    )
    assert cli.returncode == 4, cli.stdout + cli.stderr
    cli_error = json.loads(cli.stdout)["error"]
    codes["cli"] = cli_error["code"]
    for element in (*EXPECTED_REASONS, INSTANCE, "backend_dev"):
        assert element in cli_error["message"], element
    _still_intact("cli")

    # Four lanes, four taxonomies, pinned together.
    assert codes == {
        "rpc": "class_key_collision",
        "template": "office_actor_class_key_refused",
        "agent_create": "class_key_collision",
        "cli": "duplicate_conflict",
    }


def test_the_two_lanes_that_pass_the_stores_sentence_through_spend_it_byte_for_byte():
    """The one string that IS compared for equality across lanes.

    Two of the four translations forward the store's own refusal sentence
    verbatim (``refusal_message``): the CLI's stage-42 message and the template's
    copy warning. They must be byte-identical, because the moment either lane
    starts composing its own version of that sentence the fence has grown a
    second author — and the sentence is where the three operator exits live
    (send the binding, ``actor-restore``, ``--allow-class-key``).

    The RPC lane is deliberately excluded and asserted to DIVERGE: its sentence
    must not offer a CLI flag it has no way to accept, which is a ruling from
    ``runtime.office.upsert``'s own docstring, and a test that demanded four
    identical strings would force that regression.
    """

    workspace, _items = _migrated_workspace("One Fence Sentence Office")

    cli = _run_harness_utf8(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_colliding_payload()), "--json",
    )
    assert cli.returncode == 4, cli.stdout + cli.stderr
    cli_message = json.loads(cli.stdout)["error"]["message"]

    [warning] = _template_copy_into(workspace)["warnings"]

    assert warning["message"] == cli_message
    assert "--allow-class-key" in cli_message

    rpc_message = _rpc_upsert("p-sentence", workspace, _colliding_payload())["error"]["message"]
    assert "--allow-class-key" not in rpc_message, (
        "the wire lane is offering a CLI flag it cannot accept"
    )


# ── witness 2: the historical shape, mutated on purpose ─────────────────────


def test_deleting_the_stores_fence_unguards_every_lane_at_once(monkeypatch):
    """THE anti-vacuity mutation for this stage, executed rather than described.

    The mutation the plan names is "delete the store-level fence but keep one
    caller's local copy — the historical shape". This is its first half: with
    ``_guard_class_keyed_write`` neutered, EVERY lane's colliding write LANDS.

    That is what proves the refusals in the parity test above come from the
    store and not from four surviving copies. A single caller-local copy covers
    at most one lane by construction, so under the mutant the other lanes' probes
    go red — which is exactly what this test asserts, from the other direction.

    Each lane gets its own migrated workspace, because the first successful
    resurrection changes the state the next one would be tested against.

    Anti-vacuity for the mutation itself: the probe is the class-keyed actor FILE
    appearing beside the instance-keyed one (the double placement, the defect this
    stage names), not a return value — a lane that answered ``ok`` without
    writing would not satisfy it.

    The D1 tombstone fence is neutered ALONGSIDE it, and only here. The
    migration archives the class key, so every write in this test is also a
    re-add — meaning the second fence refuses all of them and the class-key
    claim would read as proven no matter what ``_guard_class_keyed_write``
    did. Isolating one fence's claim requires standing the other one down; that
    the tombstone fence independently refuses this same write is a real
    property, and it is asserted where it belongs, in the D1 suite.
    """

    monkeypatch.setattr(
        OfficeStore, "_guard_class_keyed_write", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        OfficeStore, "_guard_archived_actor", lambda self, *a, **k: None
    )

    rpc_ws, _ = _migrated_workspace("Unfenced RPC Office")
    reply = _rpc_upsert("m-rpc", rpc_ws, _colliding_payload())
    assert "error" not in reply, reply
    assert _keys(rpc_ws) == {"backend_dev", INSTANCE}, (
        "the RPC lane held its own copy of the fence"
    )

    tpl_ws, _ = _migrated_workspace("Unfenced Template Office")
    outcome = _template_copy_into(tpl_ws)
    assert outcome["warnings"] == [], outcome["warnings"]
    assert _keys(tpl_ws) == {"backend_dev", INSTANCE}, (
        "the template lane held its own copy of the fence"
    )

    create_ws, _ = _migrated_workspace("Unfenced Create Office")
    created = _agent_create_into(create_ws, monkeypatch)
    assert created.refusal is None, created.refusal
    assert _keys(create_ws) == {"backend_dev", INSTANCE}, (
        "the create lane held its own copy of the fence"
    )


def test_no_lane_holds_a_second_copy_of_the_predicate():
    """The mutation's other half, as a source-text gate — and the only form that
    reaches the CLI.

    ``harness office actor-upsert`` runs in a child process, so no monkeypatch can
    prove it is unguarded when the store's fence goes away. What CAN be pinned is
    that it does not call the predicate: a lane that never evaluates
    ``class_key_collision`` cannot refuse on its own, whatever the store does.

    ANTI-VACUITY. The kill-mutation is "put the caller-side check back beside the
    store's", and the probe is the caller's own source text, which the mutant
    necessarily contains — the mutation IS the presence of that call, so it cannot
    pass by also satisfying the probe. Guarding the guard: each lane must still
    NAME the typed refusal, so a function emptied of both would red rather than
    read as clean.
    """

    from agent_runtime import agent_create, serve_rpc, workspace_template
    from hermes_cli.harness_parts import office as office_cli

    lanes = {
        "rpc": serve_rpc._runtime_office_upsert,
        "agent_create": agent_create.perform_agent_create,
        "cli": office_cli._cmd_office_actor_upsert,
        "template": workspace_template._copy_office,
    }
    for name, fn in lanes.items():
        source = inspect.getsource(fn)
        assert "class_key_collision(" not in source, (
            f"the {name} lane evaluates the class-key predicate itself again — "
            "the fence is back at N call sites"
        )
        assert "refusal_message(" not in source, (
            f"the {name} lane composes the refusal sentence itself again"
        )
        assert "ClassKeyedPlacementRefused" in source, (
            f"the {name} lane no longer names the store's typed refusal at all"
        )

    # And the predicate is spent in exactly one place per fence.
    assert "class_key_collision(" in inspect.getsource(OfficeStore._guard_class_keyed_write)
    assert "class_key_collision(" in inspect.getsource(OfficeStore._guard_class_keyed_adoption)


# ── witness 3: enumeration — a fourth writer reds ───────────────────────────


#: Every production function that writes a LIVE actor file, with the disposition
#: that makes it safe. Adding a writer means adding a line here BY HAND, which is
#: the cost of the pin and much cheaper than the hole it closes.
FENCED_ACTOR_WRITERS = {
    ("agent_runtime/office_store.py", "upsert_actor"): (
        "FENCED — _guard_class_keyed_write, first inside the lock; override is the "
        "explicit allow_class_key parameter"
    ),
    ("agent_runtime/office_store.py", "resolve_conflict"): (
        "FENCED — _guard_class_keyed_adoption (peer-authored record, past upsert_actor); "
        "override is the explicit allow_class_key parameter"
    ),
    ("agent_runtime/office_store.py", "restore_actor"): (
        "SANCTIONED OVERRIDE — un-archiving IS the deliberate resurrection the fence "
        "refuses elsewhere, and the exit refusal_message points at. Fencing it would "
        "make the refusal a dead end."
    ),
    ("agent_runtime/office_store.py", "_write_actor"): (
        "THE PRIMITIVE — the one-line atomic write every entry above funnels into. "
        "Not a writer, the thing writers use."
    ),
}

#: NOT blessed. Named, with its ruling, so the witness DOCUMENTS the hole instead
#: of silently passing over it.
CARVED_OUT_ACTOR_WRITERS = {
    ("agent_runtime/office_store.py", "adopt_remote_actor"): (
        "OPEN HOLE, HELD FOR A RULING — task #33. The realm-sync pull's "
        "PullAction.WRITE_REMOTE arm writes a peer's actor row verbatim, past "
        "upsert_actor and therefore past the class-key fence, so a peer that never "
        "migrated can land an archived class key as ACTIVE beside its "
        "instance-keyed sibling with no operator in the loop. It is carved out "
        "rather than fenced because the fix is a decision this stage does not own: "
        "a pull is not an operator action, so it has no consent to offer, and the "
        "choices (refuse the actor and write a conflict sidecar, adopt it and warn "
        "on the pull summary, or re-key it on arrival) each change what realm sync "
        "means. EG-6.6 fences the four OPERATOR-INTENT writers and records this one "
        "as outstanding; #33 decides it. "
        "RELOCATED 2026-08-30 (plan realm-pull-live-projection H1): the write moved "
        "OUT of office_sync.apply_office_pull's raw atomic_json_write and INTO this "
        "store verb so the adopt arm finally emits its office.actor.upserted + "
        "state.patched pair. H1 changed WHERE the unfenced write lives and what it "
        "emits; it did not change WHETHER it is fenced. "
        "RULED 2026-08-30 (operator, realm-actor-lifecycle-refactor D3): the adopt "
        "arm STAYS UNFENCED. A pull is REPLICATION, not authoring — the class-key, "
        "tombstone and desk fences all refuse local operator intent, and a pull has "
        "no operator behind it to offer consent, so fencing it would mean refusing "
        "to hold a fact a peer already published. What #33 named as one hole was "
        "three: the two REAL ones are closed instead — the surface arm no longer "
        "overwrites the local tombstone ledger (C1, office_store.merge_archived_"
        "ledgers) and the archive arm no longer discards the outcome of a delete "
        "it could not take (C2, office_sync.OfficeArchiveOutcome). What remains is "
        "the DISPOSITION above, not an open question: a pulled duplicate desk or a "
        "peer's un-migrated class key is a conflict-lane fact, which is why the "
        "launcher's render-time duplicate_desk warning stays. This entry no longer "
        "moves to FENCED_ACTOR_WRITERS on a future ruling — it is the ruling."
    ),
}


def _live_actor_writers() -> dict[tuple[str, str], list[int]]:
    """Every production call that writes a live actor file, by enclosing function.

    Detects the two shapes that exist: a call to ``_write_actor`` (the store's
    primitive) and a call to ``atomic_json_write`` whose destination is a
    ``office_actor_path(...)`` call. Deliberately syntactic rather than
    behavioural — the point is to see a writer that no test exercises yet, which
    is exactly the writer a runtime chokepoint probe cannot see.

    Scope note, stated so it is not mistaken for a total guarantee: a writer that
    reached ``Path.write_text`` or built its destination in a variable would slip
    past this scan. It catches the shapes this repo actually uses, and both
    existing shapes route through helpers, which is why the scan is worth having.
    """

    root = pathlib.Path(__file__).resolve().parents[2]
    found: dict[tuple[str, str], list[int]] = {}
    for package in ("agent_runtime", "hermes_cli", "scripts"):
        for path in sorted((root / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            stack: list[str] = []

            def _visit(node: ast.AST) -> None:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stack.append(node.name)
                    for child in ast.iter_child_nodes(node):
                        _visit(child)
                    stack.pop()
                    return
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    hit = name == "_write_actor"
                    if not hit and name == "atomic_json_write" and node.args:
                        dest = node.args[0]
                        if isinstance(dest, ast.Call):
                            dest_func = dest.func
                            dest_name = (
                                dest_func.id
                                if isinstance(dest_func, ast.Name)
                                else getattr(dest_func, "attr", None)
                            )
                            hit = dest_name == "office_actor_path"
                    if hit:
                        key = (
                            path.relative_to(root).as_posix(),
                            stack[-1] if stack else "<module>",
                        )
                        found.setdefault(key, []).append(node.lineno)
                for child in ast.iter_child_nodes(node):
                    _visit(child)

            _visit(tree)
    return found


def test_every_production_writer_of_a_live_actor_file_is_enumerated_and_dispositioned():
    """THE second witness: a FOURTH writer reds by enumeration.

    The reply-shape tests cannot see a new writer — an unfenced fifth caller
    ships with every one of them green, which is exactly how this fence came to
    live at four call sites. So the pin is not a shape: it is the SET of
    functions that write a live actor file, compared for equality against a
    hand-maintained disposition table. A new writer fails this test on the day it
    is written, with the disposition question in the failure message.

    ``apply_office_pull`` is EXCLUDED BY NAME with its ruling attached (task #33)
    rather than blessed into the fenced set. That distinction is the whole value:
    an equality check with the hole quietly inside the allow-list would document
    nothing, while an equality check that refuses to name it would red until
    someone wrote a fence this stage was told not to write.

    Anti-vacuity: the disposition table's own keys are asserted to be REACHED —
    an entry for a function that no longer writes anything (a rename, a
    deletion) reds too, so the table cannot rot into a list of ghosts that
    happens to be a superset of the truth.
    """

    found = _live_actor_writers()

    unexpected = set(found) - set(FENCED_ACTOR_WRITERS) - set(CARVED_OUT_ACTOR_WRITERS)
    assert not unexpected, (
        "a new production writer of a live actor file appeared and the class-key "
        f"fence has not been dispositioned for it: {sorted(unexpected)}. Route it "
        "through OfficeStore.upsert_actor (which is fenced), give it its own "
        "store-level fence like resolve_conflict's, or add it to "
        "CARVED_OUT_ACTOR_WRITERS with the ruling that permits the hole."
    )
    vanished = (set(FENCED_ACTOR_WRITERS) | set(CARVED_OUT_ACTOR_WRITERS)) - set(found)
    assert not vanished, (
        f"the disposition table names writers that no longer write: {sorted(vanished)}"
    )

    # The two fenced entry points really do consult the fence, in source, so the
    # table's word "FENCED" is checked rather than asserted.
    for method_name in ("upsert_actor", "resolve_conflict"):
        source = inspect.getsource(getattr(OfficeStore, method_name))
        assert "_guard_class_keyed_" in source, method_name
        assert "allow_class_key" in source, (
            f"{method_name} lost its explicit override parameter — an override that "
            "is not a parameter is a caller that forgot to call the guard"
        )
    # And the sanctioned-override writer really is override-shaped: no fence, and
    # no override parameter to pass, because the whole verb IS the override.
    restore_source = inspect.getsource(OfficeStore.restore_actor)
    assert "_guard_class_keyed_" not in restore_source
    assert "allow_class_key" not in restore_source


def test_the_carve_out_is_a_live_hole_and_not_a_stale_note(tmp_path):
    """The carve-out has to keep being TRUE, or the witness is documenting a
    fiction.

    A note saying "the realm pull writes past the fence" outlives the condition
    it describes: someone fences that path, the note stays, and the next reader
    budgets for a hole that is already closed (this repo's
    four-commits-inherited-a-dead-sentence precedent). So the carve-out asserts
    the hole is still open — the pull's adopt arm still writes a peer's row
    directly and still names no class-key guard.

    Two halves, because H1 (plan ``realm-pull-live-projection``) split the one
    function this used to inspect into a caller and a store verb. The pull arm
    must still route to the UNFENCED verb, and the verb must still be unfenced;
    asserting only the second half would pass on the day someone quietly pointed
    the pull at ``upsert_actor`` instead, which is a #33 ruling wearing a
    refactor's clothes.

    The first half asks the RUNTIME, not the source. It used to grep
    ``apply_office_pull``'s text for ``adopt_remote_actor(`` — a POSITIVE claim
    resting on a spelling, and the 2026-08-30 C2 extraction proved the cost: a
    behaviour-preserving move of the arm into ``_reconcile_actors`` reddened this
    gate while the property it guards never changed. A real pull is driven here
    and the verb it reaches is recorded. The SECOND half stays a source walk,
    correctly: "this function contains no fence" is a negative claim, where
    over-approximation is the safe direction.

    D3 (2026-08-30) ruled the hole stays open BY DISPOSITION, so this test no
    longer waits for #33 to land — it pins the ruling.
    """

    from agent_runtime import office_models, office_sync
    from agent_runtime.models import OfficeActor, OfficeItem
    from agent_runtime.serde import to_jsonable
    from agent_runtime.store import RealmStore, WorkspaceStore
    from utils import atomic_json_write

    realm = RealmStore().create(name="Carve")
    workspace = WorkspaceStore().create(name="Carve", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(workspace.id)
    RealmStore().save(realm)
    remote = OfficeActor(
        actor_key="pulled_dev",
        workspace_id=workspace.id,
        persona_id="pulled_dev",
        items=[OfficeItem(item_id="pulled_dev", persona_id="pulled_dev", kind="agent", position=[1.0, 1.0])],
        revision=1,
    )
    office_dir = tmp_path / "subtree" / "store" / "office" / workspace.id
    atomic_json_write(
        office_dir / "office.json",
        to_jsonable(office_models.default_surface(workspace.id, created_at=None)),
        indent=2,
        sort_keys=True,
    )
    atomic_json_write(
        office_dir / "actors" / f"{office_models.actor_file_token(remote.actor_key)}.json",
        to_jsonable(remote),
        indent=2,
        sort_keys=True,
    )

    reached: list[str] = []

    def _spying(verb: str):
        """A SCOPED patch, never ``monkeypatch``. The shared instance's
        ``undo()`` takes no argument and would drop this suite's autouse root
        and credential pins with it (tests/conftest.py's tripwire), and the
        source walk below must read the real verb, not a wrapper."""

        real = getattr(OfficeStore, verb)

        def _spy(self, *args, **kwargs):
            reached.append(verb)
            return real(self, *args, **kwargs)

        return mock.patch.object(OfficeStore, verb, _spy)

    with _spying("adopt_remote_actor"), _spying("upsert_actor"):
        summary = office_sync.apply_office_pull(realm.id, tmp_path / "subtree")

    assert summary.adopted == 1, summary.as_dict()
    assert reached == ["adopt_remote_actor"], (
        "the realm pull's WRITE_REMOTE arm no longer routes through the carved-out "
        f"store verb — it reached {reached}. If it now reaches upsert_actor, the D3 "
        "ruling (a pull is replication, not authoring) was reversed."
    )

    source = inspect.getsource(OfficeStore.adopt_remote_actor)
    assert "_write_actor(" in source
    assert "_guard_class_keyed_" not in source and "class_key_collision(" not in source, (
        "adopt_remote_actor grew a class-key fence — which REVERSES D3 (2026-08-30: "
        "a pull is replication, not authoring). Move its entry from "
        "CARVED_OUT_ACTOR_WRITERS into FENCED_ACTOR_WRITERS with the new ruling, "
        "and delete this test."
    )
    assert ("agent_runtime/office_store.py", "adopt_remote_actor") in CARVED_OUT_ACTOR_WRITERS


# ── witness 4: the fence cannot be blinded by a file it cannot read ─────────


def _duplicate_only_workspace(name: str) -> str:
    """An office authored INSTANCE-keyed from the start: the sibling holds the
    canvas items, and no class key was ever archived.

    Deliberately not the migrated fixture. That one fires
    ``resurrects_archived_class_key`` from the SURFACE, which needs no actor
    directory read at all — so a fence blinded in its duplicate half would still
    refuse there, and the mutation below would stay green.
    """

    workspace = _workspace(name)
    OfficeStore().upsert_actor(
        workspace, _payload("backend_dev", instance=INSTANCE, agent_item_id=INSTANCE)
    )
    assert "backend_dev" not in OfficeStore().get_surface(workspace).archived_actor_keys
    return workspace


def _blind_the_sibling(workspace_id: str) -> pathlib.Path:
    """Make the instance-keyed sibling undecodable — an AV quarantine stub, a
    half-flushed write, a disk error. The row is still THERE; it just cannot be
    read, which is the state ``list_actors`` reports as "absent"."""

    from agent_runtime import paths

    path = paths.office_actor_path(workspace_id, INSTANCE)
    assert path.exists()
    path.write_text("{truncated", encoding="utf-8")
    assert OfficeStore().list_actors(workspace_id) == [], (
        "the list view is supposed to be the blind one — fixture no longer bites"
    )
    return path


def test_an_unreadable_sibling_makes_the_fence_refuse_typed_instead_of_guessing():
    """THE killing test for the fence's blind spot.

    ``class_key_collision``'s duplicate half asked ``list_actors`` whether a live
    sibling already holds these item ids. ``list_actors`` drops files that will
    not decode (``_read_actor_dir`` counts them and moves on), so the honest
    answer "I cannot tell" came back as "no" — for EVERY writer at once, through
    the one predicate — and the class-keyed write landed beside a sibling nobody
    could read. That is the stage's named defect class reached THROUGH the fence:
    an identical sibling nobody can find, including the fence.

    *Probed fields:* the typed refusal class and its code (naming the
    unreadability, not a collision that was never proven), AND that no
    class-keyed actor file exists afterwards, AND that the unreadable file is
    left exactly as found for an operator to repair.

    *Mutation:* point the fence back at ``list_actors``. The scan reports nothing,
    no collision is found, the write LANDS — and the first two probes go red
    together. The fixture is duplicate-only on purpose so no surface-level reason
    can rescue the mutant.
    """

    from agent_runtime import paths
    from agent_runtime.errors import ActorsUnreadable

    workspace = _duplicate_only_workspace("Blinded Fence Office")
    blinded = _blind_the_sibling(workspace)

    with pytest.raises(ActorsUnreadable) as refused:
        OfficeStore().upsert_actor(workspace, _colliding_payload())

    assert refused.value.code == "actors_unreadable"
    assert "actors_unreadable" in str(refused.value)
    assert not paths.office_actor_path(workspace, "backend_dev").exists(), (
        "the fence passed on partial knowledge and the double placement landed"
    )
    assert blinded.read_text(encoding="utf-8") == "{truncated"


def test_the_unreadable_refusal_is_a_hold_and_not_a_collision_on_both_transports():
    """The unreadable refusal is a DIFFERENT answer from a collision, and both
    transports have to say so.

    A client that read "cannot see the directory" as ``class_key_collision``
    would tell the operator to send a ``persona_instance_id`` — advice that
    cannot help, because nothing has been shown to collide. The cure is repairing
    ONE FILE, and it is transient: retrying after the repair is correct, which is
    the opposite of the collision's cure.

    So: the wire spends the ``-32600`` "cannot serve this right now" band with
    ``actors_unreadable`` as the branch (the same band and shape the archive-token
    hold already uses, inherited by subclassing it), and the CLI spends its own
    typed code rather than ``duplicate_conflict`` — and rather than
    ``internal_error``, which is what an unnamed ``AgentRuntimeError`` degrades to
    and which would read as a crash.
    """

    workspace = _duplicate_only_workspace("Blinded Transport Office")
    _blind_the_sibling(workspace)

    rpc = _rpc_upsert("u-blind", workspace, _colliding_payload())
    assert rpc["error"]["code"] == -32600, rpc
    assert rpc["error"]["data"]["reason"] == "actors_unreadable"
    assert rpc["error"]["data"]["workspace_id"] == workspace

    cli = _run_harness(
        "office", "actor-upsert", "--workspace", workspace,
        "--actor-json", json.dumps(_colliding_payload()), "--json",
    )
    error = json.loads(cli.stdout)["error"]
    assert error["code"] == "actors_unreadable", cli.stdout + cli.stderr
    assert cli.returncode != 4, "an unproven collision must not spend the collision exit"
    assert cli.returncode != 0, cli.stdout


def test_an_unreadable_sibling_does_not_refuse_the_writes_it_cannot_be_about():
    """The fence refuses only what it cannot ANSWER, not everything nearby.

    A refusal that fired on any unreadable file would make one quarantined actor
    freeze the whole office — the launcher's save, every create, every template
    apply — which is a worse failure than the one being prevented and would get
    the check deleted. Two writes must still go through with the same
    undecodable file on disk: an INSTANCE-keyed write (the migration's shape,
    which the fence never inspects the directory for) and the archived-key
    question, which is answered from the SURFACE.
    """

    workspace = _duplicate_only_workspace("Blinded Bystander Office")
    _blind_the_sibling(workspace)

    other = "personainst_backend_dev_agent_deadbeef"
    actor = OfficeStore().upsert_actor(
        workspace,
        {
            "persona_id": "backend_dev",
            "persona_instance_id": other,
            "items": [{"item_id": other, "kind": "agent", "position": [4.0, 4.0]}],
        },
    )
    assert actor.actor_key == other

    # A class-keyed write whose items overlap NOTHING still needs the scan (it
    # cannot know that without reading), so it is held — and a class-keyed write
    # into a DIFFERENT, readable workspace is untouched.
    clean = _workspace("Blinded Bystander Clean")
    OfficeStore().ensure_surface(clean, created_by="seed")
    assert OfficeStore().upsert_actor(clean, _colliding_payload()).actor_key == "backend_dev"


# ── the sanctioned overrides, still working ─────────────────────────────────


def test_the_stores_override_parameter_is_what_lets_a_consenting_write_through():
    """The override is a PARAMETER, and it is the only way past the fence.

    EG-6.6's shape rule: an override has to be a value someone passed on the
    record, never a caller that skips the guard — the two are indistinguishable
    from the store's side, and "indistinguishable from forgetting" is how a fence
    at four call sites becomes a fence at three.

    Both halves are asserted: without it the same write is refused, with it the
    class key really lands beside the instance-keyed sibling (the double
    placement becomes a recorded decision rather than an accident).
    """

    from agent_runtime.office_class_key_guard import ClassKeyedPlacementRefused

    workspace, _items = _migrated_workspace("Consent Office")

    with pytest.raises(ClassKeyedPlacementRefused):
        OfficeStore().upsert_actor(workspace, _colliding_payload())
    assert _keys(workspace) == {INSTANCE}

    OfficeStore().upsert_actor(
        workspace, _colliding_payload(), allow_class_key=True, resurrect=True
    )
    assert _keys(workspace) == {"backend_dev", INSTANCE}


def test_actor_restore_still_un_archives_the_key_the_refusal_points_at():
    """The fence's advice has to lead somewhere.

    ``refusal_message`` tells the operator to use ``harness office
    actor-restore``. ``restore_actor`` writes a live actor file with no class-key
    fence, which the enumeration above dispositions as the sanctioned override
    rather than a hole — and this is the behaviour that disposition rests on. If
    the hoist had fenced it too, every refusal in this file would be naming a
    dead end.
    """

    workspace, _items = _migrated_workspace("Restore Exit Office")
    assert "backend_dev" in OfficeStore().get_surface(workspace).archived_actor_keys

    restored = OfficeStore().restore_actor(workspace, "backend_dev")

    assert restored.actor_key == "backend_dev"
    assert restored.state == "active"
    assert _keys(workspace) == {"backend_dev", INSTANCE}
    assert "backend_dev" not in OfficeStore().get_surface(workspace).archived_actor_keys
