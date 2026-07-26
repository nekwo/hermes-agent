"""Terminal-envelope grants — ONE deterministic decision point for mission-chat.

Background (live evidence, 2026-07-26). The harness terminal safety envelope
had two opposite behaviors for the same command on the SAME lane, and both were
observed on mission-chat Dev turns within hours of each other:

* **fail-CLOSED** — a persona bound to a Hermes profile runs inside
  ``persona_profile_context``, which exports ``HERMES_AGENT_RUNTIME_ROOT``. The
  envelope fires: ``git_push_requires_operator_approval``, with no channel
  anywhere on the lane to obtain that approval.
* **fail-OPEN** — a persona binding NO Hermes profile takes the
  ``binding.profile_home is None`` early-yield, never exports that variable, so
  the envelope is inert; ``tools/approval.py``'s non-interactive default then
  auto-approves and ``git push origin main`` runs ungated and unrecorded.

Which branch a turn got was decided by whether its persona happened to carry a
``hermes_profile``. These tests pin the convergence: BOTH constructions now
produce the identical typed refusal, and the only way a gated class runs is an
explicit ROOT-config operator grant.

Ordering mirrors ``test_mcp_admission.py``: the security floor first (nothing
is granted by default, a profile cannot self-grant, the stage floor holds, a
config fault never reads as allow), the happy path second.

``tests/agent_runtime/conftest.py`` sets ``HERMES_AGENT_RUNTIME_ROOT``
autouse — the fail-open cases delete it deliberately, which is the whole point.
"""

from __future__ import annotations

import json
import textwrap
import types

import pytest

from agent_runtime.terminal_envelope import (
    CREDENTIAL_EXFIL,
    CREDENTIAL_READ,
    DESTRUCTIVE_GIT,
    ENVELOPE_COMMAND_NOT_GRANTABLE,
    ENVELOPE_COMMAND_REQUIRES_GRANT,
    ENVELOPE_DECISION_LOG,
    GIT_PUSH,
    GRANT_CLASS_NOT_GRANTABLE,
    GRANT_MALFORMED,
    GRANT_UNKNOWN_COMMAND_CLASS,
    GRANTABLE_COMMAND_CLASSES,
    COMMAND_CLASSES,
    LANE_MISSION_CHAT,
    LEGACY_REASON_BY_CLASS,
    NETWORK_EGRESS,
    OUTCOME_ALLOW,
    OUTCOME_GRANTED,
    OUTCOME_REFUSE,
    PROD_OPERATION,
    RECURSIVE_DELETE,
    TerminalEnvelopeScope,
    blocked_result,
    classify_command,
    envelope_decision,
    explain_terminal_envelope,
    record_envelope_decision,
    resolve_terminal_envelope_grants,
    terminal_envelope_scope,
)
from agent_runtime.runtime_config import TerminalEnvelopeConfig


# ── fixtures / helpers ──────────────────────────────────────────────────────


def _cfg(**grants) -> types.SimpleNamespace:
    """A stand-in runtime config carrying only the terminal-envelope block."""

    return types.SimpleNamespace(terminal_envelope=TerminalEnvelopeConfig(grants=dict(grants)))


def _dev_scope(runtime_root="") -> TerminalEnvelopeScope:
    return TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT,
        role="dev",
        persona_id="dev",
        session_id="chat-1",
        runtime_root=str(runtime_root or ""),
    )


def _payload(command, *, scope, cfg=None, monkeypatch=None):
    """Run the command through the terminal tool's envelope gate.

    Returns ``None`` when the gate lets it through to execution (the granted /
    ungated answer) and the blocked JSON payload otherwise. This is the exact
    seam ``terminal_tool`` consults, so a ``None`` here IS "executes normally"
    without the test having to shell out and actually push.
    """

    from tools import terminal_tool as terminal_tool_module

    if cfg is not None:
        # Pin the config the decision reads without touching any real config
        # file. The production reader is exercised separately by the
        # root-config-only tests below.
        import agent_runtime.terminal_envelope as te

        monkeypatch.setattr(
            te, "envelope_config", lambda cfg_arg=None: cfg.terminal_envelope
        )
    with terminal_envelope_scope(scope):
        return terminal_tool_module._harness_envelope_block(command)


