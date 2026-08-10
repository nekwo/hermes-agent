"""One test per reader of the tirith flags, proving the env override reaches it.

``hermes_cli/tips.py`` tells operators::

    TIRITH_FAIL_OPEN env var overrides the tirith_fail_open config — a quick
    toggle without editing config.yaml.

Five decision points read ``security.tirith_enabled`` /
``security.tirith_fail_open``. Before ``hermes_cli.tirith_config`` existed,
only ``tools.tirith_security._load_security_config`` honoured the override;
the other four read ``hermes_cli.config`` directly and were blind to it. The
tests below cover one site each, and each pairs the override assertion with a
near-miss run (same config, override absent) so a passing verdict has to come
from the override actually binding rather than from the config already saying
the same thing.
"""

import builtins
import types
from unittest.mock import patch

import pytest


@pytest.fixture
def no_tirith_env(monkeypatch):
    """Clear both overrides — ``tests/conftest.py`` sets ``TIRITH_ENABLED=false``
    for the whole suite to keep tirith's auto-install off the network, and
    these tests are about that exact variable."""
    monkeypatch.delenv("TIRITH_ENABLED", raising=False)
    monkeypatch.delenv("TIRITH_FAIL_OPEN", raising=False)


def _import_blocker(names):
    """Return a ``builtins.__import__`` replacement that fails for *names*."""
    real_import = builtins.__import__

    def _fake(name, *args, **kwargs):
        if name in names:
            raise ImportError(f"simulated import failure: {name}")
        return real_import(name, *args, **kwargs)

    return _fake


# ---------------------------------------------------------------------------
# Site 1 of 5 — tools/tirith_security.py::_load_security_config
# ---------------------------------------------------------------------------

