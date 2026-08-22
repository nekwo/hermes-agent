"""``agents_readiness`` times two walks; one receipt now says which one paid.

W2-H2. The section's name promises readiness, and every remedy plan that read
the number off a build log therefore convicted ``profile_readiness_for_persona``.
On this section that is the smaller half. Measured against the operator's real
profiles root (5 runtime personas, 2026-08-22): the first build in a process
costs 4,001 ms, of which the summary/tool-visibility half is 3,054 ms and the
readiness walk 947 ms; every build after it in the same process costs 183 ms,
split 36 / 146. One number over both halves cannot attribute either, and they
move for unrelated reasons — the visibility half is the tool-registry populate
and the ``check_fn`` sweep, the walk is profile config plus skill resolution.

WHY A LOG LINE AND NOT TWO ``sections_ms`` KEYS. ``sections_ms`` rides the parity
envelope, which rides the hydrate frame, which is byte-pinned by the committed
stream goldens and by the Launcher's mirror of them. Two extra keys there is a
cross-stack fixture landing, and a hermes-only half of one is the exact failure
this repo has already paid for: hermes green, the Launcher's producer-contract
byte-compare red on every push. (Verified, not assumed — the keys were tried
first and reddened ``test_committed_goldens_are_the_generators_bytes``,
``test_every_generated_golden_has_the_producer_shape`` and
``test_the_lanes_frames_have_the_committed_goldens_shape``.) The number an
operator actually reads is on the ``snapshot_build_core`` line in ``agent.log``,
so the attribution belongs beside it, where it costs no contract at all.

WHAT MAKES THIS GATE NON-VACUOUS. "A line was emitted with two numbers on it" is
true of any two numbers, including two that time the same span or nothing. So the
gate injects a KNOWN delay into one half at a time and requires the OTHER half
not to carry it. A single-number receipt, or two timers wrapping the same span,
fails that discriminator in both directions. The injected delay is large against
the timer's 1 ms floor and the assertions are one-sided, so a slow or loaded box
makes the gate more true, never flaky.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from agent_runtime import snapshot as snapshot_module
from agent_runtime.snapshot import build_snapshot

#: Long enough to dwarf the ms-resolution timer and any incidental section work,
#: short enough that the file costs well under a second of suite time.
_INJECTED_DELAY_SECONDS = 0.25
_INJECTED_DELAY_MS = _INJECTED_DELAY_SECONDS * 1000

_RECEIPT = re.compile(
    r"snapshot_agents_readiness walk_ms=(\d+) tool_visibility_ms=(\d+) pid=(\d+)"
)


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


def _split(caplog) -> tuple[int, int]:
    """The one receipt this build emitted, as ``(walk_ms, tool_visibility_ms)``."""

    matches = [
        _RECEIPT.fullmatch(record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("snapshot_agents_readiness ")
    ]
    assert len(matches) == 1, (
        "expected exactly one agents_readiness receipt, got "
        f"{[r.getMessage() for r in caplog.records if 'agents_readiness' in r.getMessage()]}"
    )
    assert matches[0] is not None, "receipt format moved"
    return int(matches[0].group(1)), int(matches[0].group(2))


def test_the_receipt_is_emitted_and_format_pinned(one_runtime_persona):
    """An operator greps this line and joins it to ``snapshot_build_core`` on
    ``pid``, so the token order is a contract, not an implementation detail."""

    with caplog_at_info() as caplog:
        build_snapshot()

    walk, visibility = _split(caplog)
    assert walk >= 0 and visibility >= 0


def test_a_slow_readiness_walk_lands_on_the_walk_number(one_runtime_persona):
    from agent_runtime import profile_readiness as profile_readiness_module

    real = profile_readiness_module.profile_readiness_for_persona

    def slow(persona, **kwargs):
        time.sleep(_INJECTED_DELAY_SECONDS)
        return real(persona, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            profile_readiness_module, "profile_readiness_for_persona", slow
        )
        with caplog_at_info() as caplog:
            build_snapshot()

    walk, visibility = _split(caplog)
    assert walk >= _INJECTED_DELAY_MS * 0.8, (walk, visibility)
    # The discriminator: the OTHER half must not have absorbed it.
    assert visibility < _INJECTED_DELAY_MS * 0.5, (walk, visibility)


def test_a_slow_summary_lands_on_the_tool_visibility_number(one_runtime_persona):
    real = snapshot_module._agent_summary

    def slow(agent, **kwargs):
        time.sleep(_INJECTED_DELAY_SECONDS)
        return real(agent, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(snapshot_module, "_agent_summary", slow)
        with caplog_at_info() as caplog:
            build_snapshot()

    walk, visibility = _split(caplog)
    assert visibility >= _INJECTED_DELAY_MS * 0.8, (walk, visibility)
    assert walk < _INJECTED_DELAY_MS * 0.5, (walk, visibility)


def test_the_split_stays_off_the_wire(one_runtime_persona):
    """The contract boundary this stage chose, asserted rather than trusted.

    ``sections_ms`` is byte-pinned by the committed stream goldens and by the
    Launcher's mirror of them. The parent key keeps its exact meaning and NO new
    key appears beside it, so a number quoted from an older log still means what
    it meant and no cross-stack fixture landing is owed.
    """

    sections = build_snapshot()["parity"]["sections_ms"]

    assert "agents_readiness" in sections
    assert isinstance(sections["agents_readiness"], int)
    leaked = [key for key in sections if key.startswith("agents_readiness_")]
    assert leaked == [], (
        f"the split leaked onto the parity wire as {leaked}; that is a "
        "cross-stack goldens landing, not an additive field"
    )


class caplog_at_info:
    """``caplog`` scoped to this module's logger, as a context manager.

    A plain ``caplog`` fixture would also capture every other INFO line a build
    emits (``snapshot_build_core`` among them), and the receipt search above is
    deliberately narrow rather than relying on that noise staying quiet.
    """

    def __enter__(self):
        self._handler = _ListHandler()
        self._logger = logging.getLogger(snapshot_module.__name__)
        self._previous_level = self._logger.level
        self._logger.setLevel(logging.INFO)
        self._logger.addHandler(self._handler)
        return self._handler

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._previous_level)
        return False


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
