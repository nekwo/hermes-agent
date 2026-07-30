from __future__ import annotations

import re
from typing import Any



NO_PRODUCT_EDIT_RECIPE_IDS = frozenset(
    {
        "archive_button_cli_contract",
        "backend_contract_smoke",
        "harness_runtime_status_snapshot",
        "launcher_contract_smoke",
        "qa_release_verdict_smoke",
    }
)

_PRODUCT_EDIT_MARKERS = frozenset(
    {
        "add",
        "build",
        "change",
        "code edit",
        "edit",
        "fix",
        "implement",
        "modify",
        "patch",
        "polish",
        "redesign",
        "refactor",
        "render",
        "replace",
        "upgrade",
        "widget",
    }
)

_NO_PRODUCT_EDIT_MARKERS = (
    "no product edit",
    "no product edits",
    "no-product-edit",
    "no-product-edits",
    "no_product_edit",
    "no_product_edits",
    "no edit",
    "no edits",
    "proof-only",
    "proof only",
    "smoke only",
    "verification only",
)

_NEGATED_EDIT_SPAN_PATTERNS = (
    r"\bdo\s+not\b[^.;\n]*(?:\bedit\b|\bedits\b|\bedited\b|\bediting\b|\bpatch\b|\bpatches\b|\bpatched\b|\bpatching\b|\bmodify\b|\bmodifies\b|\bmodified\b|\bmodifying\b)[^.;\n]*",
    r"\bwithout\b[^.;\n]*(?:\bedit\b|\bedits\b|\bedited\b|\bediting\b|\bpatch\b|\bpatches\b|\bpatched\b|\bpatching\b|\bmodify\b|\bmodifies\b|\bmodified\b|\bmodifying\b)[^.;\n]*",
    r"\bno\b[^.;\n]*(?:\bedit\b|\bedits\b|\bedited\b|\bediting\b|\bpatch\b|\bpatches\b|\bpatched\b|\bpatching\b|\bmodify\b|\bmodifies\b|\bmodified\b|\bmodifying\b)[^.;\n]*",
    r"\bnot\b[^.;\n]*(?:\bedit\b|\bedits\b|\bedited\b|\bediting\b|\bpatch\b|\bpatches\b|\bpatched\b|\bpatching\b|\bmodify\b|\bmodifies\b|\bmodified\b|\bmodifying\b)[^.;\n]*",
)


def no_product_edit_recipe_id(recipe_id: str | None) -> bool:
    return str(recipe_id or "").strip() in NO_PRODUCT_EDIT_RECIPE_IDS


def no_product_edit_recipe_for_stage(stage: object | None) -> str | None:
    if stage is None:
        return None
    identity_sources = [
        str(getattr(stage, "id", "") or ""),
        str(getattr(stage, "title", "") or ""),
    ]
    for source in identity_sources:
        matches = _recipe_references(source)
        if len(matches) == 1:
            return matches[0]
    evidence_sources = [
        str(getattr(stage, "objective", "") or ""),
        " ".join(str(item) for item in (getattr(stage, "test_plan", []) or [])),
        " ".join(str(item) for item in (getattr(stage, "acceptance_criteria", []) or [])),
        " ".join(str(item) for item in (getattr(stage, "audit_notes", []) or [])),
        " ".join(str(item) for item in (getattr(stage, "corrections", []) or [])),
    ]
    seen: list[str] = []
    for source in evidence_sources:
        matches = _recipe_references(source)
        seen.extend(matches)
    unique = sorted(set(seen))
    return unique[0] if len(unique) == 1 else None
    return None


