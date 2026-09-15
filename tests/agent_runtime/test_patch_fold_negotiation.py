"""Patch-lane capability negotiation: the client declares what it can fold.

The defect this closes. ``patch_coverage`` decided promotion purely on the OP
(``upsert``/``remove`` fold, ``refresh`` does not). Nothing looked at the
ENTITY — but the consumer's fold table has exactly two entries
(``mission_read_model.dart``: ``persona_instance -> persona_instances``,
``incident -> incidents``) and an unknown entity returns ``needsResync``, i.e.
"re-hydrate from a fresh checkpoint". So the first patch emitted for a third
entity would have made every such frame cost a connected launcher a FULL
re-hydrate — strictly worse than the full-core delta the patch replaced, because
the client pays the patch and then the core anyway.

So the client now declares its fold set and the producer promotes only within
it. The load-bearing default is that **absence is the historical
``{persona_instance, incident}``, not the empty set**: absence is what every
client in the field sends, and the historical set is exactly today's wire.

Anti-vacuity is the discipline these tests are written under: a demotion test
that passes because nothing was promotable at all proves nothing, so every
demotion assertion is paired with the SAME batch promoting under a wider
declaration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.patch_coverage import (
    HISTORICAL_FOLD_ENTITIES,
    PERSONA_INSTANCE_CREATE_CAPABILITY,
    accepted_fold_entities,
    batch_is_patch_coverable,
    event_is_patch_coverable,
    normalize_fold_entities,
    parse_fold_entities_option,
)
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.state_patches import (
    PATCH_OP_REFRESH,
    PATCH_OP_REMOVE,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
)
from agent_runtime.stream import hydrate_frame, stream_frames
from tests.agent_runtime.persona_instance_mint import mint_free_floating

#: An entity NO client folds today. Every "undeclared" case below uses it, so a
#: test can never accidentally pass by naming something the historical default
#: already covers.
UNFOLDED_ENTITY = "office_actor"


@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for BOTH the producer chokepoints
    (root-pinned via ``load_root_runtime_config``) and the stream lane —
    the same fixture ``test_stream_patch.py`` uses."""

    from agent_runtime import state_patches as sp
    from agent_runtime import stream as st
    from agent_runtime.config import load_agent_runtime_config

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        monkeypatch.setattr(sp, "load_root_runtime_config", _loader)
        monkeypatch.setattr(st, "delta_patches_enabled", lambda config=None: enabled)

    return _apply


def _patch_event(entity: str, entity_id: str, op: str = PATCH_OP_UPSERT, changed=None) -> Event:
    payload = {"entity": entity, "id": entity_id, "op": op}
    if changed is not None:
        payload["changed"] = changed
    return Event(
        ts=datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc),
        type=STATE_PATCHED_EVENT_TYPE,
        task_id=None,
        run_id=None,
        persona_id=None,
        payload=payload,
    )


def _instance_upsert(entity_id: str = "personainst_a") -> Event:
    return _patch_event("persona_instance", entity_id, PATCH_OP_UPSERT, {"display_name": "A"})


# --------------------------------------------------------------------------- #
# The default: absence is the HISTORICAL set, never the empty set
# --------------------------------------------------------------------------- #
def test_absence_resolves_to_the_historical_set_and_an_explicit_empty_does_not():
    """The single most load-bearing line of this change.

    ``None`` (the client said nothing — what every fielded client sends) must
    resolve to the historical set. Had it resolved to the empty set, the day
    this landed every connected launcher would have silently dropped to full
    cores forever: an 822 KB frame where the patch is 486 bytes, which is the
    exact regression the S7-A lane already suffered once from a config that
    resolved somewhere nobody read.

    An EXPLICIT empty declaration is a different statement — "I fold nothing" —
    and must stay distinguishable from saying nothing at all.
    """

    assert HISTORICAL_FOLD_ENTITIES == frozenset({"persona_instance", "incident"})
    assert normalize_fold_entities(None) == HISTORICAL_FOLD_ENTITIES
    assert normalize_fold_entities(None), "absence must NOT be the empty set"
    assert normalize_fold_entities([]) == frozenset()
    assert normalize_fold_entities(["persona_instance"]) == frozenset({"persona_instance"})


