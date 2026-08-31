"""``agent_create.perform_agent_create`` — the create sequence, off the wire.

``tests/agent_runtime/test_serve_rpc_agent_create.py`` is the fence that the
JSON-RPC lane still behaves exactly as it did; this file is the fence for the
property that lane no longer owns: the sequence RUNS with no ``serve_rpc`` in
the picture at all. That is what makes ``harness agent create`` (UC-H3), a cron
script and any future MCP wrapper the same create rather than three.

Anti-vacuity notes are attached to each test, because the failure mode this
repo keeps re-discovering is a probe the mutated path also satisfies.
"""

from __future__ import annotations

import pytest

# Deliberately NOT importing ``agent_runtime.serve_rpc`` at module scope. The
# whole claim under test is that the sequence needs no RPC surface; a module
# level import would make every test below pass even if the shim were the only
# real implementation and the service a dead copy.
from agent_runtime import paths
from agent_runtime.agent_create import perform_agent_create
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_create_service"


def _seed_workspace(workspace_id: str = WORKSPACE):
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    seed_workspace_record(workspace_id)
    store.ensure_surface(workspace_id, created_by="seed")
    return store


def _params(**overrides) -> dict:
    params = {
        "persona_id": "qa",
        "workspace_id": WORKSPACE,
        "position": [3.5, -1.25],
        "idempotency_key": "service-1",
    }
    params.update(overrides)
    return params


def _actors(workspace_id: str = WORKSPACE) -> dict:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key: actor for actor in OfficeStore().scan_actors(workspace_id).actors}


def _reservation_record(key: str) -> dict:
    """The receipt FILE. The witness that a replay wrote nothing.

    It used to be the ack's ``revision``, and that field is exactly what the
    replay re-read stops freezing — so the witness moved to the artifact a reply
    cannot fabricate.
    """

    import hashlib
    import json

    path = paths.agent_create_reservation_path(
        hashlib.sha256(key.encode("utf-8")).hexdigest()
    )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def qa_persona():
    """A persona whose configured display name differs from its title-cased id."""

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


# ── the sequence, with no serve in the picture ───────────────────────────────


def test_the_service_places_an_agent_with_no_rpc_layer_involved(qa_persona):
    """UC-H1's load-bearing test.

    ANTI-VACUITY. The kill-mutation is "the shim keeps its own inline copy and
    ``perform_agent_create`` is dead code". Under that mutant this call writes
    NOTHING, so the probes below — the instance FILE on disk under
    ``paths.persona_instances_dir()`` and the office actor's ``revision`` — are
    unreachable, not merely different: the mutated path never executes, so it
    cannot also set them. Both probes are read back off the STORES rather than
    off the returned dict, so a service that fabricated a plausible reply
    without writing would also fail.
    """

    _seed_workspace()

    outcome = perform_agent_create(_params(placement_id="qa_service_agent_2"))

    assert outcome.refusal is None
    assert outcome.refusal is None
    result = outcome.result
    assert result["persona_instance_id"] == "personainst_qa_service_agent_2"
    assert result["idempotent_replay"] is False

    # Witness 1 — the roster row is a FILE, not a reply field.
    assert paths.persona_instance_path("personainst_qa_service_agent_2").exists()
    assert (
        paths.persona_instance_path("personainst_qa_service_agent_2").parent
        == paths.persona_instances_dir()
    )

    # Witness 2 — the placement landed, at its first revision.
    actors = _actors()
    assert result["actor_key"] in actors
    assert actors[result["actor_key"]].revision == 1
    assert actors[result["actor_key"]].persona_instance_id == "personainst_qa_service_agent_2"

    # Witness 3 — the shared naming rule came with the sequence, not with the
    # RPC handler. "Qa" here means the hoist dropped the policy layer.
    assert result["display_name"] == "QA Agent"
    assert actors[result["actor_key"]].items[0].display_name == "QA Agent"


def test_the_service_refuses_a_bad_position_without_a_reply_envelope(qa_persona):
    """A refusal is a typed object, not a JSON-RPC frame, and still writes nothing."""

    _seed_workspace()
    before = set(_actors())

    outcome = perform_agent_create(_params(position="3,4", placement_id="qa_badpos_agent_2"))

    assert outcome.refusal is not None
    assert outcome.result is None
    assert outcome.refusal.data["reason"] == "position_invalid"
    assert not paths.persona_instance_path("personainst_qa_badpos_agent_2").exists()
    assert set(_actors()) == before


def test_a_replay_through_the_service_writes_nothing(qa_persona):
    """ANTI-VACUITY. The witness is the actor's REVISION, never the ids: the
    ids derive from the placement id, so a duplicating replay returns them
    unchanged while ``upsert_actor`` bumps the revision monotonically. A
    mutation that bypasses the reservation cannot re-write the actor without
    moving the probed field."""

    _seed_workspace()
    first = perform_agent_create(_params(placement_id="qa_svc_replay_agent_2")).result

    second = perform_agent_create(_params(placement_id="qa_svc_replay_agent_2")).result

    assert second["idempotent_replay"] is True
    assert second["persona_instance_id"] == first["persona_instance_id"]
    assert _actors()[first["actor_key"]].revision == 1


# ── the roster refusal, on the lane with no wire (UC-H2) ─────────────────────


def test_the_service_itself_refuses_an_unknown_bare_persona(qa_persona):
    """The refusal must live in the SERVICE, not in the RPC handler — otherwise
    ``harness agent create`` and every future MCP wrapper fail open while the
    wire lane fails closed, which is the two-spellings bug one layer up.

    ANTI-VACUITY: absence probes, and the kill-mutation's whole effect is to
    make those absences exist. See the sibling test in
    ``test_serve_rpc_agent_create.py`` for the three-witness argument.
    """

    _seed_workspace()

    outcome = perform_agent_create(
        _params(persona_id="qa_agent", placement_id="qa_agent_svc_agent_2")
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "persona_not_found"
    assert not paths.persona_instance_path("personainst_qa_agent_svc_agent_2").exists()
    assert _actors() == {}


def test_a_caller_supplied_persona_object_settles_the_question(qa_persona):
    """The CLI resolves personas through its own richer ``_persona_by_id``
    (profile synthesis, instance-id spellings). When it hands one over, the
    narrower :func:`resolve_persona` must not overrule it — otherwise the CLI
    lane refuses ids it can itself resolve."""

    from agent_runtime.models import AgentPersona

    _seed_workspace()
    synthesized = AgentPersona(
        id="only_in_the_callers_hand",
        display_name="Synthesised Agent",
        role="profile",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )

    outcome = perform_agent_create(
        _params(persona_id="only_in_the_callers_hand", placement_id="handed_in_agent_2"),
        persona=synthesized,
    )

    assert outcome.refusal is None
    assert outcome.result["display_name"] == "Synthesised Agent"


# ── the shim is a shim ───────────────────────────────────────────────────────


def test_the_rpc_handler_holds_no_second_copy_of_the_sequence():
    """The single-copy gate, made precise.

    The plan's version of this was "``add_instance``/``upsert_actor`` appear
    zero times in serve_rpc.py", which cannot work: ``runtime.office.upsert``
    legitimately calls ``upsert_actor`` in the same file. So the gate reads the
    HANDLER's own source instead of the module's.

    ANTI-VACUITY. The kill-mutation is "leave the inline sequence behind beside
    the extracted function". The probe is the handler's source text, which the
    mutant necessarily contains — the mutation IS the presence of those calls,
    so it cannot pass by also satisfying the probe.
    """

    import inspect

    from agent_runtime import serve_rpc

    source = inspect.getsource(serve_rpc._runtime_agent_create)

    for forbidden in (
        "add_instance",
        "upsert_actor",
        "reserve_agent_create",
        "mark_instance_minted",
        "mark_done",
        "class_key_collision",
        "placement_actor_payload",
    ):
        assert forbidden not in source, (
            f"{forbidden!r} is back inside the RPC handler: the create sequence "
            "has a second copy again, and the two lanes can now drift."
        )
    assert "perform_agent_create" in source
    # Guard the guard: a handler that had been emptied entirely would satisfy
    # every assertion above.
    assert "err(" in source and "ok(" in source


def test_verb_authors_no_desk(qa_persona):
    """D6, proven at RUNTIME rather than by reading ``placement_actor_payload``.

    Two independent witnesses, because either alone is walkable:

    1. The actor the create WROTE, read back off the store, holds exactly one
       item and it is ``kind: "agent"``. A source walk over
       ``placement_actor_payload`` would answer a question about a SPELLING —
       every legal respelling of a desk item walks through it.
    2. The create succeeds into a workspace where another actor ALREADY holds a
       desk for this persona. That is the strong form: the store's desk fence
       (``_guard_duplicate_desk``) is armed and pointed straight at this write,
       so a payload that authored a desk would be REFUSED here, not merely
       different. ``duplicate_desk`` is unreachable from ``agent create`` and
       this is what makes that a fact rather than a claim.

    THE killing mutation is adding a desk item to ``placement_actor_payload``:
    witness 1 reds on the item list and witness 2 reds on the refusal.
    """

    store = _seed_workspace()
    # A desk for ``qa``, held by somebody else entirely.
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "qa",
            "items": [
                {"item_id": "qa_desk", "persona_id": "qa", "kind": "desk", "position": [0.0, 0.0]}
            ],
        },
    )

    outcome = perform_agent_create(_params(placement_id="qa_no_desk_agent_2"))

    assert outcome.refusal is None, outcome.refusal
    placed = _actors()[outcome.result["actor_key"]]
    assert [item.kind for item in placed.items] == ["agent"]
    assert all(item.kind != "desk" for item in placed.items)
    # And the pre-existing desk is untouched — the create did not "win" by
    # replacing the holder.
    assert [i.item_id for i in _actors()["qa"].items] == ["qa_desk"]


