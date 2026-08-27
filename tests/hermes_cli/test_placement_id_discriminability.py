"""R1 — a caller-supplied placement id must be classifiable by BOTH repos.

The wrong-alice incident of 2026-08-27: `--placement-id known_alice` minted
`personainst_known_alice`, which fails the launcher's deliberate-placement
discriminator, so the launcher read the row as a conversational channel, folded
it into the operator-channel dedupe on the shared key
`(profile:alice, "Alice Agent")`, and — newer-wins — evicted the operator's own
`personainst_profile_alice` from the roster.

Every test here drives the REAL argparse tree through `args.func`, the same rule
`test_agent_create_verb` states: a handler nothing routes to is a verb no
operator can run. All three placement doors are exercised, because the fence had
to land in three places — the two `persona instance` verbs do not pass through
`agent_create` at all.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from agent_runtime import paths
from agent_runtime.models import (
    DELIBERATE_PLACEMENT_SUFFIX,
    looks_like_deliberate_placement,
)
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_placement_discriminability"

#: The id the operator actually typed on 2026-08-27.
INCIDENT_ID = "known_alice"
#: The two shapes the launcher mints, both of which must be accepted.
LAUNCHER_HEX_ID = "qa_agent_a1b2c3d4"
LAUNCHER_COUNTER_ID = "qa_agent_2"


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}"
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
    OfficeStore().ensure_surface(WORKSPACE, created_by="seed")


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _agent_create(capsys, *extra: str):
    code = _dispatch([
        "harness", "agent", "create",
        "--persona", "qa",
        "--workspace", WORKSPACE,
        "--pos", "3.5", "-1.25",
        "--json",
        *extra,
    ])
    return code, json.loads(capsys.readouterr().out)


def _instance_create(capsys, *extra: str):
    code = _dispatch([
        "harness", "persona", "instance", "create",
        "--persona", "qa",
        "--title", "QA Agent (2)",
        "--display-name", "QA Agent (2)",
        "--add-instance",
        "--json",
        *extra,
    ])
    return code, json.loads(capsys.readouterr().out)


def _instance_open(capsys, *extra: str):
    code = _dispatch([
        "harness", "persona", "instance", "open-chat",
        "--persona", "qa",
        "--add-instance",
        "--json",
        *extra,
    ])
    return code, json.loads(capsys.readouterr().out)


# ── the shape authority, and its cross-repo peer ─────────────────────────────


def test_the_incident_id_is_exactly_what_the_predicate_rejects():
    assert not looks_like_deliberate_placement(INCIDENT_ID)
    assert looks_like_deliberate_placement(LAUNCHER_HEX_ID)
    assert looks_like_deliberate_placement(LAUNCHER_COUNTER_ID)


@pytest.mark.parametrize(
    "token",
    [
        # ANTI-VACUITY: near-misses, not obvious rubbish. Each of these would
        # pass a lazier predicate (a bare `_agent_` search, a bare-hex tail, a
        # `startswith`), and each derives an id the launcher classifies as
        # conversational.
        "qa_agent_",           # marker, no tail
        "qa_agent_xyz",        # tail is neither digits nor hex
        "qa_agent_a1b2c3d",    # 7 hex, not 8
        "qa_agent_a1b2c3d4e",  # 9 hex, not 8
        "qa_a1b2c3d4",         # the OLD hermes mint: hex tail, no marker
        "qa_agent_2_trailing",  # shape is not at the END
        "agent_2",             # marker without its leading separator
    ],
)
def test_near_misses_are_refused_by_the_predicate(token):
    assert not looks_like_deliberate_placement(token)


def test_the_pattern_is_byte_identical_to_the_launchers():
    """The one fact that makes this fence worth having.

    Two repos must answer the same question the same way about the same id. A
    hermes-only pattern that drifted from the launcher's would re-open the
    incident silently: hermes would accept an id the launcher still folds into
    the operator-channel dedupe. Read from the Dart SOURCE rather than restated
    here, so drift on either side reds this test rather than passing on a copy
    that agrees only with itself.
    """

    dart = Path(
        "X:/Unreal Engine/Engine/Launcher/EterniaLauncher/lib/features/"
        "mission_control/data/mission_agent_identity.dart"
    )
    if not dart.is_file():
        pytest.skip(f"launcher checkout not present at {dart}")
    declaration = re.search(
        r"_deliberatePlacementSuffix\s*=\s*RegExp\(r'([^']+)'\)",
        dart.read_text(encoding="utf-8"),
    )
    assert declaration, "the launcher's _deliberatePlacementSuffix moved or was renamed"
    assert declaration.group(1) == DELIBERATE_PLACEMENT_SUFFIX.pattern


def test_the_server_mint_clears_the_fence_it_installs():
    """The mint had NO `_agent_` marker until R1.

    `agent create` with `--placement-id` omitted was the incident's other door,
    and it needs no operator mistake at all: the mint's own docstring claimed
    parity with `missionMintDeliberatePlacementId` and did not have it, so every
    server-minted placement derived a conversational-looking id.
    """

    from agent_runtime.agent_create import mint_placement_id

    for persona in ("qa", "profile:alice", "backend_dev"):
        assert looks_like_deliberate_placement(mint_placement_id(persona))


# ── door 1: agent create ─────────────────────────────────────────────────────


def test_agent_create_refuses_the_incident_id(qa_persona, seeded_workspace, capsys):
    code, data = _agent_create(
        capsys, "--idempotency-key", "r1-create-bad", "--placement-id", INCIDENT_ID
    )
    assert code != 0
    assert data["reason"] == "placement_id_not_discriminable"
    # BOTH cures named: omitting is right for a caller that just wants a
    # placement, supplying the shape is right for one PREDICTING the actor key.
    assert "--placement-id" in data["error"]
    assert "_agent_" in data["error"]


def test_agent_create_refuses_before_writing_anything(
    qa_persona, seeded_workspace, capsys
):
    """ANTI-VACUITY for the refusal above: a reason with a half-built agent
    behind it would be worse than no fence, because the wedge would exist AND
    the operator would have been told it did not."""

    from agent_runtime.office_store import OfficeStore
    from agent_runtime.persona_assignments import PersonaInstanceStore

    _agent_create(
        capsys, "--idempotency-key", "r1-create-clean", "--placement-id", INCIDENT_ID
    )
    assert OfficeStore().list_actors(WORKSPACE) == []
    assert not [
        row for row in PersonaInstanceStore().list_all() if INCIDENT_ID in row.id
    ]


@pytest.mark.parametrize("placement", [LAUNCHER_HEX_ID, LAUNCHER_COUNTER_ID])
def test_agent_create_accepts_the_launcher_mints(
    qa_persona, seeded_workspace, capsys, placement
):
    code, data = _agent_create(
        capsys, "--idempotency-key", f"r1-ok-{placement}", "--placement-id", placement
    )
    assert code == 0, data
    assert data["persona_instance_id"] == f"personainst_{placement}"


def test_agent_create_accepts_an_omitted_placement_id(
    qa_persona, seeded_workspace, capsys
):
    code, data = _agent_create(capsys, "--idempotency-key", "r1-ok-minted")
    assert code == 0, data
    # The minted id must clear the fence its own lane installs — see
    # test_the_server_mint_clears_the_fence_it_installs.
    assert looks_like_deliberate_placement(data["placement_id"])
    assert looks_like_deliberate_placement(data["persona_instance_id"])


# ── doors 2 and 3: the two persona instance verbs ────────────────────────────


def test_persona_instance_create_refuses_the_incident_id(qa_persona, capsys):
    code, data = _instance_create(capsys, "--placement-id", INCIDENT_ID)
    assert code != 0
    assert data["reason"] == "placement_id_not_discriminable"


def test_persona_instance_create_accepts_a_launcher_mint(qa_persona, capsys):
    code, data = _instance_create(capsys, "--placement-id", LAUNCHER_HEX_ID)
    assert code == 0, data
    assert data["persona_instance_id"] == f"personainst_{LAUNCHER_HEX_ID}"


def test_persona_instance_open_chat_refuses_the_incident_id(qa_persona, capsys):
    code, data = _instance_open(capsys, "--placement-id", INCIDENT_ID)
    assert code != 0
    assert data["reason"] == "placement_id_not_discriminable"


def test_persona_instance_open_chat_accepts_a_launcher_mint(qa_persona, capsys):
    code, data = _instance_open(capsys, "--placement-id", LAUNCHER_HEX_ID)
    assert code == 0, data
    assert data["persona_instance_id"] == f"personainst_{LAUNCHER_HEX_ID}"


def test_the_refusal_is_the_same_reason_on_all_three_doors(
    qa_persona, seeded_workspace, capsys
):
    """One incident, one branch point.

    A client switching on `data.reason` must not need three cases for one
    mistake — which is what a per-door refusal string would have cost it.
    """

    reasons = set()
    _, data = _agent_create(
        capsys, "--idempotency-key", "r1-same", "--placement-id", INCIDENT_ID
    )
    reasons.add(data["reason"])
    _, data = _instance_create(capsys, "--placement-id", INCIDENT_ID)
    reasons.add(data["reason"])
    _, data = _instance_open(capsys, "--placement-id", INCIDENT_ID)
    reasons.add(data["reason"])
    assert reasons == {"placement_id_not_discriminable"}
