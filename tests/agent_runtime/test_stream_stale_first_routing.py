"""Who the boot's ONE stale-first core goes to (MC-4 / P6).

EG-3.1's mismatch half serves the persisted core immediately, labelled stale,
rather than showing the operator an empty canvas for the length of a full build.
Measured on the operator's launcher 2026-08-18, it was a race and it lost two
boots in three: ``take_stale_first_core`` was one-shot per PROCESS, a boot runs
TWO ``stream_frames`` generators, and the one that attaches first is the hub
producer started by ``runtime.office.subscribe`` — whose ``office_patch_sink``
discards every row that is not an ``office_actor``. The stale paint was taken
and thrown away while the launcher waited.

This file pins the ROUTING half at the generator seam: whether ``stream_frames``
asks for the stale core at all, and what a one-shot budget is allowed to be
spent on. The two claims it does NOT own, and where they live instead:

* the per-subscriber one-shot and its armed-lane bound — ``core_cache``'s own
  contract, pinned in ``test_core_fingerprint_cache`` (the second-asker case and
  the disarmed-lane case beside it);
* the derivation of the hub's answer from the attached room — a ``serve_loop``
  closure, pinned against the REAL loop in ``test_serve_socket_lane``
  (``test_an_office_only_room_does_not_ask_the_producer_for_a_stale_paint`` and
  the attach-order pair beside it).

The stale core is INJECTED here rather than seeded. These cases are about
routing, and a seeded cache would make each of them pay two real snapshot builds
to re-assert a judgement ``test_core_fingerprint_cache`` already owns — while
also making a routing regression indistinguishable from a fingerprint one.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent_runtime import core_cache
from agent_runtime.stream import stream_frames
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness

#: A marker the real builder can never produce, so "the stale frame arrived" is
#: never confusable with "the authoritative build happened to look like this".
STALE_MARKER = "stale-first-core-under-test"


class _StaleFirstSpy:
    """Counts ASKS, not deliveries.

    The distinction is the whole gate for an office-only room: a frame that
    never arrives has two causes — the generator declined to ask, or it asked
    and the cache had nothing to give — and only the first is the property under
    test. Absence of a frame cannot tell them apart; a call counter can.
    """

    def __init__(self) -> None:
        self.callers: list[str] = []
        self.core: dict | None = None

    def install(self, monkeypatch, *, core: dict | None) -> None:
        self.core = core

        def _fake(*, caller: str):
            self.callers.append(caller)
            if self.core is None:
                return None
            import copy

            return copy.deepcopy(self.core)

        monkeypatch.setattr(core_cache, "take_stale_first_core", _fake)

    @property
    def asks(self) -> int:
        return len(self.callers)


def _stale_core() -> dict:
    """A minimally shaped core wearing the labels the real one wears."""

    return {
        "schema_version": 2,
        "workspaces": [{"id": "ws_probe", "name": STALE_MARKER}],
        "parity": {
            "core_source": core_cache.CORE_SOURCE_CACHE,
            "core_stale": True,
            "freshness": {"state": "stale"},
            "watermark": {"event_offset": 0},
        },
    }


def _is_stale_frame(frame: dict) -> bool:
    core = frame.get("core") or {}
    return bool((core.get("parity") or {}).get("core_stale"))


# --------------------------------------------------------------------------- #
# 1. The declaration is what routes the paint
# --------------------------------------------------------------------------- #
def test_a_room_that_declares_it_paints_is_served_the_stale_core(
    isolate_agent_runtime_root, monkeypatch
):
    spy = _StaleFirstSpy()
    spy.install(monkeypatch, core=_stale_core())

    frames = list(
        stream_frames(
            max_frames=2,
            poll_interval_seconds=0.01,
            heartbeat_interval_seconds=60,
            wants_stale_first=True,
            caller="cli",
        )
    )

    assert spy.asks == 1, f"the painting lane asked {spy.asks} times, not once"
    assert spy.callers == ["cli"], spy.callers
    assert len(frames) == 2, [frame.get("type") for frame in frames]
    assert frames[0]["type"] == "hydrate"
    assert _is_stale_frame(frames[0]), frames[0]["core"]["parity"]
    # The authoritative frame follows and is NOT the stale one — otherwise a
    # generator that yielded the same frame twice would pass the line above.
    assert frames[1]["type"] == "hydrate"
    assert not _is_stale_frame(frames[1]), frames[1]["core"]["parity"]


def test_a_room_with_no_painter_does_not_even_ask_for_the_stale_core(
    isolate_agent_runtime_root, monkeypatch
):
    """The A-x1 defect, stated as the absence of a QUESTION.

    An office-only room must leave the boot's single stale core untouched so the
    lane that actually paints can have it. Asserted on the call and not on the
    frame: the sink discards it either way, so a frame-shaped assertion would
    stay green against a producer that took the core and dropped it — which is
    precisely the behaviour that shipped.

    ``max_frames=2`` and not 1, ON PURPOSE. A one-shot declines the stale core
    for a SECOND, independent reason (its whole budget would go on a
    non-authoritative frame — see the case below), so a one-frame driver here
    would stay green against a generator that ignored the declaration entirely.
    Measured: it did. The mutation "drop ``wants_stale_first`` from the
    condition and keep only the budget rule" left the one-frame version of this
    case passing, which is the vacuity C30 exists to catch. With room for two
    frames the declaration is the only thing that can refuse.
    """

    spy = _StaleFirstSpy()
    spy.install(monkeypatch, core=_stale_core())

    frames = list(
        stream_frames(
            max_frames=2,
            poll_interval_seconds=0.01,
            # Short, because the second frame of this budget is a HEARTBEAT: with
            # the stale core correctly refused there is no second core to wait
            # for, and a 60s cadence would park the generator until the per-test
            # cap. The cases that DO expect a stale frame keep the long cadence,
            # so a heartbeat can never be mistaken for one.
            heartbeat_interval_seconds=0.05,
            wants_stale_first=False,
            caller="hub",
        )
    )

    assert spy.asks == 0, (
        "an office-only room consumed the boot's stale core; the painting lane "
        f"behind it gets nothing (asked by: {spy.callers})"
    )
    # The budget was NOT the reason: two frames of room, and the first CORE
    # frame is already authoritative. Read past the boot build's own liveness,
    # which at this 0.05s cadence precedes the hydrate (MC-4 arm 2).
    first_core = drain_boot_liveness(frames)
    assert first_core["type"] == "hydrate"
    assert not _is_stale_frame(first_core)


# --------------------------------------------------------------------------- #
# 2. A one-shot budget is spent on an AUTHORITATIVE core
# --------------------------------------------------------------------------- #
def test_a_one_shot_request_is_never_answered_with_a_stale_core(
    isolate_agent_runtime_root, monkeypatch
):
    """``--max-frames 1`` is the launcher's FORCED-refresh lane.

    The stale frame is yielded at the head and the budget check returns
    immediately after it, so a one-shot that took it would answer a forced
    refresh with a core that is by definition not authoritative — and
    ``mission_control_bridge.dart::_loadSnapshotFromStreamHydrate`` (read
    2026-08-18) scans that stdout for a ``type == "hydrate"`` line and applies
    whatever it finds through ``applyForcedSnapshot``, i.e. PAST its own
    sequence gate. "Force a refresh" would mean "re-paint what you were already
    unhappy with".

    Driven with ``wants_stale_first=True`` on purpose: the room DOES paint, so
    the refusal under test is the budget rule and not the routing rule.
    """

    spy = _StaleFirstSpy()
    spy.install(monkeypatch, core=_stale_core())

    frames = list(
        stream_frames(
            max_frames=1,
            poll_interval_seconds=0.01,
            heartbeat_interval_seconds=60,
            wants_stale_first=True,
            caller="cli",
        )
    )

    assert spy.asks == 0, "a one-shot asked for a stale core it has no room to replace"
    assert len(frames) == 1, [frame.get("type") for frame in frames]
    frame = frames[0]
    assert frame["type"] == "hydrate"
    assert not _is_stale_frame(frame), frame["core"]["parity"]
    assert (frame["core"]["parity"].get("freshness") or {}).get("state") != "stale"
    assert STALE_MARKER not in str(frame["core"].get("workspaces") or [])


def test_a_two_frame_budget_has_room_for_the_stale_core(
    isolate_agent_runtime_root, monkeypatch
):
    """Anti-vacuity for the case above: the refusal is the BUDGET, not the flag.

    A mutant that simply stopped asking whenever ``max_frames`` was set at all
    would pass the one-shot case. This is the same driver with one more frame of
    room, and it must still be served.
    """

    spy = _StaleFirstSpy()
    spy.install(monkeypatch, core=_stale_core())

    frames = list(
        stream_frames(
            max_frames=2,
            poll_interval_seconds=0.01,
            heartbeat_interval_seconds=60,
            wants_stale_first=True,
            caller="cli",
        )
    )

    assert spy.asks == 1
    assert _is_stale_frame(frames[0])


# --------------------------------------------------------------------------- #
# 3. Both production callers STATE it; nobody inherits the default
# --------------------------------------------------------------------------- #
#: ``wants_stale_first`` defaults to ``False`` — the safe direction, so a caller
#: that has not said it paints gets the pre-EG-3.1 wire rather than eating the
#: boot's single stale core. That safety is exactly what makes a SILENT default
#: cheap to fall into, which is why the two production call sites are pinned by
#: AST rather than trusted to a code review. Grepping would not do: the argument
#: could be present in a comment or a docstring.
_PRODUCTION_CALL_SITES = (
    ("hermes_cli/harness_parts/runtime_commands.py", "_cmd_stream"),
    ("hermes_cli/harness_parts/serve.py", "_stream_source"),
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _stream_frames_calls(path: pathlib.Path, function_name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "stream_frames":
                found.append(inner)
    return found


@pytest.mark.parametrize(("relative_path", "function_name"), _PRODUCTION_CALL_SITES)
def test_every_production_stream_frames_call_states_wants_stale_first(
    relative_path, function_name
):
    path = _REPO_ROOT / relative_path
    calls = _stream_frames_calls(path, function_name)
    assert calls, (
        f"{relative_path}::{function_name} no longer calls stream_frames — this "
        "pin has stopped watching the site it was written for"
    )
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "wants_stale_first" in keywords, (
            f"{relative_path}::{function_name} inherits the wants_stale_first "
            "default instead of stating it; the boot's stale paint is routed by "
            "this argument and a silent caller is how it was lost before"
        )


def test_the_pin_covers_every_production_call_site_there_is():
    """The pin above is only as good as its inventory.

    A THIRD production caller would inherit the default silently and the
    parametrized case above would never know. So the inventory is derived here
    rather than declared: every ``stream_frames`` call outside ``tests/`` and
    outside the module that defines it must be one of the two named sites.
    """

    sites: set[tuple[str, str]] = set()
    for path in _REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative.startswith("tests/") or relative.endswith("agent_runtime/stream.py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our files
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "stream_frames":
                sites.add((relative, "?"))

    assert {relative for relative, _ in sites} == {
        relative for relative, _ in _PRODUCTION_CALL_SITES
    }, (
        "the set of production stream_frames callers moved; add the new one to "
        f"_PRODUCTION_CALL_SITES so it is pinned too. Found: {sorted(sites)}"
    )