def test_the_services_error_codes_are_serve_rpcs_error_codes():
    """The four constants are re-spelled in ``agent_create`` so a CLI process
    need not import the RPC registry. This is the fence that keeps the two
    spellings from drifting into two different wire contracts."""

    from agent_runtime import agent_create, serve_rpc

    assert agent_create.ERR_INVALID_PARAMS == serve_rpc.ERR_INVALID_PARAMS
    assert agent_create.ERR_HANDLER_FAILED == serve_rpc.ERR_HANDLER_FAILED
    assert agent_create.ERR_NOT_FOUND == serve_rpc.ERR_NOT_FOUND
    assert agent_create.ERR_CONFLICT == serve_rpc.ERR_CONFLICT


# ── the roster fault is not the roster's answer (reviewer addition) ──────────
#
# UC-H2 made the roster load-bearing: before it, a config this process could not
# read degraded quietly to a title-cased display name. After it, routing that
# fault through the same ``None`` as "no such persona" would have refused EVERY
# bare-id create on EVERY lane with a message blaming the operator's id.
#
# ANTI-VACUITY. Each test probes the ``reason`` STRING, and the two tests demand
# DIFFERENT values from the same call shape — a mutant that collapses the two
# branches (either direction) can satisfy at most one of them. Probing "was the
# create refused?" alone would pass under the collapse, which is exactly the
# vacuous witness this file's other tests were careful to avoid.


def test_an_unreadable_roster_is_its_own_reason_not_persona_not_found(
    monkeypatch, qa_persona
):
    """A runtime fault must not wear a bad id's costume."""

    from agent_runtime import agent_create

    # Patched at the CONFIG loader, not at ``persona_roster`` — so the real
    # wrapping runs and the test proves the typed fault is actually minted
    # rather than assuming it. Patching the wrapper would have tested nothing
    # but the wrapper's absence.
    from agent_runtime import config as runtime_config

    def _explode(*args, **kwargs):
        raise OSError("config file is locked by another process")

    monkeypatch.setattr(runtime_config, "load_agent_runtime_config", _explode)

    with pytest.raises(agent_create.AgentCreateInvalid) as caught:
        agent_create.normalize_agent_create(
            {
                "persona_id": "qa",
                "workspace_id": "ws_probe",
                "position": [0.0, 0.0],
                "idempotency_key": "k-roster-fault",
            }
        )

    assert caught.value.reason == agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON
    assert caught.value.reason != agent_create.PERSONA_NOT_FOUND_REASON
    # The message must not send the operator hunting a typo.
    assert "not in the agent roster" not in str(caught.value)


def test_an_empty_roster_that_LOADED_still_refuses_as_persona_not_found(
    monkeypatch, qa_persona
):
    """An empty roster is a real answer, and keeps the actionable reason.

    The counterpart to the test above: this is what stops the fix from being a
    blanket excuse that reopens the hole UC-H2 closed.
    """

    from agent_runtime import agent_create

    monkeypatch.setattr(agent_create, "persona_roster", lambda: [])

    with pytest.raises(agent_create.AgentCreateInvalid) as caught:
        agent_create.normalize_agent_create(
            {
                "persona_id": "qa",
                "workspace_id": "ws_probe",
                "position": [0.0, 0.0],
                "idempotency_key": "k-empty-roster",
            }
        )

    assert caught.value.reason == agent_create.PERSONA_NOT_FOUND_REASON
    assert caught.value.reason != agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON


# ── every pre-store refusal stamps ``rolled_back: true`` (plan F3) ────────────
#
# ``1da669d908`` stamped the reservation and placement arms and stopped there,
# leaving every ``AgentCreateInvalid`` arm — the eight refusals that provably
# run before ``OfficeStore`` is even constructed — with NO ``rolled_back`` key.
# An absent key is not neutral: the launcher's decoder reads it as ``false`` and
# renders "the placement could not be undone", so a mistyped persona id told the
# operator to go check the runtime for wreckage that cannot exist.


def _invalid_reasons_declared_in_the_module() -> set[str]:
    """Every ``AgentCreateInvalid`` reason the module can RAISE, read off its AST.

    Enumerated from the source rather than typed here, and the split matters:
    the source walk only builds the PARAMETER LIST, and every reason it finds is
    then driven through the live ``perform_agent_create`` below and asserted at
    runtime. A typed list would go stale the day somebody adds a ninth arm — the
    parametrisation would keep passing while the new arm shipped unstamped,
    which is precisely the shape of the hole this test closes.
    """

    import ast
    import inspect

    from agent_runtime import agent_create

    tree = ast.parse(inspect.getsource(agent_create))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # BOTH spellings. Matching only the bare ``ast.Name`` made this walk a
        # question about how the raise happens to be WRITTEN: a refusal
        # re-spelled ``agent_create.AgentCreateInvalid(...)`` — the ordinary
        # shape the moment this module is imported rather than star-imported —
        # would vanish from ``declared`` AND from ``covered`` at once, and the
        # parametrisation would keep passing while the arm shipped unstamped.
        # That is the same class of hole the typed-list version had.
        named = isinstance(func, ast.Name) and func.id == "AgentCreateInvalid"
        qualified = (
            isinstance(func, ast.Attribute) and func.attr == "AgentCreateInvalid"
        )
        if not (named or qualified):
            continue
        assert node.args, "AgentCreateInvalid is constructed reason-first"
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            reasons.add(first.value)
        elif isinstance(first, ast.Name):
            # A module-level constant (``PERSONA_NOT_FOUND_REASON``); resolved
            # through the module so a renamed constant cannot go unnoticed.
            reasons.add(getattr(agent_create, first.id))
        else:  # pragma: no cover - a shape nobody has written
            raise AssertionError(f"unrecognised reason expression: {ast.dump(first)}")
    # A FLOOR, not a roster. Its job is to fail a walk that stopped matching —
    # a renamed exception, an ``ast`` change, a raise moved behind a helper —
    # because a walk that finds nothing satisfies every ``for`` below it. Eight
    # is what §0.1 F3 enumerates; a ninth arm raises this number on purpose.
    assert len(reasons) >= 8, sorted(reasons)
    return reasons


