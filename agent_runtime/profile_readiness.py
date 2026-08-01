from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_cli.auth import AuthError
from hermes_cli.runtime_environment import missing_runtime_packages_for
from hermes_cli.runtime_provider import resolve_runtime_provider

from .machine_roots import (
    contains_path_tokens,
    mcp_server_issues,
    path_token_issues,
)
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

_RUNTIME_PROVIDER_RESOLVER = resolve_runtime_provider

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


def profile_readiness_for_persona(
    persona, *, task=None, stage=None, skill_resolver=None
) -> dict[str, Any]:
    binding = resolve_persona_profile(persona)
    issues: list[tuple[str, str]] = []
    missing_mcp: list[str] = []
    missing_skills: list[str] = []
    skill_hash_mismatches: list[str] = []
    skill_resolutions: list[dict[str, Any]] = []
    machine_root_issues: list[dict[str, Any]] = []
    effective_required_mcp = _effective_required_mcp_servers(persona, task=task, stage=stage)
    machine_root_issues.extend(_persona_path_token_issues(persona))

    if binding.readiness != READINESS_READY:
        issues.append((binding.readiness, binding.summary))
    elif binding.profile_home is not None:
        try:
            with persona_profile_context(binding):
                skill_resolutions = _resolve_skill_names(
                    list(persona.skills), skill_resolver=skill_resolver
                )
                missing_skills = _missing_skill_ids(skill_resolutions)
                skill_hash_mismatches = harness_skill_hash_mismatches(list(persona.skills), hermes_home=binding.profile_home)
                cfg_path = binding.profile_home / "config.yaml"
                raw = cached_yaml_file(cfg_path, default={}) or {}
                configured_mcp = _configured_mcp_server_names(raw or {})
                missing_mcp = [name for name in effective_required_mcp if name not in configured_mcp]
                # A server that IS configured but whose machine binding cannot
                # resolve here (unbound logical root, root bound to a path that
                # no longer exists, gated to another OS) is not "present" — it
                # would be dropped before spawn. Report the typed reason rather
                # than letting the agent discover a dead path at tool time.
                #
                # The same lane also carries canonical-template drift, which is
                # blocking as of 2026-08-01: a block that matches the template
                # only up to a fallback living in another repo is a declaration
                # nobody can trust, and it fails here loudly rather than at tool
                # time. Report-only — readiness never rewrites a config; the
                # issue's fix_hint carries the pasteable canonical block.
                #
                # SCOPE (widened 2026-08-01, ledger item 10). ``required`` is
                # passed as a scope, not a filter: the lane validates the
                # required names AND every configured block that has a canonical
                # template. The two classes answer different questions —
                # "this DECLARATION is wrong" is a defect whoever requires it,
                # while "this server is MISSING" (``missing_mcp`` above) only
                # means anything against a requirement and stays required-scoped.
                # Scoping both to ``required`` is what made the blocking drift
                # check evaluate nothing on the live snapshot lane, where every
                # persona's required list is empty.
                machine_root_issues.extend(
                    issue.row()
                    for issue in mcp_server_issues(
                        _configured_mcp_servers(raw or {}),
                        required=effective_required_mcp,
                    )
                )
                runtime_issue = _runtime_dependency_issue(persona)
                if runtime_issue:
                    issues.append(runtime_issue)
                provider_issue = _provider_issue(persona)
                if provider_issue:
                    issues.append(provider_issue)
        except Exception as exc:  # pragma: no cover - defensive, covered by behavior tests with monkeypatch
            issues.append((READINESS_CONFIG_ERROR, f"Profile config read failed: {type(exc).__name__}"))
    else:
        skill_resolutions = _resolve_skill_names(
            list(persona.skills), skill_resolver=skill_resolver
        )
        missing_skills = _missing_skill_ids(skill_resolutions)
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
    for row in machine_root_issues:
        issues.append(
            (
                READINESS_MCP_ATTENTION,
                f"{row['summary']} — fix: {row['fix_hint']}" if row.get("fix_hint") else row["summary"],
            )
        )

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
        # Binding failures AND canonical-template drift. One list because both
        # now mean the same thing to an operator: this MCP declaration is not
        # usable as written. (The separate advisory ``mcp_template_drift`` key
        # is retired with the advisory class it existed for.)
        "machine_root_issues": machine_root_issues,
    }


def _dominant_issue(issues: list[tuple[str, str]]) -> tuple[str, str]:
    if not issues:
        return READINESS_READY, "ready"
    return max(issues, key=lambda item: _SEVERITY.get(item[0], 0))