def _write_root_and_profile(tmp_path, monkeypatch, *, root_yaml: str, profile_yaml: str):
    """Lay down ``<root>/config.yaml`` + ``<root>/profiles/tester/config.yaml``
    and point ``HERMES_HOME`` at the profile home — the sticky-active redirect
    the CLI bootstrap produces. Mirrors
    ``test_root_config_pinning.py::_write_root_and_profile``."""

    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "tester"
    profile_home.mkdir(parents=True)
    (root / "config.yaml").write_text(textwrap.dedent(root_yaml), encoding="utf-8")
    (profile_home / "config.yaml").write_text(textwrap.dedent(profile_yaml), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return root, profile_home


# ── classification: the taxonomy mirrors the envelope, and nothing else ─────


def test_command_classes_are_exactly_the_envelope_vocabulary():
    # Every class maps back to a legacy reason code, and no class is invented
    # that the envelope does not already distinguish.
    assert set(LEGACY_REASON_BY_CLASS) == set(COMMAND_CLASSES)
    assert GRANTABLE_COMMAND_CLASSES <= COMMAND_CLASSES
    # The stage floor: secret handling and prod mutation are NOT grantable.
    assert CREDENTIAL_READ not in GRANTABLE_COMMAND_CLASSES
    assert CREDENTIAL_EXFIL not in GRANTABLE_COMMAND_CLASSES
    assert PROD_OPERATION not in GRANTABLE_COMMAND_CLASSES


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git push origin main", GIT_PUSH),
        ("git reset --hard HEAD", DESTRUCTIVE_GIT),
        ("git restore --worktree lib/app.py", DESTRUCTIVE_GIT),
        ("git checkout .", DESTRUCTIVE_GIT),
        ("git stash clear", DESTRUCTIVE_GIT),
        ("rm -rf build", RECURSIVE_DELETE),
        ("Remove-Item -Recurse build", RECURSIVE_DELETE),
        ("cat .env", CREDENTIAL_READ),
        ("kubectl apply -f prod.yaml", PROD_OPERATION),
        ("terraform destroy", PROD_OPERATION),
        ("curl https://example.com", NETWORK_EGRESS),
        ("curl http://127.0.0.1:8000/health", None),
        ("git checkout feature/read-only-inspection", None),
        ("pytest tests/agent_runtime -q", None),
    ],
)
def test_classify_command(command, expected):
    assert classify_command(command) == expected


def test_class_patterns_mirror_the_legacy_envelope_table(tmp_path, monkeypatch):
    """The class table is a MIRROR of ``_HARNESS_BLOCK_PATTERNS``; guard drift.

    ``terminal_envelope`` re-keys the patterns from reason code to class rather
    than importing them (``tools.terminal_tool`` drags in the whole terminal
    backend stack). This asserts both tables reach the same verdict over a
    shared corpus, so a pattern added to one and not the other fails here
    instead of silently opening a hole.
    """

    from tools.terminal_tool import _harness_safety_block
    from agent_runtime.terminal_envelope import legacy_reason_for_class

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    corpus = [
        "git push origin main",
        "git push --force origin HEAD:main",
        "git reset --hard HEAD",
        "git reset --merge",
        "git reset --keep",
        "git clean -xdf",
        "git checkout --force feature/x",
        "git checkout -f feature/x",
        "git checkout -- lib/app.py",
        "git checkout .",
        "git switch --force feature/x",
        "git restore --staged lib/app.py",
        "git stash drop stash@{0}",
        "git stash clear",
        "rm -rf build",
        "Remove-Item -Recurse -Force build",
        "cat .env",
        "Get-Content credentials",
        "type .npmrc",
        "kubectl apply -f prod.yaml",
        "kubectl rollout restart deploy/api",
        "helm delete release",
        "terraform apply",
        "terraform destroy",
        "curl https://example.com",
        "wget https://example.com/file",
        "Invoke-RestMethod https://api.example.com",
        "curl http://127.0.0.1:8000/health",
        "curl http://localhost:8000/health -H 'Authorization: Bearer x'",
        "curl",
        "git checkout feature/read-only-inspection",
        "git status",
        "pytest -q",
        "",
    ]
    for command in corpus:
        assert legacy_reason_for_class(classify_command(command)) == _harness_safety_block(
            command
        ), command