#: One request per arm, each keeping every EARLIER field valid so the refusal
#: under test is the one the normaliser actually reaches.
_INVALID_ARM_PARAMS: dict[str, dict] = {
    "persona_id_required": {"persona_id": ""},
    "persona_not_found": {"persona_id": "qa_agent"},
    "workspace_id_required": {"workspace_id": ""},
    "position_invalid": {"position": "3,4"},
    "idempotency_key_required": {"idempotency_key": ""},
    "idempotency_key_invalid": {"idempotency_key": "k" * 241},
    "placement_id_invalid": {"placement_id": "!!!"},
    # R1. The id the wrong-alice incident was actually typed with: it TOKENISES
    # (so it clears the arm above) and still derives an instance id neither
    # repo's discriminator can classify.
    "placement_id_not_discriminable": {"placement_id": "known_alice"},
    # A bare string is the client mistake this arm exists for: iterating it
    # would assign one skill per CHARACTER, so the container shape is refused
    # before any write rather than normalised into nonsense.
    "skills_invalid": {"skills": "harness-qa-verdict"},
}


def test_every_invalid_arm_has_a_case():
    """The parametrisation covers the module, not the author's memory.

    ``persona_roster_unavailable`` is driven by its own test below (it needs a
    fault injected, not a parameter), so it is excluded here by name rather than
    by being forgotten.
    """

    from agent_runtime import agent_create

    declared = _invalid_reasons_declared_in_the_module()
    assert declared, "the AST walk found no arms at all"
    covered = set(_INVALID_ARM_PARAMS) | {
        agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON
    }
    assert declared == covered, {
        "unparametrised": sorted(declared - covered),
        "stale": sorted(covered - declared),
    }


@pytest.mark.parametrize("reason", sorted(_INVALID_ARM_PARAMS))
def test_invalid_arms_stamp_rolled_back(reason, qa_persona):
    """KILLING MUTATION: drop the stamp on one arm and that parameter reds.

    ANTI-VACUITY. The stamp is asserted beside the two witnesses that make it
    TRUE rather than merely present — no roster row file and no new actor — so a
    mutant that stamped ``rolled_back: True`` on an arm that had in fact written
    something would fail on the witnesses, not pass on the stamp.
    """

    _seed_workspace()
    before = set(_actors())

    overrides = {"placement_id": "qa_stamp_agent_2", **_INVALID_ARM_PARAMS[reason]}
    outcome = perform_agent_create(_params(**overrides))

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == reason
    assert outcome.refusal.data["rolled_back"] is True
    # The claim, not just the key.
    assert not paths.persona_instance_path("personainst_qa_stamp_agent_2").exists()
    assert set(_actors()) == before


def test_the_roster_fault_arm_stamps_rolled_back(monkeypatch, qa_persona):
    """The eighth arm, which needs a fault rather than a parameter.

    Patched at the config loader for the same reason the sibling test one file
    section up is: patching ``persona_roster`` itself would prove the wrapper's
    absence rather than the typed fault's presence.
    """

    from agent_runtime import agent_create
    from agent_runtime import config as runtime_config

    _seed_workspace()

    def _explode(*args, **kwargs):
        raise OSError("config file is locked by another process")

    monkeypatch.setattr(runtime_config, "load_agent_runtime_config", _explode)

    outcome = perform_agent_create(_params(placement_id="qa_roster_stamp_agent_2"))

    assert outcome.refusal is not None
    assert (
        outcome.refusal.data["reason"]
        == agent_create.PERSONA_ROSTER_UNAVAILABLE_REASON
    )
    assert outcome.refusal.data["rolled_back"] is True


def test_the_argv_lanes_roster_refusal_renders_the_same_block(qa_persona):
    """``roster_unavailable_outcome`` is the CLI's copy of one refusal.

    The two constructions are compared for equality rather than trusted to
    agree — a ``data`` block that differed by a key would put the two lanes back
    to rendering one fault two ways, which is the defect that constructor exists
    to end. This is the stamp's half of that comparison.
    """

    from agent_runtime.agent_create import roster_unavailable_outcome

    outcome = roster_unavailable_outcome(OSError("locked"))

    assert outcome.refusal is not None
    assert outcome.refusal.data["rolled_back"] is True
# ── S2 / D2: the position is optional, and the ack says what was written ─────


def _no_position(**overrides) -> dict:
    """``_params`` with the key REMOVED, not set to ``None``.

    Both spellings mean absence to the service and there is a test below for
    the ``null`` one; this helper is the omitted-key shape, which is what every
    door without a canvas actually sends.
    """

    params = _params(**overrides)
    params.pop("position")
    return params


def _policy_slot(row: int, column: int):
    from agent_runtime.office_layout_policy import slot_at

    return list(slot_at(row, column))


def test_a_create_with_no_position_lands_on_the_policys_slot(
    qa_persona, isolate_agent_runtime_root
):
    """ANTI-VACUITY. The probe is the position READ BACK OUT OF THE STORE, not
    the one on the ack: an implementation that reported a slot and wrote (0, 0)
    — which is what a payload builder falling back to its default would do —
    passes an ack-only assertion and fails this one.

    KILLING MUTATION (plan §C): make ``normalize_agent_create`` refuse an absent
    position again and this reds on ``position_invalid``.
    """

    _seed_workspace()
    outcome = perform_agent_create(_no_position(placement_id="qa_unaimed_agent_2"), persona=qa_persona)

    assert outcome.refusal is None, outcome.refusal
    stored = _actors()[outcome.result["actor_key"]]
    assert [float(v) for v in stored.items[0].position] == _policy_slot(0, 0)


def test_a_create_with_a_position_still_lands_there_verbatim(
    qa_persona, isolate_agent_runtime_root
):
    """The other half of the same fence, and the reason it is a separate test.

    KILLING MUTATION (plan §C): make the service substitute the policy's slot
    when a position IS given and this reds — the policy's first slot is the world
    origin ``(0.0, 0.0)`` (operator ruling 2026-08-27; it was ``(-5.0, 6.4)``
    until then) and nothing about ``(3.5, -1.25)`` is near either.
    """

    _seed_workspace()
    outcome = perform_agent_create(_params(placement_id="qa_aimed_agent_2"), persona=qa_persona)

    assert outcome.refusal is None, outcome.refusal
    stored = _actors()[outcome.result["actor_key"]]
    assert [float(v) for v in stored.items[0].position] == [3.5, -1.25]
    assert stored.items[0].position != _policy_slot(0, 0)