def stage_requires_product_edit(task: Task, stage: object | None = None) -> bool:
    """Best-effort contract classifier for proof routing.

    The Harness uses this as a safety gate, not as product semantics. It is
    intentionally conservative: explicit no-edit/smoke/contract stages are
    allowed to stay proof-only, while UI/backend implementation language plus
    product paths require a real patch/test loop before no-edit smoke recipes
    can be treated as useful evidence.
    """

    text = _combined_text(task, stage)
    if not text:
        return False
    if stage is not None and _typed_stage_declares_no_product_edit(stage):
        return False
    if stage is not None and no_product_edit_recipe_id(getattr(stage, "id", None)):
        return False
    stage_recipe_id = no_product_edit_recipe_for_stage(stage)
    stage_text = _stage_text(stage) if stage is not None else ""
    if stage is not None and getattr(stage, "requires_product_edit", None) is False and _explicit_no_product_edit(stage_text) and not _has_strong_edit_marker(stage_text):
        return False
    if stage_recipe_id and _explicit_no_product_edit(stage_text) and not _has_strong_edit_marker(stage_text):
        return False
    if _explicit_no_product_edit(text) and not _has_strong_edit_marker(text):
        return False
    if stage is not None and _looks_like_no_edit_contract_stage(stage) and not _has_strong_edit_marker(stage_text):
        return False
    if _has_product_path(task, stage) and _has_strong_edit_marker(text):
        return True
    if _has_ui_feature_language(text) and _has_strong_edit_marker(text):
        return True
    return False


def first_incomplete_product_edit_stage(task: Task, *, excluding_stage_id: str | None = None) -> object | None:
    excluded = str(excluding_stage_id or "").strip()
    for stage in []:
        if excluded and stage.id == excluded:
            continue
        status = getattr(stage, "status", None)
        status_value = status.value if hasattr(status, "value") else str(status or "")
        if status_value in {"ready_for_qa", "passed"}:
            continue
        if stage_requires_product_edit(task, stage):
            return stage
    return None


def no_product_edit_recipe_conflicts_with_stage(task: Task, stage: object | None, recipe_id: str | None) -> bool:
    safe_recipe_id = str(recipe_id or "").strip()
    if not no_product_edit_recipe_id(safe_recipe_id):
        return False
    if stage is not None and _typed_stage_declares_no_product_edit(stage):
        return False
    if stage is not None and str(getattr(stage, "id", "") or "").strip() == safe_recipe_id:
        return False
    if safe_recipe_id == no_product_edit_recipe_for_stage(stage) and not stage_requires_product_edit(task, stage):
        return False
    return stage_requires_product_edit(task, stage)


def stage_is_committed_verification_gate(task: Task, stage: object | None) -> bool:
    """Return true when an implementation-shaped stage should go straight to proof.

    Live Neko can correctly preserve a Hermes code-change as an implementation
    stage while also saying the fix is already committed and only focused proof
    is required. In that shape, broad Dev inspection is waste; the Harness
    should steer Dev to request the named proof command immediately.
    """

    if stage is None or not _stage_has_command_gate(stage):
        return False
    text = _combined_text(task, stage)
    if not any(marker in text for marker in ("verify", "verification", "proof", "inspect")):
        return False
    if "committed" in text or "already landed" in text:
        return True
    if _explicit_no_product_edit(text) and any(marker in text for marker in ("focused test", "focused proof", "command proof")):
        return True
    return False


def extract_single_known_stage_reference(task: Task, *, source_stage_id: str, text: str) -> str | None:
    haystack = str(text or "")
    if not haystack:
        return None
    matches = []
    for stage in []:
        sid = str(stage.id or "").strip()
        if not sid or sid == source_stage_id:
            continue
        if sid in haystack:
            matches.append(sid)
    unique = sorted(set(matches))
    if len(unique) != 1:
        return None
    lowered = haystack.lower()
    route_markers = (
        "advance",
        "back to",
        "current stage",
        "return",
        "route",
        "set",
        "switch",
        "target",
    )
    if any(marker in lowered for marker in route_markers):
        return unique[0]
    return None


def _combined_text(task: Task, stage: object | None) -> str:
    values: list[str] = [
        str(getattr(task, "title", "") or ""),
        str(getattr(task, "description", "") or ""),
        " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
        " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
        " ".join(str(item) for item in (getattr(task, "affected_repos", []) or [])),
    ]
    if stage is not None:
        values.append(_stage_text(stage))
    return _normalize_text(" ".join(values))