# Provider readiness TTL memo.
#
# ``resolve_runtime_provider`` selects a credential and, when a token is near
# expiry, performs a LIVE OAuth refresh (``refresh_codex_oauth_pure``, ~2.8s and
# the dominant 8s↔20s snapshot variance driver, measured 2026-07-23). A snapshot
# is a read-only observability build and must NOT do a network round-trip per
# agent per build. Cleanly separating "inspect cached token state" from "refresh"
# lives deep in the auth credential pool and would risk changing auth semantics
# (explicitly out of scope), so this caches the READINESS ANSWER with a short TTL
# instead: the same resolve (and, on the TTL boundary, the same possible refresh)
# runs at most once per 60s per (provider, model), and a genuinely expired
# credential surfaces the SAME ``auth_attention`` answer it does today — just
# without a per-build refresh. The memo also keys on the resolver's identity, so
# a monkeypatched/hot-swapped ``resolve_runtime_provider`` invalidates the entry
# immediately (same pattern as ``_profile_template_memo``/``_installed_skill_catalog``).
#
# The key MUST carry the active profile/auth scope, not just (provider, model):
# ``_provider_issue`` runs inside ``persona_profile_context``, which diverts the
# process-global HERMES_HOME/HERMES_AUTH_HOME so ``resolve_runtime_provider``
# reads PER-PROFILE config and secrets. Keyed on (provider, model) alone, the
# first profile's verdict leaks to every other profile requesting the same pair
# within the TTL — one build could show agent B "ready" on agent A's credentials
# (or falsely flag A with B's auth_attention). Reading the env INSIDE this call
# reflects the caller's active profile context.
_PROVIDER_ISSUE_TTL_SECONDS = 60.0
_provider_issue_memo: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def _provider_issue_cache_clear() -> None:
    """Test hook — drop the provider readiness TTL memo."""
    _provider_issue_memo.clear()


def _provider_issue(persona) -> tuple[str, str] | None:
    provider = getattr(persona, "provider", None)
    model = getattr(persona, "model", None)
    if not provider and not model:
        return None
    import os
    import time

    key = (
        os.environ.get("HERMES_HOME") or "",
        os.environ.get("HERMES_AUTH_HOME") or "",
        str(provider or ""),
        str(model or ""),
    )
    fn = resolve_runtime_provider
    now = time.monotonic()
    entry = _provider_issue_memo.get(key)
    if (
        entry is not None
        and entry["fn"] is fn
        and now - entry["at"] < _PROVIDER_ISSUE_TTL_SECONDS
    ):
        return entry["issue"]
    issue = _compute_provider_issue(fn, provider, model)
    _provider_issue_memo[key] = {"at": now, "fn": fn, "issue": issue}
    return issue


def _compute_provider_issue(resolver, provider, model) -> tuple[str, str] | None:
    if resolver is _RUNTIME_PROVIDER_RESOLVER and provider == "openai-codex":
        return _pooled_provider_issue(provider)
    try:
        resolver(requested=provider, target_model=model)
    except AuthError as exc:
        return (READINESS_AUTH_ATTENTION, _safe_provider_summary(str(exc)))
    except Exception as exc:
        return (READINESS_CONFIG_ERROR, f"Provider readiness check failed: {type(exc).__name__}")
    return None


def _pooled_provider_issue(provider: str) -> tuple[str, str] | None:
    """Inspect cached Codex credentials without refreshing during a snapshot."""

    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        entry = pool.peek()
        if entry is None or not (
            getattr(entry, "runtime_api_key", None)
            or getattr(entry, "access_token", None)
        ):
            return (
                READINESS_AUTH_ATTENTION,
                "Provider credential attention required",
            )
    except Exception as exc:
        return (
            READINESS_CONFIG_ERROR,
            f"Provider readiness check failed: {type(exc).__name__}",
        )
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


def _configured_mcp_servers(raw: dict[str, Any]) -> dict[str, Any]:
    """Merged ``mcp_servers`` map across the three accepted config spellings."""

    merged: dict[str, Any] = {}
    for key_path in (("mcp", "servers"), ("mcp_servers",), ("mcpServers",)):
        node: Any = raw
        for key in key_path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict):
            for name, cfg in node.items():
                merged[str(name)] = cfg
    return merged


def _configured_mcp_server_names(raw: dict[str, Any]) -> set[str]:
    return set(_configured_mcp_servers(raw))