def test_an_explicit_null_position_is_absence_not_a_malformed_aim(
    qa_persona, isolate_agent_runtime_root
):
    """A JSON client spelling "no opinion" as ``null`` means what one omitting
    the key means. Refusing one while accepting the other would make the wire's
    meaning depend on a serializer's omit-none setting.
    """

    _seed_workspace()
    outcome = perform_agent_create(
        _params(position=None, placement_id="qa_null_agent_2"), persona=qa_persona
    )

    assert outcome.refusal is None, outcome.refusal
    assert _actors()[outcome.result["actor_key"]].items[0].position == _policy_slot(0, 0)


def test_a_malformed_position_still_refuses(qa_persona, isolate_agent_runtime_root):
    """Absence became legal; a MANGLED aim did not. A transport that lost half a
    coordinate is not an operator who had no opinion, and the two must not
    collapse into one silently-placed agent.
    """

    _seed_workspace()
    for bad in ([1.0], "3,4", [float("inf"), 0.0], [True, False]):
        outcome = perform_agent_create(
            _params(position=bad, placement_id="qa_bad_agent_2"), persona=qa_persona
        )
        assert outcome.refusal is not None, bad
        assert outcome.refusal.data["reason"] == "position_invalid", bad
    assert _actors() == {}


def test_the_policy_scans_the_requests_folder_and_skips_the_occupied_slot(
    qa_persona, isolate_agent_runtime_root
):
    """Two placements, no aim: the second must not land on the first.

    That is the whole reason the policy reads the store rather than returning a
    constant, and it is the property a "just use the origin" implementation
    satisfies for exactly one agent.

    This is also the END-TO-END proof of the 2026-08-27 ruling, through the real
    service rather than the policy alone: the first unaimed create lands on the
    world origin and the second stands one full grid step ABOVE it, not beside
    it. Before that ruling the second answer was ``slot(0, 1)``.
    """

    _seed_workspace()
    first = perform_agent_create(
        _no_position(idempotency_key="unaimed-1", placement_id="qa_one_agent_2"),
        persona=qa_persona,
    )
    second = perform_agent_create(
        _no_position(idempotency_key="unaimed-2", placement_id="qa_two_agent_2"),
        persona=qa_persona,
    )

    assert first.refusal is None and second.refusal is None
    actors = _actors()
    assert [float(v) for v in actors[first.result["actor_key"]].items[0].position] == _policy_slot(0, 0)
    assert [float(v) for v in actors[second.result["actor_key"]].items[0].position] == _policy_slot(1, 0)


def test_a_different_folder_is_a_different_scan(qa_persona, isolate_agent_runtime_root):
    """The scan is FOLDER-scoped, so an agent filed under a custom folder starts
    that folder's lattice at column 0 rather than queueing behind ``Agents``.

    KILLING MUTATION: scan every item regardless of folder and the second
    assertion reds at column 1.
    """

    _seed_workspace()
    perform_agent_create(
        _no_position(idempotency_key="folder-1", placement_id="qa_agents_agent_2"),
        persona=qa_persona,
    )
    other = perform_agent_create(
        _no_position(idempotency_key="folder-2", placement_id="qa_ops_agent_2", folder="Ops"),
        persona=qa_persona,
    )

    assert other.refusal is None, other.refusal
    placed = _actors()[other.result["actor_key"]].items[0]
    assert placed.folder == "Ops"
    assert [float(v) for v in placed.position] == _policy_slot(0, 0)


def test_the_policy_excludes_the_actor_it_is_about_to_write(
    qa_persona, isolate_agent_runtime_root
):
    """A resumed attempt whose placement already landed must recompute the SAME
    slot, not step one along.

    Proved at the pure seam (``placement_slot_for``) rather than by arranging a
    crashed reservation, because the property is about which actor set the scan
    sees and that is exactly what this function decides. Without the exclusion
    an idempotent replay WALKS: every retry reads its own item as a blocker and
    moves the agent one column.

    The seam is now PURE — actors in, point out — because H-H10 moved the store
    read into ``upsert_actor``'s lock; the exclusion stayed here, with the lane
    that knows which row is "mine".
    """

    from agent_runtime.agent_create import (
        normalize_agent_create,
        placement_slot_for,
    )
    from agent_runtime.office_store import OfficeStore

    store = _seed_workspace()
    request = normalize_agent_create(
        _no_position(placement_id="qa_resume_agent_2"), persona=qa_persona
    )
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "qa",
            "persona_instance_id": request.persona_instance_id,
            "items": [
                {
                    "item_id": request.persona_instance_id,
                    "persona_id": "qa",
                    "kind": "agent",
                    "position": _policy_slot(0, 0),
                    "folder": request.folder,
                }
            ],
        },
    )

    actors = OfficeStore().scan_actors(WORKSPACE).actors
    assert actors, "the seeded blocker must be visible, or this proves nothing"
    assert list(placement_slot_for(actors, request)) == _policy_slot(0, 0)


def test_the_ack_carries_the_position_that_was_written(
    qa_persona, isolate_agent_runtime_root
):
    """Both arms, because a result that echoed the REQUEST would be right for
    the aimed one and silent for the unaimed one — which is the arm that needs
    it, since an unaimed caller has no other way to learn where its agent went.
    """

    _seed_workspace()
    aimed = perform_agent_create(_params(placement_id="qa_ack_a_agent_2"), persona=qa_persona)
    unaimed = perform_agent_create(
        _no_position(idempotency_key="ack-2", placement_id="qa_ack_b_agent_2"),
        persona=qa_persona,
    )

    assert aimed.result["position"] == [3.5, -1.25]
    # Slot 0, not slot 1: the aimed placement is off in the corner at
    # (3.5, -1.25), nowhere near the lattice, so it blocks nothing. That is the
    # policy working — occupancy is a DISTANCE question, never "how many agents
    # are on this floor".
    assert unaimed.result["position"] == _policy_slot(0, 0)
    for outcome in (aimed, unaimed):
        stored = _actors()[outcome.result["actor_key"]].items[0]
        assert outcome.result["position"] == [float(v) for v in stored.position]


def test_the_acks_actor_is_the_row_the_store_returned(
    qa_persona, isolate_agent_runtime_root
):
    """KILLING MUTATION (plan §C): build the ack's ``actor`` from the request
    payload instead of the store's return value and this reds on ``revision``
    — the payload has none, and the store's is 1 on a fresh create and 2 after
    a second write to the same key.

    So the second create here is not decoration: it is the assertion that the
    number came from the store.
    """

    from agent_runtime.office_models import office_actor_wire_row
    from agent_runtime.office_store import OfficeStore

    _seed_workspace()
    first = perform_agent_create(_params(placement_id="qa_row_agent_2"), persona=qa_persona)
    actor_key = first.result["actor_key"]

    assert first.result["actor"] == office_actor_wire_row(
        OfficeStore().get_actor(WORKSPACE, actor_key)
    )
    assert first.result["actor"]["revision"] == 1
    assert first.result["actor"]["items"][0]["position"] == [3.5, -1.25]
    assert first.result["actor"]["persona_instance_id"] == first.result["persona_instance_id"]

    # A move on the same key: the ack's revision has to follow the store's.
    OfficeStore().upsert_actor(
        WORKSPACE,
        {
            "persona_id": "qa",
            "persona_instance_id": first.result["persona_instance_id"],
            "items": [
                {
                    "item_id": first.result["persona_instance_id"],
                    "persona_id": "qa",
                    "kind": "agent",
                    "position": [9.0, 9.0],
                }
            ],
        },
    )
    assert OfficeStore().get_actor(WORKSPACE, actor_key).revision == 2


