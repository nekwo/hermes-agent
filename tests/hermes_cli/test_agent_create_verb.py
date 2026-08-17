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
