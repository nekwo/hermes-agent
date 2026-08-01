"""Regression: harness-global policy readers resolve against the ROOT config.

The CLI bootstrap (``hermes_cli.main._apply_profile_override``) runs at import
time, reads ``<root>/active_profile`` and points ``HERMES_HOME`` at
``<root>/profiles/<name>``.  From that point a bare
``load_agent_runtime_config()`` silently reads THAT profile's ``config.yaml``,
so whichever profile is sticky-active could shadow harness-global operator
policy (live proof 2026-07-23: with ``alice`` active the mission-chat lane
resolved the wrong ``chat_lane_restore_toolsets``).  The fix pins the
harness-global policy readers to ``load_root_runtime_config()`` /
``harness_root_config_path()``.

Each test mirrors
``test_chat_lane_toolsets.py::test_config_restore_resolves_root_config_under_profile_home``:
write a ROOT ``config.yaml`` with policy value X, a profile ``config.yaml``
shadowing with a different value Y, point ``HERMES_HOME`` at the profile home,
and assert the pinned reader returns X.  A reader still on the bare
``load_agent_runtime_config()`` would read Y and fail.

``tests/agent_runtime/conftest.py`` isolates ``HERMES_AGENT_RUNTIME_ROOT``;
``HERMES_HOME`` is what these tests vary (per the existing root-pin test).
Each test gets a fresh pytest ``tmp_path`` so the per-path YAML parse cache
never bleeds a value across cases.
"""

from __future__ import annotations

import textwrap

import pytest


def _write_root_and_profile(tmp_path, monkeypatch, *, root_yaml: str, profile_yaml: str):
    """Lay down ``<root>/config.yaml`` + ``<root>/profiles/tester/config.yaml``
    and point ``HERMES_HOME`` at the profile home (the sticky-active redirect
    the CLI bootstrap would produce).  ``harness_root_config_path()`` maps the
    ``<root>/profiles/tester`` home back to ``<root>`` so the ROOT config is the
    one a pinned reader must resolve."""

    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "tester"
    profile_home.mkdir(parents=True)
    (root / "config.yaml").write_text(textwrap.dedent(root_yaml), encoding="utf-8")
    (profile_home / "config.yaml").write_text(textwrap.dedent(profile_yaml), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return root, profile_home


def test_root_pin_maps_profile_home_back_to_root(tmp_path, monkeypatch):
    # Guardrail for the harness: the root-config path must resolve to
    # <root>/config.yaml even though HERMES_HOME points into the profile.
    from agent_runtime.config import harness_root_config_path

    root, _profile_home = _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="agent_runtime: {}\n",
        profile_yaml="agent_runtime: {}\n",
    )
    assert harness_root_config_path() == root / "config.yaml"


def test_redaction_mode_reads_root_not_profile(tmp_path, monkeypatch):
    from agent_runtime.redaction_mode import redaction_mode

    # An env override would win over config — clear it so the config value is
    # what the reader resolves.
    monkeypatch.delenv("HERMES_REDACTION_MODE", raising=False)
    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          redaction_mode: observe
        """,
        profile_yaml="""
        agent_runtime:
          redaction_mode: strict
        """,
    )
    # Root says observe; the profile shadows with strict (also the default) —
    # the pin must surface observe.
    assert redaction_mode() == "observe"






def test_event_log_rotation_cap_reads_root_not_profile(tmp_path, monkeypatch):
    from agent_runtime import events

    # Env override wins over config; the cap is memoized per store root. Clear
    # both so the reader resolves the config value freshly.
    monkeypatch.delenv("HERMES_EVENT_LOG_ROTATION_CAP_BYTES", raising=False)
    monkeypatch.setattr(events, "_ROTATION_CAP_CACHE", {})
    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          event_log:
            rotation_cap_bytes: 123456
        """,
        profile_yaml="""
        agent_runtime:
          event_log:
            rotation_cap_bytes: 999
        """,
    )
    assert events._rotation_cap_bytes() == 123456


