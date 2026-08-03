"""S57 empties the S56 reader-gate's ``UNRULED_DEBT`` bucket: 29 reader-less
``RuntimeConfig`` scalars are CUT — field, ``config.py`` load line, and
``migrations`` range validator, in one pass.

Operator-ruled CUT on 2026-08-01 ("they aren't used and it's something I
added"). S56's gate MEASURED these; it was not ruled to delete them, so it
carried them, frozen, with a per-field reason. S57 re-verified every one by hand
(AST attribute form + ``getattr`` string form + a plain repo-wide text scan, the
last because the gate's own resolver has a documented blind spot the
``persona_commands.py:1293`` ``getattr`` trap exposed) and cut all 29. None
survived re-verification with a reader the gate had missed.

**Why this is a CONTRACT move and not a code-only cut.** ``effective_config_summary``
is ``asdict(cfg)`` and ``snapshot.py`` publishes it as the frame's
``runtime_config`` block. All 29 were confirmed PRESENT on the LIVE frame
(``harness snapshot --json`` against ``X:\\Eternia\\.hermes\\agent-runtime``, the
root ``paths.store_root()`` resolves to) at contract 47. Removing them edits the
wire, so the contract moves 47 -> 48 and the Launcher pin moves in the same wave.

**What survived, and why the survivor matters.**
``lock_acquire_timeout_seconds`` sits in the same neighbourhood of the dataclass,
looks exactly like the fields around it, and is LIVE: ``locks.py:133`` reads it —
through ``getattr(load_root_runtime_config(), "lock_acquire_timeout_seconds", 15)``,
the string form. A prefix-based or eyeball-based cut takes it; the AST +
string-form gate keeps it. That is the whole argument for the gate's shape.

**Four cross-field validators went too**, and they are the sharper finding: the
``live_run_max_total_tokens <= mission_max_total_tokens`` ceiling pair, the
``liveness_poll_seconds`` 30..120 range, the ``liveness_hung_seconds <
heartbeat_ttl_seconds`` ordering, and the ``artifact_storage_*`` low<=high<=critical
ordering. Each related two or three DEAD knobs to each other. A relationship
between unread fields is not governance — it is the same "looks configured"
illusion one level up, and it is what made these fields survive four previous
audits.

RED-FIRST: written against the pre-cut tree, where
``test_every_field_below_is_gone_from_the_dataclass`` fails naming all 29 and
``test_the_unruled_debt_bucket_is_empty`` fails at 29 != 0.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01) — AND WHY ALMOST
NOTHING HERE COULD GO
-----------------------------------------------------------------------------

The registry carries a repo-wide ``CODE`` row for TWENTY-EIGHT of the 29 fields.
**No test function left this file**, and that is a finding rather than an
omission:

* ``root_node_mode`` is the 29th and is DELIBERATELY NOT a registry ``CODE``
  row. Sixty other hits in the tree are a ``ContextVar`` and a kwarg of the SAME
  NAME (``skill_utils``, ``prompt_builder``, ``skills_tool``) that never read the
  config field, so banning it repo-wide would fail against live code. The
  registry says so beside its ``S57_FIELD_ONLY`` constant and points here. Every
  parametrized case below runs over ``REMOVED_FIELDS``, which INCLUDES
  ``root_node_mode`` — so deleting any of them would drop the only coverage that
  name has.
* ``test_every_field_below_is_gone_from_the_dataclass`` is a FIELD-set pin over
  ``RuntimeConfig`` / ``AgentRuntimeConfig``, not a name scan, and is the pin the
  registry defers to for ``root_node_mode``.
* ``test_the_load_plumbing_is_gone_too`` and
  ``test_no_range_validator_survives_its_field`` are scoped to the bodies of
  ``config.load_agent_runtime_config`` and ``migrations.validate_runtime_config``
  — narrower than the registry's repo-wide rows for the other 28, but the only
  form that can carry the 29th.
* Everything else is behaviour the registry has no form for: the load-and-ignore
  proof (a root still setting all 29, plus the nested ``daemon:`` spelling, must
  LOAD), the emitted-frame wire tests at contract 48, the surviving-scalar KEEP
  (``lock_acquire_timeout_seconds``, live only through the ``getattr`` STRING
  form at ``locks.py:133``), the four cross-field validator message pins, the
  anti-vacuity proof that the validator did not become an empty function, the
  count pin on the ruled set, and the ``UNRULED_DEBT == {}`` closeout plus the
  pure-tripwire assertion over S56's gate.
"""