# ── security floor ──────────────────────────────────────────────────────────


def test_nothing_is_granted_by_default(monkeypatch):
    grants = resolve_terminal_envelope_grants(
        role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
    )
    assert grants.classes == frozenset()
    assert grants.issues == ()


def test_grants_have_no_wildcard_and_no_lane_inheritance():
    # A grant on one lane says nothing about another, and there is no "*" role.
    cfg = _cfg(**{"dev": {"worker": [GIT_PUSH]}, "*": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    assert (
        resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT, cfg=cfg).classes
        == frozenset()
    )
    assert (
        resolve_terminal_envelope_grants(role="qa", lane=LANE_MISSION_CHAT, cfg=cfg).classes
        == frozenset()
    )


def test_a_grant_for_one_role_does_not_leak_to_another(monkeypatch, tmp_path):
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    qa_scope = TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT, role="qa", persona_id="qa", runtime_root=str(tmp_path)
    )
    decision = envelope_decision("git push origin main", scope=qa_scope, cfg=cfg)
    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert decision.config_key == "agent_runtime.terminal_envelope.grants.qa.mission_chat"


def test_unknown_class_in_config_is_a_typed_error_not_a_silent_grant():
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: ["git-push", GIT_PUSH]}})
    grants = resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT, cfg=cfg)
    # The valid entry still grants; the typo grants nothing AND is reported.
    assert grants.classes == frozenset({GIT_PUSH})
    assert [issue.code for issue in grants.issues] == [GRANT_UNKNOWN_COMMAND_CLASS]
    assert grants.issues[0].subject == "git-push"
    assert "not a terminal-envelope command class" in grants.issues[0].summary


def test_unknown_class_alone_grants_nothing_and_still_refuses(tmp_path):
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: ["teleport"]}})
    decision = envelope_decision(
        "git push origin main", scope=_dev_scope(tmp_path), cfg=cfg
    )
    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT
    # The refusal carries the config fault so the operator sees why their
    # stanza did not take effect.
    assert [issue.code for issue in decision.grant_issues] == [GRANT_UNKNOWN_COMMAND_CLASS]
    assert "teleport" in decision.fix_hint


def test_config_cannot_grant_a_class_outside_the_stage_floor(tmp_path):
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [CREDENTIAL_READ, PROD_OPERATION]}})
    grants = resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT, cfg=cfg)
    assert grants.classes == frozenset()
    assert {issue.code for issue in grants.issues} == {GRANT_CLASS_NOT_GRANTABLE}

    decision = envelope_decision("cat .env", scope=_dev_scope(tmp_path), cfg=cfg)
    assert decision.outcome == OUTCOME_REFUSE
    # A class that CANNOT be granted must not tell the agent to go ask for a
    # grant — that would be a new lie in place of the old one.
    assert decision.failure_class == ENVELOPE_COMMAND_NOT_GRANTABLE
    assert decision.config_key is None
    assert "hard floor" in decision.summary


def test_malformed_grant_value_grants_nothing_and_reports_the_shape():
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: GIT_PUSH}})  # str, not a list
    grants = resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT, cfg=cfg)
    assert grants.classes == frozenset()
    assert [issue.code for issue in grants.issues] == [GRANT_MALFORMED]


def test_config_load_fault_grants_nothing(monkeypatch, tmp_path):
    import agent_runtime.terminal_envelope as te

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(te, "load_root_runtime_config", _boom, raising=False)
    monkeypatch.setattr(
        "agent_runtime.config.load_root_runtime_config", _boom, raising=False
    )
    decision = envelope_decision("git push origin main", scope=_dev_scope(tmp_path))
    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT


# ── root-config only: a profile cannot self-grant ───────────────────────────