def test_lock_acquire_timeout_reads_root_not_profile(tmp_path, monkeypatch):
    from agent_runtime.locks import _lock_timeout_seconds

    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          lock_acquire_timeout_seconds: 42
        """,
        profile_yaml="""
        agent_runtime:
          lock_acquire_timeout_seconds: 7
        """,
    )
    # None => resolve from config (the fallback path we pinned); an explicit
    # value still wins for callers/tests that inject one.
    assert _lock_timeout_seconds(None) == 42.0


def test_read_model_db_filename_reads_root_not_profile(tmp_path, monkeypatch):
    from agent_runtime.read_model import ReadModel

    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          read_model:
            db_filename: root_rm.db
        """,
        profile_yaml="""
        agent_runtime:
          read_model:
            db_filename: profile_rm.db
        """,
    )
    assert ReadModel._read_model_db_path().name == "root_rm.db"


def test_neko_extension_cap_config_loads_and_is_ignored(tmp_path, monkeypatch):
    # INVERTED at S57 (2026-08-01), not deleted — the same treatment as the swarm
    # pin below. The comment this test used to carry claimed neko_extension_cap
    # "is consumed inside embedded seams that need live Incident + RunStore state
    # to reach (snapshot._run_blocked_reason, snapshot._next_action_summary);
    # those read load_root_runtime_config().neko_extension_cap".
    #
    # That claim was FALSE by the time S56's reader gate measured it, and S57
    # re-verified it by hand (AST + string form): neither seam reads the config
    # field, the bounded-continuation lane reads its own constants, and the only
    # thing still touching the name was a `_positive` range validator. The field
    # is gone. The old claim is quoted rather than deleted because it IS the
    # failure mode this wave keeps finding — a test comment asserting a reader
    # no scan can locate.
    #
    # The FIXTURE is kept (both yamls still set the key) so this proves
    # load-and-ignore, and the root-over-profile property the test actually
    # guards is re-asserted on a field that is still live.
    from agent_runtime.config import load_root_runtime_config

    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          neko_extension_cap: 7
          lock_acquire_timeout_seconds: 42
        """,
        profile_yaml="""
        agent_runtime:
          neko_extension_cap: 3
          lock_acquire_timeout_seconds: 9
        """,
    )
    cfg = load_root_runtime_config()
    assert not hasattr(cfg, "neko_extension_cap")
    # The root-over-profile pin is unchanged, proven on a LIVE field:
    # `locks.py` reads `lock_acquire_timeout_seconds`.
    assert cfg.lock_acquire_timeout_seconds == 42


def test_swarm_config_resolves_from_root(tmp_path, monkeypatch):
    # INVERTED at S56 (2026-08-01), not deleted. This pinned
    # `load_root_runtime_config().swarm.max_active_lanes` for the swarm CLI
    # seams; S56 deleted `SwarmConfig`, the `_swarm_config` loader and the
    # report-only `status.swarm` / `swarm_budget` / `production_envelope` rows
    # it fed. The FIXTURE is kept deliberately (both yamls still SET the block)
    # so this now proves the other half of the ruling: an operator yaml that
    # still carries a removed block must LOAD AND BE IGNORED, never start
    # refusing to load. The removal itself is owned by
    # tests/agent_runtime/test_s56_config_block_removal.py.
    from agent_runtime.config import load_root_runtime_config

    _write_root_and_profile(
        tmp_path,
        monkeypatch,
        root_yaml="""
        agent_runtime:
          swarm:
            max_active_lanes: 9
        """,
        profile_yaml="""
        agent_runtime:
          swarm:
            max_active_lanes: 1
        """,
    )
    cfg = load_root_runtime_config()
    assert not hasattr(cfg, "swarm")
    # ...and the root pin itself is still live for the fields that survived.
    # (S57 retargeted this from `neko_extension_cap`, which it removed, onto the
    # scalar that still has a production reader.)
    assert cfg.lock_acquire_timeout_seconds is not None
