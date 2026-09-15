"""Downstream compact skill search over installed skills and the existing hub."""
import json
from typing import Any, Dict, List, Set
from tools import skills_tool as _st
from tools.registry import tool_error
from tools.skills_hub_search import create_source_router, unified_search


def _skill_identifier(skill: Dict[str, Any]) -> str:
    """Return the stable identifier a model can pass to ``skill_view``."""
    identifier = str(skill.get("identifier") or "").strip().strip("/")
    if identifier:
        return identifier
    name = str(skill.get("name") or "").strip()
    category = str(skill.get("category") or "").strip().strip("/")
    return f"{category}/{name}" if category else name


def _installed_skill_matches(skill: Dict[str, Any], query: str) -> bool:
    haystack = " ".join(
        str(value or "")
        for value in (
            skill.get("name"),
            skill.get("description"),
            skill.get("category"),
            " ".join(skill.get("tags") or []),
        )
    ).lower()
    return query.lower() in haystack


def _compact_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _compact_tags(value: Any, *, max_tags: int = 20, max_len: int = 64) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_compact_text(tag, max_len) for tag in value[:max_tags]]


def _compact_installed_skill_result(skill: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": _compact_text(skill.get("name", ""), _st.MAX_NAME_LENGTH),
        "identifier": _compact_text(_skill_identifier(skill), 256),
        "source": "installed",
        "description": _compact_text(skill.get("description", ""), _st.MAX_DESCRIPTION_LENGTH),
        "installed": True,
        "category": skill.get("category"),
        "tags": _compact_tags(skill.get("tags") or []),
    }


def _compact_remote_skill_result(meta: Any) -> Dict[str, Any]:
    result = {
        "name": _compact_text(getattr(meta, "name", ""), _st.MAX_NAME_LENGTH),
        "identifier": _compact_text(getattr(meta, "identifier", ""), 256),
        "source": _compact_text(getattr(meta, "source", ""), 64),
        "description": _compact_text(getattr(meta, "description", ""), _st.MAX_DESCRIPTION_LENGTH),
        "installed": False,
        "trust_level": _compact_text(getattr(meta, "trust_level", "community"), 64),
        "tags": _compact_tags(getattr(meta, "tags", []) or []),
    }
    repo = getattr(meta, "repo", None)
    path = getattr(meta, "path", None)
    if repo:
        result["repo"] = _compact_text(repo, 256)
    if path:
        result["path"] = _compact_text(path, 256)
    return result


def skill_search(
    query: str,
    source: str = "all",
    limit: int = 10,
    include_installed: bool = True,
    task_id: str = None,
) -> str:
    """Search installed skills and the Hermes Skills Hub without loading bodies."""
    try:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return tool_error("skill_search requires a non-empty query", success=False)

        normalized_source = str(source or "all").strip() or "all"
        allowed_sources = {
            "all",
            "installed",
            "official",
            "hermes-index",
            "skills-sh",
            "skills.sh",
            "well-known",
            "github",
            "clawhub",
            "claude-marketplace",
            "lobehub",
            "browse-sh",
        }
        if normalized_source not in allowed_sources:
            return tool_error(
                f"Unsupported skill search source '{normalized_source}'. "
                f"Use one of: {', '.join(sorted(allowed_sources))}",
                success=False,
            )

        capped_limit = max(1, min(int(limit or 10), 50))
        results: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        if include_installed or normalized_source == "installed":
            installed = [
                skill
                for skill in _st._find_all_skills()
                if _installed_skill_matches(skill, normalized_query)
            ]
            for skill in _st._sort_skills(installed):
                compact = _compact_installed_skill_result(skill)
                identifier = str(compact.get("identifier") or compact.get("name") or "")
                if identifier in seen:
                    continue
                seen.add(identifier)
                results.append(compact)
                if len(results) >= capped_limit:
                    break

        if normalized_source != "installed" and len(results) < capped_limit:
            hub_source = "all" if normalized_source in {"all", "installed"} else normalized_source
            if hub_source == "skills.sh":
                hub_source = "skills-sh"
            remote_limit = capped_limit - len(results)
            remote_results = unified_search(
                normalized_query,
                create_source_router(),
                source_filter=hub_source,
                limit=remote_limit,
            )
            for meta in remote_results:
                compact = _compact_remote_skill_result(meta)
                identifier = str(compact.get("identifier") or compact.get("name") or "")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                results.append(compact)
                if len(results) >= capped_limit:
                    break

        return json.dumps(
            {
                "success": True,
                "query": normalized_query,
                "source": normalized_source,
                "include_installed": include_installed,
                "limit": capped_limit,
                "results": results,
                "count": len(results),
                "hint": "Use skill_view(name=<identifier>) for installed results; use hermes skills install <identifier> for external hub results.",
            },
            ensure_ascii=False,
        )
    except Exception as e:
        return tool_error(str(e), success=False)
