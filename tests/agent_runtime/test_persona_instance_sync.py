"""H1 — the persona-instance record split, as code.

The centre of this file is :func:`test_every_persona_instance_field_is_classified`.
Everything else guards one door; that one guards the SPLIT, which is the thing
a future field can rot silently. The plan
(``docs/mission_control/planned/instance-replication.md`` §1) classifies 32
fields by hand; this asserts the classification is total over
``dataclasses.fields(PersonaInstance)`` so a field added tomorrow cannot compile
green until somebody decides which side of the machine boundary it lives on.

Everything here is pure: no store, no git, no realm. Records are constructed
in-memory and the projection/admission functions take them as arguments, which
is the property H1 was scoped to have.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest
import yaml

from agent_runtime.models import PersonaInstance
from agent_runtime.persona_instance_sync import (
    PERSONA_INSTANCE_ALLOWED_KEYS,
    PERSONA_INSTANCE_DERIVED_KEYS,
    PERSONA_INSTANCE_LOCAL_ONLY_KEYS,
    PERSONA_INSTANCE_NEVER_TRAVELS_KEYS,
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    REFUSAL_CANONICAL_CHANNEL,
    REFUSAL_INCOMPLETE,
    REFUSAL_INVALID_INSTANCE_ID,
    REFUSAL_STEERING_SHAPE,
    REFUSAL_UNEXPECTED_KEY,
    cleared_travel_value,
    persona_instance_def_hash,
    project_persona_instance,
    project_persona_instances,
    read_projection_document,
    refuse_persona_instance,
    valid_persona_instance_id,
)
from agent_runtime.states import WorkerSessionState


def _placement_instance(**overrides) -> PersonaInstance:
    """A placement-backed row — the ONLY shape that replicates (plan §2)."""

    base = dict(
        id="personainst_dev_agent_9682caf4",
        persona_id="dev",
        role="developer",
        display_name="Neko",
        profile_id="alice",
        runtime_root=r"X:\Eternia\.hermes\agent-runtime",
        state=WorkerSessionState.IDLE,
        realm_id="realm_home",
        workspace_id="ws_testv4_afb811",
    )
    base.update(overrides)
    return PersonaInstance(**base)


# --- THE anti-drift gate -----------------------------------------------------


def test_every_persona_instance_field_is_classified():
    """THE point of H1. Every field is in exactly one of travels /
    derived-on-mint / local-only, and the three cover the record.

    A new field fails this until it is classified, which is the only mechanism
    that keeps "runtime state never travels" from decaying into an English
    category somebody re-interprets per field."""

    names = {item.name for item in dataclasses.fields(PersonaInstance)}
    assert len(names) == 32, "the plan's tables classify exactly 32 fields"

    unclassified = names - (
        PERSONA_INSTANCE_ALLOWED_KEYS
        | PERSONA_INSTANCE_DERIVED_KEYS
        | PERSONA_INSTANCE_LOCAL_ONLY_KEYS
    )
    assert unclassified == set(), (
        f"unclassified PersonaInstance field(s): {sorted(unclassified)} — decide "
        "whether each travels, is re-derived on mint, or is local-only, and add "
        "it to the matching set in agent_runtime/persona_instance_sync.py"
    )

    invented = (
        PERSONA_INSTANCE_ALLOWED_KEYS
        | PERSONA_INSTANCE_DERIVED_KEYS
        | PERSONA_INSTANCE_LOCAL_ONLY_KEYS
    ) - names
    assert invented == set(), f"classified name(s) that are not fields: {sorted(invented)}"

    assert PERSONA_INSTANCE_ALLOWED_KEYS & PERSONA_INSTANCE_NEVER_TRAVELS_KEYS == set()
    assert PERSONA_INSTANCE_DERIVED_KEYS & PERSONA_INSTANCE_LOCAL_ONLY_KEYS == set()
    # The plan's counts, pinned so a silent reclassification reads as a change.
    assert len(PERSONA_INSTANCE_ALLOWED_KEYS) == 14
    assert len(PERSONA_INSTANCE_NEVER_TRAVELS_KEYS) == 18
    assert len(PERSONA_INSTANCE_DERIVED_KEYS) == 6


def test_the_fields_the_ruling_named_are_on_the_side_it_named():
    """The ruling's own sentence, field by field: identity and authored
    definition travel; sessions, worktrees, credentials and machine roots do
    not. Named individually because a count cannot catch a SWAP."""

    for name in ("id", "persona_id", "display_name", "realm_id", "workspace_id"):
        assert name in PERSONA_INSTANCE_ALLOWED_KEYS
    for name in ("model", "provider", "api_mode", "reasoning_effort", "model_override_issued_at"):
        assert name in PERSONA_INSTANCE_ALLOWED_KEYS, "the override tier and its clock travel together"
    for name in ("runtime_root", "default_chat_session_id", "session_id", "chat_head_home"):
        assert name in PERSONA_INSTANCE_NEVER_TRAVELS_KEYS
    for name in ("active_run_id", "current_task_id", "current_assignment_id", "goal_id", "returned_to"):
        assert name in PERSONA_INSTANCE_LOCAL_ONLY_KEYS, "a live execution binding is never imported"
    # `[AUDIT]` 2026-08-31: ruled never-travels by the survey's own safe-default
    # argument (a wrong "travels" is a clobber; a wrong "never" is an absence).
    assert "current_chat_goal" in PERSONA_INSTANCE_LOCAL_ONLY_KEYS
    # Derived rather than carried, so a stale copy cannot shadow the persona
    # definition that arrived in the SAME pull.
    assert {"role", "profile_id"} <= PERSONA_INSTANCE_DERIVED_KEYS


def test_every_travelling_field_has_a_cleared_value_for_the_adopt_arm():
    """Adoption takes the remote's travelling SURFACE wholesale, so a field the
    publisher cleared must be clearable here. ``id``/``persona_id`` are excluded
    by construction — an admitted body must carry both."""

    for name in sorted(PERSONA_INSTANCE_ALLOWED_KEYS - {"id", "persona_id"}):
        cleared_travel_value(name)  # raises if a field has no defined cleared value
    assert cleared_travel_value("display_name") == ""
    assert cleared_travel_value("steered_by") == []
    assert cleared_travel_value("mode") == "configured"
    assert cleared_travel_value("model") is None


# --- projection ---------------------------------------------------------------


def test_the_projection_carries_the_travelling_fields_and_drops_runtime_root():
    dropped: list[str] = []
    body = project_persona_instance(_placement_instance(), dropped=dropped)

    assert body["id"] == "personainst_dev_agent_9682caf4"
    assert body["persona_id"] == "dev"
    assert body["display_name"] == "Neko"
    assert body["realm_id"] == "realm_home"
    assert body["workspace_id"] == "ws_testv4_afb811"
    assert set(body) <= PERSONA_INSTANCE_ALLOWED_KEYS

    # The most portability-hostile field on the record is REPORTED as withheld,
    # not silently absent — the dropped-keys precedent.
    assert "instances.personainst_dev_agent_9682caf4.runtime_root" in dropped
    assert "instances.personainst_dev_agent_9682caf4.default_chat_session_id" in dropped
    assert "runtime_root" not in body
    assert "role" not in body
    assert "profile_id" not in body


def test_a_live_binding_never_reaches_the_wire():
    """Importing a peer's run binding would make a replica look busy with a run
    this machine has never heard of — and ``_has_live_binding`` reads exactly
    these fields to refuse retiring a working agent."""

    body = project_persona_instance(
        _placement_instance(
            active_run_id="run_abc",
            current_task_id="task_abc",
            current_assignment_id="asg_abc",
            goal_id="goal_abc",
            returned_to="personainst_other",
            default_chat_session_id="persona_chat_x_0123456789ab",
            chat_head_home="alice",
            session_id="persona_chat_legacy",
            current_chat_goal="ship the thing",
        )
    )
    for name in (
        "active_run_id",
        "current_task_id",
        "current_assignment_id",
        "goal_id",
        "returned_to",
        "default_chat_session_id",
        "chat_head_home",
        "session_id",
        "current_chat_goal",
    ):
        assert name not in body


def test_the_supersession_clock_travels_as_a_parseable_timestamp():
    """``model_override_issued_at`` must travel WITH the four override fields it
    orders, and it must come back as a datetime — a clock that arrives as an
    unparseable blob lets a stale local write silently win."""

    from agent_runtime.serde import from_jsonable

    issued = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    body = project_persona_instance(
        _placement_instance(model="claude-x", provider="anthropic", model_override_issued_at=issued)
    )
    assert isinstance(body["model_override_issued_at"], str)
    round_tripped = from_jsonable(PersonaInstance, {**body, "role": "", "profile_id": None,
                                                   "runtime_root": "", "state": "idle"})
    assert round_tripped.model_override_issued_at == issued


def test_the_projection_is_deterministic_and_lf():
    """Republishing an unchanged projection must be a byte-for-byte no-op or the
    publish change-detector starts reporting churn.

    RED-PROOF NOTE, so the next reader does not conclude this is uncovered:
    determinism has TWO mechanisms — the walk sorts its ids/field names, and
    ``yaml.safe_dump(sort_keys=True)`` sorts the dump — and they are genuinely
    redundant. Removing either ALONE leaves this green (measured); removing BOTH
    reds it. That redundancy is the point rather than an accident, so the test
    claims the observable property (sorted, LF, order-independent bytes) instead
    of one of the two lines that produce it."""

    records = {
        "personainst_dev_agent_9682caf4": _placement_instance(),
        "personainst_qa_agent_11112222": _placement_instance(
            id="personainst_qa_agent_11112222", persona_id="qa", display_name="QA"
        ),
    }
    first = project_persona_instances(sorted(records), records=records).to_bytes()
    second = project_persona_instances(sorted(records, reverse=True), records=records).to_bytes()
    assert first == second
    assert b"\r" not in first
    assert yaml.safe_load(first.decode("utf-8"))["kind"] == PROJECTION_KIND

    # The dump itself must SORT, not merely inherit whatever insertion order the
    # walk happened to build. Order-independence of the input set does not prove
    # that: both calls above build their dict the same way, so a dump that
    # preserved insertion order would pass while a differently-ordered record
    # map republished a byte-different file for identical content.
    text = first.decode("utf-8")
    assert text.index("instances:") < text.index("kind:") < text.index("schema_version:")
    assert text.index("personainst_dev_agent_9682caf4") < text.index("personainst_qa_agent_11112222")
    reversed_records = dict(reversed(list(records.items())))
    assert project_persona_instances(sorted(records), records=reversed_records).to_bytes() == first


def test_a_canonical_row_is_skipped_not_refused():
    """Replication is scoped to placement-backed rows (plan §2). Every machine
    derives its own canonical channel, so withholding it is the scoping ruling
    working — a different fact from a refusal, and it gets its own word."""

    canonical = _placement_instance(id="personainst_dev", persona_id="dev")
    projection = project_persona_instances(["personainst_dev"], records={"personainst_dev": canonical})
    assert projection.instances == {}
    assert projection.skipped_canonical == ["personainst_dev"]
    assert projection.refused == []


def test_a_machine_shaped_display_name_refuses_the_record_rather_than_shipping_it():
    """Withholding ``runtime_root`` is the allowlist's job. Catching an absolute
    path that reached the wire through an AUTHORED field is this one's."""

    record = _placement_instance(display_name=r"X:\Eternia\checkout")
    projection = project_persona_instances([record.id], records={record.id: record})
    assert projection.instances == {}
    assert [row["code"] for row in projection.refused] == ["nonportable_path"]


