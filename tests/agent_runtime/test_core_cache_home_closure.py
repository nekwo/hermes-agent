"""MC-2 / P3 — the input closure is a function of the STORE, not of an env var
the build itself exports.

WHAT THIS FILE EXISTS TO STOP
=============================

``core_cache.build_input_fingerprint`` resolves four of its seven input classes
home-relative, and the ladder underneath them ends at
``os.environ["HERMES_HOME"]``. The BUILD exports that variable: the
``agents_readiness`` section runs ``profile_readiness_for_persona`` inside
``profile_context.persona_profile_context``, which sets the context-local
override AND writes the env var process-globally for the length of a per-persona
scope. So a consult on another thread — a one-shot hydrate, a status probe, the
hub — computed its closure over whichever profile happened to be exported.

Measured in the field 2026-08-18: ``inputs=24344`` and ``inputs=23107`` from ONE
process, over ONE store, thirteen seconds apart. That falsified the standing
claim (ledger A6) that non-filesystem inputs are outside the closure "by
construction" — an argument from reading, which this file replaces with a driven
witness.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant is a fingerprint that agrees with itself because the persona scope
never did anything. So every case first PROVES THE SCOPE APPLIED — it asserts
that the ambient resolvers really did flip to the other profile at the point the
fingerprint was taken — and only then asserts that the fingerprint did not. A
fixture whose two profiles happened to hold identical trees would satisfy the
equality half forever; ``test_the_fixture_can_tell_the_two_profiles_apart``
refuses that by driving the UNPINNED shape and requiring it to diverge.

Two shapes are driven, and they fail for different reasons, which is why neither
stands in for the other:

* **same thread, inside the scope** — the context-local override is visible, so
  ``get_hermes_head_home()`` answers with the home ``persona_profile_context``
  recorded. Classes 2 and 3 are already safe here; class 6 is not.
* **another thread while the scope is held** — ContextVars do not cross a thread
  boundary, so the recorded head is INVISIBLE there and every head-resolving
  authority degenerates to the flipped ambient home. Classes 2, 3 and 6 are all
  exposed. This is the shape the field divergence was measured on, and it is the
  one an argument from reading ``running_work._head_home`` would have missed.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime import core_cache
from agent_runtime.profile_context import (
    PersonaProfileBinding,
    persona_profile_context,
)


# --------------------------------------------------------------------------- #
# A real two-profile layout on a throwaway root
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """``<root>/profiles/{head,other}``, each with its OWN config and skills.

    The other profile's skills tree deliberately holds files the head's does
    not: the field symptom was a COUNT delta of 1,237 entries, so a fixture whose
    profiles differed only in name could not reproduce the defect it is here to
    pin.
    """

    root = tmp_path / "hermes"
    head = root / "profiles" / "head"
    other = root / "profiles" / "other"

    for name, home in (("head", head), ("other", other)):
        _write(home / "config.yaml", "skills:\n  external_dirs: []\n")
        _write(home / "profile.yaml", f"name: {name}\n")
        _write(home / "skills" / f"only_{name}" / "SKILL.md", f"# {name}\n")
    for index in range(7):
        _write(other / "skills" / "only_other" / f"extra_{index}.md", "x\n")
    _write(root / "config.yaml", "agent_runtime: {}\n")
    _write(root / "shared" / "skills" / "shared_one" / "SKILL.md", "# shared\n")
    _write(root / "active_profile", "head\n")

    monkeypatch.setenv("HERMES_HOME", str(head))
    # The head authority must be ABSENT for these cases: with it set, the head is
    # explicit on every thread and the cross-thread exposure below cannot be
    # reproduced. The unauthoritative shape is the operator's default and is the
    # weaker one, so it is what the gate drives.
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.delenv("HERMES_SHARED_SKILLS", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    core_cache.reset_process_state()
    yield SimpleNamespace(
        root=root,
        head=head,
        other=other,
        binding=PersonaProfileBinding(
            persona_id="probe", hermes_profile="other", profile_home=other
        ),
    )
    core_cache.reset_process_state()


def _ambient_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _fingerprint_off_thread(scope_is_held: threading.Event, released: threading.Event):
    """Take a fingerprint on a thread that never entered the persona scope."""

    box: dict[str, object] = {}

    def worker() -> None:
        scope_is_held.wait(timeout=10)
        box["ambient"] = _ambient_home()
        box["fingerprint"] = core_cache.build_input_fingerprint()
        released.set()

    thread = threading.Thread(target=worker, name="mc2-off-thread")
    thread.start()
    return thread, box


def _paths(fingerprint) -> set[str]:
    return set(core_cache.iter_fingerprint_paths(fingerprint))


def _under(paths: set[str], home: Path) -> list[str]:
    prefix = str(home)
    return sorted(path for path in paths if path.startswith(prefix))


# --------------------------------------------------------------------------- #
# 0. Anti-vacuity — the fixture CAN tell the two profiles apart
# --------------------------------------------------------------------------- #
def test_the_fixture_can_tell_the_two_profiles_apart(two_profiles):
    """Drive the UNPINNED shape and require it to diverge.

    Every equality below is only evidence if inequality was reachable. This case
    resolves the skill roots the way ``build_input_fingerprint`` used to — through
    the ambient home — and asserts the two profiles produce different trees. If
    this ever passes trivially, the whole file is measuring nothing.
    """

    from agent.skill_utils import get_all_skills_dirs

    outside = [str(item) for item in get_all_skills_dirs()]
    with persona_profile_context(two_profiles.binding):
        inside = [str(item) for item in get_all_skills_dirs()]

    assert outside != inside, (
        "the persona scope did not move the ambient skill roots, so nothing in "
        f"this file is being tested: {outside}"
    )
    assert str(two_profiles.other) in inside[0], inside
    assert str(two_profiles.head) in outside[0], outside


# --------------------------------------------------------------------------- #
# 1. The digest and the count survive a persona scope — same thread
# --------------------------------------------------------------------------- #
def test_the_digest_survives_a_persona_scope_on_the_same_thread(two_profiles):
    """(i) The closure asks the same question inside a scope as outside it."""

    plain = core_cache.build_input_fingerprint()
    assert plain is not None
    with persona_profile_context(two_profiles.binding):
        assert _ambient_home() == two_profiles.other, (
            "the scope did not export the other profile's home, so this case "
            "measured nothing"
        )
        scoped = core_cache.build_input_fingerprint()
    assert scoped is not None

    assert scoped.digest == plain.digest, (
        "the input fingerprint changed because a persona profile scope was open "
        "while it was taken, so a sidecar written by one asker cannot be judged "
        "by another in the same process. That is the measured 2026-08-18 "
        f"divergence: {plain.count} inputs plain, {scoped.count} inside the "
        "scope."
    )


def test_the_count_survives_a_persona_scope_on_the_same_thread(two_profiles):
    """(ii) Its OWN assertion, because the field symptom was a COUNT delta.

    ``inputs=24344`` vs ``inputs=23107`` is what the operator's log showed. A
    digest-only claim could pass while one file swapped for another of the same
    shape, and the count is the number a census actually reads off the line.
    """

    plain = core_cache.build_input_fingerprint()
    assert plain is not None
    with persona_profile_context(two_profiles.binding):
        scoped = core_cache.build_input_fingerprint()
    assert scoped is not None

    assert scoped.count == plain.count, (
        f"the closure gained/lost entries under a persona scope ({plain.count} "
        f"-> {scoped.count}). The 2026-08-18 field shape exactly: 1,237 extra "
        "entries from walking another profile's entire skills tree."
    )


def test_class_6_keeps_the_heads_skill_roots_inside_a_scope(two_profiles):
    """The class the measured divergence came from, named on its own.

    ``get_all_skills_dirs()`` puts the AMBIENT home's ``skills/`` at index 0, so
    an unpinned class 6 enumerates the other profile's whole tree.
    """

    with persona_profile_context(two_profiles.binding):
        scoped = core_cache.build_input_fingerprint()
    assert scoped is not None
    paths = _paths(scoped)

    assert str(two_profiles.head / "skills" / "only_head" / "SKILL.md") in paths, (
        "the head profile's skill package left the closure"
    )
    foreign = _under(paths, two_profiles.other / "skills")
    assert foreign == [], (
        "the closure walked ANOTHER PROFILE'S skills tree because a persona "
        f"scope was open while it was taken: {foreign[:5]}"
    )


# --------------------------------------------------------------------------- #
# 2. The same, on a thread that never entered the scope
# --------------------------------------------------------------------------- #
def _fingerprint_while_scope_held(two_profiles):
    """Hold the scope on this thread; fingerprint on another one.

    The capture is PRIMED first, which is the boot ordering rather than a
    convenience: a serve child's first fingerprint is the stale-first read at
    boot, long before ``agents_readiness`` opens its first persona scope. The
    opposite ordering is a real (and named) residual — see
    ``test_a_capture_taken_during_a_persona_scope_pins_that_scopes_home``.
    """

    primed = core_cache.resolved_fingerprint_home()
    assert primed[0] == two_profiles.head, primed

    held = threading.Event()
    released = threading.Event()
    thread, box = _fingerprint_off_thread(held, released)
    try:
        with persona_profile_context(two_profiles.binding):
            held.set()
            released.wait(timeout=10)
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the off-thread fingerprint never finished"
    return box


def test_the_digest_survives_a_persona_scope_held_on_another_thread(two_profiles):
    """The shape the field divergence was actually measured on.

    ContextVars do not cross a thread boundary, so the head
    ``persona_profile_context`` recorded is invisible here and every
    head-resolving authority falls back to the flipped ambient home. An argument
    from reading ``running_work._head_home`` — which DOES ask the head authority
    — would have declared this safe.
    """

    plain = core_cache.build_input_fingerprint()
    assert plain is not None
    box = _fingerprint_while_scope_held(two_profiles)

    assert box["ambient"] == two_profiles.other, (
        "the other thread did not see the exported profile home, so this case "
        f"measured nothing (saw {box['ambient']})"
    )
    off_thread = box["fingerprint"]
    assert off_thread is not None, "the off-thread fingerprint refused"
    assert off_thread.digest == plain.digest, (
        "a consult on another thread computed a DIFFERENT closure while a "
        "persona scope was open on this one — two askers, one process, one "
        f"store, two answers ({plain.count} vs {off_thread.count} inputs)."
    )


def test_class_2_keeps_the_heads_running_work_stores_off_thread(two_profiles):
    """``running_work_store_paths()`` is head-resolved and still exposed HERE.

    Its own witness because its file — ``processes.json`` — belongs to no other
    class, so this claim can only be killed by unpinning class 2.
    """

    box = _fingerprint_while_scope_held(two_profiles)
    paths = _paths(box["fingerprint"])

    assert str(two_profiles.head / "processes.json") in paths, (
        "the head's running-work checkpoint left the closure"
    )
    assert str(two_profiles.other / "processes.json") not in paths, (
        "the closure watched ANOTHER PROFILE'S running-work checkpoint: the head "
        "authority degenerated to the flipped ambient home on a thread that "
        "never entered the persona scope"
    )


def test_class_3_keeps_the_heads_chat_sessiondb_off_thread(two_profiles):
    """The chat SessionDB, same cross-thread degradation.

    In the ordinary layout this file is the same ``state.db`` class 2 already
    names, so an unpinned class 3 shows up as the OTHER profile's database
    APPEARING beside the head's — which is what is asserted, rather than the
    head's absence it cannot cause.
    """

    box = _fingerprint_while_scope_held(two_profiles)
    paths = _paths(box["fingerprint"])

    assert str(two_profiles.head / "state.db") in paths, (
        "the head's chat SessionDB left the closure"
    )
    foreign = [
        path
        for path in paths
        if path.startswith(str(two_profiles.other / "state.db"))
    ]
    assert foreign == [], (
        "the closure fingerprinted another profile's chat SessionDB (and its WAL "
        f"siblings): {foreign}"
    )


# --------------------------------------------------------------------------- #
# 3. The capture is per PROCESS, not forever
# --------------------------------------------------------------------------- #
def test_the_captured_home_is_dropped_by_reset_process_state(two_profiles, monkeypatch):
    """(iii) A second process must be able to resolve a different head.

    Freezing the home for the life of the interpreter would make the closure
    stable and WRONG: an install that legitimately moves its home would be served
    a core built against the old one forever. ``reset_process_state`` is the
    declared seam for "as a fresh process would", so the capture belongs in it.
    """

    first = core_cache.build_input_fingerprint()
    assert first is not None
    assert core_cache.resolved_fingerprint_home()[0] == two_profiles.head

    core_cache.reset_process_state()
    monkeypatch.setenv("HERMES_HOME", str(two_profiles.other))

    second = core_cache.build_input_fingerprint()
    assert second is not None
    assert core_cache.resolved_fingerprint_home()[0] == two_profiles.other, (
        "reset_process_state did not forget the captured home, so this process "
        "cannot exercise a second process's behaviour"
    )
    assert second.digest != first.digest, (
        "a genuinely different head home produced the same closure — the capture "
        "survived the reset and is answering with the previous home's tree"
    )


def test_a_capture_taken_during_a_persona_scope_pins_that_scopes_home(two_profiles):
    """THE RESIDUAL, asserted rather than left as prose.

    ``resolved_fingerprint_home`` resolves through
    ``hermes_constants.get_hermes_head_home``, which FALLS BACK to the ambient
    home when no head authority is present — and under an active persona
    override that fallback IS the override. So resolving "through the head" does
    not make the closure pure on its own; taking the resolution ONCE, before any
    persona scope in the process can run, is what does. This case pins the honest
    limit of that: a process whose very first fingerprint happens inside a scope
    captures the scope's home and pins it for its life.

    That state is NOT silent, which is why it is acceptable: the sidecar records
    ``fingerprint_home``, and the next boot judging against it demotes
    ``home_mismatch`` — the field signal that a capture was taken too late.
    """

    held = threading.Event()
    released = threading.Event()
    thread, box = _fingerprint_off_thread(held, released)
    try:
        with persona_profile_context(two_profiles.binding):
            held.set()
            released.wait(timeout=10)
    finally:
        thread.join(timeout=10)

    assert box["fingerprint"] is not None
    assert core_cache.resolved_fingerprint_home()[0] == two_profiles.other, (
        "a first-ever capture taken while a persona scope was exported did NOT "
        "record that scope's home — if this became true by construction the "
        "residual is gone and this case should be retired, not edited"
    )
    assert core_cache.resolved_fingerprint_home()[1] is False, (
        "the capture reported itself as taken from an explicit head authority "
        "while none was set; an unauthoritative capture must say so, because "
        "that flag is what tells an operator the home is only as good as the "
        "moment it was taken"
    )


# --------------------------------------------------------------------------- #
# 4. The sidecar carries the home the closure resolved through
# --------------------------------------------------------------------------- #
def _write_back_sidecar(monkeypatch) -> dict:
    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:stamp:clean")
    key = core_cache.build_input_fingerprint()
    assert key is not None
    assert core_cache.write_back({}, fingerprint=key) is True
    return json.loads(core_cache.sidecar_path().read_text(encoding="utf-8"))


def test_the_sidecar_records_the_home_the_closure_resolved_through(
    two_profiles, monkeypatch
):
    """(iv) Without it, a pair written under one closure and judged under another
    is indistinguishable from an ordinary store change."""

    sidecar = _write_back_sidecar(monkeypatch)

    assert sidecar["fingerprint_home"] == str(two_profiles.head), (
        "the sidecar does not name the home its key was taken under, so a demote "
        "cannot tell 'the store moved' from 'we asked a different question'"
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_the_sidecar_records_whether_the_head_was_authoritative(
    two_profiles, monkeypatch, explicit
):
    """Driven to BOTH states, so a constant matches at most one.

    ``False`` means the head degenerated to the ambient resolution at capture
    time — the home is then only as good as the moment it was taken, which is a
    fact a demote should be able to name rather than infer.
    """

    if explicit:
        monkeypatch.setenv("HERMES_HEAD_HOME", str(two_profiles.head))
        core_cache.reset_fingerprint_home()

    sidecar = _write_back_sidecar(monkeypatch)

    assert sidecar["fingerprint_home_authoritative"] is explicit, (
        "the sidecar's record of WHERE the home came from disagrees with "
        f"hermes_head_home_is_authoritative() (expected {explicit})"
    )


# --------------------------------------------------------------------------- #
# 5. A cache judged under a DIFFERENT home says so (MC-2 arm 2)
# --------------------------------------------------------------------------- #
_DROP = object()


def _persist_pair(monkeypatch, **overrides):
    """Write a real pair, then edit the sidecar into the shape under test.

    The pair is produced by ``write_back`` rather than hand-built, so the
    ``core_sha256`` binding and every other clause of the conjunction are
    genuinely satisfied and the case can only be decided by the clause it is
    about.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:stamp:clean")
    key = core_cache.build_input_fingerprint()
    assert key is not None
    assert core_cache.write_back({}, fingerprint=key) is True

    path = core_cache.sidecar_path()
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    for name, value in overrides.items():
        if value is _DROP:
            sidecar.pop(name, None)
        else:
            sidecar[name] = value
    path.write_text(json.dumps(sidecar, sort_keys=True), encoding="utf-8")
    return key