def test_the_historical_default_only_names_entities_the_wire_actually_folds():
    """One-directional cross-repo pin against the byte-shared coverage manifest.

    Every entity we default a silent client into must be one the fold contract
    calls foldable. Deliberately NOT an equality: the manifest may legitimately
    grow a new foldable entity in a cross-stack landing BEFORE every client in
    the field folds it, and that is precisely the moment the historical default
    must not move on its own.
    """

    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "stream_frames"
            / "patch_coverage_manifest.json"
        ).read_text(encoding="utf-8")
    )
    foldable_entities = {
        case["entity"] for case in manifest["cases"] if case.get("foldable") and case.get("entity")
    }
    assert foldable_entities, "manifest names no foldable entity — the pin would be vacuous"
    assert HISTORICAL_FOLD_ENTITIES <= foldable_entities, (
        "the historical default names an entity the fold contract does not call "
        f"foldable: {sorted(HISTORICAL_FOLD_ENTITIES - foldable_entities)}"
    )


def test_the_default_call_shape_every_existing_caller_uses_is_unchanged():
    """Byte-for-byte compatibility, stated at the classifier boundary.

    Every call site that existed before this change passes no ``fold_entities``.
    That call must classify a historical-entity batch exactly as it did, and the
    kwargless call and the explicit historical call must be indistinguishable.
    """

    batch = [_instance_upsert(), _patch_event("incident", "inc_1", PATCH_OP_REMOVE)]
    assert batch_is_patch_coverable(batch) is True
    assert batch_is_patch_coverable(batch, fold_entities=None) is True
    assert batch_is_patch_coverable(batch, fold_entities=HISTORICAL_FOLD_ENTITIES) is True
    for event in batch:
        assert event_is_patch_coverable(event) is True


# --------------------------------------------------------------------------- #
# The classifier: declared promotes, undeclared demotes the WHOLE batch
# --------------------------------------------------------------------------- #
def test_a_batch_naming_only_declared_entities_stays_coverable():
    batch = [_instance_upsert()]
    assert batch_is_patch_coverable(batch, fold_entities=["persona_instance"]) is True
    assert batch_is_patch_coverable(batch, fold_entities=["persona_instance", UNFOLDED_ENTITY]) is True


def test_one_undeclared_entity_demotes_the_whole_batch():
    """Conservative-by-construction, extended from the op rule to the entity.

    The batch below is entirely foldable at the OP level and one of its two
    entries is declared — so a rule that demoted only the offending ENTRY would
    still ship a patch frame, and the launcher would re-hydrate on it. The whole
    batch must go.
    """

    declared = _instance_upsert()
    undeclared = _patch_event(UNFOLDED_ENTITY, "office_1", PATCH_OP_UPSERT, {"label": "x"})

    # Anti-vacuity FIRST: this exact batch is promotable when both are declared,
    # so the demotion below can only be the entity gate.
    assert (
        batch_is_patch_coverable(
            [declared, undeclared], fold_entities=["persona_instance", UNFOLDED_ENTITY]
        )
        is True
    )
    assert (
        batch_is_patch_coverable([declared, undeclared], fold_entities=["persona_instance"])
        is False
    )
    # And under the historical default (what a fielded client gets) it is also
    # demoted — the entity is in nobody's fold table.
    assert batch_is_patch_coverable([declared, undeclared]) is False


def test_the_op_rules_run_first_and_are_unchanged_by_the_entity_gate():
    """A declared entity does not rescue an unfoldable op."""

    refresh = _patch_event("persona_instance", "personainst_a", PATCH_OP_REFRESH)
    assert batch_is_patch_coverable([refresh], fold_entities=["persona_instance"]) is False
    # A covered domain event carries no fold state of its own and is not
    # entity-gated; the gate lives on the paired ``state.patched``.
    steered = Event(
        ts=datetime(2026, 8, 13, 12, 0, 1, tzinfo=timezone.utc),
        type="persona_instance.steered",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={},
    )
    assert batch_is_patch_coverable([_instance_upsert(), steered], fold_entities=["persona_instance"])