def test_a_wanted_id_with_no_record_is_reported_missing():
    projection = project_persona_instances(["personainst_ghost_agent_1"], records={})
    assert projection.missing == ["personainst_ghost_agent_1"]
    assert projection.instances == {}


def test_the_hash_is_key_order_independent_and_moves_with_the_override_clock():
    body = {"id": "personainst_a_agent_1", "persona_id": "a", "model": "m"}
    assert persona_instance_def_hash(body) == persona_instance_def_hash(
        {"persona_id": "a", "model": "m", "id": "personainst_a_agent_1"}
    )
    assert persona_instance_def_hash(body) != persona_instance_def_hash(
        {**body, "model_override_issued_at": "2026-08-31T12:00:00.000000Z"}
    )


def test_an_older_publishers_subtree_reads_as_absent_not_as_empty():
    """Absence is never a removal (plan §3.3 ``upstream_absent``), and the
    launcher's whole version-skew story keys on this distinction."""

    assert read_projection_document(None) is None
    assert read_projection_document({"kind": "something_else"}) is None
    assert read_projection_document({"kind": PROJECTION_KIND, "instances": {}}) == {}
    assert read_projection_document({"kind": PROJECTION_KIND, "instances": {"a": {"id": "a"}}}) == {
        "a": {"id": "a"}
    }


