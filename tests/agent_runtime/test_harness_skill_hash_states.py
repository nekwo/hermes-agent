"""ABSENT is not MATCHING: the skill-hash read says which of the two it saw.

H-H7. `harness_skill_hash_mismatches` `continue`d past a package that does not
exist, so a skill nobody ever installed produced the same empty list as a skill
whose two copies were read and agreed. Three advisory readers spent that list —
`profile_readiness`, the chat HUD's `_accessible_skills_context`, and the
install gate — and all three read the empty list as a clean bill. That is the
false-all-clear shape of an unrun gate, and this repo already paid for it once:
the 2026-08-24 incident ran an agent against a 14457-byte copy of a 14906-byte
skill while every advisory surface reported the install fine.

ANTI-VACUITY throughout: every case establishes the file state it is about, and
the matching case is asserted beside the absent one from the same helper, so
"the absent case is named" cannot be satisfied by a helper that names
everything.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from agent_runtime.skill_install import (
    SKILL_HASH_MATCHES,
    SKILL_HASH_MISMATCH,
    SKILL_HASH_NO_SOURCE,
    SKILL_HASH_NOT_INSTALLED,
    harness_skill_destination,
    harness_skill_hash_absences,
    harness_skill_hash_mismatches,
    harness_skill_hash_states,
    install_harness_skill,
)


SKILL = "harness-runtime-model"


def _states(skill: str = SKILL) -> dict[str, str]:
    return {row.skill: row.state for row in harness_skill_hash_states([skill])}


def test_a_package_that_was_never_installed_is_absent_and_not_matching(
    isolate_agent_runtime_root,
):
    """THE pin. Nothing is installed, so nothing was compared — and the two
    projections disagree, which is the whole point of having two."""

    assert not harness_skill_destination(SKILL).exists()

    assert _states() == {SKILL: SKILL_HASH_NOT_INSTALLED}
    # Unchanged, and deliberately: "not installed" is not "installed wrong".
    assert harness_skill_hash_mismatches([SKILL]) == []
    # The companion that stops the line above from reading as a clean bill.
    assert harness_skill_hash_absences([SKILL]) == [SKILL]


def test_an_installed_package_that_agrees_is_a_positive_claim(isolate_agent_runtime_root):
    """ANTI-VACUITY for the case above: the same skill, the same helper, one
    real install — and every answer flips. So `not_installed` was the file
    state's doing and not a helper that never says `matches`."""

    install_harness_skill(SKILL)

    assert _states() == {SKILL: SKILL_HASH_MATCHES}
    assert harness_skill_hash_mismatches([SKILL]) == []
    assert harness_skill_hash_absences([SKILL]) == []


def test_a_stale_installed_package_is_a_mismatch_and_not_an_absence(
    isolate_agent_runtime_root,
):
    """The third state, so `mismatch` and `not_installed` are not one bucket
    wearing two names."""

    install_harness_skill(SKILL)
    destination = harness_skill_destination(SKILL)
    destination.write_text(
        destination.read_text(encoding="utf-8") + "\n# a stale local edit\n",
        encoding="utf-8",
    )

    assert _states() == {SKILL: SKILL_HASH_MISMATCH}
    assert harness_skill_hash_mismatches([SKILL]) == [SKILL]
    assert harness_skill_hash_absences([SKILL]) == []


