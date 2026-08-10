"""Regression tests for the execute_code approval-bypass cluster.

Covers the canonical fix for issues #4146, #27303, #30882, #33057:

  1. tools.thread_context.propagate_context_to_thread — propagates the agent
     turn's ContextVars AND thread-local approval/sudo callbacks into worker
     threads, and clears the callbacks on teardown.
  2. Both execute_code RPC threads are wrapped with that helper (source guard).
  3. tools.approval.check_execute_code_guard — the entry-point guard decision
     matrix (isolated backends, yolo/off, cron-deny, headless-local,
     gateway approve/deny/timeout/missing-notify, smart mode).
  4. tools.code_execution_tool._scrub_child_env — broad HERMES_ prefix dropped,
     operational allowlist kept, DSN/WEBHOOK blocked, passthrough precedence.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import threading

import pytest

from tools import approval as A
from tools.thread_context import propagate_context_to_thread


# ---------------------------------------------------------------------------
# 1. Context + callback propagation helper
# ---------------------------------------------------------------------------

def test_helper_propagates_contextvar_and_approval_callback():
    from tools import terminal_tool as TT

    probe: contextvars.ContextVar[str] = contextvars.ContextVar(
        "cluster_probe", default="unset"
    )
    probe.set("parent-value")
    sentinel = object()
    TT.set_approval_callback(sentinel)
    try:
        seen: dict = {}

        def worker():
            seen["probe"] = probe.get()
            seen["cb"] = TT._get_approval_callback()

        t = threading.Thread(target=propagate_context_to_thread(worker))
        t.start()
        t.join(timeout=5)

        assert seen["probe"] == "parent-value"  # ContextVar propagated
        assert seen["cb"] is sentinel            # thread-local callback propagated
    finally:
        TT.set_approval_callback(None)


def test_helper_clears_callbacks_on_teardown():
    """A recycled worker thread must not retain the propagated callback after
    the wrapped target finishes (mirrors the GHSA-qg5c-hvr5-hjgr teardown)."""
    from tools import terminal_tool as TT

    sentinel = object()
    TT.set_approval_callback(sentinel)
    try:
        seen: dict = {}

        def first():
            seen["during"] = TT._get_approval_callback()

        def second():  # NOT wrapped — runs on the same recycled worker thread
            seen["after"] = TT._get_approval_callback()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(propagate_context_to_thread(first)).result(timeout=5)
            ex.submit(second).result(timeout=5)

        assert seen["during"] is sentinel  # installed for the wrapped target
        assert seen["after"] is None       # cleared on teardown
    finally:
        TT.set_approval_callback(None)


def test_both_rpc_threads_use_propagation_helper():
    """The propagation wiring, asserted structurally rather than by substring.

    RE-AIMED 2026-08-09. It used to be::

        src = inspect.getsource(cet)
        assert "propagate_context_to_thread(_rpc_server_loop)" in src

    Two problems. The lesser one: a reformat of that call across lines breaks it
    while the wiring is intact. The serious one: it is a *positive* claim proven
    by substring, so it can only ever tell you the text is present — and the
    change it exists to catch (a restructure that loses the callback) can leave
    the text exactly where it is.

    Kept rather than deleted, because the guarantee is real and is NOT covered
    by the behavioural test above: that one drives the guard on the CALLER
    thread, while this covers the two RPC worker threads, whose bug (#33057) is
    that a gateway approval raised from inside them finds no callback and
    silently returns. Driving those for real means standing up a UDS server and
    a file-poll loop, so the wiring is pinned structurally instead — on the AST,
    where a reflow cannot break it and a comment cannot satisfy it.
    """
    import ast
    import inspect
    import tools.code_execution_tool as cet

    tree = ast.parse(inspect.getsource(cet))
    wrapped: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != "propagate_context_to_thread":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name):
                wrapped.add(arg.id)

    for target in ("_rpc_server_loop", "_rpc_poll_loop"):
        assert target in wrapped, (
            f"{target} is not wrapped with propagate_context_to_thread "
            f"(wrapped targets found: {sorted(wrapped) or 'none'}) — a gateway "
            "approval raised from that thread finds no callback and the request "
            "silently returns unapproved (#33057)."
        )


# ---------------------------------------------------------------------------
# 3. check_execute_code_guard decision matrix
# ---------------------------------------------------------------------------

@pytest.fixture
def gw_session(monkeypatch):
    """A clean gateway session: HERMES_GATEWAY_SESSION set, a bound session
    key, and isolated gateway queues/callbacks. Yields the session_key."""
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    # Force manual mode regardless of host config and disable any process-level
    # yolo inherited from the developer's live environment.
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)

    session_key = "cluster-test-session"
    token = A.set_current_session_key(session_key)
    with A._lock:
        A._gateway_queues.pop(session_key, None)
        A._gateway_notify_cbs.pop(session_key, None)
    try:
        yield session_key
    finally:
        A.reset_current_session_key(token)
        with A._lock:
            A._gateway_queues.pop(session_key, None)
            A._gateway_notify_cbs.pop(session_key, None)


def _register_resolver(session_key: str, result):
    """Register a gateway notify callback that immediately resolves the most
    recent queued approval entry with *result* (simulating a user response)."""
    def cb(_approval_data):
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entry = entries[-1]
                entry.result = result
                entry.event.set()
    with A._lock:
        A._gateway_notify_cbs[session_key] = cb


def test_execute_code_refuses_when_approval_is_denied(gw_session, tmp_path):
    """THE guarantee, driven end to end: unapproved code does not run.

    ADDED 2026-08-09, replacing the behavioural half of what
    ``test_both_rpc_threads_use_propagation_helper`` was reaching for. That gate
    proved the approval PLUMBING was spelled a certain way in the source and
    proved nothing about whether unapproved code executes — the exact change it
    must catch (a restructure that skips the guard, or ignores its verdict)
    leaves the watched strings sitting untouched, and the gate stays green
    through an approval bypass. That is the class every issue in this file's
    header belongs to (#4146, #27303, #30882, #33057).

    So the refusal is driven against the real entry point: a gateway session
    that DENIES must make ``execute_code`` return a typed error and run nothing.

    The probe writes a file. That matters — a refusal asserted only on the
    returned JSON would pass against a guard that reports denial and executes
    anyway, which is precisely a bypass wearing a refusal's clothes.

    RED-PROOF: deleting the ``if not _guard.get("approved")`` early return in
    ``tools/code_execution_tool.py`` fails this test on the side-effect
    assertion, not merely on the status field.
    """

    import json as _json

    from tools import code_execution_tool as cet

    # "deny" is the resolver vocabulary the rest of this file uses ("once" /
    # "deny"); a dict here is silently NOT a denial, which is worth knowing —
    # the first draft of this test passed a dict, the guard did not read it as a
    # refusal, and the code RAN. That is the same shape as the bug being
    # guarded, arriving via the test rig.
    _register_resolver(gw_session, "deny")

    marker = tmp_path / "cluster_bypass_probe_should_never_run"
    raw = cet.execute_code(f"open({str(marker)!r}, 'w').write('executed')")
    result = _json.loads(raw)

    assert result["status"] == "error", (
        "execute_code did not refuse a DENIED approval — this is the bypass "
        f"class this file exists to guard. Returned: {result}"
    )
    assert result.get("tool_calls_made", 0) == 0, "denied code must not run"
    assert not marker.exists(), (
        "the denied code executed anyway — the guard's verdict was reported but "
        "not enforced"
    )


def _register_capturing_resolver(session_key: str, result):
    """Resolve immediately and retain the exact approval payload shown."""
    seen = {}

    def cb(approval_data):
        seen["approval_data"] = approval_data
        with A._lock:
            entries = A._gateway_queues.get(session_key, [])
            if entries:
                entries[-1].result = result
                entries[-1].event.set()

    with A._lock:
        A._gateway_notify_cbs[session_key] = cb
    return seen


def test_guard_isolated_backend_approved():
    # Container backends already sandbox the child — no-op approve.
    assert A.check_execute_code_guard("import os", "docker")["approved"] is True


def test_guard_headless_local_approved(monkeypatch):
    # Documented #30882 limitation: no approval surface → preserve auto-run.
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    assert A.check_execute_code_guard("import os", "local")["approved"] is True


def test_guard_cron_deny_blocks(monkeypatch):
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_get_cron_approval_mode", lambda: "deny")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["outcome"] == "blocked"


def test_guard_gateway_user_approves_is_one_shot(gw_session):
    _register_resolver(gw_session, "once")
    res = A.check_execute_code_guard("import os; print(1)", "local")
    assert res["approved"] is True
    assert res.get("user_approved") is True
    # One-shot: approval must NOT persist to future scripts.
    assert A.is_approved(gw_session, "execute_code") is False


def test_guard_session_approval_short_circuits_prompt(gw_session):
    """Once session-approved, execute_code skips the approval prompt (#39275)."""
    # Manually set session approval.
    A.approve_session(gw_session, "execute_code")
    try:
        # Even with a denier registered, the is_approved check short-circuits.
        _register_resolver(gw_session, "deny")
        res = A.check_execute_code_guard("import os", "local")
        assert res["approved"] is True
    finally:
        with A._lock:
            s = A._session_approved.get(gw_session, set())
            s.discard("execute_code")


def test_guard_gateway_missing_notify_is_pending(gw_session):
    # No notify callback registered → backward-compat pending approval.
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False
    assert res["status"] == "pending_approval"


def test_guard_smart_mode(gw_session, monkeypatch):
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")

    monkeypatch.setattr(A, "_smart_approve", lambda c, d: "approve")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True and res.get("smart_approved") is True

    # Smart DENY on an interactive surface now asks the owner. With no bound
    # notifier it remains pending rather than being hard-denied.
    monkeypatch.setattr(A, "_smart_approve", lambda c, d: "deny")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is False and res["status"] == "pending_approval"

    # escalate → falls through to manual gateway approval
    monkeypatch.setattr(A, "_smart_approve", lambda c, d: "escalate")
    _register_resolver(gw_session, "once")
    res = A.check_execute_code_guard("import os", "local")
    assert res["approved"] is True


def test_terminal_smart_deny_owner_override_is_one_operation(gw_session, monkeypatch):
    """A human may override DENY, but a broad UI choice must not be persisted."""
    with A._lock:
        A._permanent_approved.discard("owner-override-test-danger")
        A._session_approved.get(gw_session, set()).discard("owner-override-test-danger")
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description: "deny")
    monkeypatch.setattr(
        A,
        "detect_dangerous_command",
        lambda command: (True, "owner-override-test-danger", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    shown = _register_capturing_resolver(gw_session, "always")
    result = A.check_all_command_guards("dangerous /tmp/first", "local")

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert shown["approval_data"]["smart_denied"] is True
    assert shown["approval_data"]["allow_permanent"] is False
    assert A.is_approved(gw_session, "owner-override-test-danger") is False

    _register_resolver(gw_session, "deny")
    changed = A.check_all_command_guards("dangerous /tmp/second", "local")
    assert changed["approved"] is False
    assert changed["outcome"] == "denied"


def test_execute_code_smart_deny_owner_override_is_one_operation(gw_session, monkeypatch):
    """Never persist the coarse execute_code key after overriding smart DENY."""
    with A._lock:
        A._permanent_approved.discard("execute_code")
        A._session_approved.get(gw_session, set()).discard("execute_code")
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description: "deny")

    shown = _register_capturing_resolver(gw_session, "session")
    result = A.check_execute_code_guard("print('first')", "local")

    assert result["approved"] is True
    assert result["user_approved"] is True
    assert shown["approval_data"]["smart_denied"] is True
    assert shown["approval_data"]["allow_permanent"] is False
    assert A.is_approved(gw_session, "execute_code") is False

    _register_resolver(gw_session, "deny")
    changed = A.check_execute_code_guard("print('second')", "local")
    assert changed["approved"] is False
    assert changed["outcome"] == "denied"


def test_smart_escalate_still_persists_session_choice(gw_session, monkeypatch):
    """The DENY restriction must not alter Smart ESCALATE's manual choices."""
    key = "smart-escalate-persistence"
    with A._lock:
        A._session_approved.get(gw_session, set()).discard(key)
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description: "escalate")
    monkeypatch.setattr(
        A, "detect_dangerous_command",
        lambda command: (True, key, f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    shown = _register_capturing_resolver(gw_session, "session")
    result = A.check_all_command_guards("dangerous escalate", "local")

    assert result["approved"] is True
    assert shown["approval_data"]["allow_permanent"] is True
    assert "smart_denied" not in shown["approval_data"]
    assert A.is_approved(gw_session, key) is True


def test_terminal_smart_deny_pending_payload_is_one_operation(gw_session, monkeypatch):
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description: "deny")
    monkeypatch.setattr(
        A, "detect_dangerous_command",
        lambda command: (True, "pending-smart-deny", f"risk:{command}"),
    )
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
        raising=False,
    )

    result = A.check_all_command_guards("dangerous pending", "local")

    assert result["status"] == "pending_approval"
    assert result["smart_denied"] is True
    assert result["allow_permanent"] is False
    with A._lock:
        pending = dict(A._pending[gw_session])
    assert pending["smart_denied"] is True
    assert pending["allow_permanent"] is False


def test_execute_code_smart_deny_pending_payload_is_one_operation(gw_session, monkeypatch):
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(A, "_smart_approve", lambda _command, _description: "deny")

    result = A.check_execute_code_guard("print('pending')", "local")

    assert result["status"] == "pending_approval"
    assert result["smart_denied"] is True
    assert result["allow_permanent"] is False
    with A._lock:
        pending = dict(A._pending[gw_session])
    assert pending["smart_denied"] is True
    assert pending["allow_permanent"] is False


def test_terminal_serializes_smart_deny_pending_capabilities(monkeypatch):
    from tools import terminal_tool as terminal_module

    monkeypatch.setattr(
        terminal_module,
        "_check_all_guards",
        lambda *_args, **_kwargs: {
            "approved": False,
            "status": "pending_approval",
            "command": "rm -rf /tmp/example",
            "description": "recursive delete",
            "pattern_key": "rm-rf",
            "smart_denied": True,
            "allow_permanent": False,
        },
    )

    payload = json.loads(terminal_module.terminal_tool(command="rm -rf /tmp/example"))

    assert payload["smart_denied"] is True
    assert payload["allow_permanent"] is False


def test_guard_session_yolo_bypasses(gw_session):
    A.enable_session_yolo(gw_session)
    try:
        # Even with a denier registered, yolo short-circuits before the prompt.
        _register_resolver(gw_session, "deny")
        assert A.check_execute_code_guard("import os", "local")["approved"] is True
    finally:
        A.disable_session_yolo(gw_session)


# ---------------------------------------------------------------------------
# 4. Env scrubbing (#27303)
# ---------------------------------------------------------------------------

def test_env_scrub_hermes_allowlist_and_secret_blocks():
    from tools.code_execution_tool import _scrub_child_env

    env = {
        # operational allowlist → kept
        "HERMES_HOME": "/h", "HERMES_PROFILE": "p",
        "HERMES_CONFIG": "/c.yaml", "HERMES_ENV": "/e",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        # other HERMES_* → dropped (broad prefix removed)
        "HERMES_BASE_URL": "https://x", "HERMES_INTERACTIVE": "1",
        "HERMES_KANBAN_DB": "postgres://u:p@h/db",
        # secret substrings (incl. new DSN/WEBHOOK) → dropped
        "SENTRY_DSN": "https://a@s.io/1", "SLACK_WEBHOOK": "https://h/x",
        "OPENAI_API_KEY": "sk", "GITHUB_TOKEN": "ghp",
        # safe prefix → kept; uncategorized → dropped
        "PATH": "/usr/bin", "RANDOM_X": "y",
    }
    out = _scrub_child_env(env, is_passthrough=lambda _: False, is_windows=False)

    for kept in (
        "HERMES_HOME", "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV",
        "HERMES_DELEGATED_CHILD_CONTEXT", "PATH",
    ):
        assert kept in out, f"{kept} should be kept"
    for dropped in (
        "HERMES_BASE_URL", "HERMES_INTERACTIVE", "HERMES_KANBAN_DB",
        "SENTRY_DSN", "SLACK_WEBHOOK", "OPENAI_API_KEY", "GITHUB_TOKEN",
        "RANDOM_X",
    ):
        assert dropped not in out, f"{dropped} should be dropped"


def test_env_scrub_passthrough_overrides_secret_block():
    """A skill/config-declared passthrough var is an explicit user opt-in and
    passes even if it matches a secret substring (precedence is intentional)."""
    from tools.code_execution_tool import _scrub_child_env

    env = {"MY_SERVICE_DSN": "value"}
    out = _scrub_child_env(env, is_passthrough=lambda k: k == "MY_SERVICE_DSN",
                           is_windows=False)
    assert out.get("MY_SERVICE_DSN") == "value"


# ---------------------------------------------------------------------------
# 5. File-tool sensitive-path refusal (security B1)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. Env-scrub diagnosability mitigation (#27303 follow-up)
# ---------------------------------------------------------------------------


def test_env_scrub_no_log_when_nothing_dropped(caplog):
    """No diagnostic noise when there are no dropped HERMES_* vars."""
    import logging

    from tools.code_execution_tool import _scrub_child_env

    with caplog.at_level(logging.DEBUG, logger="tools.code_execution_tool"):
        _scrub_child_env(
            {"HERMES_HOME": "/h", "PATH": "/usr/bin"},
            is_passthrough=lambda _: False,
            is_windows=False,
        )
    assert "dropped" not in "\n".join(r.getMessage() for r in caplog.records)
