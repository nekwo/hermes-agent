"""The ``launcher_qa`` MCP block must be declared the same way in every profile.

Nine live profiles carry a ``launcher_qa`` block and they had split into two
variants: one setting ``STAGEC_LAUNCH_HELPER`` explicitly, one omitting it. The
omission is *silently* equivalent today — the Launcher's ``launch_manager.dart``
falls back to the same helper path — which is precisely why it is debt: the two
blocks agree only by coincidence of a fallback in another repo.

Operator ruling (2026-07-31): the explicit variant is canonical
(``machine_roots.CANONICAL_LAUNCHER_QA_MCP_SERVER``).

This file is a DATA test over the live profile tree, and it is deliberately
ratcheted rather than red:

* the five known-divergent profiles are ledgered in :data:`EXPECTED_DRIFT`, so
  the test documents current reality instead of failing on a state nobody has
  been given the chance to fix;
* any NEW drift — a different field, a different profile, a changed value —
  fails immediately;
* a ledgered profile that has been fixed also fails, so the ledger shrinks and
  can never quietly outlive the drift it describes.

Fixing the five is an operator/live action against files this repo does not
own. The failure message therefore carries the exact YAML block to paste; no
test and no production path here rewrites a config (the single config writer
stays upstream ``save_config``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_runtime.machine_roots import (
    ADVISORY_ISSUE_CODES,
    CANONICAL_LAUNCHER_QA_MCP_SERVER,
    ISSUE_MCP_TEMPLATE_DRIFT,
    canonical_mcp_server_template,
    canonical_mcp_server_yaml,
    machine_roots_cache_clear,
    mcp_server_issues,
    mcp_server_template_diffs,
    mcp_server_template_issues,
)
from agent_runtime.profile_readiness import profile_readiness_for_persona
from hermes_constants import get_default_hermes_root

SERVER = "launcher_qa"

# profile -> {field path: drift kind}. Kind is pinned; the long expected VALUE
# is not, so a template reformat does not have to be mirrored here.
EXPECTED_DRIFT: dict[str, dict[str, str]] = {
    "backend-dev": {"env.STAGEC_LAUNCH_HELPER": "missing"},
    "gpt-launcher": {"env.STAGEC_LAUNCH_HELPER": "missing"},
    "launcher-dev": {"env.STAGEC_LAUNCH_HELPER": "missing"},
    "launcher-qa": {"env.STAGEC_LAUNCH_HELPER": "missing"},
    "qa": {"env.STAGEC_LAUNCH_HELPER": "missing"},
}


@pytest.fixture(autouse=True)
def _clear_roots_cache():
    machine_roots_cache_clear()
    yield
    machine_roots_cache_clear()


# ── Live profile tree ───────────────────────────────────────────────────────


def _configured_servers(raw: Any) -> dict[str, Any]:
    """The merged ``mcp_servers`` map across the three accepted spellings."""

    merged: dict[str, Any] = {}
    for key_path in (("mcp", "servers"), ("mcp_servers",), ("mcpServers",)):
        node: Any = raw
        for key in key_path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            for name, cfg in node.items():
                merged[str(name)] = cfg
    return merged


def _pre_sandbox_hermes_home() -> str:
    """HERMES_HOME as it stood BEFORE ``tests/conftest.py`` sandboxed it.

    Reading the env (or calling ``get_default_hermes_root()``) from inside a
    test resolves the throwaway session tempdir, which has no ``profiles/`` —
    the data assertion would then skip and report a clean tree that was never
    read. That is the false-all-clear trap; ``_capture_real_kanban_root`` in
    conftest exists for the same reason. Use the snapshot it records.
    """

    # Several conftest modules are loaded (root + per-package); only the root
    # one records the snapshot, and its sys.modules key depends on rootdir/
    # importmode. Scan for the ATTRIBUTE rather than guessing the key — probing
    # one name and finding a different conftest silently returns "" and skips.
    for name, module in list(sys.modules.items()):
        if "conftest" not in name:
            continue
        recorded = getattr(module, "_PRE_SANDBOX_HERMES_HOME", None)
        if recorded:
            return str(recorded)
    return ""


def _live_profiles_root(monkeypatch) -> Path:
    pre = _pre_sandbox_hermes_home()
    if pre:
        monkeypatch.setenv("HERMES_HOME", pre)
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    return get_default_hermes_root() / "profiles"


def _live_launcher_qa_blocks(monkeypatch) -> dict[str, Any]:
    root: Path = _live_profiles_root(monkeypatch)
    if not root.is_dir():
        pytest.skip(
            f"no live profile tree at {root} — this is NOT an all-clear; run with "
            "HERMES_HOME pointed at the live Hermes root to actually check the configs"
        )
    blocks: dict[str, Any] = {}
    for profile_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        config = profile_dir / "config.yaml"
        if not config.is_file():
            continue
        try:
            raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:  # a broken config is not this test's finding
            pytest.fail(f"{config} is unparseable: {type(exc).__name__}: {exc}")
        cfg = _configured_servers(raw).get(SERVER)
        if cfg is not None:
            blocks[profile_dir.name] = cfg
    if not blocks:
        pytest.skip(f"no profile under {root} declares '{SERVER}'")
    return blocks


def _patch_message(profile: str, diffs: list[str]) -> str:
    return (
        f"\n  profile '{profile}' — {len(diffs)} drifted field(s):\n"
        + "".join(f"      - {row}\n" for row in diffs)
        + f"    yaml patch for {profile}/config.yaml (under `mcp_servers:`):\n"
        + "\n".join(
            f"      {line}" for line in canonical_mcp_server_yaml(SERVER).splitlines()
        )
        + "\n"
    )


def test_live_launcher_qa_blocks_match_the_template_or_are_ledgered_drift(monkeypatch):
    blocks = _live_launcher_qa_blocks(monkeypatch)
    unexpected: list[str] = []
    for profile, cfg in blocks.items():
        diffs = mcp_server_template_diffs(SERVER, cfg)
        ledgered = EXPECTED_DRIFT.get(profile, {})
        surprises = [
            row
            for row in diffs
            if not any(
                row.startswith(f"{field}: {kind}") for field, kind in ledgered.items()
            )
        ]
        if surprises:
            unexpected.append(_patch_message(profile, surprises))
    assert not unexpected, (
        f"NEW '{SERVER}' template drift (not in EXPECTED_DRIFT). The canonical block "
        f"is agent_runtime.machine_roots.CANONICAL_LAUNCHER_QA_MCP_SERVER; apply the "
        f"patch below by hand (this lane is report-only and never writes a config):"
        + "".join(unexpected)
    )


def test_the_expected_drift_ledger_never_outlives_the_drift(monkeypatch):
    """A ledgered profile that has been fixed must be de-ledgered, not left."""

    blocks = _live_launcher_qa_blocks(monkeypatch)
    stale: list[str] = []
    for profile, ledgered in EXPECTED_DRIFT.items():
        cfg = blocks.get(profile)
        if cfg is None:
            stale.append(f"{profile}: no longer declares '{SERVER}'")
            continue
        diffs = mcp_server_template_diffs(SERVER, cfg)
        for field, kind in ledgered.items():
            if not any(row.startswith(f"{field}: {kind}") for row in diffs):
                stale.append(f"{profile}: '{field}' is no longer '{kind}' drift")
    assert not stale, (
        "EXPECTED_DRIFT has stale entries — the drift was fixed; delete these rows so "
        "the ratchet keeps closing:\n  " + "\n  ".join(stale)
    )


def test_the_ledger_records_exactly_the_variant_b_profiles():
    """Pins the audited split so a silent re-widening is visible in the diff."""

    assert sorted(EXPECTED_DRIFT) == [
        "backend-dev",
        "gpt-launcher",
        "launcher-dev",
        "launcher-qa",
        "qa",
    ]
    assert {field for rows in EXPECTED_DRIFT.values() for field in rows} == {
        "env.STAGEC_LAUNCH_HELPER"
    }


# ── The template itself ─────────────────────────────────────────────────────


def test_canonical_template_is_the_explicit_variant():
    env = CANONICAL_LAUNCHER_QA_MCP_SERVER["env"]
    assert "STAGEC_LAUNCH_HELPER" in env, "variant A is canonical by operator ruling"
    assert env["STAGEC_QA_TRANSPORT"] == "direct_control"
    assert CANONICAL_LAUNCHER_QA_MCP_SERVER["platforms"] == ("windows",)
    assert canonical_mcp_server_template(SERVER) is CANONICAL_LAUNCHER_QA_MCP_SERVER
    assert canonical_mcp_server_template("nope") is None


def test_the_template_cannot_be_mutated_by_a_consumer():
    with pytest.raises(TypeError):
        CANONICAL_LAUNCHER_QA_MCP_SERVER["timeout"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        CANONICAL_LAUNCHER_QA_MCP_SERVER["env"]["STAGEC_LAUNCH_HELPER"] = "x"  # type: ignore[index]


def test_the_emitted_yaml_patch_round_trips_to_the_template():
    parsed = yaml.safe_load(canonical_mcp_server_yaml(SERVER))
    assert list(parsed) == [SERVER]
    assert parsed[SERVER] == yaml.safe_load(yaml.safe_dump(_plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)))


def _plain(value: Any) -> Any:
    if hasattr(value, "items"):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


# ── Diff semantics ──────────────────────────────────────────────────────────


def test_an_identical_block_produces_no_diff_regardless_of_key_order():
    reordered = dict(reversed(list(_plain(CANONICAL_LAUNCHER_QA_MCP_SERVER).items())))
    assert mcp_server_template_diffs(SERVER, reordered) == []


def test_separator_style_is_not_drift():
    """A config authored with ``/`` resolves identically — flagging it is noise."""

    forward = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    forward["command"] = forward["command"].replace("\\", "/")
    forward["env"] = {
        key: value.replace("\\", "/") for key, value in forward["env"].items()
    }
    assert mcp_server_template_diffs(SERVER, forward) == []


@pytest.mark.parametrize(
    "mutate,expected_prefix",
    [
        (lambda cfg: cfg["env"].pop("STAGEC_LAUNCH_HELPER"), "env.STAGEC_LAUNCH_HELPER: missing"),
        (lambda cfg: cfg["env"].update(EXTRA="x"), "env.EXTRA: unexpected"),
        (lambda cfg: cfg.update(timeout=99), "timeout: 99 (expected 260)"),
        (lambda cfg: cfg.update(platforms=["linux"]), "platforms: "),
        (lambda cfg: cfg["sampling"].update(enabled=True), "sampling.enabled: true"),
    ],
)
def test_each_drift_shape_names_the_field(mutate, expected_prefix):
    cfg = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    mutate(cfg)
    diffs = mcp_server_template_diffs(SERVER, cfg)
    assert len(diffs) == 1
    assert diffs[0].startswith(expected_prefix), diffs[0]


def test_a_server_with_no_template_is_never_drift():
    assert mcp_server_template_diffs("backend_mcp", {"command": "anything"}) == []
    assert mcp_server_template_issues({"backend_mcp": {"command": "x"}}) == []


def test_template_issues_carry_the_advisory_code_and_a_pasteable_hint():
    cfg = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    cfg["env"].pop("STAGEC_LAUNCH_HELPER")
    (issue,) = mcp_server_template_issues({SERVER: cfg})
    assert issue.code == ISSUE_MCP_TEMPLATE_DRIFT
    assert issue.code in ADVISORY_ISSUE_CODES
    assert issue.field == f"mcp_servers.{SERVER}"
    assert "STAGEC_LAUNCH_HELPER" in issue.summary
    assert "Report-only" in issue.fix_hint


def test_template_issues_honour_the_only_filter():
    cfg = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    cfg["env"].pop("STAGEC_LAUNCH_HELPER")
    assert mcp_server_template_issues({SERVER: cfg}, only=["other"]) == []
    assert len(mcp_server_template_issues({SERVER: cfg}, only=[SERVER])) == 1


# ── The lane ────────────────────────────────────────────────────────────────


def test_drift_is_opt_in_on_the_binding_lane():
    """The original contract — every returned issue means 'unavailable' — holds."""

    # Tokenless and platform-agnostic: nothing here can fail to BIND, so the
    # only thing the lane could report is the drift.
    servers = {SERVER: {"command": "launcher-qa"}}

    assert mcp_server_issues(servers) == []
    codes = [issue.code for issue in mcp_server_issues(servers, include_template_drift=True)]
    assert codes == [ISSUE_MCP_TEMPLATE_DRIFT]


def test_readiness_reports_drift_without_calling_a_working_profile_broken(
    tmp_path, monkeypatch
):
    from agent_runtime import profile_context
    from agent_runtime.models import AgentPersona

    profile_home = tmp_path / "profiles" / "qa"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n", encoding="utf-8"
    )
    monkeypatch.setattr(profile_context, "profile_exists", lambda name: name == "qa")
    monkeypatch.setattr(profile_context, "get_profile_dir", lambda name: profile_home)

    readiness = profile_readiness_for_persona(
        AgentPersona(
            id="qa",
            display_name="QA",
            role="qa",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=["file"],
            system_prompt_path="personas/qa/system.md",
            hermes_profile="qa",
            skills=[],
            required_mcp_servers=[SERVER],
        )
    )

    # Drift is visible...
    assert [row["code"] for row in readiness["mcp_template_drift"]] == [
        ISSUE_MCP_TEMPLATE_DRIFT
    ]
    # ...and does NOT masquerade as a binding failure or downgrade the verdict.
    assert readiness["machine_root_issues"] == []
    assert readiness["readiness"] == "ready"
    assert ISSUE_MCP_TEMPLATE_DRIFT not in readiness["summary"]