def test_a_persona_instance_create_is_gated_and_a_subset_upsert_is_not():
    """D3's gate, both ways, on payloads that differ ONLY by the stamp.

    ``persona_instance`` has been declared by every fielded launcher since S7-A,
    and every upsert under it was a subset merge. D3 adds a complete-row upsert
    stamped ``created: true`` for a row the client does not hold — which a
    fielded fold answers with ``patch_without_target`` and a full re-hydrate, so
    the client pays the patch AND the core. Hence the second capability token.

    The two payloads below are byte-identical apart from ``created``, which is
    what makes this a test of the GATE rather than of the entity rule: if the
    gate were removed, the create becomes coverable under bare
    ``persona_instance`` and the first assertion goes red; if the gate were
    widened to the whole entity, the subset upsert stops being coverable and the
    third goes red.
    """

    create = _patch_event(
        "persona_instance", "personainst_new", PATCH_OP_UPSERT, {"display_name": "A"}
    )
    create.payload["created"] = True
    subset = _instance_upsert("personainst_new")

    # A fielded declaration: the create demotes, the subset still promotes.
    assert event_is_patch_coverable(create, fold_entities=HISTORICAL_FOLD_ENTITIES) is False
    assert (
        event_is_patch_coverable(subset, fold_entities=HISTORICAL_FOLD_ENTITIES) is True
    )
    # With the token, the create promotes too.
    widened = HISTORICAL_FOLD_ENTITIES | {PERSONA_INSTANCE_CREATE_CAPABILITY}
    assert event_is_patch_coverable(create, fold_entities=widened) is True
    # And the token alone is not an entity — it does not promote anything by
    # itself, which is what keeps it inert as a set member on an old runtime.
    assert (
        event_is_patch_coverable(
            create, fold_entities=frozenset({PERSONA_INSTANCE_CREATE_CAPABILITY})
        )
        is False
    )
    assert PERSONA_INSTANCE_CREATE_CAPABILITY not in HISTORICAL_FOLD_ENTITIES


def test_a_created_false_or_absent_stamp_is_never_treated_as_a_create():
    """The stamp is read as an identity, not as truthiness.

    ``created`` is optional and additive: a producer that has no opinion omits
    it. Treating a missing or ``False`` stamp as a create would demote every
    subset upsert every fielded launcher folds today — a silent return to full
    cores for the lane whose whole measured value is 486 bytes against 822,671.

    Kill-mutation: change the gate's ``is True`` to a truthiness check and pass
    ``created: 0`` / ``created: "false"``; or invert it to ``is not False``.
    """

    for stamp in (False, None, 0, "", "true"):
        event = _instance_upsert("personainst_stamped")
        event.payload["created"] = stamp
        assert (
            event_is_patch_coverable(event, fold_entities=HISTORICAL_FOLD_ENTITIES) is True
        ), stamp


def test_a_persona_instance_remove_is_not_create_gated():
    """Only the create-upsert is widened; ``remove`` stays where it was.

    Deleting a row a client may not hold is idempotent, and every fielded fold
    already treats a missing target as a clean no-op rather than a resync
    (``mission_read_model.dart``'s generic remove). Gating it would demote the
    ``persona_instance.retired`` half of the DELETE gesture that O-H3 promoted —
    a regression inside the stage that pays for this one.
    """

    remove = _patch_event("persona_instance", "personainst_gone", PATCH_OP_REMOVE)
    assert event_is_patch_coverable(remove, fold_entities=HISTORICAL_FOLD_ENTITIES) is True


def test_a_malformed_entity_demotes_rather_than_slipping_through():
    for payload_entity in (None, "", 17):
        event = Event(
            ts=datetime(2026, 8, 13, 12, 0, 2, tzinfo=timezone.utc),
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"entity": payload_entity, "id": "x", "op": PATCH_OP_UPSERT, "changed": {"a": 1}},
        )
        assert batch_is_patch_coverable([event]) is False