def test_grants_read_the_root_config_not_the_active_profile(tmp_path, monkeypatch):
    """A sticky-active profile's own config.yaml must not be able to grant push.

    Same shape as ``test_root_config_pinning.py``: ROOT says one thing, the
    profile home says another, ``HERMES_HOME`` points at the profile. A reader
    on the bare ``load_agent_runtime_config()`` would read the profile and
    grant; the pinned reader must not.
    """

    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="agent_runtime: {}\n",
        profile_yaml=f"""
            agent_runtime:
              terminal_envelope:
                grants:
                  dev:
                    {LANE_MISSION_CHAT}: [{GIT_PUSH}]
            """,
    )
    grants = resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT)
    assert grants.classes == frozenset(), "a profile config granted itself git_push"

    decision = envelope_decision("git push origin main", scope=_dev_scope(tmp_path))
    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT


def test_root_config_grant_is_honored_over_a_shadowing_profile(tmp_path, monkeypatch):
    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml=f"""
            agent_runtime:
              terminal_envelope:
                grants:
                  dev:
                    {LANE_MISSION_CHAT}: [{GIT_PUSH}]
            """,
        profile_yaml="agent_runtime: {}\n",
    )
    grants = resolve_terminal_envelope_grants(role="dev", lane=LANE_MISSION_CHAT)
    assert grants.classes == frozenset({GIT_PUSH})