def test_the_published_path_is_unknown_to_older_hermes():
    """``store/persona_instances.yaml`` must fall through
    ``_destination_for_sync_path`` to ``None`` so an old member SKIPS the
    artifact instead of writing it somewhere wrong."""

    from agent_runtime.realm_sync import _destination_for_sync_path

    assert PROJECTION_RELATIVE_PATH == "store/persona_instances.yaml"
    assert _destination_for_sync_path(PROJECTION_RELATIVE_PATH) is None


# --- admission (plan §4) ------------------------------------------------------


def _admissible_body(**overrides) -> dict:
    body = {
        "id": "personainst_dev_agent_9682caf4",
        "persona_id": "dev",
        "display_name": "Neko",
        "mode": "configured",
        "realm_id": "realm_home",
        "workspace_id": "ws_testv4_afb811",
    }
    body.update(overrides)
    return body


def test_an_admissible_row_passes_the_door():
    assert refuse_persona_instance("personainst_dev_agent_9682caf4", _admissible_body()) is None


def test_the_door_refuses_runtime_root_as_a_machine_shaped_value():
    """The single most portability-hostile field on the record. It cannot be
    projected (the allowlist drops it) — this is the pull-side half, against a
    hostile or older publisher that put it on the wire anyway."""

    refusal = refuse_persona_instance(
        "personainst_dev_agent_9682caf4",
        _admissible_body(runtime_root=r"X:\Eternia\.hermes"),
    )
    assert refusal is not None
    assert refusal.code == "nonportable_path"