class TestTirithSecurityLoader:
    def test_fail_open_override_reaches_the_loader(self, monkeypatch, no_tirith_env):
        from tools.tirith_security import _load_security_config

        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": True}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert _load_security_config()["tirith_fail_open"] is True  # near miss

            monkeypatch.setenv("TIRITH_FAIL_OPEN", "false")
            assert _load_security_config()["tirith_fail_open"] is False

    def test_enabled_override_reaches_the_loader(self, monkeypatch, no_tirith_env):
        from tools.tirith_security import _load_security_config

        cfg = {"security": {"tirith_enabled": True}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert _load_security_config()["tirith_enabled"] is True  # near miss

            monkeypatch.setenv("TIRITH_ENABLED", "false")
            assert _load_security_config()["tirith_enabled"] is False

    def test_path_and_timeout_still_resolve(self, no_tirith_env):
        """The two single-reader keys stayed local to this loader; pin that the
        migration of the booleans did not drop them."""
        from tools.tirith_security import _load_security_config

        cfg = {"security": {"tirith_path": "/opt/tirith", "tirith_timeout": 11}}
        with patch("hermes_cli.config.load_config", return_value=cfg):
            resolved = _load_security_config()
        assert resolved["tirith_path"] == "/opt/tirith"
        assert resolved["tirith_timeout"] == 11


# ---------------------------------------------------------------------------
# Site 2 of 5 — tools/approval.py, main flow ImportError lane
# ---------------------------------------------------------------------------

class TestApprovalMainFlow:
    def _run(self, approval_callback=None):
        from tools.approval import check_all_command_guards

        with patch("builtins.__import__", side_effect=_import_blocker({"tools.tirith_security"})):
            with patch("tools.approval.detect_dangerous_command", return_value=(False, None, None)):
                return check_all_command_guards(
                    "echo hello", "local", approval_callback=approval_callback
                )

    def test_fail_open_override_reaches_the_main_flow(self, monkeypatch, no_tirith_env):
        cfg = {
            "approvals": {"mode": "manual"},
            # config says fail-OPEN; only the env var can make this fail closed
            "security": {"tirith_enabled": True, "tirith_fail_open": True},
        }
        monkeypatch.setenv("HERMES_INTERACTIVE", "1")

        with patch("hermes_cli.config.load_config", return_value=cfg):
            # Near miss: without the override the command sails through with
            # nobody consulted.
            calls = []
            result = self._run(lambda command, description, **kw: calls.append(1) or "deny")
            assert result.get("approved") is True
            assert calls == []

            monkeypatch.setenv("TIRITH_FAIL_OPEN", "false")
            calls = []
            result = self._run(lambda command, description, **kw: calls.append(1) or "deny")

        assert result.get("approved") is False, (
            "TIRITH_FAIL_OPEN=false did not reach the main-flow ImportError lane"
        )
        assert calls, "approval callback was never invoked — command slipped through"


# ---------------------------------------------------------------------------
# Site 3 of 5 — tools/approval.py, cron-deny lane
# ---------------------------------------------------------------------------

class TestApprovalCronLane:
    @pytest.fixture(autouse=True)
    def _cron_session(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        for var in (
            "HERMES_INTERACTIVE",
            "HERMES_GATEWAY_SESSION",
            "HERMES_EXEC_ASK",
            "HERMES_YOLO_MODE",
        ):
            monkeypatch.delenv(var, raising=False)

    def _run(self):
        from tools.approval import check_all_command_guards

        with patch("tools.approval._get_cron_approval_mode", return_value="deny"):
            with patch("tools.approval.detect_dangerous_command", return_value=(False, None, None)):
                with patch(
                    "builtins.__import__",
                    side_effect=_import_blocker({"tools.tirith_security"}),
                ):
                    return check_all_command_guards("echo hi", "local")

    def test_fail_open_override_reaches_the_cron_lane(self, monkeypatch, no_tirith_env):
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": True}}

        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert self._run()["approved"] is True  # near miss

            monkeypatch.setenv("TIRITH_FAIL_OPEN", "false")
            result = self._run()

        assert result["approved"] is False, (
            "TIRITH_FAIL_OPEN=false did not reach the cron-deny ImportError lane"
        )
        assert "tirith_fail_open" in result["message"]


# ---------------------------------------------------------------------------
# Site 4 of 5 — gateway/run.py, "no automated risk assessor" startup heads-up
# ---------------------------------------------------------------------------

class TestGatewayRiskAssessorWarning:
    def test_enabled_override_reaches_the_gateway_heads_up(self, monkeypatch, no_tirith_env):
        from gateway.run import _needs_risk_assessor_warning

        cfg = {
            "approvals": {"mode": "manual"},
            "security": {"tirith_enabled": True},
            "auxiliary": {},
        }
        # Near miss: scanning is on, so there IS an automated assessor and the
        # heads-up must stay quiet.
        assert _needs_risk_assessor_warning(cfg) is False

        monkeypatch.setenv("TIRITH_ENABLED", "false")
        assert _needs_risk_assessor_warning(cfg) is True

    def test_the_other_two_conditions_are_unchanged(self, monkeypatch, no_tirith_env):
        """The extraction to a pure predicate must not have re-decided anything
        beyond where tirith_enabled is read from."""
        from gateway.run import _needs_risk_assessor_warning

        monkeypatch.setenv("TIRITH_ENABLED", "false")
        assert _needs_risk_assessor_warning({"approvals": {"mode": "manual"}}) is True
        # A non-manual mode never warns.
        assert _needs_risk_assessor_warning({"approvals": {"mode": "auto"}}) is False
        # An auxiliary.approval model counts as an automated assessor.
        assert _needs_risk_assessor_warning(
            {"approvals": {"mode": "manual"}, "auxiliary": {"approval": "some-model"}}
        ) is False


# ---------------------------------------------------------------------------
# Site 5 of 5 — cli.py, "enabled but not available" startup notice
# ---------------------------------------------------------------------------

class TestCliStartupNotice:
    def _emit(self, config):
        """Drive HermesCLI._ensure_tirith_security against a minimal stub and
        return the lines it printed."""
        import cli as cli_mod

        printed = []
        stub = types.SimpleNamespace(config=config)
        with patch("tools.tirith_security.ensure_installed", return_value=None):
            with patch("tools.tirith_security.is_platform_supported", return_value=True):
                with patch.object(cli_mod, "_cprint", lambda text: printed.append(text)):
                    cli_mod.HermesCLI._ensure_tirith_security(stub)
        return printed

    def test_enabled_override_reaches_the_cli_notice(self, monkeypatch, no_tirith_env):
        cfg = {"security": {"tirith_enabled": True}}

        # Near miss: with scanning on and the binary missing, the notice fires.
        # (_ensure_tirith_security swallows every exception, so without this
        # the "silent" assertion below would pass on a broken stub.)
        printed = self._emit(cfg)
        assert printed and "tirith security scanner enabled" in printed[0]

        monkeypatch.setenv("TIRITH_ENABLED", "false")
        assert self._emit(cfg) == [], (
            "TIRITH_ENABLED=false did not reach the CLI startup notice — it "
            "still claims scanning is enabled"
        )