def test_an_idempotent_replay_re_reads_the_actor_instead_of_echoing_the_receipt(
    qa_persona, isolate_agent_runtime_root
):
    """A replay reports where the agent IS, not where it was first put.

    RED-FIRST against the arm this replaces. Until now the ``done`` arm returned
    ``record.result`` verbatim, so this test's ``moved`` assertions FAIL on that
    code: the receipt was written at the first create and the actor has been
    dragged since.

    WHY IT MATTERS RATHER THAN BEING TIDY. Plan S7 makes the launcher ADOPT the
    ack's actor — key, position and revision — into its scene and its
    ``expect_revision`` bookkeeping. A replay that hands back the revision the
    row had at first write makes the client's very next guarded write refuse
    ``stale_revision``, and hands back a position that snaps the agent to where
    it used to be. The office actor is mutable by drag, by realm pull and by
    conflict resolution, so "the receipt is current" is false the moment anyone
    touches the level.

    KILLING MUTATION: return ``record.result`` verbatim from the ``STATE_DONE``
    arm and the three ``moved`` assertions red.

    ANTI-VACUITY. The move is made through the REAL store write
    (``upsert_actor``), so the revision genuinely advances and the position
    genuinely differs from the recorded one — a replay that happened to
    recompute the same slot cannot pass by coincidence. And "no second write
    happened" is asserted on the RECEIPT FILE and on the store's own revision,
    NOT on the reply's revision: that field is exactly what this change stops
    freezing, so leaning on it would be asserting the bug.
    """

    from agent_runtime.office_store import OfficeStore

    _seed_workspace()
    first = perform_agent_create(
        _no_position(idempotency_key="replay-1", placement_id="qa_replay_agent_2"),
        persona=qa_persona,
    )
    actor_key = first.result["actor_key"]
    assert first.result["actor_fresh"] is True
    receipt_before = _reservation_record("replay-1")

    # The agent is dragged somewhere else, the way an operator moves one.
    stored = OfficeStore().get_actor(WORKSPACE, actor_key)
    payload = {
        "actor_key": actor_key,
        "persona_id": stored.persona_id,
        "persona_instance_id": stored.persona_instance_id,
        "items": [
            {
                "item_id": item.item_id,
                "kind": item.kind,
                "position": [41.0, -17.0],
                "folder": item.folder,
                "display_name": item.display_name,
            }
            for item in stored.items
        ],
    }
    moved_actor = OfficeStore().upsert_actor(WORKSPACE, payload, updated_by="operator")
    assert moved_actor.revision > stored.revision

    again = perform_agent_create(
        _no_position(idempotency_key="replay-1", placement_id="qa_replay_agent_2"),
        persona=qa_persona,
    )

    assert again.result["idempotent_replay"] is True
    # The three keys a client adopts, all as the row is NOW.
    assert again.result["position"] == [41.0, -17.0]
    assert again.result["position"] != first.result["position"]
    assert again.result["revision"] == moved_actor.revision
    assert again.result["actor"]["revision"] == moved_actor.revision
    assert again.result["actor_fresh"] is True

    # IDENTITY is the recorded decision and is NOT re-derived.
    assert again.result["persona_instance_id"] == first.result["persona_instance_id"]
    assert again.result["placement_id"] == first.result["placement_id"]
    assert (
        again.result["default_chat_session_id"]
        == first.result["default_chat_session_id"]
    )

    # The witness that the replay wrote nothing — the receipt, and the store's
    # own revision, neither of which the reply can fabricate.
    assert _reservation_record("replay-1") == receipt_before
    assert OfficeStore().get_actor(WORKSPACE, actor_key).revision == moved_actor.revision


def test_a_replay_whose_actor_is_gone_says_so_instead_of_inventing_one(
    qa_persona, isolate_agent_runtime_root
):
    """``actor_fresh: false``, and the recorded row returned UNCHANGED.

    The other half of the re-read, and the half that keeps it honest: an actor
    that has been archived since the create cannot be re-read, and the two wrong
    answers are (a) fabricate a row and (b) raise, stranding a client that only
    wanted its recorded ack back. The reply degrades to "here is what was
    recorded, and it may be stale", which is a thing a client can act on.

    KILLING MUTATION: stamp ``actor_fresh: True`` unconditionally and this reds.
    """

    from agent_runtime.office_store import OfficeStore

    _seed_workspace()
    first = perform_agent_create(
        _no_position(idempotency_key="replay-gone", placement_id="qa_replay_gone_agent_2"),
        persona=qa_persona,
    )
    actor_key = first.result["actor_key"]
    OfficeStore().remove_actor(WORKSPACE, actor_key, updated_by="operator")

    again = perform_agent_create(
        _no_position(idempotency_key="replay-gone", placement_id="qa_replay_gone_agent_2"),
        persona=qa_persona,
    )

    assert again.result["idempotent_replay"] is True
    assert again.result["actor_fresh"] is False
    # Unchanged, not invented.
    assert again.result["actor"] == first.result["actor"]
    assert again.result["position"] == first.result["position"]
    assert again.result["revision"] == first.result["revision"]


# ── the skills phase (plan S4 / D4 / D5) ─────────────────────────────────────
#
# The phase that is deliberately NOT in the reservation's atomic join. Its
# refusals leave a placed agent standing, and every test below reads that claim
# off the STORES — the roster row file and the office actor — rather than off the
# reply, because a reply is exactly what a mutant can fabricate.


@pytest.fixture
def isolated_shared_skills(tmp_path, monkeypatch):
    """Point the shared skills root at this test's tmp dir.

    The operator's real shared root is ``<hermes root>/shared/skills``, and
    ``install_harness_skill`` writes into it with a staged ``copytree`` +
    ``os.replace``. ``tests/conftest.py`` already blanks ``HERMES_SHARED_SKILLS``
    and sandboxes ``HERMES_HOME``, so the default already resolves inside
    ``tmp_path`` — this makes the pin EXPLICIT and asserts it, because "already"
    is an assumption and the thing assumed is whether these tests rewrite the
    packages the operator's live agents load.

    The ENV var and not a monkeypatched attribute: ``skill_install`` and
    ``agent.skill_utils.skill_source_kind`` both resolve the shared root
    independently, and patching one would leave the resolver classifying the
    installed copy as ``external`` — which is ``invalid_source`` for a canonical
    id, i.e. a test failing for a reason that has nothing to do with its subject.
    """

    from hermes_constants import get_shared_skills_dir

    shared = tmp_path / "shared-skills"
    monkeypatch.setenv("HERMES_SHARED_SKILLS", str(shared))
    assert get_shared_skills_dir() == shared
    return shared


def _overrides(instance_id: str):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    return PersonaInstanceStore().get(instance_id).skill_overrides


def _reservation_state(key: str) -> str:
    return _reservation_record(key)["state"]


