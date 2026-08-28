"""One serve process, two lanes, one ``os.environ`` — and the argv lane lost.

THE DEFECT THESE TESTS PLANT
----------------------------
``agent_runtime.profile_context.persona_profile_context`` mirrors a persona's
``HERMES_HOME`` / ``HOME`` / ``HERMES_AUTH_HOME`` into **process-global**
``os.environ`` (``export_env=True``, the default). That mirror is deliberate and
still load-bearing — it is the only channel a spawned subprocess or a raw-env
in-process plugin can see, and ContextVars never cross either boundary. What it
is NOT is thread-scoped: for the width of that binding, EVERY other thread in
the process that resolves ``hermes_constants.get_hermes_home()`` without a
binding of its own answers the bound persona's home.

Measured on the operator's screen, 2026-08-27. The launcher's serve child was
booted onto ``profiles\\alice`` and ran its boot actor prewarm
(``agent_runtime.persona_chat_actor_prewarm``) across every placed persona at
20:56:06-20:56:14 — four instances bound to the profile ``launcher-qa``, the
first bind held for 11.25 s. A concurrent argv request on the serve's stdio
lane, ``harness characters status --draft 20260827-150945-7ba0cb``, resolved its
drafts directory under ``...\\profiles\\launcher-qa`` — another persona's home —
and reported a base-authored draft as nonexistent. Nothing was wrong with the
draft; the request was simply asked to read the wrong disk.

The victim was ``agent.charsheet.draft.drafts_dir()`` — at the time
``get_hermes_home() / "characters" / ".drafts"``, with no home argument to pass
and no binding of its own, exactly like the handler that filed the incident.

**It is no longer the observable, and why it stopped being one is the point.**
The character library was head-homed to ``<hermes_root>/shared/characters``
(launcher plan §A-1/§A-2, 2026-08-27), so every profile home under one root now
resolves the SAME drafts directory — ``alice`` and ``launcher-qa`` included.
That is the reversal's central claim made mechanical: a mis-resolved persona
home is no longer a characters incident, because there is nothing per-home left
to mis-resolve. So the probe below resolves ``get_hermes_home()`` itself, which
is still bleed-sensitive and is what every OTHER profile-scoped reader on that
lane rides, and it asserts the library's invariance beside it as the dividend.
The bleed is fixed at the lane, not at one reader.

WHY THE TEST DRIVES ``serve_loop`` RATHER THAN A BARE RESOLVER
--------------------------------------------------------------
A bare ``drafts_dir()`` on a second thread is bled today and is STILL bled after
the fix — because the fix is not "make the mirror thread-safe" (it cannot be)
but "give the argv lane a home of its own that the mirror cannot reach". That
home is pinned at the request seam, ``serve_loop``'s ``_run``, so the seam is
what has to be exercised. The injected ``dispatch`` stands in for every
``_cmd_*`` handler: whatever it resolves, a real handler resolves the same way.

Ordering matters and is enforced rather than hoped for. The serve's boot home is
captured at a boot instant that provably precedes any persona scope in the
process (the same argument ``core_cache``'s ``capture_fingerprint_home`` makes
for the fingerprint home), so the concurrent binding here is entered from INSIDE
the dispatch — after boot, mid-request. That is the stronger shape: it proves
the request's home survives a flip that arrives after the request started, which
a re-read of ``os.environ`` at request entry would not.
"""

from __future__ import annotations

import io
import json
import os
import threading

import pytest

from hermes_constants import get_hermes_home

from agent_runtime.profile_context import (
    PersonaProfileBinding,
    persona_profile_context,
    process_home_scope,
)
from hermes_cli.harness_parts.serve import serve_loop


SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"

#: How long a helper thread may keep the suite waiting before we call it wedged.
#: Generous — every wait here is on an event another thread sets unconditionally
#: in a ``finally``, so a timeout means a real deadlock, not a slow machine.
_WAIT_SECONDS = 20.0


def _request(rid: str, argv: list[str]) -> str:
    return json.dumps({"id": rid, "argv": argv}) + "\n"