def test_the_door_scans_display_name_because_an_instance_body_is_all_wiring():
    """``prose_keys=frozenset()``, the persona-definition lane's argument: an
    instance body's keys ARE an allowlist, so nothing in it is exempt prose.

    ``display_name`` is in the SHARED ``PROSE_KEYS`` set — where it belongs, for
    a board card description or an office folder name. Inheriting that default
    here would leave the one authored free-text field on the record as an
    unscanned channel for a machine path."""

    from agent_runtime.sync_admission import PROSE_KEYS

    assert "display_name" in PROSE_KEYS, "the shared default exempts it; this lane must not"
    refusal = refuse_persona_instance(
        "personainst_dev_agent_9682caf4",
        _admissible_body(display_name="/home/tony/eternia/checkout"),
    )
    assert refusal is not None
    assert refusal.code == "nonportable_path"


@pytest.mark.parametrize(
    "body",
    [
        # Pass 1: the raw JSON dump — a secret embedded INSIDE a value.
        _admissible_body(display_name="api_key: sk-live-abcdef123456"),
        # Pass 2: the flattened assignment render — a secret carried as a FIELD,
        # which JSON quoting (``"token": "…"``) hides from ``\btoken\b\s*[:=]``.
        _admissible_body(mode={"token": "sk-live-abcdef123456"}),
    ],
    ids=["embedded_in_value", "carried_as_field"],
)
def test_the_door_refuses_secret_shaped_content_through_both_scanner_passes(body):
    refusal = refuse_persona_instance("personainst_dev_agent_9682caf4", body)
    assert refusal is not None
    assert refusal.code == "secret_shaped_value"


