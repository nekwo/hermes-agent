"""A granted command reports its own audit account to the agent that ran it.

Background (live evidence, 2026-08-09). The unbounded-default ruling
(``bc7928685``) removed the preventive refusal for every grantable command class
and substituted a detective control: the receipt in
``terminal_envelope_decisions.jsonl``. The operator verified that receipt on
disk. The QA agent could not — it ran a formerly-refused ``network_egress``
command (``curl https://example.com``), was asked to confirm the mechanism, and
reported that "the terminal receipt did not expose an audit record or
permission_mode provenance, so the required unbounded audit proof is missing",
returning an honest BLOCKED verdict on facts that were all fine.

It was right about what it could see. ``_harness_envelope_gate``'s granted
branch returned ``None`` and the tool result carried the ordinary
``output``/``exit_code``/``error`` shape — nothing about the class, the mode,
the grant source, or the receipt. The runtime knew; the receipt's own subject
was structurally blind to it.

These tests pin the fix at the level the guarantee lives: not "the shape
function returns a dict" (both halves of that seam were already fine), but
**what the agent actually receives from ``terminal_tool``** — the join, which is
where this codebase's recurring defect class lives.

The cost half is pinned just as hard. This fragment rides a governed command's
result, so an ungated command — nearly every command an agent ever runs — must
pay exactly zero, and a test that only proved the grant case would let a
regression tax every turn silently.
"""

from __future__ import annotations

import json
import types

import pytest

from agent_runtime.permission_modes import PERMISSION_MODE_UNBOUNDED
from agent_runtime.runtime_config import TerminalEnvelopeConfig
from agent_runtime.terminal_envelope import (
    ENVELOPE_DECISION_LOG,
    ENVELOPE_RESULT_KEY,
    GIT_PUSH,
    GRANT_SOURCE_CONFIG,
    GRANT_SOURCE_PERMISSION_MODE,
    LANE_MISSION_CHAT,
    OUTCOME_GRANTED,
    TerminalEnvelopeScope,
    envelope_decision,
    envelope_provenance,
    record_envelope_decision,
    terminal_envelope_scope,
)

# The formerly-refused command from the live report, and an ordinary one.
GATED_COMMAND = "curl https://example.com"
UNGATED_COMMAND = "ls -la"


def _cfg(**grants) -> types.SimpleNamespace:
    return types.SimpleNamespace(terminal_envelope=TerminalEnvelopeConfig(grants=dict(grants)))


def _scope(*, runtime_root="", permission_mode=PERMISSION_MODE_UNBOUNDED) -> TerminalEnvelopeScope:
    return TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT,
        role="dev",
        persona_id="dev",
        session_id="chat-1",
        runtime_root=str(runtime_root or ""),
        permission_mode=permission_mode,
    )


def _run(command, *, scope, monkeypatch, result='{"output":"ok","exit_code":0,"error":""}'):
    """Drive the PUBLIC ``terminal_tool`` with execution stubbed out.

    The point is the wrapper's composition — gate, then merge, then hand the
    string to the caller — so the 800-line body is replaced by a canned result.
    Executing a real ``curl`` would prove nothing extra about provenance and
    would make the test depend on the network.
    """

    from tools import terminal_tool as terminal_tool_module

    monkeypatch.setattr(
        terminal_tool_module, "_terminal_tool_run", lambda *a, **kw: result
    )
    with terminal_envelope_scope(scope):
        return json.loads(terminal_tool_module.terminal_tool(command))


# ── the gap: a granted command's result names why it ran ────────────────────