from __future__ import annotations

import inspect
from dataclasses import fields

import pytest
import yaml

from agent_runtime import config as config_module
from agent_runtime import migrations, snapshot
from agent_runtime.config import AgentRuntimeConfig, load_agent_runtime_config
from agent_runtime.runtime_config import RuntimeConfig
from tests.agent_runtime.test_s56_runtime_config_reader_gate import UNRULED_DEBT


#: The 29 fields, exactly as S56's frozen ledger named them.
REMOVED_FIELDS = (
    "heartbeat_ttl_seconds",
    "max_actions_per_tick",
    "daemon_enabled",
    "daemon_interval_seconds",
    "daemon_idle_interval_seconds",
    "daemon_heartbeat_seconds",
    "task_create_auto_start_daemon",
    "root_node_mode",
    "preferred_goal_execution_mode",
    "live_run_max_wall_seconds",
    "live_run_max_api_calls",
    "live_run_max_total_tokens",
    "live_run_iteration_budget",
    "scope_wait_deadline_seconds",
    "run_lease_seconds",
    "tool_wait_timeout_seconds",
    "liveness_enabled",
    "liveness_poll_seconds",
    "liveness_quiet_strikes",
    "liveness_hung_seconds",
    "child_progress_min_interval_seconds",
    "deploy_timeout_seconds",
    "mission_max_total_tokens",
    "mission_wall_clock_deadline_seconds",
    "neko_recovery_attempt_cap",
    "neko_extension_cap",
    "artifact_storage_low_watermark_mb",
    "artifact_storage_high_watermark_mb",
    "artifact_storage_critical_watermark_mb",
)

#: The one scalar in the same neighbourhood that KEPT its reader.
SURVIVING_SCALAR = "lock_acquire_timeout_seconds"


def test_the_removed_set_is_exactly_twenty_nine():
    """A count pin, so a field cannot be quietly dropped from (or added to) the
    ruled set between the doc-19 disposition table and this file."""
    assert len(REMOVED_FIELDS) == 29
    assert len(set(REMOVED_FIELDS)) == 29


@pytest.mark.parametrize("name", REMOVED_FIELDS)
def test_every_field_below_is_gone_from_the_dataclass(name):
    assert name not in {f.name for f in fields(RuntimeConfig)}
    assert name not in {f.name for f in fields(AgentRuntimeConfig)}
    assert not hasattr(AgentRuntimeConfig(), name)


def test_the_surviving_scalar_is_still_there_and_still_read():
    """The keep is the load-bearing half of this cut: ``locks.py`` reads
    ``lock_acquire_timeout_seconds``, through the ``getattr`` STRING form. If a
    later wave prefix-cuts this neighbourhood, this fails first."""
    assert SURVIVING_SCALAR in {f.name for f in fields(RuntimeConfig)}
    from agent_runtime import locks

    assert SURVIVING_SCALAR in inspect.getsource(locks)


