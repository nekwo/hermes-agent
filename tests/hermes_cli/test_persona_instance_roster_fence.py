"""UC-H4 — the legacy argv create lanes stop minting for phantom personas.

`harness persona instance create --add-instance` and `... open-chat
--add-instance` called ``_persona_by_id`` and then never looked at the answer.
An id that merely TOKENISED minted a durable roster row and a chat root bound
to a persona that does not exist — reproduced live on 2026-08-16 with
``--persona qa_agent`` against a roster of base/backend_dev/dev/
neko_supervisor/qa.

This is a deliberate contract change: fail-open becomes fail-closed for bogus
BARE ids only. Every non-breaking caller class in the plan's §4 has a witness
here, because "no caller is affected" is a claim and these are the proof.

Everything drives the REAL argparse tree, so the refusal is fenced where an
operator actually meets it rather than at a handler nothing routes to.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root inside this test's tmp dir, and PROVE it landed.

    These tests mint durable roster rows and chat roots. Writing them into the
    operator's live root would be silent and permanent, so the pin is asserted
    rather than assumed.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}."
    )
    return root


class _RecordingSessionDB:
    """Records whether a chat session was ever ensured.

    This is the SECOND, independent witness. ``_ensure_persona_chat_session``
    runs strictly AFTER the mint in both handlers, so "no session row" cannot
    be produced by a create that minted and then failed later — it can only be
    produced by a create that never reached the mint at all.
    """

    def __init__(self):
        self.created: list[str] = []
        self.titles: dict[str, str] = {}

    def create_session(self, session_id, source, **kwargs):
        self.created.append(session_id)
        return session_id

    def get_session(self, session_id):
        return None

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title

    def append_message(self, session_id, role, content=None, **kwargs):
        pass

    def get_messages(self, session_id, include_inactive=False):
        return []


@pytest.fixture
def session_db(monkeypatch):
    from hermes_cli import harness

    db = _RecordingSessionDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    return db


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


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _instance_ids() -> set[str]:
    directory = paths.persona_instances_dir()
    if not directory.exists():
        return set()
    return {path.name for path in directory.iterdir()}


def _create(capsys, persona: str, placement: str = "ph_one_agent_2", **flags):
    argv = [
        "harness", "persona", "instance", "create",
        "--persona", persona,
        "--title", "T",
        "--message", "m",
        "--display-name", "Placed Agent",
        "--add-instance",
        "--placement-id", placement,
        "--json",
    ]
    code = _dispatch(argv)
    return code, json.loads(capsys.readouterr().out)


def _open_chat(capsys, persona: str, placement: str = "ph_open_agent_2", extra: list | None = None):
    argv = [
        "harness", "persona", "instance", "open-chat",
        "--persona", persona,
        "--add-instance",
        "--placement-id", placement,
        "--json",
        *(extra or []),
    ]
    code = _dispatch(argv)
    return code, json.loads(capsys.readouterr().out)


# ── the fence ────────────────────────────────────────────────────────────────


def test_create_add_instance_with_an_unknown_persona_exits_2_and_mints_nothing(
    qa_persona, session_db, capsys
):
    """ANTI-VACUITY, two independent absences.

    Kill-mutation: delete the ``require_known_persona`` branch. Its whole
    effect is to let the create PROCEED, which is exactly what makes both
    probed absences stop holding — the mutant cannot satisfy a probe defined by
    its own writes not happening.

    Witness 1 is the persona-instances directory listing. Witness 2 is the chat
    session, and it is genuinely independent of witness 1: it lives in the
    SessionDB, not the runtime root, and is written by a different call
    (``_ensure_persona_chat_session``) at a strictly later point in the
    handler. A change that made the mint silently no-op while still opening the
    chat would pass witness 1 and fail witness 2, and vice versa.
    """

    before = _instance_ids()

    code, data = _create(capsys, "qa_agent")

    assert code == 2
    assert data["ok"] is False
    assert data["reason"] == "persona_not_found"
    assert "harness agent list" in data["error"]

    assert _instance_ids() == before
    assert session_db.created == []


def test_open_chat_add_instance_with_an_unknown_persona_exits_2_and_mints_nothing(
    qa_persona, session_db, capsys
):
    """Same fence, other door. Same anti-vacuity argument."""

    before = _instance_ids()

    code, data = _open_chat(capsys, "qa_agent")

    assert code == 2
    assert data["reason"] == "persona_not_found"
    assert _instance_ids() == before
    assert session_db.created == []


# ── the non-breaking witnesses, one per caller class in the plan's §4 ────────


def test_create_with_a_roster_persona_still_mints(qa_persona, session_db, capsys):
    """The over-broad-guard witness. A fence that refused everything would make
    both tests above pass forever.

    Kill-mutation (MEASURED): make ``require_known_persona`` always refuse.
    Both this test and the ``profile:`` one below go red, which is what an
    over-broad guard looks like from the caller's side.
    """

    code, data = _create(capsys, "qa", placement="ph_real_agent_2")

    assert code == 0
    assert data["ok"] is True
    assert data["persona_instance_id"] == "personainst_ph_real_agent_2"
    assert paths.persona_instance_path("personainst_ph_real_agent_2").exists()
    assert session_db.created == [data["default_chat_session_id"]]


def test_a_profile_id_for_a_profile_that_owns_nothing_still_instantiates(
    qa_persona, session_db, capsys
):
    """Decision D-U1, on the argv lane. The launcher's template/preset browser
    lowers ``persona.profile.instantiate`` to THIS verb with a ``profile:``
    id, and ``_persona_by_id`` synthesises a persona for profiles that own
    nothing. The token here deliberately matches no persona and no profile —
    ``profile:qa`` would pass even under a mutant that validated profile ids,
    since ``qa`` is seeded, and would prove nothing.

    ANTI-VACUITY, corrected after MEASURING rather than asserting. The obvious
    claim — "kill: drop the ``profile:`` carve-out" — is FALSE on this lane,
    and the mutant survives:

    * ``_persona_is_unknown`` short-circuits on a caller-supplied persona
      object, and the CLI's ``_persona_by_id`` SYNTHESISES one for any
      ``profile:<token>``. So this lane never reaches the carve-out;
    * dropping the object short-circuit alone also survives, because THEN it
      reaches the carve-out.

    The two protections are redundant here by design, so only the combined
    mutation is lethal — measured, and it kills exactly this test. The
    single-mutation witness for this lane is the over-broad guard
    (``require_known_persona`` always refusing), which kills it too. The
    carve-out's own single-mutation witness lives on the RPC lane, where no
    persona object is ever supplied:
    ``test_serve_rpc_agent_create.py::test_a_profile_id_for_a_profile_that_owns_nothing_still_creates``.
    """

    code, data = _create(capsys, "profile:nosuchprofile", placement="ph_profile_agent_2")

    assert code == 0
    assert data["persona_id"] == "profile:nosuchprofile"
    assert paths.persona_instance_path("personainst_ph_profile_agent_2").exists()


def test_open_chat_without_add_instance_is_untouched_by_the_fence(
    qa_persona, session_db, capsys
):
    """The recovery lane the fence must NOT break.

    ``open-chat --session-id`` REBINDS an existing instance and mints no
    roster row; the 2026-07-25 incident recovery replayed ten bindings through
    it. Guarding it would refuse a repair for a persona whose config row had
    been removed — the very situation an operator uses it in. So the fence is
    scoped to ``--add-instance``, and this proves the scoping.

    MEASURED, and stronger than expected: this rebind SUCCEEDS today for an
    unknown persona id (exit 0), so the probe is a plain success. Under a
    mutant that guarded the whole verb rather than the ``--add-instance``
    branch, this becomes exit 2 with ``reason: persona_not_found`` — which the
    mutant cannot avoid, since refusing is the entirety of what it does.

    Recorded rather than tightened: whether a rebind SHOULD accept an
    unknown-persona id is a separate ruling. UC-H4 fences MINTING, and
    smuggling a second behaviour change in beside it is the delete-and-see this
    program keeps getting burned by.
    """

    code = _dispatch(
        [
            "harness", "persona", "instance", "open-chat",
            "--persona", "qa_agent",
            "--session-id", "no_such_session",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data.get("reason") != "persona_not_found"
    assert "persona" not in str(data.get("error", ""))
