"""IC-3 — an unreadable persona-instance row repairs ONCE, then reports.

WHY THIS FILE EXISTS
====================

``PersonaInstanceStore.ensure_for_persona`` caught bare ``Exception`` around its
read and minted a fresh row. That is right for a row that is NOT THERE and wrong
for a row that IS there and will not decode: every build calls this for every
persona (``snapshot.build_snapshot`` -> ``ensure_for_personas``), so a corrupt
row produced a file write and a ``persona_instance.created`` event on every pass,
silently, forever — and each of those writes moved an input the read-model
cache's pre-build key had already recorded, which is one of the named triggers
behind ``snapshot_core_cache never_converged``.

WHAT MAKES THESE NON-VACUOUS
============================

Three mutants, and each case is aimed at one of them:

* **the old bare except** — reds the loop case, which counts writes and events
  across repeated builds;
* **"narrow it to nothing"** (report and never repair) — reds the repair case,
  which is the behaviour every caller already depends on;
* **"treat a missing row as corrupt"** — reds the cold case, which is the
  ordinary cold-store path and must stay byte-for-byte what it was.
"""

from __future__ import annotations

import json
import logging

import pytest

from agent_runtime import paths, persona_assignments
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    persona_instance_id_for,
)

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")


@pytest.fixture(autouse=True)
def fresh_remint_history():
    """Every case starts as a process that has repaired nothing."""

    persona_assignments.reset_unreadable_instance_rows()
    yield
    persona_assignments.reset_unreadable_instance_rows()


def _persona(persona_id: str = "dev", *, role: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role=role,
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=f"profile-{persona_id}",
    )


def _row_path(persona_id: str = "dev"):
    return paths.persona_instance_path(persona_instance_id_for(persona_id))