def test_skill_refusal_keeps_placement_and_resumes(
    qa_persona, isolated_shared_skills
):
    """THE D4 pin, both halves in one test because they are one claim.

    KILLING MUTATION (plan §C): route the skills refusal through
    ``compensate_failed_placement`` and the roster row is gone — the first block
    reds.

    ANTI-VACUITY. "The placement survived" is read as the instance FILE plus the
    office actor, not as a field in the refusal; "the resume did not re-place" is
    read as the actor's REVISION being unmoved, which a mutant that re-ran
    ``upsert_actor`` cannot leave at 1; and "it did not re-mint" is the instance
    id being the same one, from a store that would have minted a second row
    under a second placement id.
    """

    _seed_workspace()
    key = "skills-resume"
    refused = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_skills_resume_agent_2",
            skills=["not-a-skill-anyone-has"],
        ),
        persona=qa_persona,
    )

    assert refused.refusal is not None
    data = refused.refusal.data
    assert data["reason"] == "skill_unresolved"
    assert data["skill"] == "not-a-skill-anyone-has"
    assert data["status"] == "missing"
    assert data["phase"] == "skills"
    # The field the launcher branches on, and here it is the literal truth.
    assert data["rolled_back"] is False
    assert "SAME idempotency_key" in data["next_expected"]

    # The agent is STANDING. Both stores, not the reply.
    assert paths.persona_instance_path("personainst_qa_skills_resume_agent_2").exists()
    actors = _actors()
    placed = [
        actor
        for actor in actors.values()
        if actor.persona_instance_id == "personainst_qa_skills_resume_agent_2"
    ]
    assert len(placed) == 1
    assert placed[0].revision == 1
    assert _overrides("personainst_qa_skills_resume_agent_2") is None

    # ...and the receipt says where to resume from.
    assert _reservation_state(key) == "placed"

    # The operator fixes the id and retries under the SAME key.
    resumed = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_skills_resume_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )

    assert resumed.refusal is None
    assert resumed.result["persona_instance_id"] == "personainst_qa_skills_resume_agent_2"
    assert resumed.result["skills"]["assigned"] == ["harness-qa-verdict"]
    assert _overrides("personainst_qa_skills_resume_agent_2") == ["harness-qa-verdict"]
    # No second roster row, and no second placement write.
    assert len(_actors()) == len(actors)
    assert _actors()[placed[0].actor_key].revision == 1
    assert _reservation_state(key) == "done"


def test_a_retry_that_is_still_wrong_refuses_again_and_still_keeps_the_agent(
    qa_persona, isolated_shared_skills
):
    """The RESUME arm's refusal is the same refusal, and it compensates nothing.

    The sibling above proves a resume that SUCCEEDS; this proves a resume that
    fails again, which is a different branch — it is raised inside the
    ``placed`` re-entry rather than inside the first attempt, and it was
    genuinely uncovered until this test (found by a mutation that survived).
    An operator who mistypes twice must be answered twice, not have their agent
    archived on the second try.

    KILLING MUTATION: compensate the placement in the ``STATE_PLACED`` resume
    arm's ``except`` and this reds — the roster row is gone and the receipt
    leaves ``placed``.
    """

    _seed_workspace()
    key = "skills-retry-wrong"
    first = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_retry_wrong_agent_2",
            skills=["still-not-a-skill"],
        ),
        persona=qa_persona,
    )
    assert first.refusal is not None

    again = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_retry_wrong_agent_2",
            skills=["also-not-a-skill"],
        ),
        persona=qa_persona,
    )

    assert again.refusal is not None
    # The SECOND id, so the resume read the current request and not the receipt.
    assert again.refusal.data["skill"] == "also-not-a-skill"
    assert again.refusal.data["phase"] == "skills"
    assert again.refusal.data["rolled_back"] is False
    # Still standing, still resumable.
    assert paths.persona_instance_path("personainst_qa_retry_wrong_agent_2").exists()
    assert any(
        actor.persona_instance_id == "personainst_qa_retry_wrong_agent_2"
        for actor in _actors().values()
    )
    assert _reservation_state(key) == "placed"


def test_a_resumed_placed_create_reports_the_actor_as_it_is(
    qa_persona, isolated_shared_skills
):
    """The ``placed`` resume re-reads the actor, exactly as the ``done`` arm does.

    RED-FIRST against the arm this replaces. The resume built its reply with
    ``result = dict(record.result)`` — the ack the FIRST attempt rendered,
    frozen at the moment the placement landed — and then stamped it
    ``idempotent_replay: false`` because this attempt genuinely did work. So the
    one reply in the whole verb that ADVERTISES itself as fresh was the one
    carrying the stalest actor: ``actor_fresh: true`` copied out of the receipt,
    beside a ``position`` and a ``revision`` from before the drag.

    WHY IT MATTERS RATHER THAN BEING TIDY. The S7 precondition tells the
    launcher it may adopt an ack whose ``idempotent_replay`` is false OR whose
    ``actor_fresh`` is not false. This arm answers false to the first and true
    to the second, so it is adopted on both counts — the client takes a stale
    ``revision`` into its ``expect_revision`` bookkeeping (its next guarded
    write then refuses ``stale_revision``) and a stale ``position`` that snaps
    the agent back to where it used to be. And the resume is not an exotic
    path: it is what an operator who mistyped a skill id does, and looking that
    id up is exactly when someone drags the agent.

    KILLING MUTATION: restore ``result = dict(record.result)`` in the
    ``STATE_PLACED`` arm and the ``moved`` assertions red.

    ANTI-VACUITY. The move goes through the REAL store write, so the revision
    genuinely advances and the position genuinely differs from the recorded one
    — a resume that happened to recompute the same slot cannot pass by
    coincidence. The recorded values are read off the RECEIPT FILE and asserted
    to differ, so "the reply equals the receipt" is measured rather than
    assumed. And the placement is asserted not to have been re-written (the
    store's own revision is the operator's, not a third on top), so a mutant
    that got a fresh actor by re-placing the agent fails too.
    """

    from agent_runtime.office_store import OfficeStore

    _seed_workspace()
    key = "resume-actor-moved"
    refused = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_resume_moved_agent_2",
            skills=["not-a-skill-anyone-has"],
        ),
        persona=qa_persona,
    )
    assert refused.refusal is not None
    assert _reservation_state(key) == "placed"

    recorded = _reservation_record(key)["result"]
    actor_key = recorded["actor_key"]
    assert recorded["actor_fresh"] is True

    # The operator goes to look up the id they mistyped, and drags the agent on
    # the way past. A real store write, through the door an operator drag uses.
    stored = OfficeStore().get_actor(WORKSPACE, actor_key)
    moved_actor = OfficeStore().upsert_actor(
        WORKSPACE,
        {
            "actor_key": actor_key,
            "persona_id": stored.persona_id,
            "persona_instance_id": stored.persona_instance_id,
            "items": [
                {
                    "item_id": item.item_id,
                    "kind": item.kind,
                    "position": [41.0, -17.0],
                    "folder": item.folder,
                    "display_name": item.display_name,
                }
                for item in stored.items
            ],
        },
        updated_by="operator",
    )
    assert moved_actor.revision > stored.revision

    resumed = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_resume_moved_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )

    assert resumed.refusal is None
    # This attempt DID work, so it is not a replay — and that is precisely why
    # the actor it reports has to be the live one.
    assert resumed.result["idempotent_replay"] is False
    assert resumed.result["skills"]["assigned"] == ["harness-qa-verdict"]

    # The three keys a client adopts, all as the row is NOW, and all different
    # from what the receipt froze.
    assert resumed.result["position"] == [41.0, -17.0]
    assert resumed.result["position"] != recorded["position"]
    assert resumed.result["revision"] == moved_actor.revision
    assert resumed.result["revision"] != recorded["revision"]
    assert resumed.result["actor"]["revision"] == moved_actor.revision
    assert resumed.result["actor"] != recorded["actor"]
    assert resumed.result["actor_fresh"] is True

    # IDENTITY is the recorded decision and is NOT re-derived.
    assert resumed.result["persona_instance_id"] == recorded["persona_instance_id"]
    assert resumed.result["placement_id"] == recorded["placement_id"]
    assert (
        resumed.result["default_chat_session_id"]
        == recorded["default_chat_session_id"]
    )

    # The resume re-placed nothing: the store still carries the operator's
    # write and not a third revision on top of it.
    assert (
        OfficeStore().get_actor(WORKSPACE, actor_key).revision == moved_actor.revision
    )
    assert _reservation_state(key) == "done"


