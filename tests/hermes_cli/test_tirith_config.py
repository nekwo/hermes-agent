"""Unit pins for the single tirith-flag accessor (``hermes_cli.tirith_config``).

The accessor exists because ``hermes_cli/tips.py`` promises operators that
``TIRITH_FAIL_OPEN`` overrides ``security.tirith_fail_open``, and four of the
five readers of that flag could not see the override. These tests pin the
three things the accessor now states once: the default, the ``tirith_enabled``
interaction, and the env override — plus the import-weight property that lets
it be read from inside an ``except ImportError`` branch.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import tirith_config
from hermes_cli.config_defaults import DEFAULT_CONFIG


@pytest.fixture
def no_tirith_env(monkeypatch):
    """Remove both overrides.

    ``tests/conftest.py`` sets ``TIRITH_ENABLED=false`` for every test (to stop
    tirith's auto-install reaching the network). That is ambient for most of
    the suite, but this module is about exactly that variable, so any
    "unset env" assertion has to clear it explicitly.
    """
    monkeypatch.delenv("TIRITH_ENABLED", raising=False)
    monkeypatch.delenv("TIRITH_FAIL_OPEN", raising=False)


class TestDefaults:
    def test_defaults_are_sourced_from_default_config_not_retyped(self):
        """The accessor must not become a fourth copy of the defaults."""
        assert (
            tirith_config.SECURITY_DEFAULTS["tirith_enabled"]
            is DEFAULT_CONFIG["security"]["tirith_enabled"]
        )
        assert (
            tirith_config.SECURITY_DEFAULTS["tirith_fail_open"]
            is DEFAULT_CONFIG["security"]["tirith_fail_open"]
        )

    def test_empty_config_and_no_env_yields_the_shipped_defaults(self, no_tirith_env):
        """Nothing set anywhere → scanning on, fail-open. Unchanged behaviour."""
        assert tirith_config.tirith_enabled({}) is True
        assert tirith_config.tirith_fail_open({}) is True
        assert tirith_config.fail_open_when_scanner_unavailable({}) is True

    def test_config_values_are_honoured_when_no_env_var_is_set(self, no_tirith_env):
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": False}}
        assert tirith_config.tirith_enabled(cfg) is True
        assert tirith_config.tirith_fail_open(cfg) is False
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False


class TestEnvOverride:
    def test_fail_open_env_overrides_a_true_config(self, monkeypatch, no_tirith_env):
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": True}}
        # Near miss first: without the env var the config value stands, so a
        # False below cannot come from the config being fail-closed already.
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is True

        monkeypatch.setenv("TIRITH_FAIL_OPEN", "false")
        assert tirith_config.tirith_fail_open(cfg) is False
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False

    def test_fail_open_env_overrides_a_false_config_in_the_other_direction(
        self, monkeypatch, no_tirith_env
    ):
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": False}}
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False

        monkeypatch.setenv("TIRITH_FAIL_OPEN", "true")
        assert tirith_config.tirith_fail_open(cfg) is True
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is True

    def test_enabled_env_overrides_config(self, monkeypatch, no_tirith_env):
        cfg = {"security": {"tirith_enabled": True}}
        assert tirith_config.tirith_enabled(cfg) is True

        monkeypatch.setenv("TIRITH_ENABLED", "false")
        assert tirith_config.tirith_enabled(cfg) is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", True),
            ("true", True),
            ("TRUE", True),
            ("Yes", True),
            ("0", False),
            ("false", False),
            ("no", False),
            # Deliberately NOT truthy — utils.TRUTHY_STRINGS accepts "on", but
            # tools.tirith_security._env_bool never did, and it was the only
            # place either override has ever bound. Widening the set here
            # would silently re-decide an operator's existing value.
            ("on", False),
            ("banana", False),
            ("", False),
        ],
    )
    def test_truthy_parsing_matches_the_historical_tirith_set(
        self, monkeypatch, no_tirith_env, raw, expected
    ):
        monkeypatch.setenv("TIRITH_FAIL_OPEN", raw)
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": not expected}}
        assert tirith_config.tirith_fail_open(cfg) is expected


class TestEnabledInteraction:
    def test_disabled_scanning_forces_fail_open_even_when_config_is_fail_closed(
        self, monkeypatch, no_tirith_env
    ):
        """Scanning off ⇒ no scanner to fail against ⇒ an unavailable scanner
        is not a reason to withhold a command.

        This mirrors ``check_command_security``'s own ``if not
        cfg["tirith_enabled"]: return allow`` arm.
        """
        cfg = {"security": {"tirith_enabled": False, "tirith_fail_open": False}}
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is True

        # Near miss: flip only tirith_enabled back on and the same config is
        # fail-closed again — so the True above came from the interaction
        # firing, not from fail_open being ignored.
        cfg["security"]["tirith_enabled"] = True
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False

    def test_the_interaction_also_honours_the_enabled_env_override(
        self, monkeypatch, no_tirith_env
    ):
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": False}}
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False

        monkeypatch.setenv("TIRITH_ENABLED", "false")
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is True

    def test_raw_fail_open_does_not_fold_in_enabled(self, no_tirith_env):
        """``tirith_fail_open`` stays the raw flag — tools.tirith_security
        checks the two independently and must keep seeing the raw value."""
        cfg = {"security": {"tirith_enabled": False, "tirith_fail_open": False}}
        assert tirith_config.tirith_fail_open(cfg) is False
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is True


class TestTotality:
    """The accessor replaced four inline ``try/except: pass`` copies whose
    documented posture was "safe default if config is unreadable". It must
    keep that posture rather than propagating."""

    # ``None`` is excluded on purpose: it is the documented "load it yourself"
    # sentinel, covered by test_unreadable_config_degrades_to_defaults.
    @pytest.mark.parametrize("bad", [[], 0, "not-a-config", object()])
    def test_security_section_never_raises_on_a_malformed_config(
        self, no_tirith_env, bad
    ):
        assert tirith_config.security_section(bad) == {}

    def test_security_section_survives_a_config_whose_get_raises(self, no_tirith_env):
        class Hostile(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        assert tirith_config.security_section(Hostile()) == {}
        assert tirith_config.fail_open_when_scanner_unavailable(Hostile()) is True

    def test_unreadable_config_degrades_to_defaults(self, monkeypatch, no_tirith_env):
        import hermes_cli.config as real_config

        def _explode():
            raise OSError("config.yaml unreadable")

        monkeypatch.setattr(real_config, "load_config", _explode)
        assert tirith_config.load_config_or_empty() == {}
        assert tirith_config.fail_open_when_scanner_unavailable() is True

    def test_a_passed_config_is_used_without_loading_from_disk(
        self, monkeypatch, no_tirith_env
    ):
        """Callers on a hot path (cli startup, gateway startup) pass their
        already-loaded config; the accessor must not load a second time."""
        import hermes_cli.config as real_config

        calls = []

        def _counted():
            calls.append(1)
            return {"security": {"tirith_fail_open": True}}

        monkeypatch.setattr(real_config, "load_config", _counted)
        cfg = {"security": {"tirith_enabled": True, "tirith_fail_open": False}}
        assert tirith_config.fail_open_when_scanner_unavailable(cfg) is False
        assert calls == [], "passed-in config must not trigger a config load"

        # Non-vacuity: omitting the argument DOES load, so the empty list
        # above is a real observation and not a broken counter.
        assert tirith_config.fail_open_when_scanner_unavailable() is True
        assert calls == [1]


class TestImportWeight:
    """The two ``tools/approval.py`` readers sit inside
    ``except ImportError: from tools.tirith_security import ...`` — they run
    precisely when that module failed to import. The accessor therefore has to
    be importable on its own and must not drag ``hermes_cli.config`` (roughly
    200 transitive modules, incl. urllib/http/agent.*) in at import time."""

    def test_importing_the_accessor_does_not_import_hermes_cli_config(self):
        probe = textwrap.dedent(
            """
            import sys
            before = len(sys.modules)
            import hermes_cli.tirith_config  # noqa: F401
            after_accessor = len(sys.modules)
            assert "hermes_cli.config" not in sys.modules, (
                "accessor dragged in hermes_cli.config at import time"
            )
            import hermes_cli.config  # noqa: F401
            after_config = len(sys.modules)
            # Non-vacuity: the assertion above only means something if
            # hermes_cli.config is genuinely heavy. Prove it here rather than
            # trusting the claim.
            accessor_cost = after_accessor - before
            config_cost = after_config - after_accessor
            assert config_cost > 100, config_cost
            assert accessor_cost < 20, accessor_cost
            print("OK", accessor_cost, config_cost)
            """
        )
        repo_root = Path(tirith_config.__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