def declared_mcp_server_names(persona) -> list[str]:
    """Every MCP server this persona DECLARES — required plus profile-configured.

    ``required_mcp_servers`` is the persona's explicit dependency; the profile's
    ``mcp_servers`` config block is the surface it would actually be handed on a
    lane that runs discovery. Both are declarations, and a lane that registers
    neither drops both, so the capability accounting in ``mcp_lane`` reads the
    union.

    A persona with no bound profile contributes only its ``required_mcp_servers``:
    it inherits whatever profile the process happens to be running under, and
    attributing the ambient operator config to it would make this probe answer
    differently depending on ``HERMES_HOME`` — the exact class of lie this
    module exists to retire.

    Cheap by construction: the profile binding is path arithmetic and the config
    read is the same mtime-cached parse ``profile_readiness_for_persona`` already
    performs. Never raises — a declaration probe must not be able to break a
    visibility resolve.
    """

    names = set(_effective_required_mcp_servers(persona))
    try:
        binding = resolve_persona_profile(persona)
        if binding.profile_home is not None:
            raw = cached_yaml_file(binding.profile_home / "config.yaml", default={}) or {}
            names.update(_configured_mcp_server_names(raw))
    except Exception:  # pragma: no cover - defensive; declaration probe is best-effort
        pass
    return sorted(names)


def _persona_path_token_issues(persona) -> list[dict[str, Any]]:
    """Typed issues for persona path fields whose tokens never expanded.

    ``agent_runtime.config`` expands ``${roots.…}`` at load time; a token that
    is still literal here means resolution failed, and the persona would
    otherwise run against a path that cannot exist. Surfacing it as readiness
    keeps the failure visible instead of degrading into a silent no-workdir.
    """

    scope = getattr(persona, "repo_scope", None)
    if not contains_path_tokens(scope):
        return []
    persona_id = str(getattr(persona, "id", "") or "persona")
    return [
        issue.row()
        for issue in path_token_issues(
            scope, field=f"agent_runtime.personas.{persona_id}.repo_scope"
        )
    ]


def _resolve_skill_names(
    skill_names: list[str], *, skill_resolver=None
) -> list[dict[str, Any]]:
    from agent.skill_utils import (
        resolve_skills,
        skill_package_content_hash,
        skill_runtime_compatibility,
    )
    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    from .skill_install import harness_skill_source

    cleaned = [name for name in (str(item).strip() for item in skill_names) if name]
    if not cleaned:
        return []
    # ONE registry walk for every assigned name. ``resolve_skills`` is the
    # batched form of ``resolve_skill`` (same authoritative resolver semantics,
    # same per-name candidates/status) — the per-name loop below was measured at
    # 102 recursive rglob/scandir walks × ~104ms across one snapshot core
    # (2026-07-23). Iterate over ``cleaned`` (not the deduped resolver keys) so
    # a name repeated in ``persona.skills`` still yields one row per occurrence,
    # exactly as the old per-name loop did.
    resolutions = (
        skill_resolver.resolve(cleaned)
        if skill_resolver is not None
        else resolve_skills(cleaned)
    )
    rows: list[dict[str, Any]] = []
    for clean in cleaned:
        resolution = resolutions.get(clean)
        if resolution is None:
            continue
        selected = (
            resolution.candidates[0] if len(resolution.candidates) == 1 else None
        )
        standard = skill_runtime_compatibility(
            selected, surface="mission_chat", root_node_mode=False
        )
        root_node = skill_runtime_compatibility(
            selected, surface="mission_worker", root_node_mode=True
        )
        installed_hash = None
        if selected:
            installed_hash = (
                skill_resolver.content_hash(selected)
                if skill_resolver is not None
                else skill_package_content_hash(selected.skill_dir, selected.skill_md)
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
                "content_hash": (
                    skill_resolver.content_hash(candidate)
                    if skill_resolver is not None
                    else skill_package_content_hash(
                        candidate.skill_dir, candidate.skill_md
                    )
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


def _missing_skill_ids(skill_resolutions: list[dict[str, Any]]) -> list[str]:
    """Missing-skill ids derived from already-resolved rows.

    Single source of truth for "missing" (the resolver's ``status == 'missing'``)
    so ``profile_readiness_for_persona`` derives the missing set from the rows it
    already computed instead of walking the resolver a second time.
    """

    return [row["skill_id"] for row in skill_resolutions if row["status"] == "missing"]


def _missing_skill_names(
    skill_names: list[str], *, skill_root: Path | None = None
) -> list[str]:
    """Compatibility wrapper backed by the canonical effective resolver."""

    del skill_root
    return _missing_skill_ids(_resolve_skill_names(skill_names))


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
