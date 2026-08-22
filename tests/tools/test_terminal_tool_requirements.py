"""Tests for terminal/file tool availability in local dev environments."""

import importlib

import pytest

from model_tools import get_tool_definitions

terminal_tool_module = importlib.import_module("tools.terminal_tool")


@pytest.fixture(autouse=True)
def _clear_caches():
    """Invalidate check_fn and tool-definitions caches before each test
    so that monkeypatched env vars / config take effect."""
    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    yield
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()


class TestTerminalRequirements:
    def test_local_backend_requirements(self, monkeypatch):
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "local"},
        )
        assert terminal_tool_module.check_terminal_requirements() is True


    def test_terminal_and_execute_code_tools_resolve_for_managed_modal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("tools.tool_backend_helpers.managed_nous_tools_enabled", lambda: True)
        monkeypatch.setattr(terminal_tool_module, "managed_nous_tools_enabled", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "modal", "modal_mode": "managed"},
        )
        monkeypatch.setattr(
            terminal_tool_module,
            "is_managed_tool_gateway_ready",
            lambda _vendor: True,
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "code_execution"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}

        assert "terminal" in names
        assert "execute_code" in names


class TestCheckFnTransientFailureSuppression:
    """The check_fn TTL cache should absorb transient probe failures.

    Regression coverage for #21658 / #5304: a single flaky
    ``check_terminal_requirements()`` (Docker daemon busy, probe timeout)
    must not silently strip the terminal/file toolset from a subagent. After
    a recent success, a transient False is treated as a flake; a failure with
    no recent success — or past the grace window — is honored.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        from tools.registry import invalidate_check_fn_cache

        invalidate_check_fn_cache()
        yield
        invalidate_check_fn_cache()

    def test_transient_failure_after_success_is_suppressed(self, monkeypatch):
        import tools.registry as reg

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            # First call succeeds, second flakes (False).
            return calls["n"] == 1

        # Pin the cache clock so the TTL doesn't serve a stale entry between
        # the two probes — we want both to actually run.
        t = {"now": 1000.0}
        monkeypatch.setattr(reg.time, "monotonic", lambda: t["now"])

        assert reg._check_fn_cached(flaky) is True  # records last-good
        t["now"] += reg._CHECK_FN_TTL_SECONDS + 1  # expire the TTL cache
        # Within grace window of the success → flake suppressed, stays True.
        assert reg._check_fn_cached(flaky) is True
        assert calls["n"] == 2  # the probe actually ran (not just cached)

    def test_persistent_failure_after_grace_is_honored(self, monkeypatch):
        import tools.registry as reg

        def good():
            return True

        def bad():
            return False

        t = {"now": 1000.0}
        monkeypatch.setattr(reg.time, "monotonic", lambda: t["now"])

        assert reg._check_fn_cached(good) is True
        # Advance past the failure grace window, then fail.
        t["now"] += reg._CHECK_FN_FAILURE_GRACE_SECONDS + 1
        # Different fn so last-good for `good` doesn't apply; bad has no success.
        assert reg._check_fn_cached(bad) is False


    def test_grace_expiry_lets_real_outage_through(self, monkeypatch):
        import tools.registry as reg

        state = {"ok": True}

        def probe():
            return state["ok"]

        t = {"now": 1000.0}
        monkeypatch.setattr(reg.time, "monotonic", lambda: t["now"])

        assert reg._check_fn_cached(probe) is True
        state["ok"] = False
        # Just past TTL, within grace → flake suppressed.
        t["now"] += reg._CHECK_FN_TTL_SECONDS + 1
        assert reg._check_fn_cached(probe) is True
        # Now move well past the grace window since the last success → honored.
        t["now"] += reg._CHECK_FN_FAILURE_GRACE_SECONDS + 1
        assert reg._check_fn_cached(probe) is False

    def test_subagent_keeps_file_tools_through_docker_flake(self, monkeypatch):
        """End-to-end: a docker probe that flakes on the 2nd build keeps the
        file/terminal toolset available for the subagent being constructed."""
        import tools.registry as reg

        flake = {"first": True}

        def flaky_terminal_check():
            if flake["first"]:
                flake["first"] = False
                return True
            return False  # transient flake on the subagent build

        monkeypatch.setattr(
            terminal_tool_module, "check_terminal_requirements", flaky_terminal_check
        )
        # file tools delegate to the same check via tools.check_file_requirements.
        import tools as tools_pkg

        monkeypatch.setattr(
            tools_pkg, "check_file_requirements", flaky_terminal_check
        )

        t = {"now": 5000.0}
        monkeypatch.setattr(reg.time, "monotonic", lambda: t["now"])

        from model_tools import get_tool_definitions, _clear_tool_defs_cache

        reg.invalidate_check_fn_cache()
        _clear_tool_defs_cache()
        # Parent build (probe ok) → records last-good.
        parent = get_tool_definitions(enabled_toolsets=["terminal", "file"], quiet_mode=True)
        assert "read_file" in {x["function"]["name"] for x in parent}

        # Subagent build moments later: TTL expired, probe flakes False, but
        # within grace → file/terminal tools must still resolve.
        t["now"] += reg._CHECK_FN_TTL_SECONDS + 1
        _clear_tool_defs_cache()
        child = get_tool_definitions(enabled_toolsets=["terminal", "file"], quiet_mode=True)
        child_names = {x["function"]["name"] for x in child}
        assert {"read_file", "write_file", "patch", "search_files", "terminal"}.issubset(
            child_names
        )
    def test_terminal_and_execute_code_tools_resolve_for_vercel_sandbox(self, monkeypatch):
        monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {"env_type": "vercel_sandbox", "container_disk": 51200},
        )
        monkeypatch.setattr(
            terminal_tool_module.importlib.util,
            "find_spec",
            lambda _name: object(),
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "code_execution"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}

        assert "terminal" in names
        assert "execute_code" in names

    def test_terminal_and_execute_code_tools_hide_for_unsupported_vercel_runtime(self, monkeypatch):
        monkeypatch.setenv("VERCEL_OIDC_TOKEN", "oidc-token")
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {
                "env_type": "vercel_sandbox",
                "container_disk": 51200,
                "vercel_runtime": "node20",
            },
        )
        monkeypatch.setattr(
            terminal_tool_module.importlib.util,
            "find_spec",
            lambda _name: object(),
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "code_execution"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}

        assert "terminal" not in names
        assert "execute_code" not in names

    def test_terminal_and_execute_code_tools_hide_for_vercel_without_auth(self, monkeypatch):
        monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
        monkeypatch.delenv("VERCEL_TOKEN", raising=False)
        monkeypatch.delenv("VERCEL_PROJECT_ID", raising=False)
        monkeypatch.delenv("VERCEL_TEAM_ID", raising=False)
        monkeypatch.setattr(
            terminal_tool_module,
            "_get_env_config",
            lambda: {
                "env_type": "vercel_sandbox",
                "container_disk": 51200,
                "vercel_runtime": "node22",
            },
        )
        monkeypatch.setattr(
            terminal_tool_module.importlib.util,
            "find_spec",
            lambda _name: object(),
        )
        tools = get_tool_definitions(enabled_toolsets=["terminal", "code_execution"], quiet_mode=True)
        names = {tool["function"]["name"] for tool in tools}

        assert "terminal" not in names
        assert "execute_code" not in names


class TestCheckFnGraceReprobeBackoff:
    """The grace window must absorb a flake, not turn it into a storm.

    The suppression above originally implemented "do not cache the failure" as
    "do not cache anything", so every call inside the 60 s grace window re-ran
    the FULL probe -- subprocess, socket dial, SDK import -- and re-logged the
    transient warning. Measured on the operator's serve: 40 probes and 40
    warning lines in 2 s for one ``check_fn``, 759 such lines in one log, and
    the storm fired DURING the persona prewarm walk it was meant to make cheap.

    The gate counts PROBES and WARNINGS, never milliseconds: the cost this
    stage removes is "the probe body ran again", and a wall-clock budget on a
    probe whose body is ``return False`` would assert nothing. The clock is
    pinned so the arithmetic is exact rather than raced.
    """

    @staticmethod
    def _flaky_after_first_success():
        """A probe that succeeds once and fails forever after.

        Returns ``(probe, calls)`` where ``calls["n"]`` counts real bodies run
        -- exactly the number the storm inflated.
        """

        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return calls["n"] == 1

        return probe, calls

    @staticmethod
    def _transient_warnings(caplog):
        return [
            r.getMessage()
            for r in caplog.records
            if "treating as transient" in r.getMessage()
        ]

    def test_a_storm_of_calls_inside_the_grace_window_costs_one_probe(self, caplog):
        """40 calls in the window -- the live shape -- probe and warn ONCE."""
        import logging

        import tools.registry as reg

        probe, calls = self._flaky_after_first_success()
        t = {"now": 1000.0}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(reg.time, "monotonic", lambda: t["now"])
            assert reg._check_fn_cached(probe) is True  # records last-good
            assert calls["n"] == 1

            t["now"] += reg._CHECK_FN_TTL_SECONDS + 1  # expire the normal TTL
            with caplog.at_level(logging.WARNING, logger=reg.__name__):
                for _ in range(40):
                    # No clock movement between calls: this is the burst one
                    # check_fn takes from a single readiness/prewarm walk.
                    assert reg._check_fn_cached(probe) is True

        assert calls["n"] == 2, (
            "the grace window re-ran the full probe per call: "
            f"{calls['n'] - 1} probes for 40 calls"
        )
        warnings = self._transient_warnings(caplog)
        assert len(warnings) == 1, (
            "the transient warning fired once per call instead of once per "
            f"backoff interval: {len(warnings)} lines"
        )

    def test_the_backoff_expires_and_the_next_call_re_probes(self, caplog):
        """The backoff DELAYS the re-probe; it must never cancel it."""
        import logging

        import tools.registry as reg

        probe, calls = self._flaky_after_first_success()
        t = {"now": 1000.0}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(reg.time, "monotonic", lambda: t["now"])
            assert reg._check_fn_cached(probe) is True
            t["now"] += reg._CHECK_FN_TTL_SECONDS + 1
            with caplog.at_level(logging.WARNING, logger=reg.__name__):
                assert reg._check_fn_cached(probe) is True
                assert calls["n"] == 2
                # Still inside the backoff -> served from the cache entry.
                t["now"] += reg._CHECK_FN_GRACE_REPROBE_SECONDS / 2
                assert reg._check_fn_cached(probe) is True
                assert calls["n"] == 2
                # Past the backoff, still inside the grace window -> re-probe.
                t["now"] += reg._CHECK_FN_GRACE_REPROBE_SECONDS
                assert reg._check_fn_cached(probe) is True
                assert calls["n"] == 3, (
                    "the backoff never expired: a check_fn that fails forever "
                    "would be served from one stale probe for the whole window"
                )

        assert len(self._transient_warnings(caplog)) == 2

    def test_a_recovery_inside_the_window_is_cached_on_the_normal_ttl(self):
        """A backend that comes back must not stay on the 5 s backoff."""
        import tools.registry as reg

        state = {"ok": True}
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return state["ok"]

        t = {"now": 1000.0}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(reg.time, "monotonic", lambda: t["now"])
            assert reg._check_fn_cached(probe) is True
            state["ok"] = False
            t["now"] += reg._CHECK_FN_TTL_SECONDS + 1
            assert reg._check_fn_cached(probe) is True  # grace-served
            assert calls["n"] == 2
            state["ok"] = True
            t["now"] += reg._CHECK_FN_GRACE_REPROBE_SECONDS + 0.5
            assert reg._check_fn_cached(probe) is True  # a real recovery
            assert calls["n"] == 3
            # A real True rides the full TTL, not the grace backoff.
            t["now"] += reg._CHECK_FN_GRACE_REPROBE_SECONDS + 0.5
            assert reg._check_fn_cached(probe) is True
            assert calls["n"] == 3, (
                "a recovered check_fn was re-probed on the grace backoff "
                "instead of the normal TTL"
            )

    def test_a_grace_true_never_outlives_the_grace_window(self):
        """Served at second 59, unreadable at second 61.

        The backoff is clamped to the grace window's end. Without the clamp a
        True served just before expiry would answer up to 5 s past it -- the
        cache extending the very window it is supposed to ride inside.
        """
        import tools.registry as reg

        probe, calls = self._flaky_after_first_success()
        t = {"now": 1000.0}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(reg.time, "monotonic", lambda: t["now"])
            assert reg._check_fn_cached(probe) is True
            # One second short of the grace window's end.
            t["now"] += reg._CHECK_FN_FAILURE_GRACE_SECONDS - 1
            assert reg._check_fn_cached(probe) is True
            assert calls["n"] == 2
            # Two seconds on, the grace window is over: the cached True must
            # not answer, and the honored failure must come through.
            t["now"] += 2
            assert reg._check_fn_cached(probe) is False
            assert calls["n"] == 3
