from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from hermes_cli.auth import AuthError
from hermes_cli.runtime_environment import missing_runtime_packages_for
from hermes_cli.runtime_provider import resolve_runtime_provider

from .profile_context import persona_profile_context, resolve_persona_profile
from .skill_install import harness_skill_hash_mismatches, harness_skill_installed_ok

READINESS_READY = "ready"
READINESS_MISSING_PROFILE = "missing_profile"
READINESS_CONFIG_ERROR = "config_error"
READINESS_AUTH_ATTENTION = "auth_attention"
READINESS_MCP_ATTENTION = "mcp_attention"
READINESS_MISSING_SKILL = "missing_skill"
READINESS_SKILL_HASH_MISMATCH = "skill_hash_mismatch"
READINESS_RUNTIME_DEPENDENCY_MISSING = "runtime_dependency_missing"

_SEVERITY = {
    READINESS_MISSING_PROFILE: 60,
    READINESS_RUNTIME_DEPENDENCY_MISSING: 50,
    READINESS_CONFIG_ERROR: 40,
    READINESS_AUTH_ATTENTION: 30,
    READINESS_MCP_ATTENTION: 20,
    READINESS_SKILL_HASH_MISMATCH: 15,
    READINESS_MISSING_SKILL: 10,
    READINESS_READY: 0,
}


def profile_readiness_for_persona(persona, *, task=None, stage=None) -> dict[str, Any]:
    binding = resolve_persona_profile(persona)
    issues: list[tuple[str, str]] = []
    missing_mcp: list[str] = []
    missing_skills: list[str] = []
    skill_hash_mismatches: list[str] = []
    effective_required_mcp = _effective_required_mcp_servers(persona, task=task, stage=stage)

    if binding.readiness != READINESS_READY:
        issues.append((binding.readiness, binding.summary))
    elif binding.profile_home is not None:
        try:
            with persona_profile_context(binding):
                missing_skills = _missing_skill_names(list(persona.skills), skill_root=binding.profile_home / "skills")
                skill_hash_mismatches = harness_skill_hash_mismatches(list(persona.skills), hermes_home=binding.profile_home)
                missing_skills = [name for name in missing_skills if not harness_skill_installed_ok(name, hermes_home=binding.profile_home)]
                cfg_path = binding.profile_home / "config.yaml"
                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
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
        missing_skills = _missing_skill_names(list(persona.skills))
        skill_hash_mismatches = harness_skill_hash_mismatches(list(persona.skills))
        missing_skills = [name for name in missing_skills if not harness_skill_installed_ok(name)]
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


def _missing_skill_names(skill_names: list[str], *, skill_root: Path | None = None) -> list[str]:
    from agent.skill_commands import _load_skill_payload

    missing: list[str] = []
    for name in skill_names:
        clean = str(name).strip()
        if not clean:
            continue
        if skill_root is not None:
            if not _skill_exists_in_tree(clean, skill_root):
                missing.append(clean)
            continue
        if _load_skill_payload(clean, task_id=None) is None:
            missing.append(clean)
    return missing


def _skill_exists_in_tree(skill_name: str, skill_root: Path) -> bool:
    if not skill_root.exists():
        return False
    normalized = skill_name.strip().strip("/\\")
    if not normalized:
        return False
    direct = skill_root / Path(normalized) / "SKILL.md"
    if direct.exists():
        return True
    wanted = normalized.rsplit("/", 1)[-1]
    for skill_md in skill_root.rglob("SKILL.md"):
        if ".archive" in skill_md.parts:
            continue
        if skill_md.parent.name == wanted:
            return True
        if _skill_frontmatter_name(skill_md) == wanted:
            return True
    return False


def _skill_frontmatter_name(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        raw = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    return str(name).strip() if name else None


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
