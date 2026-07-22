from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_cli.auth import AuthError
from hermes_cli.runtime_environment import missing_runtime_packages_for
from hermes_cli.runtime_provider import resolve_runtime_provider

from .parse_cache import cached_yaml_file
from .profile_context import persona_profile_context, resolve_persona_profile
from .skill_install import harness_skill_hash_mismatches

READINESS_READY = "ready"
READINESS_MISSING_PROFILE = "missing_profile"
READINESS_CONFIG_ERROR = "config_error"
READINESS_AUTH_ATTENTION = "auth_attention"
READINESS_MCP_ATTENTION = "mcp_attention"
READINESS_MISSING_SKILL = "missing_skill"
READINESS_SKILL_COLLISION = "skill_collision"
READINESS_SKILL_INVALID_SOURCE = "skill_invalid_source"
READINESS_SKILL_HASH_MISMATCH = "skill_hash_mismatch"
READINESS_RUNTIME_DEPENDENCY_MISSING = "runtime_dependency_missing"

_SEVERITY = {
    READINESS_MISSING_PROFILE: 60,
    READINESS_RUNTIME_DEPENDENCY_MISSING: 50,
    READINESS_CONFIG_ERROR: 40,
    READINESS_AUTH_ATTENTION: 30,
    READINESS_MCP_ATTENTION: 20,
    READINESS_SKILL_HASH_MISMATCH: 15,
    READINESS_SKILL_COLLISION: 16,
    READINESS_SKILL_INVALID_SOURCE: 17,
    READINESS_MISSING_SKILL: 10,
    READINESS_READY: 0,
}


def profile_readiness_for_persona(persona, *, task=None, stage=None) -> dict[str, Any]:
    binding = resolve_persona_profile(persona)
    issues: list[tuple[str, str]] = []
    missing_mcp: list[str] = []
    missing_skills: list[str] = []
    skill_hash_mismatches: list[str] = []
    skill_resolutions: list[dict[str, Any]] = []
    effective_required_mcp = _effective_required_mcp_servers(persona, task=task, stage=stage)

    if binding.readiness != READINESS_READY:
        issues.append((binding.readiness, binding.summary))
    elif binding.profile_home is not None:
        try:
            with persona_profile_context(binding):
                skill_resolutions = _resolve_skill_names(list(persona.skills))
                missing_skills = _missing_skill_names(list(persona.skills))
                skill_hash_mismatches = harness_skill_hash_mismatches(list(persona.skills), hermes_home=binding.profile_home)
                cfg_path = binding.profile_home / "config.yaml"
                raw = cached_yaml_file(cfg_path, default={}) or {}
                configured_mcp = _configured_mcp_server_names(raw or {})
                missing_mcp = [name for name in effective_required_mcp if name not in configured_mcp]
                runtime_issue = _runtime_dependency_issue(persona)
                if runtime_issue:
                    issues.append(runtime_issue)
                provider_issue = _provider_issue(persona)
                if provider_issue:
                    issues.append(provider_issue)
        except Exception as exc:  # pragma: no cover - defensive, covered by behavior tests with monkeypatch
            issues.append((READINESS_CONFIG_ERROR, f"Profile config read failed: {type(exc).__name__}"))
    else:
        skill_resolutions = _resolve_skill_names(list(persona.skills))
        missing_skills = _missing_skill_names(list(persona.skills))
        skill_hash_mismatches = harness_skill_hash_mismatches(list(persona.skills))
        if effective_required_mcp:
            missing_mcp = list(effective_required_mcp)
        runtime_issue = _runtime_dependency_issue(persona)
        if runtime_issue:
            issues.append(runtime_issue)
        provider_issue = _provider_issue(persona)
        if provider_issue:
            issues.append(provider_issue)

    if missing_skills:
        issues.append((READINESS_MISSING_SKILL, f"Missing skills: {', '.join(missing_skills)}"))
    collisions = [row["skill_id"] for row in skill_resolutions if row["status"] == "collision"]
    if collisions:
        issues.append((READINESS_SKILL_COLLISION, f"Skill collisions: {', '.join(collisions)}"))
    invalid_sources = [
        row["skill_id"]
        for row in skill_resolutions
        if row["status"] == "invalid_source"
    ]
    if invalid_sources:
        issues.append(
            (
                READINESS_SKILL_INVALID_SOURCE,
                "Harness skills must resolve from shared_core: "
                + ", ".join(invalid_sources),
            )
        )
    if skill_hash_mismatches:
        issues.append((READINESS_SKILL_HASH_MISMATCH, f"Skill hash mismatch: {', '.join(skill_hash_mismatches)}"))
    if missing_mcp:
        issues.append((READINESS_MCP_ATTENTION, f"Missing MCP servers: {', '.join(missing_mcp)}"))

    readiness, summary = _dominant_issue(issues)
    return {
        "readiness": readiness,
        "summary": summary,
        "hermes_profile": binding.hermes_profile,
        "skills": list(persona.skills),
        "missing_skills": missing_skills,
        "skill_hash_mismatches": skill_hash_mismatches,
        "skill_resolutions": skill_resolutions,
        "required_mcp_servers": list(persona.required_mcp_servers),
        "effective_required_mcp_servers": list(effective_required_mcp),
        "missing_mcp_servers": missing_mcp,
    }