# --------------------------------------------------------------------------- #
# The SHARED producer: intersection, not union
# --------------------------------------------------------------------------- #
def test_the_shared_producer_accepts_the_intersection_of_its_subscribers():
    """One producer feeds N subscribers, so a promotion must be safe for ALL.

    The union would be the original bug pointed at a different client: the
    launcher sitting beside the new client would receive a frame it answers with
    a re-hydrate.
    """

    assert accepted_fold_entities([]) == HISTORICAL_FOLD_ENTITIES, "an empty room is today's wire"
    assert accepted_fold_entities([None]) == HISTORICAL_FOLD_ENTITIES
    # A silent legacy client beside one that declared a new entity: the new
    # entity is NOT accepted, and what they share is.
    accepted = accepted_fold_entities([None, ["persona_instance", UNFOLDED_ENTITY]])
    assert accepted == frozenset({"persona_instance"})
    assert UNFOLDED_ENTITY not in accepted
    # Anti-vacuity: without the silent client the same declaration IS accepted.
    assert UNFOLDED_ENTITY in accepted_fold_entities([["persona_instance", UNFOLDED_ENTITY]])
    # An explicit "I fold nothing" narrows the room to nothing.
    assert accepted_fold_entities([[], None]) == frozenset()


# --------------------------------------------------------------------------- #
# The wire: assert the FRAME KIND, end to end
# --------------------------------------------------------------------------- #
def _stream(**kwargs):
    return stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=60, delta_debounce_seconds=0, **kwargs
    )


def _append_unfolded_patch() -> None:
    """Append a ``state.patched`` for an entity no client folds.

    Written straight to the log on purpose: no chokepoint emits this entity yet
    (this change makes negotiation possible, it does not enable an entity), and
    the wire behaviour must be pinned BEFORE one does — a promotion rule tested
    only against entities that cannot be produced is a rule tested against
    nothing.
    """

    EventLog().append(_patch_event(UNFOLDED_ENTITY, "office_1", PATCH_OP_UPSERT, {"label": "x"}))


def test_absence_still_promotes_a_persona_instance_batch_to_a_patch_frame(
    set_delta_patches, isolate_agent_runtime_root
):
    """The un-updated client keeps exactly today's wire: it declares nothing and
    its steer still arrives as a patch frame, with the same op payload."""

    set_delta_patches(True)
    store = PersonaInstanceStore()
    parent = mint_free_floating("profile:parent", store=store)
    child = mint_free_floating("profile:child", store=store)

    frames = _stream(max_frames=2)
    hydrate = next(frames)
    assert hydrate["type"] == "hydrate"
    assert hydrate.get("delta_patches") is True

    store.set_parents(child.id, [parent.id])
    frame = next(frames)
    assert frame["type"] == "patch", f"expected a patch frame, got {frame['type']}"
    assert "core" not in frame
    patches = [p for p in frame["patches"] if p["entity"] == "persona_instance"]
    assert patches and patches[0]["op"] == PATCH_OP_UPSERT
    assert child.id in {p["id"] for p in patches}


def test_an_undeclared_entity_demotes_the_frame_to_a_full_core(
    set_delta_patches, isolate_agent_runtime_root
):
    """The frame KIND, not a boolean: the batch that would have shipped as a
    ``patch`` ships as a full-core ``delta`` — the honest fallback that already
    exists, not a new lane."""

    set_delta_patches(True)
    mint_free_floating("profile:a")

    frames = _stream(max_frames=2, fold_entities=["persona_instance"])
    assert next(frames)["type"] == "hydrate"
    _append_unfolded_patch()
    frame = next(frames)
    assert frame["type"] == "delta", f"expected the full-core fallback, got {frame['type']}"
    assert "core" in frame, "the fallback must carry the core the client will need"


