"""``agents_readiness`` times two walks; ``sections_ms`` now says which one paid.

W2-H2. The section's name promises readiness, and a remedy plan reading the
number off a build log therefore convicts ``profile_readiness_for_persona``. On
this section that is the smaller half. Measured against the operator's real
profiles root (5 runtime personas, 2026-08-22): the first build in a process
costs 4,001 ms, of which the summary/tool-visibility half is 3,054 ms and the
readiness walk 947 ms; every build after it in the same process costs 183 ms,
split 36 / 146. One number over both halves cannot attribute either, and the
halves move for unrelated reasons — the visibility half is the tool-registry
populate and the ``check_fn`` sweep, the walk is profile config plus skill
resolution.

WHAT MAKES THIS GATE NON-VACUOUS. "Both keys exist and are ints" is true of any
two keys, including two that time the same thing or nothing. So the gate injects
a KNOWN delay into one half at a time and requires the other half not to carry
it. A single-key section, or two keys wrapping the same span, fails that
discriminator in both directions. The injected delay is large against the timer's
1 ms floor and the assertions are one-sided (``>=`` a fraction of it), so a slow
or loaded box makes the gate MORE true, never flaky.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime import snapshot as snapshot_module
from agent_runtime.snapshot import build_snapshot

#: Long enough to dwarf the ms-resolution timer and any incidental section work,
#: short enough that two builds cost well under a second of suite time.
_INJECTED_DELAY_SECONDS = 0.25
_INJECTED_DELAY_MS = _INJECTED_DELAY_SECONDS * 1000


@pytest.fixture
def one_runtime_persona(isolate_agent_runtime_root):
    """A single persona, so exactly one delay is injected per build.

    The section iterates the roster; an empty roster would run neither half and
    the gate would pass by having measured nothing.
    """

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="readiness_attribution_probe",
        display_name="Readiness Attribution Probe",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


def _sections(snapshot) -> dict:
    return snapshot["parity"]["sections_ms"]


def test_the_two_halves_are_always_reported(one_runtime_persona):
    """Present even at zero — a consumer must tell "cheap" from "not reported"."""

    sections = _sections(build_snapshot())

    for key in (
        "agents_readiness",
        "agents_readiness_walk",
        "agents_readiness_tool_visibility",
    ):
        assert key in sections, key
        assert isinstance(sections[key], int) and sections[key] >= 0, key
    # The parent still spans both halves: it is unchanged in meaning, so a
    # number quoted from an older log still means what it meant.
    assert sections["agents_readiness"] >= sections["agents_readiness_walk"]
    assert (
        sections["agents_readiness"]
        >= sections["agents_readiness_tool_visibility"]
    )


def test_a_slow_readiness_walk_lands_on_the_walk_key(one_runtime_persona):
    from agent_runtime import profile_readiness as profile_readiness_module

    real = profile_readiness_module.profile_readiness_for_persona

    def slow(persona, **kwargs):
        time.sleep(_INJECTED_DELAY_SECONDS)
        return real(persona, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            profile_readiness_module, "profile_readiness_for_persona", slow
        )
        sections = _sections(build_snapshot())

    assert sections["agents_readiness_walk"] >= _INJECTED_DELAY_MS * 0.8, sections
    # The discriminator: the OTHER half must not have absorbed it.
    assert (
        sections["agents_readiness_tool_visibility"] < _INJECTED_DELAY_MS * 0.5
    ), sections
    assert sections["agents_readiness"] >= _INJECTED_DELAY_MS * 0.8, sections


def test_a_slow_summary_lands_on_the_tool_visibility_key(one_runtime_persona):
    real = snapshot_module._agent_summary

    def slow(agent, **kwargs):
        time.sleep(_INJECTED_DELAY_SECONDS)
        return real(agent, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snapshot_module, "_agent_summary", slow)
        sections = _sections(build_snapshot())

    assert (
        sections["agents_readiness_tool_visibility"] >= _INJECTED_DELAY_MS * 0.8
    ), sections
    assert sections["agents_readiness_walk"] < _INJECTED_DELAY_MS * 0.5, sections
    assert sections["agents_readiness"] >= _INJECTED_DELAY_MS * 0.8, sections
