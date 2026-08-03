"""The ``launcher_qa`` MCP block must be declared the same way in every profile.

Nine live profiles carry a ``launcher_qa`` block and they had split into two
variants: one setting ``STAGEC_LAUNCH_HELPER`` explicitly, one omitting it. The
omission was *silently* equivalent — the Launcher's ``launch_manager.dart``
falls back to the same helper path — which is precisely why it was debt: the two
blocks agreed only by coincidence of a fallback in another repo.

Operator ruling (2026-07-31): the explicit variant is canonical
(``machine_roots.CANONICAL_LAUNCHER_QA_MCP_SERVER``).

Executed 2026-08-01: the five variant-B profiles (backend-dev, gpt-launcher,
launcher-dev, launcher-qa, qa) were patched live, the expected-drift ledger they
occupied is gone, and the drift code was flipped from advisory to blocking.

This file has an unconditional synthetic profile-tree tripwire plus a separate
live-environment check. The synthetic tree is built from the canonical template
authority, not from a hand-maintained mirror, so CI always executes the drift
logic. The live case still checks every real profile when that tree is present
and skips honestly when it is not.

Fixing a config is an operator/live action against files this repo does not
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
    CANONICAL_LAUNCHER_QA_MCP_SERVER,
    ISSUE_MCP_TEMPLATE_DRIFT,
    canonical_mcp_server_template,
    canonical_mcp_server_yaml,
    machine_roots_cache_clear,
    mcp_server_issues,
    mcp_server_template_diffs,
    mcp_server_template_issues,
)
from agent_runtime.profile_readiness import (
    READINESS_MCP_ATTENTION,
    profile_readiness_for_persona,
)
from hermes_constants import get_default_hermes_root

SERVER = "launcher_qa"


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


def _launcher_qa_blocks(root: Path, *, missing_is_skip: bool) -> dict[str, Any]:
    if not root.is_dir():
        if missing_is_skip:
            pytest.skip(
                f"no live profile tree at {root} — synthetic coverage is green, but "
                "this live-environment check did not run"
            )
        pytest.fail(f"synthetic profile tree was not created: {root}")
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
        if missing_is_skip:
            pytest.skip(f"no live profile under {root} declares '{SERVER}'")
        pytest.fail(f"synthetic profile tree declares no '{SERVER}' block")
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


def _assert_launcher_qa_blocks_match(blocks: dict[str, Any]) -> None:
    drifted = [
        _patch_message(profile, diffs)
        for profile, cfg in blocks.items()
        if (diffs := mcp_server_template_diffs(SERVER, cfg))
    ]
    assert not drifted, (
        f"'{SERVER}' template drift across {len(drifted)} of {len(blocks)} profiles "
        f"({', '.join(sorted(blocks))}). The canonical block is "
        f"agent_runtime.machine_roots.CANONICAL_LAUNCHER_QA_MCP_SERVER; apply the "
        f"patch below by hand (this lane is report-only and never writes a config). "
        f"Do NOT re-add an expected-drift ledger:" + "".join(drifted)
    )


def _write_synthetic_profile_tree(root: Path, block: dict[str, Any]) -> None:
    profile = root / "profiles" / "synthetic-qa"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {SERVER: block}}, sort_keys=True),
        encoding="utf-8",
    )


def test_synthetic_profile_tree_executes_the_drift_tripwire_unconditionally(tmp_path):
    root = tmp_path / "hermes"
    _write_synthetic_profile_tree(root, _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER))

    blocks = _launcher_qa_blocks(root / "profiles", missing_is_skip=False)
    _assert_launcher_qa_blocks_match(blocks)


def test_synthetic_profile_tree_rejects_a_deliberately_drifted_block(tmp_path):
    root = tmp_path / "hermes"
    drifted = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    drifted["env"].pop("STAGEC_LAUNCH_HELPER")
    _write_synthetic_profile_tree(root, drifted)

    blocks = _launcher_qa_blocks(root / "profiles", missing_is_skip=False)
    with pytest.raises(AssertionError, match="env.STAGEC_LAUNCH_HELPER: missing"):
        _assert_launcher_qa_blocks_match(blocks)


def test_every_live_launcher_qa_block_matches_the_canonical_template(monkeypatch):
    """Environment checkpoint, separate from the unconditional CI tripwire."""

    blocks = _launcher_qa_blocks(
        _live_profiles_root(monkeypatch),
        missing_is_skip=True,
    )
    _assert_launcher_qa_blocks_match(blocks)


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


def test_template_issues_carry_the_typed_code_and_a_pasteable_hint():
    cfg = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    cfg["env"].pop("STAGEC_LAUNCH_HELPER")
    (issue,) = mcp_server_template_issues({SERVER: cfg})
    assert issue.code == ISSUE_MCP_TEMPLATE_DRIFT
    assert issue.field == f"mcp_servers.{SERVER}"
    assert "STAGEC_LAUNCH_HELPER" in issue.summary
    assert "Report-only" in issue.fix_hint
    assert canonical_mcp_server_yaml(SERVER) in issue.fix_hint
    assert "canonical_mcp_server_yaml(" not in issue.fix_hint


def test_template_issues_honour_the_only_filter():
    cfg = _plain(CANONICAL_LAUNCHER_QA_MCP_SERVER)
    cfg["env"].pop("STAGEC_LAUNCH_HELPER")
    assert mcp_server_template_issues({SERVER: cfg}, only=["other"]) == []
    assert len(mcp_server_template_issues({SERVER: cfg}, only=[SERVER])) == 1


# ── The lane ────────────────────────────────────────────────────────────────


def test_drift_rides_the_binding_lane_by_default():
    """No opt-in left: one lane, and every row on it is blocking."""

    # Tokenless and platform-agnostic: nothing here can fail to BIND, so the
    # only thing the lane can report is the drift — and it does, unasked.
    servers = {SERVER: {"command": "launcher-qa"}}

    assert [issue.code for issue in mcp_server_issues(servers)] == [
        ISSUE_MCP_TEMPLATE_DRIFT
    ]
    with pytest.raises(TypeError):
        mcp_server_issues(servers, include_template_drift=True)  # type: ignore[call-arg]


def _readiness_for_config(tmp_path, monkeypatch, config_yaml: str, *, required: list[str]):
    """Readiness for a persona bound to a throwaway profile carrying ``config_yaml``."""

    from agent_runtime import profile_context
    from agent_runtime.models import AgentPersona

    profile_home = tmp_path / "profiles" / "qa"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    monkeypatch.setattr(profile_context, "profile_exists", lambda name: name == "qa")
    monkeypatch.setattr(profile_context, "get_profile_dir", lambda name: profile_home)

    return profile_readiness_for_persona(
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
            required_mcp_servers=list(required),
        )
    )


def test_readiness_fails_loudly_on_drift_and_names_the_field(
    tmp_path, monkeypatch
):
    readiness = _readiness_for_config(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n",
        required=[SERVER],
    )

    # Drift rides the one issue list...
    (row,) = readiness["machine_root_issues"]
    assert row["code"] == ISSUE_MCP_TEMPLATE_DRIFT
    assert row["field"] == f"mcp_servers.{SERVER}"
    # ...it is BLOCKING...
    assert readiness["readiness"] == READINESS_MCP_ATTENTION
    # ...the verdict names the drifted field, not just "something is off"...
    assert "STAGEC_LAUNCH_HELPER" in readiness["summary"]
    # ...and the retired advisory channel is not quietly still there.
    assert "mcp_template_drift" not in readiness


def test_a_configured_but_unrequired_drifted_block_degrades_readiness(
    tmp_path, monkeypatch
):
    """The ledger item 10 widening, pinned — the case that was a runtime no-op.

    ARRANGE — the live snapshot lane's exact shape: the persona requires
    NOTHING (every snapshot agent's ``required_mcp_servers`` is empty on the
    live tree) while its profile DOES declare a drifted ``launcher_qa`` block.

    BEFORE (until 2026-08-01): readiness called
    ``mcp_server_issues(only=effective_required_mcp)``, and ``only`` was a
    filter — with an empty required list the subset handed to the checker was
    ``{}``, so the blocking drift check compared nothing, ``machine_root_issues``
    came back empty, and the verdict was a clean ``ready`` that said nothing
    about the configured block. The drift line was held solely by the CI data
    test at the top of this file.

    AFTER: ``required`` is a scope, not a filter — the configured block is
    validated because it has a canonical template, whoever requires it.
    """

    readiness = _readiness_for_config(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  launcher_qa:\n    command: launcher-qa\n",
        required=[],
    )

    assert readiness["effective_required_mcp_servers"] == []
    # Not a missing-server finding — that class is still required-scoped, and
    # nothing is required here.
    assert readiness["missing_mcp_servers"] == []
    (row,) = readiness["machine_root_issues"]
    assert row["code"] == ISSUE_MCP_TEMPLATE_DRIFT
    assert row["field"] == f"mcp_servers.{SERVER}"
    assert readiness["readiness"] == READINESS_MCP_ATTENTION
    assert "STAGEC_LAUNCH_HELPER" in readiness["summary"]


def test_an_unrequired_block_with_no_template_stays_out_of_scope(tmp_path, monkeypatch):
    """The widening is gated on "has a canonical template", not on "configured".

    An operator's own unrequired block — here one that cannot even bind, its
    root being unbound in this sandbox — is NOT this lane's business: nothing
    states a correct shape for it, and failing a persona that never asked for it
    would be a different ruling than the one taken.
    """

    readiness = _readiness_for_config(
        tmp_path,
        monkeypatch,
        "mcp_servers:\n  operator_probe:\n"
        "    command: ${roots.definitely_unbound_root}/probe\n",
        required=[],
    )

    assert readiness["machine_root_issues"] == []
    assert readiness["readiness"] == "ready"