def _frames(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """The two homes of the incident: the serve's own, and a persona's.

    ``alice`` is what the serve child was booted onto and what every argv
    request on it is entitled to read. ``launcher-qa`` is the profile the four
    prewarmed instances bound — a real, separate directory, so a resolver that
    answers it is answering something that exists and looks plausible, which is
    exactly why the incident read as "the draft is gone" rather than as an
    error.
    """

    process_home = tmp_path / "profiles" / "alice"
    persona_home = tmp_path / "profiles" / "launcher-qa"
    process_home.mkdir(parents=True)
    persona_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.delenv("HERMES_AUTH_HOME", raising=False)
    return process_home, persona_home


def _persona_binding(persona_home) -> PersonaProfileBinding:
    """The binding the prewarm builds: a real profile with its own home."""

    return PersonaProfileBinding(
        persona_id="launcher-qa-instance",
        hermes_profile="launcher-qa",
        profile_home=persona_home,
    )


def test_argv_request_answers_the_serve_home_while_a_persona_bind_holds_the_env(
    homes, tmp_path
):
    """The incident, in one process: prewarm binds, the argv lane must not care.

    RED before the fix — the dispatch resolved ``profiles/launcher-qa``, the
    prewarm thread's home, because nothing on the pool worker had bound a home
    and ``get_hermes_home()``'s ladder fell through to the mirrored
    ``os.environ["HERMES_HOME"]``.
    """

    process_home, persona_home = homes
    bind_live = threading.Event()
    probe_done = threading.Event()
    holder_failed: list[BaseException] = []
    resolved: list[str] = []
    library: list[str] = []

    def _hold_persona_bind() -> None:
        """Stand in for the prewarm's ``_execute_agent_run`` scope stack.

        It binds through ``persona_profile_context`` with the env mirror ON,
        which is what ``ProfileAgentRunner._execute_agent_run`` does for a
        prewarm and a real turn alike — the prewarm has no bind site of its own,
        it rides that body.
        """

        try:
            with persona_profile_context(
                _persona_binding(persona_home), runtime_root=tmp_path / "runtime"
            ):
                # The mirror is live and global from here. Assert it, so a
                # future change that quietly stops mirroring turns this test
                # into a loud failure instead of a silent pass.
                assert os.environ["HERMES_HOME"] == str(persona_home)
                bind_live.set()
                assert probe_done.wait(_WAIT_SECONDS), "probe never finished"
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            holder_failed.append(exc)
        finally:
            bind_live.set()
            probe_done.set()

    def dispatch(argv: list[str]) -> int:
        from agent.charsheet.draft import drafts_dir

        holder = threading.Thread(target=_hold_persona_bind, name="prewarm-stand-in")
        holder.start()
        try:
            assert bind_live.wait(_WAIT_SECONDS), "persona bind never went live"
            resolved.append(str(get_hermes_home()))
            # Taken from INSIDE the same bled window, so the two answers are
            # comparable: what the lane resolves, and what the character library
            # resolves from it.
            library.append(str(drafts_dir()))
        finally:
            probe_done.set()
            holder.join(_WAIT_SECONDS)
        return 0

    out = io.StringIO()
    assert (
        serve_loop(
            iter([_request("r1", ["harness", "characters", "status"]), SHUTDOWN]),
            out,
            pool_size=2,
            dispatch=dispatch,
        )
        == 0
    )

    assert not holder_failed, holder_failed[0]
    assert resolved, "dispatch never ran"
    answered = resolved[0]
    assert answered == str(process_home), (
        "the argv request read another persona's home: "
        f"{answered!r} instead of the serve's own {str(process_home)!r}"
    )
    assert str(persona_home) not in answered
    # The dividend, asserted so a regression to a per-home library would say so
    # here too: both profiles sit under one root, so the character library is
    # the same directory whichever of them the lane had answered.
    assert library[0] == str(tmp_path / "shared" / "characters" / ".drafts")

    exits = [f for f in _frames(out) if f.get("event") == "exit"]
    assert exits == [{"id": "r1", "event": "exit", "code": 0}]


def test_process_home_scope_beats_a_concurrently_mirrored_env_var(homes, tmp_path):
    """The unit under the seam: the ContextVar wins, whenever the flip lands.

    Both orderings in one test, because both happen in the field: a request that
    STARTS while another lane's mirror is already live (boot prewarm overlapping
    an early request), and one that starts clean and is flipped mid-flight (a
    chat turn beginning while the request runs).
    """

    process_home, persona_home = homes
    binding = _persona_binding(persona_home)
    runtime_root = tmp_path / "runtime"

    # Flip first, then scope: the scope pins the home it was HANDED, not a
    # re-read of whatever the environment says at entry. That distinction is the
    # whole point — a re-read would inherit the flip.
    with persona_profile_context(binding, runtime_root=runtime_root):
        assert os.environ["HERMES_HOME"] == str(persona_home)
        with process_home_scope(process_home):
            assert get_hermes_home() == process_home

    # Scope first, then flip, on another thread — the shape the serve actually
    # has, since the mirror is written by a lane this one never joins.
    seen: list[str] = []
    flipped = threading.Event()
    release = threading.Event()

    def _flip() -> None:
        try:
            with persona_profile_context(binding, runtime_root=runtime_root):
                flipped.set()
                release.wait(_WAIT_SECONDS)
        finally:
            flipped.set()

    with process_home_scope(process_home):
        thread = threading.Thread(target=_flip, name="mirror-flip")
        thread.start()
        try:
            assert flipped.wait(_WAIT_SECONDS)
            seen.append(str(get_hermes_home()))
        finally:
            release.set()
            thread.join(_WAIT_SECONDS)

    assert seen == [str(process_home)]


def test_process_home_scope_with_no_home_binds_nothing(homes):
    """``None`` is a no-op, not a bind to nowhere.

    A plain CLI process that never booted a serve has no captured boot home to
    hand over. It must behave exactly as it does today — ambient resolution,
    unchanged — rather than pinning an empty override that would out-rank a
    legitimate persona binding.
    """

    process_home, persona_home = homes
    with process_home_scope(None):
        assert get_hermes_home() == process_home
        with persona_profile_context(
            _persona_binding(persona_home), runtime_root=None
        ):
            assert get_hermes_home() == persona_home