def _stage_text(stage: object) -> str:
    return _normalize_text(_stage_raw_text(stage))


def _typed_stage_declares_no_product_edit(stage: object) -> bool:
    kind = str(getattr(stage, "kind", "") or "").strip()
    requires_product_edit = getattr(stage, "requires_product_edit", None)
    if kind == "proof_only":
        return requires_product_edit is not True
    if requires_product_edit is False and bool(str(getattr(stage, "proof_recipe_id", "") or "").strip()):
        return True
    return False


def _stage_raw_text(stage: object) -> str:
    return " ".join(
        [
            str(getattr(stage, "id", "") or ""),
            str(getattr(stage, "title", "") or ""),
            str(getattr(stage, "objective", "") or ""),
            " ".join(str(item) for item in (getattr(stage, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(stage, "affected_paths", []) or [])),
            " ".join(str(item) for item in (getattr(stage, "test_plan", []) or [])),
        ]
    )


def _normalize_text(value: str) -> str:
    return str(value or "").lower().replace("_", " ").replace("-", " ")


def _contains_recipe_reference(raw_text: str, normalized_text: str, recipe_id: str) -> bool:
    raw = str(raw_text or "").lower()
    if re.search(rf"(?<![a-z0-9_.-]){re.escape(recipe_id.lower())}(?![a-z0-9_.-])", raw):
        return True
    normalized_id = recipe_id.lower().replace("_", " ").replace("-", " ")
    return bool(normalized_id and normalized_id in normalized_text)


def _recipe_references(text: str) -> list[str]:
    raw = str(text or "")
    normalized = _normalize_text(raw)
    return [recipe_id for recipe_id in sorted(NO_PRODUCT_EDIT_RECIPE_IDS) if _contains_recipe_reference(raw, normalized, recipe_id)]


def _explicit_no_product_edit(text: str) -> bool:
    if any(marker in text for marker in _NO_PRODUCT_EDIT_MARKERS):
        return True
    return any(re.search(pattern, text) for pattern in _NEGATED_EDIT_SPAN_PATTERNS)


def _has_strong_edit_marker(text: str) -> bool:
    scrubbed = str(text or "")
    for marker in _NO_PRODUCT_EDIT_MARKERS:
        scrubbed = scrubbed.replace(marker, " ")
    for pattern in _NEGATED_EDIT_SPAN_PATTERNS:
        scrubbed = re.sub(pattern, " ", scrubbed)
    scrubbed = re.sub(r"[^a-z0-9_.]+", " ", scrubbed)
    words = set(scrubbed.split())
    if words.intersection(_PRODUCT_EDIT_MARKERS):
        return True
    return any(marker in scrubbed for marker in ("code edit", "product edit", "flutter widget", "dart widget"))


def _has_ui_feature_language(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "bubble",
            "component",
            "event feed",
            "frontend",
            "launcher",
            "mission control",
            "terminal",
            "visual",
        )
    )


def _has_product_path(task: Task, stage: object | None) -> bool:
    paths: list[Any] = []
    if stage is not None:
        paths.extend(getattr(stage, "affected_paths", []) or [])
    paths.extend(getattr(task, "affected_repos", []) or [])
    text = " ".join(str(item) for item in paths).lower().replace("\\", "/")
    return any(
        marker in text
        for marker in (
            "lib/",
            "src/",
            "test/",
            ".dart",
            ".py",
            "eternialauncher",
            "eterniabackend",
        )
    )


def _looks_like_no_edit_contract_stage(stage: object) -> bool:
    text = _stage_text(stage)
    return any(marker in text for marker in ("contract smoke", "smoke", "proof only", "verify", "verification"))


def _stage_has_command_gate(stage: object) -> bool:
    return any(_looks_like_command(item) for item in getattr(stage, "test_plan", []) or [])


def _looks_like_command(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(
        (
            "flutter ",
            "dart ",
            "python ",
            "py ",
            "pytest",
            "powershell",
            "cmd ",
            "npm ",
            "pnpm ",
            "yarn ",
            "node ",
            ".\\",
            "./",
        )
    )
