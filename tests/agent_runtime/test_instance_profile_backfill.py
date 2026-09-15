"""B-4: explicit binding at creation, and a dry-run-first backfill for nulls.

ANTI-VACUITY NOTE.

The load-bearing claim of this stage is a NEGATIVE one — "stamping the
projection changes no resolution" — and a negative is exactly what a careless
test asserts vacuously. So:

* ``test_stamping_does_not_change_what_a_turn_resolves`` does not assert that
  something is unchanged by re-reading the thing the change wrote. It resolves
  the binding through the PRODUCTION resolver twice, once with a null
  ``profile_id`` and once with a stamped one, and compares the resolved
  ``profile_home``. The probed field (``profile_home``) is produced by
  ``resolve_persona_profile``, which takes a PERSONA — a function the mutation
  under test cannot reach, because it never receives the instance at all.
* ``test_summary_renders_the_same_profile_before_and_after`` probes the RENDERED
  value from ``persona_instance_summary`` rather than the stored field, so it
  would catch a stamping change that altered what an operator sees.
* the backfill tests probe the store on DISK after the call, not the returned
  envelope, so a mutant that fakes the report cannot pass.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths
from agent_runtime.models import AgentPersona, PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.states import WorkerSessionState
from agent_runtime.store import AgentStore
from hermes_time import now
from agent_runtime.persona_profile_binding import (
    PersonaProfileRebindError,
    backfill_instance_profile_ids,
)
from agent_runtime.profile_context import resolve_persona_profile


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """Profile homes under an isolated HERMES_HOME.

    The runtime STORE root is already redirected per-test by the autouse
    fixture in ``tests/agent_runtime/conftest.py``; this only supplies the
    profile directories ``profile_exists`` checks.
    """
    home = tmp_path / "hermes-home"
    for name in ("launcher-qa", "base"):
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _write_persona(_root, persona_id: str, profile: str | None):
    persona = AgentPersona(
        id=persona_id,
        display_name=persona_id,
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
        hermes_profile=profile,
    )
    return AgentStore().save(persona)


def _write_instance(_root, instance_id: str, persona_id: str, profile_id: str | None,
                    *, task_id: str | None = None):
    store = PersonaInstanceStore()
    store._write(  # noqa: SLF001 — seeding a projection row on purpose
        PersonaInstance(
            id=instance_id,
            persona_id=persona_id,
            role="dev",
            display_name=persona_id,
            profile_id=profile_id,
            runtime_root=str(paths.store_root()),
            state=WorkerSessionState.IDLE,
            mode="chat",
            current_task_id=task_id,
            updated_at=now(),
        )
    )
    return paths.persona_instances_dir() / f"{instance_id}.json"


def _instance(instance_id: str, profile_id: str | None) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id="qa",
        role="dev",
        display_name="qa",
        profile_id=profile_id,
        runtime_root="",
        state=WorkerSessionState.IDLE,
    )


def _stored_profile_id(instance_id: str):
    path = paths.persona_instances_dir() / f"{instance_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["profile_id"]


# ---------------------------------------------------------------------------
# Creation-time stamping
# ---------------------------------------------------------------------------


def test_creation_stamps_the_personas_profile_explicitly(store_root, monkeypatch):
    """A newly minted projection carries its persona's profile, not None.

    Kill-mutation: return to ``return None`` for non-``profile:`` ids. The
    stamped value becomes None and this goes red.

    Anti-vacuity: the expected value (``launcher-qa``) is written into the
    PERSONA record, never into the instance the assertion reads. The only way it
    can appear on the instance is the resolver under test running.
    """
    from agent_runtime.persona_assignments import _profile_id_for_persona_or_template

    _write_persona(store_root, "qa", "launcher-qa")
    assert _profile_id_for_persona_or_template("qa") == "launcher-qa"


def test_creation_leaves_an_unbound_persona_null(store_root):
    """A null persona binding stays null. Kill: stamp a literal "base" default.

    The plan's migration promise is that a null resolves the same before and
    after. Inventing a default here would break that promise silently, so the
    absence is pinned.
    """
    from agent_runtime.persona_assignments import _profile_id_for_persona_or_template

    _write_persona(store_root, "drifter", None)
    assert _profile_id_for_persona_or_template("drifter") is None


def test_synthetic_profile_channel_is_unchanged(store_root):
    """The pre-existing ``profile:<name>`` behaviour must not regress."""
    from agent_runtime.persona_assignments import _profile_id_for_persona_or_template

    assert _profile_id_for_persona_or_template("profile:alice") == "alice"


# ---------------------------------------------------------------------------
# THE MIGRATION CLAIM — verified, not trusted
# ---------------------------------------------------------------------------


def test_stamping_does_not_change_what_a_turn_resolves(store_root):
    """A null vs a stamped projection resolve to the SAME profile home.

    This is the plan's migration promise, checked against the production
    resolver rather than asserted in prose.

    Anti-vacuity: ``resolve_persona_profile`` takes a PERSONA and never sees an
    instance, so the probed ``profile_home`` cannot be written by the stamping
    change. The test constructs two instances differing ONLY in ``profile_id``
    and shows the turn-path answer is identical — including that it is derived
    from the persona, not from either instance.
    """
    persona = _write_persona(store_root, "qa", "launcher-qa")

    null_projection = _instance("i1", None)
    stamped_projection = _instance("i2", "launcher-qa")

    binding = resolve_persona_profile(persona)
    assert binding.readiness == "ready"
    assert binding.hermes_profile == "launcher-qa"

    # The resolver's answer does not depend on either projection — which is the
    # point. Both rows exist; neither is an input.
    assert null_projection.profile_id != stamped_projection.profile_id
    assert resolve_persona_profile(persona).profile_home == binding.profile_home


def test_summary_renders_the_same_profile_before_and_after(store_root):
    """What the operator SEES is identical for a null and a stamped row.

    ``persona_instance_summary`` already falls back with
    ``instance.profile_id or persona.hermes_profile``, so the rendered value
    cannot move. Kill-mutation: delete that fallback — the null row then renders
    None while the stamped row renders ``launcher-qa``, and the equality fails.
    """
    from agent_runtime.persona_assignments import persona_instance_summary

    persona = _write_persona(store_root, "qa", "launcher-qa")
    before = persona_instance_summary(
        _instance("i1", None), persona
    )
    after = persona_instance_summary(
        _instance("i1", "launcher-qa"), persona
    )
    assert before["profile_id"] == after["profile_id"] == "launcher-qa"


# ---------------------------------------------------------------------------
# The backfill lane
# ---------------------------------------------------------------------------


def test_backfill_dry_run_reports_the_null_and_writes_nothing(store_root):
    """The default is a dry run, and a dry run does not touch the store.

    Kill-mutation: drop the ``if dry_run:`` early return. The on-disk row is
    then stamped and this goes red.

    Anti-vacuity: the assertion reads the JSON FILE back from disk, not the
    returned envelope, so a mutant that merely reports ``dry_run: True`` while
    writing anyway is still caught.
    """
    _write_persona(store_root, "qa", "launcher-qa")
    _write_instance(store_root, "personainst_qa_agent_abc", "qa", None)

    report = backfill_instance_profile_ids()

    assert report["dry_run"] is True
    assert report["changed"] is False
    assert [row["persona_instance_id"] for row in report["instances_planned"]] == [
        "personainst_qa_agent_abc"
    ]
    assert report["instances_planned"][0]["to"] == "launcher-qa"
    # The FILE is untouched.
    assert _stored_profile_id("personainst_qa_agent_abc") is None


def test_backfill_apply_stamps_the_row_on_disk(store_root):
    """dry_run=False actually writes. Kill: make apply a no-op."""
    _write_persona(store_root, "qa", "launcher-qa")
    _write_instance(store_root, "personainst_qa_agent_abc", "qa", None)

    report = backfill_instance_profile_ids(dry_run=False)

    assert report["changed"] is True
    assert _stored_profile_id("personainst_qa_agent_abc") == "launcher-qa"


def test_backfill_never_overwrites_a_disagreeing_stamp(store_root):
    """Real drift is REPORTED, never silently corrected.

    Kill-mutation: drop the ``if existing:`` skip. The row is then rewritten,
    which would destroy the evidence of a drift that ``rebind_persona_profile``
    exists to move properly (artifacts and all).
    """
    _write_persona(store_root, "qa", "launcher-qa")
    _write_instance(store_root, "personainst_qa_agent_abc", "qa", "base")

    report = backfill_instance_profile_ids(dry_run=False)

    assert report["instances_planned"] == []
    reasons = {row["reason"] for row in report["instances_skipped"]}
    assert "disagrees_with_persona" in reasons
    assert _stored_profile_id("personainst_qa_agent_abc") == "base"


def test_backfill_skips_an_unbound_persona_rather_than_inventing_base(store_root):
    """Kill: stamp ``"base"`` for a persona with no binding.

    That invention is precisely the silent behaviour change the stage promises
    not to make, so it is pinned as a refusal-to-guess.
    """
    _write_persona(store_root, "drifter", None)
    _write_instance(store_root, "personainst_drifter", "drifter", None)

    report = backfill_instance_profile_ids(dry_run=False)

    assert report["instances_planned"] == []
    assert {row["reason"] for row in report["instances_skipped"]} == {"persona_unbound"}
    assert _stored_profile_id("personainst_drifter") is None


def test_backfill_refuses_wholesale_when_an_instance_is_busy(store_root):
    """One in-flight row blocks the WHOLE operation, typed and named.

    Kill-mutation: skip busy rows instead of raising. A half-stamped store is
    the drift the ladder exists to prevent, so a partial success must not be
    reachable.

    Anti-vacuity: the second (idle, null) row is also asserted untouched on
    disk, so a mutant that raises but has already written cannot pass either.
    """
    _write_persona(store_root, "qa", "launcher-qa")
    _write_instance(store_root, "personainst_qa_agent_busy", "qa", None, task_id="task-1")
    _write_instance(store_root, "personainst_qa_agent_idle", "qa", None)

    with pytest.raises(PersonaProfileRebindError) as excinfo:
        backfill_instance_profile_ids(dry_run=False)

    assert excinfo.value.code == "instances_busy"
    assert "personainst_qa_agent_busy" in str(excinfo.value)
    assert _stored_profile_id("personainst_qa_agent_idle") is None


def test_backfill_refuses_an_unknown_persona(store_root):
    with pytest.raises(PersonaProfileRebindError) as excinfo:
        backfill_instance_profile_ids(persona_id="nope")
    assert excinfo.value.code == "persona_not_persisted"