def test_a_mode_granted_command_tells_the_agent_the_mode_that_allowed_it(monkeypatch, tmp_path):
    """THE regression. Every field the QA agent looked for and did not find."""

    payload = _run(GATED_COMMAND, scope=_scope(runtime_root=tmp_path), monkeypatch=monkeypatch)

    account = payload[ENVELOPE_RESULT_KEY]
    assert account["decision"] == OUTCOME_GRANTED
    assert account["command_class"] == "network_egress"
    assert account["grant_source"] == GRANT_SOURCE_PERMISSION_MODE
    assert account["permission_mode"] == PERMISSION_MODE_UNBOUNDED
    assert account["granted_by"] == f"permission_mode={PERMISSION_MODE_UNBOUNDED}"
    # ...and that a receipt genuinely landed, which is the compensating control
    # the ruling traded the refusal for.
    assert account["receipted"] is True
    assert account["receipt_log"] == ENVELOPE_DECISION_LOG

    # The command's own output survives the merge untouched.
    assert payload["output"] == "ok"
    assert payload["exit_code"] == 0


def test_the_account_matches_the_receipt_row_actually_written(monkeypatch, tmp_path):
    """The agent's account and the operator's log must not be two stories.

    A provenance block that drifted from the receipt would be worse than none:
    the agent would cite a record that says something else.
    """

    payload = _run(GATED_COMMAND, scope=_scope(runtime_root=tmp_path), monkeypatch=monkeypatch)
    account = payload[ENVELOPE_RESULT_KEY]

    rows = [
        json.loads(line)
        for line in (tmp_path / ENVELOPE_DECISION_LOG).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    for field in ("command_class", "granted_by", "grant_source", "permission_mode"):
        assert row[field] == account[field], field
    assert row["decision"] == account["decision"]


def test_a_config_granted_command_names_its_config_key_not_the_mode(monkeypatch, tmp_path):
    """The other grant source. ``granted_by`` sends the reader to the stanza
    that actually allowed it — naming the mode here would point an operator at
    the wrong lever."""

    import agent_runtime.terminal_envelope as te

    monkeypatch.setattr(
        te,
        "envelope_config",
        lambda cfg_arg=None: _cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH]}).terminal_envelope,
    )
    payload = _run(
        "git push origin main",
        scope=_scope(runtime_root=tmp_path, permission_mode=""),
        monkeypatch=monkeypatch,
    )

    account = payload[ENVELOPE_RESULT_KEY]
    assert account["grant_source"] == GRANT_SOURCE_CONFIG
    assert account["command_class"] == GIT_PUSH
    assert "terminal_envelope" in account["granted_by"]
    assert account["granted_by"].endswith("mission_chat")
    assert "permission_mode" not in account


# ── the cost: everything that is not a grant pays nothing ───────────────────


def test_an_ungated_command_carries_no_account_at_all(monkeypatch, tmp_path):
    """The token-cost half, and the reason this rides the result rather than
    the per-turn HUD: ``ls`` is what an agent runs all day. An account on the
    ALLOW path would tax every command to describe a grant that never happened."""

    payload = _run(UNGATED_COMMAND, scope=_scope(runtime_root=tmp_path), monkeypatch=monkeypatch)

    assert ENVELOPE_RESULT_KEY not in payload
    assert payload == {"output": "ok", "exit_code": 0, "error": ""}
    # No receipt either — an allowed command is not an audit event.
    assert not (tmp_path / ENVELOPE_DECISION_LOG).exists()


def test_an_ungoverned_lane_carries_no_account(monkeypatch, tmp_path):
    """``hermes chat``/cron/gateway bind no governed scope, so the envelope has
    no opinion and the result shape is byte-identical to before this change."""

    payload = _run(GATED_COMMAND, scope=None, monkeypatch=monkeypatch)

    assert ENVELOPE_RESULT_KEY not in payload


def test_a_refusal_is_unchanged_and_carries_no_grant_account(monkeypatch, tmp_path):
    """A refusal already self-explains (class, config key, typed failure row).
    It must not also grow a grant account describing a grant that did not
    happen."""

    import agent_runtime.terminal_envelope as te

    monkeypatch.setattr(te, "envelope_config", lambda cfg_arg=None: _cfg().terminal_envelope)
    payload = _run(
        "git push origin main",
        scope=_scope(runtime_root=tmp_path, permission_mode=""),
        monkeypatch=monkeypatch,
    )

    assert payload["status"] == "blocked"
    assert payload["blocked_by"] == "harness_execution_safety"
    assert ENVELOPE_RESULT_KEY not in payload