def test_a_canonical_id_with_no_repo_package_is_named_rather_than_skipped(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """The other side of the absence, which used to pass the install gate in
    silence: something IS installed under a canonical id whose repo package is
    gone, so the mismatch list is empty because there was nothing to compare
    against — not because the bytes agree."""

    install_harness_skill(SKILL)
    from agent_runtime import skill_install

    monkeypatch.setattr(
        skill_install, "harness_skill_source", lambda _name: tmp_path / "gone" / "SKILL.md"
    )

    assert _states() == {SKILL: SKILL_HASH_NO_SOURCE}
    assert harness_skill_hash_mismatches([SKILL]) == []
    assert harness_skill_hash_absences([SKILL]) == [SKILL]


def test_a_non_canonical_id_is_not_in_the_result_at_all(isolate_agent_runtime_root):
    """It never was this function's subject, and reporting it as any state
    would invent a verdict about a package the harness does not own."""

    assert harness_skill_hash_states(["not-a-harness-skill", SKILL]) == [
        row for row in harness_skill_hash_states([SKILL])
    ]


def _readiness_row(monkeypatch, home):
    """One readiness row for a persona bound to `home`, with the walk's
    unrelated probes stubbed — provider auth and runtime packages are not this
    module's subject and answering them costs a real network-shaped resolve."""

    from agent_runtime import profile_readiness

    monkeypatch.setattr(
        profile_readiness,
        "resolve_persona_profile",
        lambda _persona: SimpleNamespace(
            profile_home=home, hermes_profile="base", readiness="ready", summary="ready"
        ),
    )
    monkeypatch.setattr(profile_readiness, "persona_profile_scope", _null_scope)
    monkeypatch.setattr(profile_readiness, "_runtime_dependency_issue", lambda _p: None)
    monkeypatch.setattr(profile_readiness, "_provider_issue", lambda _p: None)
    return profile_readiness.profile_readiness_for_persona(
        SimpleNamespace(id="hh7", hermes_profile="base", skills=[SKILL], required_mcp_servers=[])
    )


@contextmanager
def _null_scope(_binding):
    yield


def test_readiness_carries_the_absent_half_beside_the_mismatch_half(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """The first advisory reader. Its row used to answer "were the hashes
    equal" with a list that could not distinguish "yes" from "no idea"."""

    assert not harness_skill_destination(SKILL).exists()

    row = _readiness_row(monkeypatch, tmp_path / "home")

    assert row["skill_hash_mismatches"] == []
    assert row["skill_hash_absent"] == [SKILL]


def test_readiness_reports_no_absence_once_the_package_is_installed(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """ANTI-VACUITY for the row above: same persona, same walk, one real
    install — and the absent list empties."""

    install_harness_skill(SKILL)

    row = _readiness_row(monkeypatch, tmp_path / "home")

    assert row["skill_hash_mismatches"] == []
    assert row["skill_hash_absent"] == []


def _wire_persona(persona_id: str):
    """A persona whose only skill is the canonical harness package the cases
    above install and uninstall, so the two wire rows read the SAME file state
    the readiness cases establish. Distinct ids per case because
    ``snapshot._agent_summary`` resolves readiness through a TTL memo."""

    from agent_runtime.models import AgentPersona

    return AgentPersona(
        id=persona_id,
        display_name="H-H7 Wire",
        role="qa",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="personas/qa/system.md",
        hermes_profile="base",
        skills=[SKILL],
        required_mcp_servers=[],
    )


def _bind_ready(monkeypatch, home):
    """Same stubs as ``_readiness_row``: the walk must REACH the skill-hash
    split, which it only does for a persona whose profile binding resolves."""

    from agent_runtime import profile_readiness

    monkeypatch.setattr(
        profile_readiness,
        "resolve_persona_profile",
        lambda _persona: SimpleNamespace(
            profile_home=home, hermes_profile="base", readiness="ready", summary="ready"
        ),
    )
    monkeypatch.setattr(profile_readiness, "persona_profile_scope", _null_scope)
    monkeypatch.setattr(profile_readiness, "_runtime_dependency_issue", lambda _p: None)
    monkeypatch.setattr(profile_readiness, "_provider_issue", lambda _p: None)


def test_the_snapshot_persona_row_projects_the_absent_half(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """Readiness publishing the split is not the same as the LAUNCHER seeing
    it: ``_agent_summary`` is the roster row every frame carries, and it used
    to copy ``skill_hash_mismatches`` out of readiness and stop."""

    from agent_runtime.snapshot import _agent_summary

    assert not harness_skill_destination(SKILL).exists()
    _bind_ready(monkeypatch, tmp_path / "home")

    summary = _agent_summary(_wire_persona("hh7-snap-absent"))

    assert summary["skill_hash_mismatches"] == []
    assert summary["skill_hash_absent"] == [SKILL]


def test_the_snapshot_persona_row_reports_no_absence_once_installed(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """ANTI-VACUITY: the same row, one real install, and the projected list
    empties — so the field cannot be a constant."""

    from agent_runtime.snapshot import _agent_summary

    install_harness_skill(SKILL)
    _bind_ready(monkeypatch, tmp_path / "home")

    assert _agent_summary(_wire_persona("hh7-snap-present"))["skill_hash_absent"] == []


def test_the_status_agent_row_projects_the_absent_half(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """The second wire reader. ``status._agent_status`` is the other row built
    straight off a readiness dict, and it carried the same one-sided copy."""

    from agent_runtime.status import _agent_status

    assert not harness_skill_destination(SKILL).exists()
    _bind_ready(monkeypatch, tmp_path / "home")

    row = _agent_status(_wire_persona("hh7-status-absent"))

    assert row["skill_hash_mismatches"] == []
    assert row["skill_hash_absent"] == [SKILL]


def test_the_status_agent_row_reports_no_absence_once_installed(
    isolate_agent_runtime_root, monkeypatch, tmp_path
):
    """ANTI-VACUITY for the row above."""

    from agent_runtime.status import _agent_status

    install_harness_skill(SKILL)
    _bind_ready(monkeypatch, tmp_path / "home")

    assert _agent_status(_wire_persona("hh7-status-present"))["skill_hash_absent"] == []
