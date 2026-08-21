"""``harness agent create`` — the operator's unified, serve-absent create verb.

Every test drives the REAL argparse tree and dispatches through ``args.func``,
never by poking the handler directly. That is deliberate: a handler nothing
routes to is a verb no operator can run, and this program has been bitten by
exactly that before (``test_serve_rpc_agent_create``'s serve-loop test exists
for the same reason).

Nothing here spawns a ``harness serve``. The claim under test is precisely that
none is needed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_create_verb"


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    ``tests/conftest.py`` already sandboxes ``HERMES_HOME``, and the default
    resolution hangs the store root off it — but "already" is an assumption,
    and the thing being assumed is whether these tests write into the
    OPERATOR's live runtime root. The env pin wins over both the config file
    and the default (``resolution.resolve_runtime``), and the assertion below
    turns a regression in that precedence into a loud failure instead of a
    silent live-root write.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    return root


@pytest.fixture
def qa_persona():
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


@pytest.fixture
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _create(capsys, *extra: str, persona: str = "qa", workspace: str = WORKSPACE):
    argv = [
        "harness", "agent", "create",
        "--persona", persona,
        "--workspace", workspace,
        "--pos", "3.5", "-1.25",
        "--json",
        *extra,
    ]
    code = _dispatch(argv)
    return code, json.loads(capsys.readouterr().out)


def _actors(workspace_id: str = WORKSPACE) -> dict:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key: actor for actor in OfficeStore().list_actors(workspace_id)}


# ── the verb places an agent ─────────────────────────────────────────────────


def test_the_verb_creates_both_rows_and_echoes_the_rpc_reply_shape(
    qa_persona, seeded_workspace, capsys
):
    """ANTI-VACUITY. The kill-mutation is "route the verb to the old
    ``add_instance``-only sequence" (door 5, `persona instance create
    --add-instance`). That sequence contains no office write ANYWHERE in its
    handler, so the probe — an actor read back out of ``OfficeStore`` under the
    returned ``actor_key`` — is one the mutated path structurally cannot set.
    The roster row alone would pass under the mutant, which is why the roster
    row is not the probe.
    """

    code, data = _create(capsys, "--idempotency-key", "verb-1", "--placement-id", "qa_verb")

    assert code == 0
    assert data["ok"] is True
    assert data["persona_instance_id"] == "personainst_qa_verb"

    # The witness the two-call door cannot produce.
    actors = _actors()
    assert data["actor_key"] in actors
    assert actors[data["actor_key"]].persona_instance_id == "personainst_qa_verb"
    assert [float(v) for v in actors[data["actor_key"]].items[0].position] == [3.5, -1.25]

    # And the roster half is durable on disk, not merely reported.
    assert paths.persona_instance_path("personainst_qa_verb").exists()

    # The shared naming rule rode along: "Qa" here would mean the CLI lane went
    # to the store template instead of the persona's configured name.
    assert data["display_name"] == "QA Agent"


def test_the_verb_returns_the_same_dict_the_rpc_returns(
    qa_persona, seeded_workspace, capsys
):
    """§8.3's drift fence: two printers of one dict.

    Compared as KEY SETS, not values — the ids and timings differ per create by
    construction. If a consumer ever depends on the bytes, that is its bug to
    state; what must never differ is the shape.

    Two keys are CLI-envelope-only and named here rather than glossed over:
    ``ok`` (the exit status, which the wire carries as the frame's shape) and
    ``resolution`` (the root-observability block every ``--json`` harness verb
    stamps). The RESULT fields are identical, which is the claim.
    """

    from agent_runtime import serve_rpc

    _, cli = _create(capsys, "--idempotency-key", "verb-parity-cli", "--placement-id", "qa_p_cli")

    rpc = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "p1",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": "qa",
                "workspace_id": WORKSPACE,
                "position": [3.5, -1.25],
                "idempotency_key": "verb-parity-rpc",
                "placement_id": "qa_p_rpc",
            },
        }
    )["result"]

    assert set(cli) - {"ok", "resolution"} == set(rpc)
    assert set(cli["phases"]) == set(rpc["phases"])
    # The observability block is REQUIRED, not incidental: an envelope that
    # cannot say which root answered cannot be trusted when it is empty.
    assert cli["resolution"]["store_root"] == str(paths.store_root())


def test_rerunning_with_the_same_idempotency_key_replays_and_writes_nothing(
    qa_persona, seeded_workspace, capsys
):
    """ANTI-VACUITY, and this is the suite's own recorded lesson: ids re-derive
    identically, REVISIONS do not. The kill-mutation (bypass the reservation
    and call the stores directly) returns the same ``persona_instance_id`` —
    it is derived from the placement id — while ``upsert_actor`` bumps the
    revision monotonically. So the probe is the revision, plus the
    ``idempotent_replay`` flag as the cheap corroborator; the mutated path
    cannot re-write the actor without moving the revision.
    """

    _, first = _create(capsys, "--idempotency-key", "verb-same", "--placement-id", "qa_same")
    assert first["idempotent_replay"] is False

    code, second = _create(capsys, "--idempotency-key", "verb-same", "--placement-id", "qa_same")

    assert code == 0
    assert second["idempotent_replay"] is True
    assert second["persona_instance_id"] == first["persona_instance_id"]
    assert _actors()[first["actor_key"]].revision == 1


def test_an_omitted_idempotency_key_makes_every_run_a_new_gesture(
    qa_persona, seeded_workspace, capsys
):
    """The launcher's micros-stamp rule, ported. A default that was STABLE
    across runs would turn the operator's second create into a silent replay of
    the first — the surprise this default exists to avoid."""

    _, first = _create(capsys, "--placement-id", "qa_gesture_one")
    _, second = _create(capsys, "--placement-id", "qa_gesture_two")

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is False
    assert second["persona_instance_id"] != first["persona_instance_id"]
    assert len(_actors()) == 2


# ── refusals that must write NOTHING ─────────────────────────────────────────


def test_an_unknown_persona_refuses_before_any_write(
    qa_persona, seeded_workspace, capsys
):
    """The reproduced defect, on the verb. ANTI-VACUITY: absence probes, and
    the kill-mutation (drop UC-H2's roster branch) makes the create proceed —
    whose entire effect is to make those absences exist."""

    import hashlib

    before = set(_actors())

    code, data = _create(
        capsys, "--idempotency-key", "verb-bogus", "--placement-id", "qa_agent_x",
        persona="qa_agent",
    )

    assert code == 2
    assert data["ok"] is False
    assert data["reason"] == "persona_not_found"
    assert "harness agent list" in data["error"]

    assert not paths.persona_instance_path("personainst_qa_agent_x").exists()
    digest = hashlib.sha256("verb-bogus".encode("utf-8")).hexdigest()
    assert not paths.agent_create_reservation_path(digest).exists()
    assert set(_actors()) == before


# ── the roster FAULT, on both lanes (RD-H6 item 2) ───────────────────────────
#
# A roster fault answered the two create lanes differently. The RPC lane passes
# no pre-resolved persona, so the service's own strict read (``persona_roster``)
# wrapped the fault into a typed ``persona_roster_unavailable`` refusal naming
# the RUNTIME as the subject. The CLI resolves its richer persona object FIRST,
# through ``_persona_by_id`` -> ``ensure_persisted_personas`` — the same call,
# unwrapped — so an unreadable roster tracebacked out of the verb. One fault,
# two renderings, and the argv one named no cure.


@pytest.fixture
def corrupt_roster(qa_persona):
    """A roster the runtime cannot READ — a REAL fault, not a patched one.

    ``AgentStore.list_all`` catches only ``NotFound`` (an archive move racing a
    ``glob``); a file that is not JSON raises out of it. So one unparseable
    persona file is a genuine roster-read fault, and — this is why the fixture is
    a file rather than a monkeypatch — it is the SAME fault on both lanes by
    construction, reached through each lane's own resolver. A patched loader
    could be installed on one lane's binding and miss the other's, which would
    make the parity test below assert agreement it had arranged.
    """

    path = paths.agents_dir() / "broken_persona.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this file is not json", encoding="utf-8")
    return path


def test_an_unreadable_roster_is_a_typed_refusal_not_a_traceback(
    corrupt_roster, seeded_workspace, capsys
):
    """ANTI-VACUITY, and the probes are chosen against the ACTUAL mutant.

    MEASURED MUTATION: unwrap the roster read (drop the ``except`` and let
    ``_persona_by_id`` raise). ``args.func(args)`` then raises
    ``json.JSONDecodeError`` out of the dispatch, so ``code`` is never bound and
    ``data`` never parses — the mutant cannot satisfy ANY assertion here, and it
    fails at the dispatch line rather than at a probe.

    ``reason`` is the load-bearing probe, not "was it refused". SECOND MEASURED
    MUTATION: have the typed refusal spend ``persona_not_found``'s reason and
    message instead. It still refuses, still exits 2, still writes nothing and
    still prints no traceback — every other probe here is satisfied — while
    blaming the operator's id for a runtime fault and sending them to `harness
    agent list` to look for a typo that does not exist. That is the collapse
    ``PersonaRosterUnavailable`` exists to prevent, and the reason string plus
    the two negative prose probes are the only things that catch it.
    """

    import hashlib

    before = set(_actors())

    code, data = _create(
        capsys, "--idempotency-key", "verb-roster-fault", "--placement-id", "qa_fault"
    )
    captured = capsys.readouterr()

    assert code == 2
    assert data["ok"] is False
    assert data["reason"] == "persona_roster_unavailable"
    # The subject is the runtime, never the id — the operator must not be sent
    # hunting a typo that does not exist.
    assert "not in the agent roster" not in data["error"]
    assert "harness agent list" not in data["error"]
    # No traceback ANYWHERE, on either stream. Both are asserted because a
    # partially-caught fault could print one and still exit cleanly.
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out

    # A refusal before the service is still a refusal that wrote nothing.
    assert not paths.persona_instance_path("personainst_qa_fault").exists()
    digest = hashlib.sha256("verb-roster-fault".encode("utf-8")).hexdigest()
    assert not paths.agent_create_reservation_path(digest).exists()
    assert set(_actors()) == before


def test_both_create_lanes_name_the_same_roster_fault(
    corrupt_roster, seeded_workspace, capsys
):
    """Parity means the SAME token, compared for equality rather than eyeballed.

    The RPC lane's own typed-refusal witness
    (``tests/agent_runtime/test_agent_create_service.py``'s
    ``test_an_unreadable_roster_is_its_own_reason_not_persona_not_found``) stays
    byte-unchanged; this is the cross-lane half it cannot express. Both the
    machine-readable reason AND the operator prose are compared, because a fix
    that shared only the reason would still hand the two lanes different
    sentences for one fault — and the prose is what an operator acts on.
    """

    from agent_runtime import serve_rpc

    _, cli = _create(
        capsys, "--idempotency-key", "verb-parity-fault", "--placement-id", "qa_pf_cli"
    )
    rpc = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "rf1",
            "method": "runtime.agent.create",
            "params": {
                "persona_id": "qa",
                "workspace_id": WORKSPACE,
                "position": [3.5, -1.25],
                "idempotency_key": "rpc-parity-fault",
                "placement_id": "qa_pf_rpc",
            },
        }
    )["error"]

    assert cli["reason"] == rpc["data"]["reason"] == "persona_roster_unavailable"
    assert cli["error"] == rpc["message"]
    # And the code the CLI's exit-code table was keyed on is the RPC lane's, so
    # the two lanes cannot drift on severity either.
    assert _AGENT_CREATE_EXIT_CODES_FOR_TEST()[rpc["code"]] == 2


def _AGENT_CREATE_EXIT_CODES_FOR_TEST() -> dict:
    """Read the verb's own table rather than re-spelling it here."""

    from hermes_cli import harness

    return harness._AGENT_CREATE_EXIT_CODES


def test_a_roster_read_that_faults_once_refuses_instead_of_renaming_the_agent(
    qa_persona, seeded_workspace, capsys, monkeypatch
):
    """The witness for RAISING rather than degrading to ``persona=None``.

    MEASURED: catching the CLI's roster fault and returning ``None`` SURVIVES
    both tests above — with no pre-resolved persona the service's own strict read
    raises and produces the identical typed refusal, so on a DURABLE fault the
    two are indistinguishable. The honest scope of that survival is one call:
    they differ only when the roster read faults ONCE (a config being rewritten
    under the process) and then succeeds.

    That difference is not cosmetic. Under the degrade, the create PROCEEDS with
    no persona object, so ``display_name`` falls back off the id — "Qa" where the
    persona's configured name is "QA Agent" — and the wrong name is written into
    a DURABLE roster row and a placement. A silent rename on a durable write is
    strictly worse than a refusal the operator can re-run, which is why the
    resolver raises. The probes are the refusal AND the absence of the instance
    file; the mutant sets both the other way.
    """

    from agent_runtime.store import AgentStore

    real_list_all = AgentStore.list_all
    calls = {"n": 0}

    def _fault_once(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("roster directory vanished mid-read")
        return real_list_all(self)

    monkeypatch.setattr(AgentStore, "list_all", _fault_once)

    code, data = _create(
        capsys, "--idempotency-key", "verb-transient", "--placement-id", "qa_transient"
    )

    assert code == 2
    assert data["reason"] == "persona_roster_unavailable"
    assert not paths.persona_instance_path("personainst_qa_transient").exists()
    # The second read really would have succeeded — otherwise this test would
    # pass against a permanently broken roster and prove nothing about the
    # one-shot case.
    assert calls["n"] == 1
    assert AgentStore().list_all()


def test_the_verb_refuses_a_missing_workspace_before_any_write(qa_persona, capsys):
    """ANTI-VACUITY, stated precisely rather than ritually.

    Primary kill: DELETE the guard. ``upsert_actor`` calls ``ensure_surface``
    and would happily author the workspace for a typo, so the mutant does not
    refuse at all — it returns 0 and a placement in a workspace nobody created.
    ``assert code == 3`` is unsatisfiable for it.

    Second, INDEPENDENT witness: the reservation receipt's absence plus the
    retry under the SAME key. Its sensitivity was MEASURED, not assumed, and
    the honest result is narrower than "kill: reorder":

    * moving the guard below ``reserve_agent_create`` alone — survives, because
      a brand-new key writes nothing until ``mark_instance_minted``;
    * making the reservation eager alone — survives, because the guard runs
      before the reservation is ever opened;
    * BOTH — killed (measured).

    That is the correct shape for this property. "A refused create leaves the
    key clean" is a property of the COMPOSITION, and either half is harmless
    on its own; the fence has to fire exactly when the composition breaks. The
    ``code == 3`` assertion above is the independent single-mutation witness.
    """

    import hashlib

    from agent_runtime.office_store import OfficeStore

    code, data = _create(
        capsys, "--idempotency-key", "verb-nows", "--placement-id", "qa_nows",
        workspace="ws_never_authored",
    )

    assert code == 3
    assert data["reason"] == "workspace_not_found"
    digest = hashlib.sha256("verb-nows".encode("utf-8")).hexdigest()
    assert not paths.agent_create_reservation_path(digest).exists()

    # The retry half needs BOTH halves of the precondition the refusal names:
    # a workspace record (MC-8/P10 -- ensure_surface refuses without one) and
    # then the surface the verb's own guard checks for. Seeding only the
    # surface used to be possible and is exactly the hole that closed.
    seed_workspace_record("ws_never_authored")
    OfficeStore().ensure_surface("ws_never_authored", created_by="seed")
    code, retry = _create(
        capsys, "--idempotency-key", "verb-nows", "--placement-id", "qa_nows",
        workspace="ws_never_authored",
    )
    assert code == 0
    assert retry["persona_instance_id"] == "personainst_qa_nows"


def test_a_malformed_position_is_one_refusal_not_an_argparse_traceback(
    qa_persona, seeded_workspace, capsys
):
    code = _dispatch(
        [
            "harness", "agent", "create",
            "--persona", "qa",
            "--workspace", WORKSPACE,
            "--pos", "left", "up",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 2
    assert data["reason"] == "position_invalid"


def test_the_human_output_path_prints_rather_than_raising(
    qa_persona, seeded_workspace, capsys
):
    """Every other test passes ``--json``. The default path is a separate
    format string over the same dict, and an unguarded key there would be a
    KeyError an operator meets and no test does."""

    code = _dispatch(
        [
            "harness", "agent", "create",
            "--persona", "qa",
            "--workspace", WORKSPACE,
            "--pos", "1", "2",
            "--placement-id", "qa_human",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "personainst_qa_human" in out

    # And the refusal's human path, which formats a different dict.
    code = _dispatch(
        [
            "harness", "agent", "create",
            "--persona", "qa_agent",
            "--workspace", WORKSPACE,
            "--pos", "1", "2",
        ]
    )
    assert code == 2
    assert "harness agent list" in capsys.readouterr().out


# ── the verb exists on the parser at all ─────────────────────────────────────


def test_the_verb_is_reachable_and_its_siblings_are_untouched():
    """A handler nothing routes to is a verb no operator can run."""

    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))

    args = root.parse_args(
        ["harness", "agent", "create", "--persona", "qa", "--workspace", "w",
         "--pos", "1", "2"]
    )
    assert args.agent_command == "create"
    assert args.func is harness._cmd_agent_create

    # The slot was free and stays free-standing: `agent list` still routes.
    assert root.parse_args(["harness", "agent", "list"]).func is harness._cmd_agent_list


# ── the refused create's own claim about itself ──────────────────────────────


def test_a_chat_store_refusal_backs_the_verbs_wrote_nothing_sentence(
    qa_persona, seeded_workspace, capsys, monkeypatch
):
    """This verb PROMISES the reader a ``rolled_back`` field, so it must exist.

    The refusal envelope this verb prints ends with "a refused create wrote
    nothing unless it says rolled_back: false" — an instruction to read a key.
    The ``chat_session_persist_failed`` arm shipped without one, so on the argv
    lane the reader had to infer "wrote nothing" from an ABSENT field, while
    the launcher's parser reads that same absence as the opposite ("not rolled
    back", its deliberate fail-safe) and told the operator to go check the
    runtime for an orphan. One payload, two readers, opposite conclusions, and
    both of them guessing. Stamping the field makes the sentence checkable.
    """

    import hashlib

    from agent_runtime import persona_chat_durability
    from agent_runtime.persona_assignments import PersonaInstanceStore

    def _no_store():
        raise persona_chat_durability.PersonaChatPersistenceError("session_db_acquire")

    monkeypatch.setattr(
        persona_chat_durability, "default_persona_session_db", _no_store
    )

    code, data = _create(
        capsys, "--idempotency-key", "verb-nostore", "--placement-id", "qa_nostore"
    )

    assert code == 1  # -32000
    assert data["reason"] == "chat_session_persist_failed"
    assert data["rolled_back"] is True
    assert "rolled_back: false" in data["next_expected"]

    # And the claim is true on disk: no roster row, no placement, no receipt.
    assert PersonaInstanceStore().list_all() == []
    assert _actors() == {}
    digest = hashlib.sha256("verb-nostore".encode("utf-8")).hexdigest()
    assert not paths.agent_create_reservation_path(digest).exists()
