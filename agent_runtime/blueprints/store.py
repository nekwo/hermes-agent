from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .schema import Blueprint, load_blueprint


class BlueprintStore:
    """Filesystem-backed loader for Agent Runtime blueprints."""

    def __init__(self, roots: Iterable[str | Path] | None = None):
        default_root = Path(__file__).resolve().parent
        self.roots = [Path(root) for root in roots] if roots is not None else [default_root]

    def list(self) -> list[Blueprint]:
        seen: set[str] = set()
        blueprints: list[Blueprint] = []
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted([*root.glob("*.yaml"), *root.glob("*.yml"), *root.glob("*.json")]):
                try:
                    bp = load_blueprint(path)
                except Exception:
                    continue
                if bp.id in seen:
                    continue
                seen.add(bp.id)
                blueprints.append(bp)
        return blueprints

    def get(self, id_or_path: str | Path) -> Blueprint:
        text = str(id_or_path)
        candidate = Path(text)
        if candidate.exists():
            return load_blueprint(candidate)
        for root in self.roots:
            for suffix in (".yaml", ".yml", ".json"):
                path = root / f"{text}{suffix}"
                if path.exists():
                    return load_blueprint(path)
        available = ", ".join(bp.id for bp in self.list()) or "none"
        raise FileNotFoundError(f"blueprint {text!r} not found; available: {available}")


def blueprint_summary(bp: Blueprint) -> dict:
    return {
        "id": bp.id,
        "version": bp.version,
        "title": bp.title,
        "description": bp.description,
        "slots": [asdict(slot) for slot in bp.slots],
        "stages": [asdict(stage) for stage in bp.stages],
        "edges": [asdict(edge) for edge in bp.edges],
        "limits": asdict(bp.limits),
        "on_unhandled": bp.on_unhandled,
    }