def test_the_door_refuses_a_canonical_channel_id_from_a_peer():
    """Canonical rows are derived locally on every machine from a persona id
    that already travels; a peer writing one is an older scheme or an attack."""

    refusal = refuse_persona_instance(
        "personainst_dev", _admissible_body(id="personainst_dev", persona_id="dev")
    )
    assert refusal is not None
    assert refusal.code == REFUSAL_CANONICAL_CHANNEL


def test_the_door_refuses_session_and_run_state_as_unexpected_keys():
    """Opt-in, never opt-out. The session/run family is the concrete list the
    plan's §4 table names."""

    for name, value in (
        ("default_chat_session_id", "persona_chat_x_0123456789ab"),
        ("session_id", "persona_chat_legacy"),
        ("chat_head_home", "alice"),
        ("active_run_id", "run_abc"),
        ("current_task_id", "task_abc"),
        ("current_assignment_id", "asg_abc"),
        ("goal_id", "goal_abc"),
    ):
        refusal = refuse_persona_instance(
            "personainst_dev_agent_9682caf4", _admissible_body(**{name: value})
        )
        assert refusal is not None, name
        assert refusal.code == REFUSAL_UNEXPECTED_KEY, name
        assert name in refusal.message


@pytest.mark.parametrize(
    "instance_id",
    [
        "personainst_../../evil",
        "personainst_..",
        "../personainst_x",
        "dev_agent_1",  # not instance-shaped at all
        "personainst_a/b",
        "personainst_x:y",
    ],
)
def test_the_door_refuses_traversal_and_non_instance_shaped_ids(instance_id):
    """The id becomes a FILENAME under ``persona_instances/``. An id
    ``safe_path_token`` would rewrite is an id whose row lands under a key that
    is not the key the realm agreed on — the merge unit silently renamed."""

    assert valid_persona_instance_id(instance_id) is False
    refusal = refuse_persona_instance(instance_id, _admissible_body(id=instance_id))
    assert refusal is not None
    assert refusal.code == REFUSAL_INVALID_INSTANCE_ID


def test_a_body_whose_id_disagrees_with_its_key_is_refused():
    """The published key routes; the payload is truth. When the two disagree
    there is no safe reading — adopting either one writes a row somebody did not
    publish."""

    refusal = refuse_persona_instance(
        "personainst_dev_agent_9682caf4", _admissible_body(id="personainst_other_agent_1")
    )
    assert refusal is not None
    assert refusal.code == REFUSAL_INVALID_INSTANCE_ID


def test_the_door_refuses_a_non_instance_shaped_steering_parent():
    """The same guard ``__post_init__`` spends to keep a principal (the
    operator) from ever rendering as a parent edge."""

    refusal = refuse_persona_instance(
        "personainst_dev_agent_9682caf4", _admissible_body(steered_by=["operator"])
    )
    assert refusal is not None
    assert refusal.code == REFUSAL_STEERING_SHAPE

    ok = refuse_persona_instance(
        "personainst_dev_agent_9682caf4",
        _admissible_body(steered_by=["personainst_lead_agent_1"]),
    )
    assert ok is None


def test_a_body_with_no_persona_id_is_refused_rather_than_accounted():
    """Unlike a persona definition — which is ACCOUNTED as incomplete so one
    under-declared row cannot brick a whole realm publish — an instance with no
    persona pointer cannot be adopted at all: the mint reads the definition to
    derive ``role`` and ``profile_id``."""

    body = _admissible_body()
    body.pop("persona_id")
    refusal = refuse_persona_instance("personainst_dev_agent_9682caf4", body)
    assert refusal is not None
    assert refusal.code == REFUSAL_INCOMPLETE


def test_the_allowlist_is_total_over_what_the_door_admits():
    """Every key an admissible body may carry is a key the projection may
    produce, and vice versa. Two sets that drift apart is how a publisher ships
    something the puller then refuses on every pull, forever."""

    produced = set(project_persona_instance(_placement_instance()))
    assert produced <= PERSONA_INSTANCE_ALLOWED_KEYS
    assert refuse_persona_instance(_placement_instance().id, dict.fromkeys(
        PERSONA_INSTANCE_ALLOWED_KEYS - {"id", "persona_id", "steered_by"}, "x"
    ) | {"id": _placement_instance().id, "persona_id": "dev"}) is None