def test_a_resumed_create_whose_actor_is_gone_says_so_instead_of_inventing_one(
    qa_persona, isolated_shared_skills
):
    """The other half of the resume's re-read, and the half that keeps it honest.

    An actor archived between the refusal and the retry cannot be re-read. The
    reply degrades to "here is what was recorded, and it may be stale" — the
    recorded row returned UNCHANGED with ``actor_fresh: false`` — rather than
    fabricating a row or raising at a caller who only wanted their skills phase
    to finish.

    RED-FIRST against the arm this replaces: the frozen ``dict(record.result)``
    copied ``actor_fresh: true`` out of the receipt, so a resume whose agent had
    been taken off the level answered that its actor was fresh. That is the
    worst of the three stale answers, because it is the one the client is told
    it may trust.

    ANTI-VACUITY. ``actor_fresh`` is asserted alongside the recorded row coming
    back INTACT, so an implementation that reported ``false`` by blanking the
    actor fails, and one that kept the actor by reporting ``true`` fails.
    """

    from agent_runtime.office_store import OfficeStore

    _seed_workspace()
    key = "resume-actor-gone"
    refused = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_resume_gone_agent_2",
            skills=["not-a-skill-anyone-has"],
        ),
        persona=qa_persona,
    )
    assert refused.refusal is not None
    recorded = _reservation_record(key)["result"]
    OfficeStore().remove_actor(WORKSPACE, recorded["actor_key"], updated_by="operator")

    resumed = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_resume_gone_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )

    assert resumed.refusal is None
    assert resumed.result["actor_fresh"] is False
    # Unchanged, not invented.
    assert resumed.result["actor"] == recorded["actor"]
    assert resumed.result["position"] == recorded["position"]
    assert resumed.result["revision"] == recorded["revision"]
    # The phase this retry existed to run still ran.
    assert resumed.result["skills"]["assigned"] == ["harness-qa-verdict"]


def test_every_ok_exit_answers_one_observation_of_the_live_row(
    qa_persona, isolated_shared_skills
):
    """H-H2: the three ok exits stamp the four observation fields from ONE builder.

    The two existing pins each hold ONE arm to the re-read rule — S4 the
    ``done`` replay, S4b the ``placed`` resume — and both were written after
    that arm had already shipped frozen. Neither says anything about the third
    arm, and neither would notice a FOURTH exit built the old way. This one is
    pinned at the level the guarantee actually lives at: whatever exit answers
    ``ok``, ``actor``/``position``/``revision``/``actor_fresh`` describe the row
    as the store holds it at that moment, and describe it consistently with each
    other.

    ANTI-VACUITY. The actor is MOVED through a real store write between the
    fresh create and the two replays, so a reply that echoed its receipt reports
    the old coordinates and the old revision and reds. The fresh arm is in the
    same assertion loop because it is the arm whose fields used to be built
    inline — a reader has to be able to see it obeying the same rule, not merely
    to be told that it does.

    KILLING MUTATIONS: drop ``result["revision"] = actor.revision`` from
    :func:`agent_create._reply` and the two replay arms report the receipt's
    revision beside a freshly-read actor; drop
    ``result["actor"] = office_actor_wire_row(actor)`` and the fresh arm has no
    ``actor`` key at all. Either way one builder is proven load-bearing for
    every exit rather than for the arm its own test was written against.
    """

    from agent_runtime.office_models import office_actor_wire_row
    from agent_runtime.office_store import OfficeStore

    def _agent_position(actor):
        for item in actor.items:
            if item.kind == "agent":
                return [float(item.position[0]), float(item.position[1])]
        raise AssertionError("the create authors exactly one agent item")

    def _assert_describes_the_live_row(label, result):
        stored = OfficeStore().get_actor(WORKSPACE, result["actor_key"])
        assert result["actor_fresh"] is True, label
        assert result["actor"] == office_actor_wire_row(stored), label
        assert result["revision"] == stored.revision, label
        assert result["revision"] == result["actor"]["revision"], label
        assert result["position"] == _agent_position(stored), label

    _seed_workspace()

    # Exit 1 — the fresh write.
    fresh = perform_agent_create(
        _params(idempotency_key="one-obs-fresh", placement_id="qa_one_obs_a_agent_2"),
        persona=qa_persona,
    )
    assert fresh.refusal is None
    _assert_describes_the_live_row("fresh", fresh.result)

    # Exit 3 — the ``placed`` resume. Set up before the move so that BOTH
    # replay arms have a receipt older than the drag below.
    resume_key = "one-obs-resume"
    refused = perform_agent_create(
        _params(
            idempotency_key=resume_key,
            placement_id="qa_one_obs_b_agent_2",
            skills=["not-a-skill-anyone-has"],
        ),
        persona=qa_persona,
    )
    assert refused.refusal is not None
    assert _reservation_state(resume_key) == "placed"

    # The drag. A real store write through the door an operator uses, so both
    # receipts are now stale in revision AND in position.
    for actor_key, moved_to in (
        (fresh.result["actor_key"], [61.0, -23.0]),
        (_reservation_record(resume_key)["result"]["actor_key"], [62.0, -24.0]),
    ):
        stored = OfficeStore().get_actor(WORKSPACE, actor_key)
        OfficeStore().upsert_actor(
            WORKSPACE,
            {
                "actor_key": actor_key,
                "persona_id": stored.persona_id,
                "persona_instance_id": stored.persona_instance_id,
                "items": [
                    {
                        "item_id": item.item_id,
                        "kind": item.kind,
                        "position": moved_to,
                        "folder": item.folder,
                        "display_name": item.display_name,
                    }
                    for item in stored.items
                ],
            },
        )

    # Exit 2 — the ``done`` replay of the fresh key.
    replayed = perform_agent_create(
        _params(idempotency_key="one-obs-fresh", placement_id="qa_one_obs_a_agent_2"),
        persona=qa_persona,
    )
    assert replayed.refusal is None
    assert replayed.result["idempotent_replay"] is True
    assert replayed.result["position"] == [61.0, -23.0]
    _assert_describes_the_live_row("done replay", replayed.result)

    resumed = perform_agent_create(
        _params(idempotency_key=resume_key, placement_id="qa_one_obs_b_agent_2"),
        persona=qa_persona,
    )
    assert resumed.refusal is None
    assert resumed.result["idempotent_replay"] is False
    assert resumed.result["position"] == [62.0, -24.0]
    _assert_describes_the_live_row("placed resume", resumed.result)


