"""STRUCTURAL GATE (S56): every ``RuntimeConfig`` field must have a production
reader, or an explicit report-only entry with a reason.

This is the fix for the CLASS, not for the seven blocks S56 happened to remove.
Those seven were not a one-off: ``role_envelope`` (S47) was the same defect, and
so was ``open_incident_warning_threshold`` (B-5). A config field costs nothing to
add and rides ``effective_config_summary`` (``asdict(cfg)``) onto the live
snapshot wire, where an operator reads it as a knob that governs something. There
was no gate stopping a knob from outliving its last reader, so the class kept
regrowing.

**What counts as a reader.** The scan is AST-based and STRING-FORM AWARE, because
attribute access is not the only spelling:

* ``cfg.mission_chat`` — ``ast.Attribute``.
* ``getattr(cfg, "coordinator_permissions", None)`` — a string literal in a
  ``getattr`` call. This is not hypothetical: it is the ONLY reader of
  ``coordinator_permissions`` outside the config layer
  (``hermes_cli/harness_parts/persona_commands.py``), and a pure-attribute scan
  reports that live block as dead. A gate that fires on a live field is worse
  than no gate.
* Nested block fields (``supervision.child_events_enabled``) are resolved the
  same way, one level down.

**What does NOT count.** The definition module, plus two functions that touch
every field by construction: ``migrations.validate_runtime_config`` (a range
check on a field nothing reads validates nothing — that is exactly how
``continuous_role_sessions`` and ``swarm`` looked governed for months) and
``migrations.effective_config_summary`` (``asdict(cfg)``). ``config.py`` is NOT
excluded whole: its loaders read yaml via ``raw.get("name")``, which this scan
does not count anyway, while the accessor helpers beside them are real readers.

**What running it found, beyond the seven ruled blocks.** THIRTY more scalar
fields have no production reader either — the whole ``daemon_*`` family, the
``live_run_*`` budgets, the ``liveness_*`` watchdog knobs, the
``artifact_storage_*`` watermarks, ``mission_max_total_tokens`` /
``mission_wall_clock_deadline_seconds``, ``neko_recovery_attempt_cap`` /
``neko_extension_cap``, and others. They are PRE-EXISTING debt, not opened by
S56: several lost their last reader when the mission/daemon lanes were retired,
and one (``run_lease_seconds``) lost it in this very commit with
``production_envelope``. They were NOT ruled on, so they are carried in
``UNRULED_DEBT`` below — visible, frozen, and recorded in doc 19 — rather than
deleted on this wave's authority or quietly waved through.

``UNRULED_DEBT`` is FROZEN: a test asserts its size, so a NEW field with no
reader fails the gate instead of being appended to the bucket.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import MISSING, fields, is_dataclass

import pytest

from agent_runtime.runtime_config import RuntimeConfig


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

PRODUCTION_PACKAGES = ("agent_runtime", "hermes_cli", "tools")

#: The DEFINITION module. Every field appears here by construction.
EXCLUDED_FILES = frozenset({"agent_runtime/runtime_config.py"})

#: Function-level exclusions. ``config.py`` is deliberately NOT excluded whole:
#: its per-block loaders read yaml via ``raw.get("name")``, which this scan does
#: not count anyway, while the accessor helpers beside them
#: (``mission_chat_dispatch_session_policy`` and friends) are REAL readers and
#: must count. Only these two functions are skipped:
#:
#: * ``migrations.validate_runtime_config`` — a range check on a field nothing
#:   reads validates nothing. Counting it as a reader is exactly how
#:   ``continuous_role_sessions`` and ``swarm`` looked governed for months.
#: * ``migrations.effective_config_summary`` — ``asdict(cfg)``; it touches every
#:   field by definition, which is the whole problem this gate exists for.
EXCLUDED_FUNCTIONS = frozenset(
    {
        ("agent_runtime/migrations.py", "validate_runtime_config"),
        ("agent_runtime/migrations.py", "effective_config_summary"),
    }
)

#: Fields with NO production reader that are kept deliberately, each with the
#: reason. An entry here is a debt declaration, not an exemption: it says "this
#: ships on the wire and governs nothing, and here is why we keep it".
#: Adding a field without a reader and without an entry here fails the gate.
REPORT_ONLY: dict[str, str] = {
    "schema_version": (
        "wire/version metadata, not a knob: effective_config_summary stamps it and "
        "operators read it off the frame; no code branches on its value."
    ),
    # NOTE: ``store_root`` and ``personas`` were candidates here and are NOT
    # listed, because the scan DOES resolve them: both names are read as
    # attributes elsewhere in production. The over-collecting resolver is why —
    # see its docstring. Listing them would have made two live-enough fields
    # look like declared debt.
}

#: PRE-EXISTING dead-config debt this gate MEASURED but was not ruled to fix.
#: Every entry is a RuntimeConfig scalar whose only reader was
#: ``validate_runtime_config`` (a range check on a knob nothing consults) or, in
#: one case, the ``production_envelope`` prose block S56 deleted. Each entry
#: names what would retire it. Recorded in doc 19.
#:
#: FROZEN. ``test_the_unruled_debt_bucket_is_frozen`` pins the size, so a new
#: unread field cannot be waved through by appending to it.
UNRULED_DEBT: dict[str, str] = {
    "heartbeat_ttl_seconds": "run-heartbeat TTL; its consumer went with the daemon/ticker lane.",
    "max_actions_per_tick": "per-tick action budget; the tick loop is retired.",
    "daemon_enabled": "the background Mission Daemon was retired; status hardcodes execution_mode='manual'.",
    "daemon_interval_seconds": "daemon lane retired; nothing schedules on it.",
    "daemon_idle_interval_seconds": "daemon lane retired; nothing schedules on it.",
    "daemon_heartbeat_seconds": "daemon lane retired; nothing schedules on it.",
    "task_create_auto_start_daemon": "daemon lane retired; goal create never consults it.",
    "root_node_mode": "every skill-gate call site now passes root_node_mode=False literally.",
    "preferred_goal_execution_mode": "execution-mode selection went with the mission/dispatch lane.",
    "live_run_max_wall_seconds": "per-run wall budget; the run opener that enforced it is retired.",
    "live_run_max_api_calls": "per-run API budget; same retired lane.",
    "live_run_max_total_tokens": "per-run token budget; same retired lane (still cross-checked in validation).",
    "live_run_iteration_budget": "per-run iteration budget; same retired lane.",
    "scope_wait_deadline_seconds": "the scope-wait lane it bounded is retired.",
    "run_lease_seconds": "run lease; its last non-validator reader was production_envelope, deleted at S56.",
    "tool_wait_timeout_seconds": "the tool-wait lane it bounded is retired.",
    "liveness_enabled": "liveness watchdog knob; the watchdog reads its own constants, not this.",
    "liveness_poll_seconds": "liveness watchdog knob; unread outside validation.",
    "liveness_quiet_strikes": "liveness watchdog knob; unread outside validation.",
    "liveness_hung_seconds": "liveness watchdog knob; unread outside validation.",
    "child_progress_min_interval_seconds": "child.progress throttle; continuity.py does not read it.",
    "deploy_timeout_seconds": "the deploy-verification lane it bounded was never wired.",
    "mission_max_total_tokens": "mission ceiling; only the soft/hard cross-check in validation reads it.",
    "mission_wall_clock_deadline_seconds": "mission deadline; no enforcer consults it.",
    "neko_recovery_attempt_cap": "bounded-continuation cap; that lane reads its own constants.",
    "neko_extension_cap": "bounded-continuation cap; that lane reads its own constants.",
    "artifact_storage_low_watermark_mb": "artifact-storage watermark; no sweeper reads it.",
    "artifact_storage_high_watermark_mb": "artifact-storage watermark; no sweeper reads it.",
    "artifact_storage_critical_watermark_mb": "artifact-storage watermark; no sweeper reads it.",
}


def _production_files() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for package in PRODUCTION_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in EXCLUDED_FILES:
                continue
            paths.append(path)
    return paths


def _referenced_names(paths) -> set[str]:
    """Every name a production module could be reading off a config object.

    Deliberately over-collects (any attribute of any object, any getattr string
    literal). A gate that under-collects fires on live fields; a gate that
    over-collects only misses a dead one. The asymmetry is on purpose.
    """
    seen: set[str] = set()
    for path in paths:
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover - a broken file is its own failure
            continue
        skipped = {
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (rel, node.name) in EXCLUDED_FUNCTIONS
        }
        excluded_nodes = {inner for fn in skipped for inner in ast.walk(fn)}
        for node in ast.walk(tree):
            if node in excluded_nodes:
                continue
            if isinstance(node, ast.Attribute):
                seen.add(node.attr)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "getattr" and node.args[1:]:
                    target = node.args[1]
                    if isinstance(target, ast.Constant) and isinstance(target.value, str):
                        seen.add(target.value)
    return seen


_REFERENCED = _referenced_names(_production_files())


def _config_field_names() -> list[str]:
    names: list[str] = []
    for field in fields(RuntimeConfig):
        names.append(field.name)
        factory = field.default_factory  # type: ignore[attr-defined]
        if factory is not MISSING and is_dataclass(factory):
            for sub in fields(factory):
                names.append(f"{field.name}.{sub.name}")
    return names


def test_the_scan_is_not_vacuous():
    assert len(_production_files()) > 100
    # Plain attribute access.
    assert "redaction_mode" in _REFERENCED
    # The string-form trap this gate exists to survive: coordinator_permissions
    # has exactly one production reader and it is a getattr string literal.
    assert "coordinator_permissions" in _REFERENCED


def test_the_scan_finds_the_getattr_string_form_specifically():
    """Pinned separately so a refactor that drops getattr handling fails HERE,
    naming the cause, instead of failing as a false 'dead field' report."""
    attribute_only: set[str] = set()
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attribute_only.add(node.attr)
    assert "coordinator_permissions" not in attribute_only
    assert "coordinator_permissions" in _REFERENCED


@pytest.mark.parametrize("name", _config_field_names())
def test_every_runtime_config_field_is_read_or_declared_report_only(name):
    leaf = name.split(".")[-1]
    if leaf in _REFERENCED or name in _REFERENCED:
        return
    assert name in REPORT_ONLY or name in UNRULED_DEBT, (
        f"RuntimeConfig field '{name}' has no production reader and no REPORT_ONLY "
        f"or UNRULED_DEBT entry. It ships on the snapshot wire via "
        f"effective_config_summary and tells an operator it governs something. "
        f"Either wire it, delete it, or declare it with a reason."
    )


def test_every_ledger_entry_carries_a_reason():
    for ledger in (REPORT_ONLY, UNRULED_DEBT):
        for name, reason in ledger.items():
            assert reason.strip(), name
            assert len(reason.strip()) > 30, name


def test_no_ledger_entry_is_stale():
    """An entry that HAS acquired a reader must be removed, or the ledger starts
    lying in the other direction (the frozen-HERMES_HOME P3 lesson)."""
    live = {
        name
        for ledger in (REPORT_ONLY, UNRULED_DEBT)
        for name in ledger
        if name.split(".")[-1] in _REFERENCED
    }
    assert live == set(), f"ledger entries that now have readers: {sorted(live)}"


def test_the_two_ledgers_do_not_overlap():
    assert set(REPORT_ONLY) & set(UNRULED_DEBT) == set()


def test_the_ledgers_name_only_real_fields():
    known = set(_config_field_names())
    unknown = (set(REPORT_ONLY) | set(UNRULED_DEBT)) - known
    assert unknown == set(), sorted(unknown)


def test_the_unruled_debt_bucket_is_frozen():
    """The bucket exists to make MEASURED debt visible, not to absorb new debt.
    A field added tomorrow with no reader must fail the gate, not be appended
    here — so the size is pinned. Retiring an entry (wiring the field or
    deleting it) means lowering this number in the same commit."""
    assert len(UNRULED_DEBT) == 29


# --------------------------------------------------------------------------
# Red-proof against the pre-wave tree
# --------------------------------------------------------------------------

#: The seven blocks S56 removed. On the PRE-WAVE tree these were live
#: ``RuntimeConfig`` fields; the resolver above finds no production reader for
#: any of them, so this gate — had it existed one commit earlier — would have
#: failed naming exactly these and nothing else.
S56_REMOVED_BLOCKS = (
    "continuous_role_sessions",
    "enterprise_worker_sessions",
    "normal_worker_flow",
    "repo_bundle_routing",
    "simplified_agent_contract",
    "swarm",
    "supervision.recursive_enabled",
    "supervision.hierarchical_budget_enabled",
    "supervision.deploy_verification_enabled",
)


@pytest.mark.parametrize("name", S56_REMOVED_BLOCKS)
def test_the_gate_would_have_named_each_block_s56_removed(name):
    leaf = name.split(".")[-1]
    assert leaf not in _REFERENCED, (
        f"'{name}' still has a production reference — the red-proof is no longer "
        f"honest and this gate's premise needs re-deriving."
    )
    assert name not in REPORT_ONLY


def test_the_surviving_supervision_field_is_not_in_the_red_proof():
    """``supervision.child_events_enabled`` is LIVE (continuity.py reads it).
    It must resolve as read, or the gate is proving the wrong thing."""
    assert "child_events_enabled" in _REFERENCED