# ── honesty: the receipt claim is the write's real answer ───────────────────


def test_a_receipt_that_failed_to_land_is_reported_as_not_receipted(monkeypatch, tmp_path):
    """The claim must be falsifiable, or it is not evidence.

    A receipt root that cannot be written (here: a FILE where the directory
    should be) is a real failure of the detective control. Reporting
    ``receipted: true`` anyway would hand the agent — and through it the
    operator — a proof that does not exist. The command still runs: auditability
    never gates the answer.
    """

    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("this is a file", encoding="utf-8")

    payload = _run(
        GATED_COMMAND, scope=_scope(runtime_root=blocked_root), monkeypatch=monkeypatch
    )

    account = payload[ENVELOPE_RESULT_KEY]
    assert account["receipted"] is False
    # The grant itself is still fully accounted for, and the command still ran.
    assert account["decision"] == OUTCOME_GRANTED
    assert account["granted_by"] == f"permission_mode={PERMISSION_MODE_UNBOUNDED}"
    assert payload["output"] == "ok"


def test_record_envelope_decision_reports_whether_the_row_landed(tmp_path):
    """The value the honesty above is built on, at its own level."""

    scope = _scope(runtime_root=tmp_path)
    decision = envelope_decision(GATED_COMMAND, scope=scope)
    assert record_envelope_decision(decision, GATED_COMMAND, scope=scope) is True

    broken = tmp_path / "wall"
    broken.write_text("x", encoding="utf-8")
    broken_scope = _scope(runtime_root=broken)
    assert (
        record_envelope_decision(
            envelope_decision(GATED_COMMAND, scope=broken_scope),
            GATED_COMMAND,
            scope=broken_scope,
        )
        is False
    )


# ── the account leaks no operator-sensitive location ────────────────────────


def test_the_account_names_the_log_but_never_the_runtime_root(monkeypatch, tmp_path):
    """``receipt_log`` is the BARE FILENAME. Which lane the row landed in is
    what the agent needs; where the operator's store lives is not, and the same
    constant is already agent-visible in the HUD capability posture line."""

    payload = _run(GATED_COMMAND, scope=_scope(runtime_root=tmp_path), monkeypatch=monkeypatch)

    rendered = json.dumps(payload[ENVELOPE_RESULT_KEY])
    assert ENVELOPE_DECISION_LOG in rendered
    assert str(tmp_path) not in rendered
    assert "\\" not in rendered and "/" not in rendered


# ── the merge never damages the answer ──────────────────────────────────────


@pytest.mark.parametrize(
    "result",
    [
        "not json at all",
        "[1, 2, 3]",  # valid JSON, wrong shape
        "",
    ],
)
def test_a_result_the_merge_cannot_parse_is_returned_untouched(result):
    """Observability must never cost the agent its command output. Losing a
    provenance block is a degraded turn; losing the output is a broken one."""

    from tools.terminal_tool import _with_envelope_provenance

    assert _with_envelope_provenance(result, {ENVELOPE_RESULT_KEY: {"x": 1}}) == result


def test_no_provenance_returns_the_result_object_unchanged():
    from tools.terminal_tool import _with_envelope_provenance

    result = '{"output":"ok"}'
    assert _with_envelope_provenance(result, None) is result


# ── the shape function's own contract ───────────────────────────────────────


def test_envelope_provenance_answers_none_for_everything_that_is_not_a_grant():
    scope = _scope()
    assert envelope_provenance(None, receipted=False) is None
    allowed = envelope_decision(UNGATED_COMMAND, scope=scope)
    assert allowed is not None and not allowed.granted
    assert envelope_provenance(allowed, receipted=True) is None