def test_the_skills_block_says_whether_the_agent_inherits_or_was_overridden(
    qa_persona, isolated_shared_skills
):
    """``inherited`` (D11) — the key that separates two agents ``assigned: []``
    cannot.

    A create that sent no ``skills`` leaves ``skill_overrides = None`` and the
    agent inherits its persona's skills, live. A create that sent ``skills: []``
    writes an EXPLICIT empty override: an agent with no skills at all. Both
    render ``assigned: []``, so before this key a client reading the ack could
    not tell them apart — and the launcher renders them differently.

    RED-FIRST: the key does not exist on HEAD, so every ``inherited`` assertion
    below fails there.

    KILLING MUTATION: stamp ``"inherited": True`` on ``run_skills_phase``'s
    return and the two overridden arms red; stamp ``False`` in
    ``_inherited_skills_ack`` and the absent arm reds.

    ANTI-VACUITY. Each arm is asserted against the STORE's ``skill_overrides``
    in the same breath, so the flag is pinned to the thing it claims to describe
    rather than to a constant that happens to match on one path. The
    explicit-empty arm is why: it is the only case where the flag and
    ``assigned`` disagree, and a mutant deriving ``inherited`` from
    ``not assigned`` passes both other arms and fails only that one.
    """

    _seed_workspace()

    absent = perform_agent_create(
        _params(idempotency_key="inh-absent", placement_id="qa_inh_absent_agent_2"),
        persona=qa_persona,
    )
    assert absent.refusal is None
    assert _overrides("personainst_qa_inh_absent_agent_2") is None
    assert absent.result["skills"] == {
        "assigned": [],
        "installed": [],
        "inherited": True,
    }

    explicit_empty = perform_agent_create(
        _params(idempotency_key="inh-empty", placement_id="qa_inh_empty_agent_2", skills=[]),
        persona=qa_persona,
    )
    assert explicit_empty.refusal is None
    assert _overrides("personainst_qa_inh_empty_agent_2") == []
    # The same ``assigned`` as the arm above, and the opposite meaning.
    assert explicit_empty.result["skills"]["assigned"] == []
    assert explicit_empty.result["skills"]["inherited"] is False

    overridden = perform_agent_create(
        _params(
            idempotency_key="inh-set",
            placement_id="qa_inh_set_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )
    assert overridden.refusal is None
    assert _overrides("personainst_qa_inh_set_agent_2") == ["harness-qa-verdict"]
    assert overridden.result["skills"]["inherited"] is False


def test_a_resume_that_sends_no_skills_renders_the_same_block(
    qa_persona, isolated_shared_skills
):
    """The resume arm renders the same block as the fresh one.

    Two sites build the absent-request ack — the fresh path and the ``placed``
    re-entry — and a client cannot see which one answered it. The pin is here
    because the two were separately written and the second is the one no fresh
    create ever exercises.
    """

    _seed_workspace()
    key = "inh-resume"
    refused = perform_agent_create(
        _params(
            idempotency_key=key,
            placement_id="qa_inh_resume_agent_2",
            skills=["not-a-skill-anyone-has"],
        ),
        persona=qa_persona,
    )
    assert refused.refusal is not None

    resumed = perform_agent_create(
        _params(idempotency_key=key, placement_id="qa_inh_resume_agent_2"),
        persona=qa_persona,
    )

    assert resumed.refusal is None
    assert resumed.result["skills"] == {
        "assigned": [],
        "installed": [],
        "inherited": True,
    }
    # The phase never ran, so nothing was written and the agent still inherits.
    assert _overrides("personainst_qa_inh_resume_agent_2") is None


def test_an_absent_skills_key_leaves_the_override_inheriting(
    qa_persona, isolated_shared_skills
):
    """ABSENT is not ``[]``, and the difference is a different agent.

    ``None`` on ``skill_overrides`` means "inherit the persona's skills, live"
    (``models.apply_instance_model_overrides``); ``[]`` means "override with
    nothing", i.e. an agent with no skills at all. A create that wrote ``[]`` for
    every client that sent no opinion would silently strip every placed agent of
    its persona's skills — the same collapse this slice fixes one door over, in
    ``persona instance update-profile``.
    """

    _seed_workspace()
    outcome = perform_agent_create(
        _params(idempotency_key="skills-absent", placement_id="qa_skills_absent_agent_2"),
        persona=qa_persona,
    )

    assert outcome.refusal is None
    assert _overrides("personainst_qa_skills_absent_agent_2") is None
    # The ack block is still PRESENT and empty: one shape whatever was asked.
    assert outcome.result["skills"] == {
        "assigned": [],
        "installed": [],
        # S4b/D11: what makes this ack different from an explicit ``skills: []``.
        "inherited": True,
    }
    assert "skills_ms" in outcome.result["phases"]


def test_an_explicitly_empty_skills_list_is_an_explicit_override(
    qa_persona, isolated_shared_skills
):
    """The other side of the same distinction, so neither can be "simplified"
    into the other without a red test."""

    _seed_workspace()
    outcome = perform_agent_create(
        _params(
            idempotency_key="skills-empty",
            placement_id="qa_skills_empty_agent_2",
            skills=[],
        ),
        persona=qa_persona,
    )

    assert outcome.refusal is None
    assert _overrides("personainst_qa_skills_empty_agent_2") == []


def test_the_ack_reports_the_install_it_actually_did(
    qa_persona, isolated_shared_skills
):
    """``skills.installed`` is a RECEIPT, not a restatement of the request.

    ANTI-VACUITY. The second create is the probe: on a shared root that already
    holds the hash-equal package the install performs no copy, and
    ``changed: false`` is the only honest thing to say. A mutant that hard-coded
    ``changed: true`` (or echoed the request) passes the first block and fails
    the second, and the installed hash is asserted equal ACROSS the two creates
    so "changed: false" cannot mean "it silently installed something else".
    """

    _seed_workspace()
    first = perform_agent_create(
        _params(
            idempotency_key="skills-receipt-1",
            placement_id="qa_receipt_1_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )
    assert first.refusal is None
    installed = first.result["skills"]["installed"]
    assert [row["skill"] for row in installed] == ["harness-qa-verdict"]
    assert installed[0]["changed"] is True

    second = perform_agent_create(
        _params(
            idempotency_key="skills-receipt-2",
            placement_id="qa_receipt_2_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )
    assert second.refusal is None
    again = second.result["skills"]["installed"][0]
    assert again["changed"] is False
    assert again["installed_hash"] == installed[0]["installed_hash"]


def test_a_traversal_shaped_skill_id_never_reaches_the_filesystem(
    qa_persona, isolated_shared_skills, monkeypatch
):
    """D5's "never path-joined from input", asserted as a NEGATIVE.

    ANTI-VACUITY, and this is the only shape that works: the refusal alone
    proves nothing, because a resolver walk that joined the path and found
    nothing ALSO refuses ``missing``. So ``resolve_skills`` is replaced with a
    function that raises, and the test asserts the refusal arrives anyway —
    i.e. the id was rejected before any root was consulted.
    """

    from agent_runtime import agent_create

    _seed_workspace()

    def _never(*args, **kwargs):
        raise AssertionError("a traversal-shaped id reached the skill resolver")

    monkeypatch.setattr("agent.skill_utils.resolve_skills", _never)

    outcome = perform_agent_create(
        _params(
            idempotency_key="skills-traversal",
            placement_id="qa_traversal_agent_2",
            skills=["../../../etc/passwd"],
        ),
        persona=qa_persona,
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "skill_unresolved"
    assert outcome.refusal.data["status"] == "missing"
    assert agent_create.PHASE_SKILLS == "skills"


def test_the_skills_phase_is_billed_in_phases(qa_persona, isolated_shared_skills):
    """``skills_ms`` exists so a cold machine's ``copytree`` is billed, not
    hidden, and ``total_ms`` is re-stamped AFTER the phase so it is not short by
    exactly that cost.

    ANTI-VACUITY. ``total_ms >= skills_ms`` is the claim a stale ``total_ms``
    (measured before the phase ran) can fail, and it is asserted on the create
    that actually installs — the one where the phase costs real milliseconds.
    """

    _seed_workspace()
    outcome = perform_agent_create(
        _params(
            idempotency_key="skills-phases",
            placement_id="qa_phases_agent_2",
            skills=["harness-qa-verdict"],
        ),
        persona=qa_persona,
    )

    phases = outcome.result["phases"]
    assert set(phases) == {"instance_ms", "placement_ms", "skills_ms", "total_ms"}
    assert phases["total_ms"] >= phases["skills_ms"]