def _dominant_issue(issues: list[tuple[str, str]]) -> tuple[str, str]:
    if not issues:
        return READINESS_READY, "ready"
    return max(issues, key=lambda item: _SEVERITY.get(item[0], 0))


def _provider_issue(persona) -> tuple[str, str] | None:
    if not getattr(persona, "provider", None) and not getattr(persona, "model", None):
        return None
    try:
        resolve_runtime_provider(requested=persona.provider, target_model=persona.model)
    except AuthError as exc:
        return (READINESS_AUTH_ATTENTION, _safe_provider_summary(str(exc)))
    except Exception as exc:
        return (READINESS_CONFIG_ERROR, f"Provider readiness check failed: {type(exc).__name__}")
    return None


def _runtime_dependency_issue(persona) -> tuple[str, str] | None:
    missing = missing_runtime_packages_for(
        provider=getattr(persona, "provider", None),
        api_mode=getattr(persona, "api_mode", None),
        model=getattr(persona, "model", None),
    )
    if not missing:
        return None
    return (READINESS_RUNTIME_DEPENDENCY_MISSING, f"Missing runtime packages: {', '.join(missing)}")


def _safe_provider_summary(message: str) -> str:
    text = str(message or "provider credential attention required")
    for unsafe in ("api_key", "API key", "access_token", "refresh_token", "Authorization"):
        text = text.replace(unsafe, "credential")
    return text


def _configured_mcp_server_names(raw: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key_path in (("mcp", "servers"), ("mcp_servers",), ("mcpServers",)):
        node: Any = raw
        for key in key_path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            names.update(str(name) for name in node.keys())
    return names


def _resolve_skill_names(skill_names: list[str]) -> list[dict[str, Any]]:
    from agent.skill_utils import (
        resolve_skill,
        skill_package_content_hash,
        skill_runtime_compatibility,
    )
    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    from .skill_install import harness_skill_source

    rows: list[dict[str, Any]] = []
    for name in skill_names:
        clean = str(name).strip()
        if not clean:
            continue
        resolution = resolve_skill(clean)
        selected = (
            resolution.candidates[0] if len(resolution.candidates) == 1 else None
        )
        standard = skill_runtime_compatibility(
            selected, surface="mission_chat", root_node_mode=False
        )
        root_node = skill_runtime_compatibility(
            selected, surface="mission_worker", root_node_mode=True
        )
        installed_hash = (
            skill_package_content_hash(selected.skill_dir, selected.skill_md)
            if selected
            else None
        )
        expected_hash = None
        if clean in CANONICAL_SHARED_SKILL_IDS:
            expected_manifest = harness_skill_source(clean)
            if expected_manifest.is_file():
                expected_hash = skill_package_content_hash(
                    expected_manifest.parent, expected_manifest
                )
        candidate_receipts = [
            {
                "source_kind": candidate.source_kind,
                "content_hash": skill_package_content_hash(
                    candidate.skill_dir, candidate.skill_md
                ),
            }
            for candidate in resolution.candidates
        ]
        rows.append(
            {
                "skill_id": clean,
                "status": resolution.status,
                "source_kind": selected.source_kind if selected else None,
                "content_hash": installed_hash,
                "installed_hash": installed_hash,
                "expected_hash": expected_hash,
                "hash_matches_expected": (
                    installed_hash == expected_hash
                    if expected_hash is not None and installed_hash is not None
                    else None
                ),
                "loadable": bool(
                    resolution.status == "resolved"
                    and (standard.get("compatible") or root_node.get("compatible"))
                ),
                "loadability": {
                    "mission_chat": standard,
                    "root_node": root_node,
                },
                "candidate_count": len(resolution.candidates),
                "candidates": candidate_receipts,
            }
        )
    return rows


def _missing_skill_names(
    skill_names: list[str], *, skill_root: Path | None = None
) -> list[str]:
    """Compatibility wrapper backed by the canonical effective resolver."""

    del skill_root
    return [
        row["skill_id"]
        for row in _resolve_skill_names(skill_names)
        if row["status"] == "missing"
    ]


def _effective_required_mcp_servers(persona, *, task=None, stage=None) -> list[str]:
    effective = list(getattr(persona, "required_mcp_servers", []) or [])
    if getattr(persona, "role", "") == "qa" and _visual_proof_required(task, stage) and "launcher_qa" not in effective:
        effective.append("launcher_qa")
    return effective


def _visual_proof_required(task, stage=None) -> bool:
    if getattr(task, "requires_visual_proof", False):
        return True
    if getattr(stage, "requires_visual_proof", False):
        return True
    text_parts: list[str] = []
    for value in (
        getattr(task, "title", None),
        getattr(task, "description", None),
        getattr(stage, "title", None),
        getattr(stage, "objective", None),
    ):
        if value:
            text_parts.append(str(value).lower())
    haystack = " ".join(text_parts)
    return any(marker in haystack for marker in ("mission control", "screenshot", "video", "visual", "stage c", "mcp"))
