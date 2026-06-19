"""
Tests for one-shot stdio MCP env overrides.

Covers:
- ``_normalize_mcp_env_server_name``
- ``_get_process_mcp_env_overrides``
- ``_build_safe_env`` with the new ``server_name`` / ``runtime_env`` kwargs

These are pure-function tests and do not spawn any MCP subprocess.
"""

import logging

import pytest
from unittest.mock import patch

from tools.mcp_tool import (
    _build_safe_env,
    _get_process_mcp_env_overrides,
    _normalize_mcp_env_server_name,
)


# ---------------------------------------------------------------------------
# _normalize_mcp_env_server_name
# ---------------------------------------------------------------------------

class TestNormalizeMcpEnvServerName:
    def test_simple_name(self):
        assert _normalize_mcp_env_server_name("foo") == "FOO"

    def test_hyphenated(self):
        assert _normalize_mcp_env_server_name("launcher-qa") == "LAUNCHER_QA"

    def test_multiple_separators(self):
        assert _normalize_mcp_env_server_name("foo.bar-baz") == "FOO_BAR_BAZ"

    def test_already_uppercase(self):
        assert _normalize_mcp_env_server_name("FOO_BAR") == "FOO_BAR"

    def test_collapses_runs(self):
        assert _normalize_mcp_env_server_name("foo--bar") == "FOO_BAR"

    def test_trailing_separator(self):
        assert _normalize_mcp_env_server_name("foo-") == "FOO"

    def test_leading_separator(self):
        assert _normalize_mcp_env_server_name("-foo") == "FOO"

    def test_empty_string(self):
        assert _normalize_mcp_env_server_name("") == ""

    def test_none_input(self):
        assert _normalize_mcp_env_server_name(None) == ""

    def test_only_separators(self):
        assert _normalize_mcp_env_server_name("---") == ""

    def test_hyphen_vs_underscore_collision(self):
        # Documented ambiguity: foo-bar and foo_bar share FOO_BAR.
        assert _normalize_mcp_env_server_name("foo-bar") == "FOO_BAR"
        assert _normalize_mcp_env_server_name("foo_bar") == "FOO_BAR"


# ---------------------------------------------------------------------------
# _get_process_mcp_env_overrides
# ---------------------------------------------------------------------------

class TestGetProcessMcpEnvOverrides:
    def test_matches_only_namespaced(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_BAR_RUNTIME_FILE": "/tmp/r",
            "HERMES_MCP_ENV_FOO_BAR_TOKEN": "abc",
            "HERMES_MCP_ENV_OTHER_X": "no",  # different server
            "SOME_OTHER_VAR": "ignored",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _get_process_mcp_env_overrides("foo-bar")
        assert result == {"RUNTIME_FILE": "/tmp/r", "TOKEN": "abc"}

    def test_skips_invalid_child_name(self, caplog):
        leak_canary = "do-not-leak-this-value"
        env = {
            "HERMES_MCP_ENV_FOO_1BAD": leak_canary,  # cannot start with digit
            "HERMES_MCP_ENV_FOO_OK": "kept",
        }
        with patch.dict("os.environ", env, clear=True), \
             caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
            result = _get_process_mcp_env_overrides("foo")
        assert result == {"OK": "kept"}
        # The warning must name the full process-env key but not the value.
        assert any("1BAD" in rec.getMessage() for rec in caplog.records)
        for rec in caplog.records:
            assert leak_canary not in rec.getMessage()

    def test_ignores_other_servers(self):
        env = {
            "HERMES_MCP_ENV_OTHER_X": "no",
            "HERMES_MCP_ENV_FOO_X": "yes",
        }
        with patch.dict("os.environ", env, clear=True):
            assert _get_process_mcp_env_overrides("foo") == {"X": "yes"}

    def test_no_value_in_warning(self, caplog):
        env = {
            "HERMES_MCP_ENV_FOO_-BAD": "super-secret-value",
        }
        with patch.dict("os.environ", env, clear=True), \
             caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
            _get_process_mcp_env_overrides("foo")
        for rec in caplog.records:
            rendered = rec.getMessage()
            assert "super-secret-value" not in rendered

    def test_empty_server_name_returns_empty(self):
        with patch.dict("os.environ",
                        {"HERMES_MCP_ENV__X": "v"}, clear=True):
            assert _get_process_mcp_env_overrides("") == {}
            assert _get_process_mcp_env_overrides(None) == {}

    def test_no_matches_returns_empty(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            assert _get_process_mcp_env_overrides("foo") == {}


# ---------------------------------------------------------------------------
# _build_safe_env merge order
# ---------------------------------------------------------------------------

class TestBuildSafeEnvMergeOrder:
    """Layer precedence: baseline < durable < process overlay < runtime."""

    def test_baseline_only(self):
        with patch.dict("os.environ",
                        {"PATH": "/usr/bin", "SECRET": "no"}, clear=True):
            result = _build_safe_env(None)
        assert result == {"PATH": "/usr/bin"}

    def test_durable_overrides_baseline(self):
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            result = _build_safe_env({"PATH": "/custom/bin"})
        assert result["PATH"] == "/custom/bin"

    def test_process_overlay_applied_when_server_name(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_X": "from-process",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(None, server_name="foo")
        assert result["X"] == "from-process"

    def test_process_overlay_skipped_without_server_name(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_X": "should-not-leak",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(None)
        assert "X" not in result
        # And value must not have leaked under any other key either.
        assert "should-not-leak" not in result.values()

    def test_process_overlay_overrides_durable(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_TOKEN": "from-process",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env({"TOKEN": "from-durable"},
                                     server_name="foo")
        assert result["TOKEN"] == "from-process"

    def test_runtime_overrides_process(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_TOKEN": "from-process",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(
                {"TOKEN": "from-durable"},
                server_name="foo",
                runtime_env={"TOKEN": "from-runtime"},
            )
        assert result["TOKEN"] == "from-runtime"

    def test_full_merge_order(self):
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_LAYER": "process",
            "HERMES_MCP_ENV_FOO_PROCESS_ONLY": "p",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(
                {"LAYER": "durable", "DURABLE_ONLY": "d"},
                server_name="foo",
                runtime_env={"LAYER": "runtime", "RUNTIME_ONLY": "r"},
            )
        assert result["PATH"] == "/usr/bin"           # baseline
        assert result["DURABLE_ONLY"] == "d"          # durable
        assert result["PROCESS_ONLY"] == "p"          # process overlay
        assert result["RUNTIME_ONLY"] == "r"          # runtime overlay
        assert result["LAYER"] == "runtime"           # highest precedence

    def test_empty_runtime_env_is_noop(self):
        env = {"PATH": "/usr/bin"}
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(None, server_name="foo", runtime_env={})
        assert result == {"PATH": "/usr/bin"}

    def test_no_xdg_leak_from_namespaced_prefix(self):
        # Sanity: a stray HERMES_MCP_ENV_... entry must not pollute the
        # baseline filter — it should only surface when server_name matches.
        env = {
            "PATH": "/usr/bin",
            "HERMES_MCP_ENV_FOO_X": "v",
            "HERMES_MCP_ENV_BAR_Y": "v",
        }
        with patch.dict("os.environ", env, clear=True):
            result = _build_safe_env(None, server_name="bar")
        assert result.get("Y") == "v"
        assert "X" not in result
        assert "HERMES_MCP_ENV_FOO_X" not in result