def _demote_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("snapshot_core_cache core_source=rebuilt")
    ]


def _fields(line: str) -> set[str]:
    """The line's whitespace-separated ``key=value`` fields.

    Fields, not substrings, and it took a killing mutation to learn why: a
    demote that returned ``"home_mismatch_typo"`` renders
    ``reason=home_mismatch_typo``, in which ``"reason=home_mismatch" in line`` is
    still True. A census greps tokens; so does this.
    """

    return set(line.split())


def test_a_pair_keyed_under_another_home_demotes_as_itself(two_profiles, monkeypatch):
    """(i) The typed demote fires, on a pair whose DIGEST still matches.

    Everything else about this pair is valid — same store root, same stamp, same
    contract, same digest — so ``matched`` would be True without this clause. The
    only thing wrong with it is that it answers a different question.
    """

    key = _persist_pair(monkeypatch, fingerprint_home=str(two_profiles.other))

    read = core_cache.read_persisted_core(fingerprint=key)

    assert read.matched is False, (
        "a core keyed under another Hermes home was served as authoritative: its "
        "closure covered a different profile's skills, config and SessionDB"
    )
    assert read.reason == core_cache.DEMOTE_HOME_MISMATCH, read.reason


def test_the_home_demote_fires_INSTEAD_of_the_generic_one(
    two_profiles, monkeypatch, caplog
):
    """(ii) Placement is the claim, so the pair is driven to trip BOTH clauses.

    The digest is wrong AND the home is wrong. Ordered as written, the operator
    is told which of the two facts is the real one; ordered behind the digest
    compare, this pair is indistinguishable from a store that simply moved —
    which is the state A1-b filed, where a miss names nothing actionable.
    """

    _persist_pair(
        monkeypatch,
        fingerprint_home=str(two_profiles.other),
        fingerprint="0" * 64,
    )

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        decision = core_cache.consult(caller="probe")

    assert decision.core is None and decision.demoted is True
    lines = _demote_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert f"reason={core_cache.DEMOTE_HOME_MISMATCH}" in _fields(line), line
    assert f"reason={core_cache.DEMOTE_FINGERPRINT_MISMATCH}" not in line, (
        "the home mismatch was swallowed as a generic fingerprint mismatch, so "
        "the operator reads 'the store moved' for something that is not about "
        f"the store: {line}"
    )


