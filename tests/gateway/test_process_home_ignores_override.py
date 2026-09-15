"""Process-scope HERMES_HOME contract for gateway identity/lifecycle files.

Gateway identity files (PID, lock, lifecycle sentinel, heartbeat, watchdog
dump) must always live under the home the gateway PROCESS was launched with —
never under a persona profile home that happens to be active in the calling
context (issue #56986 class). ``persona_profile_context`` installs a
context-local override AND mirrors it into ``os.environ``; a background
gateway thread (heartbeat writer, shutdown watchdog, lifecycle ledger) that
resolves through the override-honoring ``get_hermes_home()`` would route its
write into the wrong profile directory.

These tests pin the two-sided contract for all three gateway resolvers:

* env var set → the env value wins, even with an active override;
* env var unset → the PLATFORM DEFAULT wins, never the override.  This is the
  case the old local copies in ``lifecycle_ledger`` / ``shutdown_watchdog``
  got wrong — their fallback called ``get_hermes_home()``, which follows the
  override.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_constants import (
    _get_platform_default_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


def _resolvers():
    from gateway import lifecycle_ledger, shutdown_watchdog, status

    return [
        ("lifecycle_ledger", lifecycle_ledger._process_hermes_home),
        ("shutdown_watchdog", shutdown_watchdog._process_hermes_home),
        ("status", status._get_process_hermes_home),
    ]


@pytest.mark.parametrize("name,resolver", _resolvers())
def test_env_set_wins_over_active_override(monkeypatch, tmp_path, name, resolver):
    head = tmp_path / "head"
    monkeypatch.setenv("HERMES_HOME", str(head))
    token = set_hermes_home_override(str(tmp_path / "profiles" / "alice"))
    try:
        assert resolver() == head, name
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("name,resolver", _resolvers())
def test_env_unset_falls_back_to_platform_default_not_override(
    monkeypatch, tmp_path, name, resolver
):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    override = tmp_path / "profiles" / "alice"
    token = set_hermes_home_override(str(override))
    try:
        resolved = resolver()
        assert resolved == _get_platform_default_hermes_home(), name
        assert resolved != override, name
    finally:
        reset_hermes_home_override(token)