def _code_only(fn) -> str:
    """The function's CODE, with comments stripped.

    ``inspect.getsource`` returns comments too, and this wave deliberately leaves
    a paragraph in both ``config.py`` and ``migrations.py`` naming every field it
    removed. A text scan over raw source would therefore report the removal
    comment as a surviving reference — the same false positive the S56 gate's
    docstring warns about in the other direction. Round-tripping through the AST
    drops comments and keeps every real name."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return ast.unparse(tree)


@pytest.mark.parametrize("name", REMOVED_FIELDS)
def test_the_load_plumbing_is_gone_too(name):
    """A field removed from the dataclass but still assigned in ``config.py``
    would be a TypeError at load; a field still READ off ``raw`` would be dead
    plumbing. Neither may remain."""
    assert name not in _code_only(config_module.load_agent_runtime_config), name


@pytest.mark.parametrize("name", REMOVED_FIELDS)
def test_no_range_validator_survives_its_field(name):
    """S47's lesson, applied for the fourth time: a range check on a field
    nothing reads validates nothing — it only makes a dead knob look governed."""
    assert name not in _code_only(migrations.validate_runtime_config), name


def test_the_four_cross_field_checks_are_gone():
    """These are the reason the 29 survived earlier audits: a validator that
    relates two dead knobs reads, at a glance, like a governed budget pair."""
    src = _code_only(migrations.validate_runtime_config)
    assert "mission token ceiling" not in src
    assert "must be between 30 and 120" not in src
    assert "must be less than heartbeat_ttl_seconds" not in src
    assert "artifact_storage_*_watermark_mb" not in src


def test_validation_still_has_a_real_arm_left():
    """Not vacuous: the validator did not become an empty function. The one
    surviving scalar is still range-checked, and so is ``schema_version``."""
    src = _code_only(migrations.validate_runtime_config)
    assert SURVIVING_SCALAR in src
    assert "schema_version" in src

    bad = AgentRuntimeConfig(lock_acquire_timeout_seconds=0)
    result = migrations.validate_runtime_config(bad)
    assert result["ok"] is False
    assert any(item["field"] == SURVIVING_SCALAR for item in result["errors"])


def test_a_default_config_validates_clean():
    result = migrations.validate_runtime_config(AgentRuntimeConfig())
    assert result["ok"] is True
    assert result["errors"] == []


# --------------------------------------------------------------------------
# Load-and-ignore (S47 precedent)
# --------------------------------------------------------------------------


def test_an_operator_yaml_still_setting_all_29_loads_and_is_ignored(tmp_path, monkeypatch):
    """The other half of the ruling. A root that still carries every removed
    knob must LOAD, not start refusing to load, and must not carry any of them
    onto the config object."""
    home = tmp_path / "profile"
    home.mkdir()
    stanza = {name: 1 for name in REMOVED_FIELDS}
    stanza["daemon_enabled"] = True
    stanza["liveness_enabled"] = False
    stanza["root_node_mode"] = True
    stanza["task_create_auto_start_daemon"] = True
    stanza["preferred_goal_execution_mode"] = "in_process_controller"
    # ...and a nested `daemon:` mapping, the second spelling `config.py` used to
    # accept for the four daemon scalars.
    stanza["daemon"] = {"enabled": True, "interval_seconds": 3, "heartbeat_seconds": 1}
    # One key that IS still live, so this proves an ignore rather than a
    # whole-file parse failure.
    stanza[SURVIVING_SCALAR] = 33
    (home / "config.yaml").write_text(
        yaml.safe_dump({"agent_runtime": stanza}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = load_agent_runtime_config()

    for name in REMOVED_FIELDS:
        assert not hasattr(cfg, name), name
    assert not hasattr(cfg, "daemon")
    assert getattr(cfg, SURVIVING_SCALAR) == 33
    assert migrations.validate_runtime_config(cfg)["ok"] is True


# --------------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", REMOVED_FIELDS)
def test_no_removed_field_reaches_the_emitted_frame(name, isolate_agent_runtime_root):
    """The whole reason this is a contract move: every one of these WAS on the
    live frame at contract 47, verified against the real store root before the
    cut. None may be on it now."""
    frame = snapshot.build_snapshot()
    assert name not in frame["runtime_config"], name


def test_the_frame_still_carries_the_surviving_scalar(isolate_agent_runtime_root):
    frame = snapshot.build_snapshot()
    assert SURVIVING_SCALAR in frame["runtime_config"]
    assert frame["parity"]["contract_version"] == 52


# --------------------------------------------------------------------------
# The gate this wave closes out
# --------------------------------------------------------------------------


def test_the_unruled_debt_bucket_is_empty():
    """S56's bucket existed to make measured debt VISIBLE until it was ruled on.
    It was ruled on. An entry left behind here after the cut would mean a field
    was reported as removed and was not."""
    assert UNRULED_DEBT == {}


def test_the_gate_is_now_a_pure_tripwire():
    """With the bucket empty and ``REPORT_ONLY`` holding one wire/version field,
    every remaining ``RuntimeConfig`` field must resolve as READ. Any new unread
    knob fails S56's gate outright — there is nowhere left to park it."""
    from tests.agent_runtime.test_s56_runtime_config_reader_gate import (
        REPORT_ONLY,
        _REFERENCED,
        _config_field_names,
    )

    unread = [
        name
        for name in _config_field_names()
        if name.split(".")[-1] not in _REFERENCED
        and name not in _REFERENCED
        and name not in REPORT_ONLY
    ]
    assert unread == [], unread