def test_a_sidecar_written_before_this_stage_is_not_demoted_for_it(
    two_profiles, monkeypatch
):
    """(iii) Absent is not mismatch.

    Every pair persisted before MC-2 carries no home at all. Treating that as a
    disagreement would demote every install a SECOND time — once for the closure
    change this stage already forces, and again for a field it could not have
    written — for no information whatsoever.
    """

    key = _persist_pair(monkeypatch, fingerprint_home=_DROP)

    read = core_cache.read_persisted_core(fingerprint=key)

    assert read.matched is True, (
        "a legacy sidecar with no fingerprint_home was demoted "
        f"({read.reason!r}); absent must not read as mismatch"
    )


def test_the_rendered_line_carries_the_spelling_the_table_tells_you_to_grep(
    two_profiles, monkeypatch, caplog
):
    """(iv) The TEXT, not the constant.

    The channel table tells a census to grep ``reason=home_mismatch`` on the
    ``snapshot_core_cache core_source=rebuilt`` family. Direction 1 of
    ``test_core_cache_channel_table.py`` reads the CONSTANTS, so a demote that
    returned a hand-worded string instead of ``DEMOTE_HOME_MISMATCH`` would
    satisfy it while emitting a token no row names. This case is the one that
    catches that, which is why it asserts the rendered text against the constant
    rather than against a literal of its own.
    """

    _persist_pair(monkeypatch, fingerprint_home=str(two_profiles.other))

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core_cache.consult(caller="probe")

    lines = _demote_lines(caplog)
    assert len(lines) == 1, lines
    fields = _fields(lines[0])
    assert f"reason={core_cache.DEMOTE_HOME_MISMATCH}" in fields, lines[0]
    assert "caller=probe" in fields, lines[0]