def test_the_same_batch_is_promoted_once_the_client_declares_the_entity(
    set_delta_patches, isolate_agent_runtime_root
):
    """Anti-vacuity for the test above: the identical event, promoted.

    Without this, the demotion assertion would pass just as well against a
    producer that had stopped promoting anything at all.
    """

    set_delta_patches(True)
    mint_free_floating("profile:a")

    frames = _stream(max_frames=2, fold_entities=["persona_instance", UNFOLDED_ENTITY])
    assert next(frames)["type"] == "hydrate"
    _append_unfolded_patch()
    frame = next(frames)
    assert frame["type"] == "patch", f"expected a patch frame, got {frame['type']}"
    ((patch,)) = [p for p in frame["patches"] if p["entity"] == UNFOLDED_ENTITY]
    assert patch["id"] == "office_1"


def test_an_explicit_empty_declaration_takes_every_batch_to_a_full_core(
    set_delta_patches, isolate_agent_runtime_root
):
    """"I fold nothing" is sayable, and it is not the same as saying nothing."""

    set_delta_patches(True)
    store = PersonaInstanceStore()
    parent = mint_free_floating("profile:parent", store=store)
    child = mint_free_floating("profile:child", store=store)

    frames = _stream(max_frames=2, fold_entities=[])
    hydrate = next(frames)
    assert hydrate["fold_entities"] == [], "the echo must report the empty set it accepted"
    store.set_parents(child.id, [parent.id])
    frame = next(frames)
    assert frame["type"] == "delta" and "core" in frame


# --------------------------------------------------------------------------- #
# The hydrate echo (the handshake's missing direction)
# --------------------------------------------------------------------------- #
def test_the_hydrate_echoes_the_accepted_set_and_stays_silent_with_the_flag_off():
    core = {"parity": {"watermark": {"event_offset": 7}}}

    off = hydrate_frame(core, delta_patches=False, fold_entities=["persona_instance"])
    assert "delta_patches" not in off
    assert "fold_entities" not in off, (
        "a flag-off hydrate must stay byte-identical — the echo may not appear "
        "on a lane that is not running"
    )

    on = hydrate_frame(core, delta_patches=True)
    assert on["delta_patches"] is True
    assert on["fold_entities"] == sorted(HISTORICAL_FOLD_ENTITIES) == ["incident", "persona_instance"]

    narrowed = hydrate_frame(core, delta_patches=True, fold_entities=["persona_instance"])
    assert narrowed["fold_entities"] == ["persona_instance"]


# --------------------------------------------------------------------------- #
# The stdio flag, parsed and threaded end to end
# --------------------------------------------------------------------------- #
def test_the_flag_parses_and_reaches_stream_frames(monkeypatch, capsys):
    """``harness stream --fold-entities`` → ``stream_frames(fold_entities=…)``.

    Driven through the real parser and the real command function, because the
    two failure modes worth catching are a flag that parses into an attribute
    nobody reads and a command that reads an attribute the parser never sets.
    """

    import agent_runtime.stream as stream_module
    from hermes_cli.harness import build_parser

    seen: dict[str, object] = {}

    def _fake_stream_frames(**kwargs):
        seen.update(kwargs)
        return iter(())

    monkeypatch.setattr(stream_module, "stream_frames", _fake_stream_frames)

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))

    args = parser.parse_args(
        ["harness", "stream", "--fold-entities", "persona_instance, office_actor", "--max-frames", "1"]
    )
    assert args.fold_entities == "persona_instance, office_actor"
    assert args.func(args) == 0
    assert seen["fold_entities"] == frozenset({"persona_instance", "office_actor"})

    # And the absence of the flag stays ABSENT all the way down — the historical
    # default is resolved by the producer, never guessed at the CLI boundary.
    seen.clear()
    bare = parser.parse_args(["harness", "stream", "--max-frames", "1"])
    assert bare.fold_entities is None
    assert bare.func(bare) == 0
    assert seen["fold_entities"] is None
    capsys.readouterr()


def test_the_comma_option_keeps_absent_and_empty_apart():
    assert parse_fold_entities_option(None) is None
    assert parse_fold_entities_option("") == frozenset()
    assert parse_fold_entities_option("  ") == frozenset()
    assert parse_fold_entities_option("persona_instance,, incident ") == frozenset(
        {"persona_instance", "incident"}
    )