def test_root_config_parses_the_grant_stanza_end_to_end(tmp_path):
    """The documented YAML stanza is the one the parser accepts."""

    from agent_runtime.config import load_agent_runtime_config

    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            agent_runtime:
              terminal_envelope:
                grants:
                  dev:
                    {LANE_MISSION_CHAT}: [{GIT_PUSH}]
            """
        ),
        encoding="utf-8",
    )
    cfg = load_agent_runtime_config(path)
    assert cfg.terminal_envelope.grants == {"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}}
    assert (
        resolve_terminal_envelope_grants(
            role="dev", lane=LANE_MISSION_CHAT, cfg=cfg
        ).classes
        == frozenset({GIT_PUSH})
    )


# ── convergence: the two former branches now produce ONE answer ─────────────


def _refusal_shape(payload):
    """Everything about a refusal except the volatile bits."""

    return {
        key: payload[key]
        for key in (
            "status",
            "blocked_by",
            "block_reason",
            "failure_class",
            "command_class",
            "lane",
            "role",
            "config_key",
            "error",
        )
    }


def test_former_fail_closed_branch_now_refuses_with_a_typed_grant_requirement(
    tmp_path, monkeypatch
):
    """Persona bound to a profile ⇒ HERMES_AGENT_RUNTIME_ROOT was exported.

    This construction previously produced the bare hard block
    ``git_push_requires_operator_approval`` with no way to lift it.
    """

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    payload = _payload(
        "git push origin main", scope=_dev_scope(tmp_path), cfg=_cfg(), monkeypatch=monkeypatch
    )
    assert payload is not None
    assert payload["failure_class"] == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert payload["command_class"] == GIT_PUSH
    # The legacy vocabulary survives for every existing consumer.
    assert payload["block_reason"] == "git_push_requires_operator_approval"


def test_former_fail_open_branch_now_refuses_instead_of_running(tmp_path, monkeypatch):
    """Persona binding NO profile ⇒ HERMES_AGENT_RUNTIME_ROOT was never set.

    This is the branch that let ``git push origin main`` execute ungated
    (envelope inert ⇒ ``tools/approval.py`` non-interactive fail-open). The
    scope is what governs now, not the env var, so it refuses.
    """

    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    scope = TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT, role="dev", persona_id="dev", session_id="chat-1"
    )
    payload = _payload(
        "git push origin main", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch
    )
    assert payload is not None
    assert payload["failure_class"] == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert payload["command_class"] == GIT_PUSH


def test_both_former_branches_converge_on_the_identical_refusal(tmp_path, monkeypatch):
    """The split is retired: same lane, same role, same command ⇒ same answer."""

    scope = TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT, role="dev", persona_id="dev", session_id="chat-1"
    )

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    closed = _payload(
        "git push origin main", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch
    )

    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    opened = _payload(
        "git push origin main", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch
    )

    assert closed is not None and opened is not None
    assert _refusal_shape(closed) == _refusal_shape(opened)


def test_refusal_names_the_config_key_the_agent_must_report(tmp_path, monkeypatch):
    """The refusal text is self-explanatory — the audit's operative-rules lie
    ("the operator's permission grant is the only gate") is not reproduced one
    layer down as an unexplained block."""

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    payload = _payload(
        "git push origin main", scope=_dev_scope(tmp_path), cfg=_cfg(), monkeypatch=monkeypatch
    )
    error = payload["error"]
    assert "agent_runtime.terminal_envelope.grants.dev.mission_chat" in error
    assert "ROOT config.yaml" in error
    assert f"[{GIT_PUSH}]" in error
    # Tells the agent not to burn the turn retrying — the failure mode the
    # untyped block produced.
    assert "Do NOT retry" in error
    assert payload["config_key"] == "agent_runtime.terminal_envelope.grants.dev.mission_chat"


def test_refusal_row_uses_the_shared_typed_row_shape(tmp_path):
    decision = envelope_decision(
        "git push origin main", scope=_dev_scope(tmp_path), cfg=_cfg()
    )
    row = decision.row()
    assert set(row) == {"code", "subject", "entry_point_lane", "summary", "fix_hint"}
    assert row["code"] == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert row["subject"] == GIT_PUSH
    assert row["entry_point_lane"] == LANE_MISSION_CHAT
    assert blocked_result(decision)["requirement_failure"] == row


# ── granted classes execute, with provenance ────────────────────────────────


def test_granted_class_passes_the_gate_and_logs_grant_provenance(tmp_path, monkeypatch):
    """A grant lets the command through AND records why it was allowed.

    ``_harness_envelope_block`` returning ``None`` is exactly the terminal
    tool's "proceed to execution" signal, so this proves the granted command
    executes normally without the test actually pushing anything.
    """

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    scope = _dev_scope(tmp_path)

    # Same command, no grant ⇒ refused. The None below is therefore meaningful.
    assert (
        _payload("git push origin main", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch)
        is not None
    )

    assert (
        _payload("git push origin main", scope=scope, cfg=cfg, monkeypatch=monkeypatch)
        is None
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / ENVELOPE_DECISION_LOG).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    granted = [row for row in rows if row["decision"] == OUTCOME_GRANTED]
    assert len(granted) == 1
    assert granted[0]["command_class"] == GIT_PUSH
    assert granted[0]["lane"] == LANE_MISSION_CHAT
    assert granted[0]["role"] == "dev"
    assert granted[0]["granted_by"] == "agent_runtime.terminal_envelope.grants.dev.mission_chat"
    assert granted[0]["command_preview"] == "git push origin main"


def test_a_grant_covers_only_the_class_it_names(tmp_path):
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    scope = _dev_scope(tmp_path)
    assert envelope_decision("git push origin main", scope=scope, cfg=cfg).outcome == (
        OUTCOME_GRANTED
    )
    # destructive_git and recursive_delete share the legacy ``tree_wipe_blocked``
    # reason but are separately grantable classes — granting push grants neither.
    for command in ("git restore lib/app.py", "rm -rf build"):
        decision = envelope_decision(command, scope=scope, cfg=cfg)
        assert decision.outcome == OUTCOME_REFUSE, command
        assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT


def test_ungated_commands_are_untouched_on_the_governed_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    scope = _dev_scope(tmp_path)
    assert (
        _payload("git status", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch) is None
    )
    decision = envelope_decision("git status", scope=scope, cfg=_cfg())
    assert decision.outcome == OUTCOME_ALLOW
    # An allowed command writes no receipt — the log is for decisions that
    # changed an outcome, not a firehose of every command.
    assert not (tmp_path / ENVELOPE_DECISION_LOG).exists()


def test_refusal_also_lands_in_the_pre_existing_blocked_attempt_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    _payload(
        "git push origin main", scope=_dev_scope(tmp_path), cfg=_cfg(), monkeypatch=monkeypatch
    )
    audit = json.loads(
        (tmp_path / "blocked_tool_attempts.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    # Existing keys, existing vocabulary — operators watching this file lose
    # nothing, and gain the typed fields.
    assert audit["reason"] == "git_push_requires_operator_approval"
    assert audit["command_preview"] == "git push origin main"
    assert audit["failure_class"] == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert audit["command_class"] == GIT_PUSH


def test_records_are_written_even_when_the_env_root_is_unset(tmp_path, monkeypatch):
    """The fail-open branch had no receipts at all — the scope supplies the root."""

    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    scope = _dev_scope(tmp_path)
    _payload("git push origin main", scope=scope, cfg=_cfg(), monkeypatch=monkeypatch)
    assert (tmp_path / ENVELOPE_DECISION_LOG).exists()


def test_recording_never_raises_when_no_root_can_be_resolved(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    scope = TerminalEnvelopeScope(lane=LANE_MISSION_CHAT, role="dev")
    decision = envelope_decision("git push origin main", scope=scope, cfg=_cfg())
    record_envelope_decision(decision, "git push origin main", scope=scope)  # no raise


# ── every other lane is unchanged ───────────────────────────────────────────


def test_no_scope_bound_means_this_module_has_no_opinion():
    # ``None`` is "fall through to the legacy envelope", never "allow".
    assert envelope_decision("git push origin main") is None


def test_worker_lane_keeps_the_legacy_hard_block(tmp_path, monkeypatch):
    """Worker ticks bind no envelope scope ⇒ byte-identical legacy behavior."""

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    from tools import terminal_tool as terminal_tool_module

    payload = terminal_tool_module._harness_envelope_block("git push origin main")
    assert payload == {
        "output": "",
        "exit_code": -1,
        "error": (
            "BLOCKED by Harness execution safety envelope: git_push_requires_operator_approval"
        ),
        "status": "blocked",
        "blocked_by": "harness_execution_safety",
        "block_reason": "git_push_requires_operator_approval",
    }
    audit = json.loads(
        (tmp_path / "blocked_tool_attempts.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert audit["reason"] == "git_push_requires_operator_approval"
    assert "failure_class" not in audit


def test_chat_lane_keeps_no_envelope_at_all(monkeypatch):
    """``hermes chat`` never set HERMES_AGENT_RUNTIME_ROOT and binds no scope."""

    monkeypatch.delenv("HERMES_AGENT_RUNTIME_ROOT", raising=False)
    from tools import terminal_tool as terminal_tool_module

    assert terminal_tool_module._harness_envelope_block("git push origin main") is None


def test_a_grant_does_not_leak_off_the_governed_lane(tmp_path):
    """An ungoverned lane is not consulted against the grant table at all."""

    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    worker = TerminalEnvelopeScope(lane="mission_worker", role="dev", runtime_root=str(tmp_path))
    assert envelope_decision("git push origin main", scope=worker, cfg=cfg) is None


def test_policy_import_failure_falls_back_to_the_hard_block(tmp_path, monkeypatch):
    """Fail CLOSED: an unavailable policy module must not open the envelope."""

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path))
    import builtins

    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name == "agent_runtime.terminal_envelope":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail)
    from tools import terminal_tool as terminal_tool_module

    payload = terminal_tool_module._harness_envelope_block("git push origin main")
    assert payload is not None
    assert payload["block_reason"] == "git_push_requires_operator_approval"


# ── operator view ───────────────────────────────────────────────────────────


def test_explain_is_a_complete_operator_answer():
    cfg = _cfg(**{"dev": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    view = explain_terminal_envelope(role="dev", lane=LANE_MISSION_CHAT, cfg=cfg)
    assert view["governed"] is True
    assert view["granted"] == [GIT_PUSH]
    assert GIT_PUSH not in view["refused"]
    assert CREDENTIAL_READ in view["refused"]
    assert view["config_key"] == "agent_runtime.terminal_envelope.grants.dev.mission_chat"


def test_supervisor_role_alias_is_honored():
    """The role is spelled ``alice_supervisor``; the live persona is Neko."""

    cfg = _cfg(**{"neko_supervisor": {LANE_MISSION_CHAT: [GIT_PUSH]}})
    grants = resolve_terminal_envelope_grants(
        role="alice_supervisor", lane=LANE_MISSION_CHAT, cfg=cfg
    )
    assert grants.classes == frozenset({GIT_PUSH})
    assert grants.config_key == (
        "agent_runtime.terminal_envelope.grants.alice_supervisor.mission_chat"
    )
