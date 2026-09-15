from __future__ import annotations

from pathlib import Path
from typing import Any


def resolve_persona_system_prompt_path(persona: Any) -> Path | None:
    """Resolve ``system_prompt_path`` once for execution and realm sync.

    Relative paths may be profile-owned custom prompts or repository-bundled
    role prompts.  Profile ownership wins when both exist; absolute paths stay
    explicit.  Callers receive only an existing regular file.
    """

    raw = str(getattr(persona, "system_prompt_path", "") or "").strip()
    if not raw:
        return None
    configured = Path(raw).expanduser()
    if configured.is_absolute():
        return configured if configured.is_file() else None

    candidates: list[Path] = []
    try:
        from .profile_context import resolve_persona_profile

        binding = resolve_persona_profile(persona)
        if binding.profile_home is not None:
            candidates.append(binding.profile_home / configured)
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parent.parent / configured)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None
