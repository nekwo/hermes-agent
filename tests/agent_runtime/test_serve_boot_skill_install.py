"""Stage H1 — ``harness serve`` boot re-joins the installed canonical skills.

The gap the operator ruled on (2026-08-30): a chat turn loads
``<hermes root>/shared/skills/<id>/SKILL.md`` and never the repo copy, the two
are joined by ``install-harness-skills`` and by nothing else, and every trigger
that ran that join fired when the machine PUBLISHED (explicit CLI verb, realm
pull, pre-push hook). A machine that only ``git pull``s and boots was repaired
by nothing. Boot is the moment a CONSUMER acquires the drift, so boot is where
the join now runs.

Plan: ``docs/agent-runtime-harness/planned/skill-install-trigger-relocation.md``.
"""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import install_harness_skills_at_boot, serve_loop

SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


def _frames(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


def _result(skill: str, *, changed: bool = False, ok: bool = True):
    from agent_runtime.skill_install import SkillInstallResult

    return SkillInstallResult(
        skill=skill,
        source=f"repo/{skill}",
        destination=f"installed/{skill}",
        source_hash="sha256:aaa",
        installed_hash="sha256:aaa" if ok else "sha256:bbb",
        installed=True,
        changed=changed,
        ok=ok,
    )


def test_boot_installs_before_the_first_request_is_dispatched(capsys):
    """The whole point: no request is answered against a stale package."""

    order: list[str] = []

    def _install() -> str:
        order.append("install")
        return "harness serve: skill install — 4 package(s), 1 refreshed, 0 failed"

    def _dispatch(argv):
        order.append("dispatch")
        return 0

    out = io.StringIO()
    code = serve_loop(
        iter([json.dumps({"id": "r1", "argv": ["harness", "status"]}) + "\n", SHUTDOWN]),
        out,
        pool_size=1,
        dispatch=_dispatch,
        skill_install=_install,
    )

    assert code == 0
    assert order == ["install", "dispatch"]

    frames = _frames(out)
    # …and it ran AFTER the frame a supervising launcher uses to tell a live
    # cold boot from a wedged child. Nothing goes in front of ``booting``.
    assert frames[0]["event"] == "booting"

    # STDOUT DISCIPLINE. Serve's stdout is the NDJSON bridge; the summary rides
    # the process's stderr/log lane and appears in NO frame, of any event type.
    captured = capsys.readouterr()
    assert "skill install — 4 package(s), 1 refreshed, 0 failed" in captured.err
    assert "skill install" not in out.getvalue()


def test_boot_skill_install_is_off_unless_the_entry_point_turns_it_on():
    """It WRITES into the machine-global shared skills root.

    Same injection contract as ``root_anchor``, and a sharper reason for it: a
    ``serve_loop`` unit test that fired this by default would edit the
    operator's live runtime rather than a tmp_path.
    """

    out = io.StringIO()
    assert serve_loop(iter([SHUTDOWN]), out, pool_size=1, dispatch=lambda argv: 0) == 0
    assert [f["event"] for f in _frames(out)][:2] == ["booting", "ready"]


def test_a_failed_boot_install_is_loud_but_never_fatal(capsys):
    """The push gate blocked; a boot must not.

    A push is one-shot, so an install that did not take had to stop it. A chat
    runtime is not: the next boot retries for free, and refusing to serve
    because a package would not copy is strictly worse than serving a stale one
    and saying so.
    """

    def _install() -> str:
        raise OSError("destination is read-only")

    out = io.StringIO()
    assert (
        serve_loop(
            iter([json.dumps({"id": "r1", "argv": ["harness", "status"]}) + "\n", SHUTDOWN]),
            out,
            pool_size=1,
            dispatch=lambda argv: 0,
            skill_install=_install,
        )
        == 0
    )

    frames = _frames(out)
    assert [f["event"] for f in frames][:2] == ["booting", "ready"]
    assert {"id": "r1", "event": "exit", "code": 0} in frames

    err = capsys.readouterr().err
    assert "skill install FAILED" in err
    assert "destination is read-only" in err


def test_boot_install_runs_exactly_what_a_realm_pull_runs(monkeypatch):
    """Both installers, with realm-sync's own arguments — not a second spelling.

    ``agent_runtime/realm_sync.py:509-511`` is the reference implementation;
    this must not drift into a variant of it.
    """

    from agent_runtime import config as runtime_config
    from agent_runtime import skill_install

    calls: dict[str, object] = {}
    personas = [SimpleNamespace(id="qa"), SimpleNamespace(id="neko")]
    cfg = object()

    def _ensure_personas(seen):
        calls["cfg"] = seen
        return personas

    def _install_skills(*, skills):
        calls["skills"] = skills
        return [_result("harness-qa-verdict", changed=True)]

    def _install_for_personas(seen):
        calls["personas"] = seen
        return [_result("harness-runtime-model")]

    monkeypatch.setattr(runtime_config, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(runtime_config, "ensure_persisted_personas", _ensure_personas)
    monkeypatch.setattr(skill_install, "install_harness_skills", _install_skills)
    monkeypatch.setattr(
        skill_install, "install_harness_skills_for_personas", _install_for_personas
    )

    summary = install_harness_skills_at_boot()

    assert calls["cfg"] is cfg
    assert calls["personas"] is personas
    assert calls["skills"] == sorted(skill_install.HARNESS_SKILLS)
    assert "2 package(s), 1 refreshed, 0 failed" in summary
    assert "harness-qa-verdict" in summary


def test_boot_install_summary_names_every_failed_package(monkeypatch):
    """A failure that is not NAMED is the false all-clear this lane retires."""

    from agent_runtime import config as runtime_config
    from agent_runtime import skill_install

    monkeypatch.setattr(runtime_config, "load_agent_runtime_config", lambda: object())
    monkeypatch.setattr(runtime_config, "ensure_persisted_personas", lambda _cfg: [])
    monkeypatch.setattr(
        skill_install,
        "install_harness_skills",
        lambda *, skills: [_result("harness-qa-verdict", ok=False)],
    )
    monkeypatch.setattr(
        skill_install, "install_harness_skills_for_personas", lambda _personas: []
    )

    summary = install_harness_skills_at_boot()

    assert "1 failed" in summary
    assert "FAILED harness-qa-verdict" in summary
    assert "installed/harness-qa-verdict" in summary


def test_cmd_serve_wires_the_real_installer(monkeypatch):
    """The injection is only a contract if the production entry point honours it."""

    seen: dict[str, object] = {}

    def _fake_claim() -> tuple[int, int]:
        return (
            os.open(os.devnull, os.O_RDONLY),
            os.open(os.devnull, os.O_WRONLY),
        )

    def _fake_serve_loop(reader, writer, **kwargs) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(serve_module, "_claim_protocol_pipes", _fake_claim)
    monkeypatch.setattr(serve_module, "serve_loop", _fake_serve_loop)

    assert (
        serve_module._cmd_serve(
            SimpleNamespace(ndjson=True, pool_size=1, no_socket=True)
        )
        == 0
    )
    assert seen["skill_install"] is install_harness_skills_at_boot