def _corrupt(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")


def _created_events(log: EventLog) -> list:
    return [
        event
        for event in log.tail(1000)
        if event.type == "persona_instance.created"
    ]


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------------------- #
# 1. The repair still happens — ONCE
# --------------------------------------------------------------------------- #
def test_an_unreadable_row_is_re_minted_once_with_a_named_receipt(
    isolate_agent_runtime_root, caplog
):
    """A corrupt row still self-heals, and now says so.

    The repair is the behaviour the suites already depend on and IC-3 does not
    remove it. What it adds is the receipt: the file and the decode error, so an
    operator can find the row instead of inferring its existence from a cache
    that will not converge.

    *Kill:* narrow the ``except`` to report only. The row stays corrupt, the
    store never heals, and this reds.
    """

    row = _row_path()
    _corrupt(row)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.persona_assignments"):
        instance = PersonaInstanceStore(event_log=EventLog()).ensure_for_persona(_persona())

    assert instance.id == persona_instance_id_for("dev")
    assert json.loads(row.read_text(encoding="utf-8"))["id"] == instance.id, (
        "the corrupt row was not replaced, so the store never heals"
    )
    warnings = [line for line in _lines(caplog) if "persona_instance_row_unreadable" in line]
    assert len(warnings) == 1, _lines(caplog)
    assert str(row) in warnings[0], warnings[0]
    assert "repeat=true" not in warnings[0], warnings[0]


def test_a_row_that_will_not_decode_again_is_reported_not_re_minted(
    isolate_agent_runtime_root, caplog
):
    """THE LOOP CASE. A row still unreadable after its repair stops being minted.

    Five builds' worth of ensure passes over a row that is corrupt every time.
    The old bare ``except`` produced five writes and five
    ``persona_instance.created`` events — a durable-store write per read-model
    build, which is exactly the never-converging trigger. IC-3 allows the first,
    and reports the rest at ERROR.

    Counting BOTH the writes and the events matters: an arm that stopped
    emitting but kept writing would still move the fingerprint, and an arm that
    stopped writing but kept emitting would still grow the event log on a read
    path.

    *Kill:* restore the bare ``except Exception`` around ``self.get``. Every pass
    mints again, and both counts red.
    """

    row = _row_path()
    log = EventLog()
    store = PersonaInstanceStore(event_log=log)
    writes: list[str] = []
    persona = _persona()

    with caplog.at_level(logging.WARNING, logger="agent_runtime.persona_assignments"):
        for _pass in range(5):
            _corrupt(row)
            before = row.read_text(encoding="utf-8")
            store.ensure_for_persona(persona)
            if row.read_text(encoding="utf-8") != before:
                writes.append("write")

    assert len(writes) == 1, (
        f"the store re-minted the unreadable row {len(writes)} times across five "
        "builds. Every one of those is a durable write inside a READ path, and "
        "an input the read-model cache's pre-build key had already recorded — so "
        "no process can ever be served that cache."
    )
    assert len(_created_events(log)) == 1, (
        "a persona_instance.created event was emitted for every build that met "
        "the corrupt row, so the event log grows without bound off a projection"
    )
    repeats = [line for line in _lines(caplog) if "repeat=true" in line]
    assert len(repeats) == 4, _lines(caplog)
    assert str(row) in repeats[0], repeats[0]
    assert any(record.levelno == logging.ERROR for record in caplog.records), (
        "the repeat arm reported at WARNING or below, so a defect that survives "
        "its own repair reads like the ordinary repair"
    )


def test_the_repeat_arm_still_answers_with_a_row(
    isolate_agent_runtime_root
):
    """Reporting is not refusing: the projection still gets a persona row.

    The alternative — raising — would turn one corrupt file into a failed build
    for the whole roster, on a path a boot waits on. The row is returned as a
    value; nothing is written.

    *Kill:* raise from the repeat arm.
    """

    row = _row_path()
    store = PersonaInstanceStore(event_log=EventLog())
    persona = _persona()
    _corrupt(row)
    store.ensure_for_persona(persona)
    _corrupt(row)

    instance = store.ensure_for_persona(persona)
    assert instance.id == persona_instance_id_for("dev")
    assert instance.persona_id == "dev"
    assert row.read_text(encoding="utf-8") == "{ this is not json", (
        "the repeat arm wrote to the store after all"
    )


# --------------------------------------------------------------------------- #
# 2. The COLD path is untouched
# --------------------------------------------------------------------------- #
def test_a_missing_row_is_minted_silently_every_time(
    isolate_agent_runtime_root, caplog
):
    """The ordinary cold path: no row, no receipt, no once-per-process limit.

    A row that is NOT THERE is not a defect and never was. It must mint, emit,
    and say nothing — and it must keep doing so if the row is deleted again,
    because the once-per-process memo belongs to the corrupt arm alone. Sharing
    it would make an operator's ``rm`` of one row unrepairable until restart.

    *Kill:* route ``FileNotFoundError`` through the unreadable arm. The second
    mint stops happening and this reds.
    """

    row = _row_path()
    log = EventLog()
    store = PersonaInstanceStore(event_log=log)
    persona = _persona()

    with caplog.at_level(logging.WARNING, logger="agent_runtime.persona_assignments"):
        first = store.ensure_for_persona(persona)
        row.unlink()
        second = store.ensure_for_persona(persona)

    assert first.id == second.id == persona_instance_id_for("dev")
    assert row.exists(), "the second cold mint did not write a row"
    assert len(_created_events(log)) == 2, (
        "a deleted row was not re-minted, so the cold path inherited the corrupt "
        "arm's once-per-process limit"
    )
    assert [line for line in _lines(caplog) if "persona_instance_row_unreadable" in line] == [], (
        "an absent row emitted the unreadable receipt, which would make the "
        "ordinary cold start of every install look like corruption"
    )


def test_the_remint_memo_is_keyed_per_row_not_per_instance_id(
    isolate_agent_runtime_root
):
    """Two personas' rows repair independently.

    The memo records WHICH row was repaired. Keyed on anything coarser — a
    boolean, the persona id alone — one corrupt row would silence the first
    legitimate repair of every other row in the roster.

    *Kill:* make ``_note_unreadable_instance_row`` take no argument and record a
    single flag.
    """

    store = PersonaInstanceStore(event_log=EventLog())
    dev_row = _row_path("dev")
    qa_row = _row_path("qa")
    _corrupt(dev_row)
    _corrupt(qa_row)

    store.ensure_for_persona(_persona("dev"))
    store.ensure_for_persona(_persona("qa"))

    assert json.loads(qa_row.read_text(encoding="utf-8"))["persona_id"] == "qa", (
        "the qa row was not repaired because the dev row had already spent the "
        "process's one repair"
    )
