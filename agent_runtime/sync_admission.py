"""The ONE per-entity admission guard for realm-sync pull appliers.

``pull_realm_sync`` runs ``_assert_no_secret_artifacts`` over the artifacts the
GENERIC write-loop maps — and only those. Every specialized applier
(``board_sync``, ``office_sync``, ``apply_skill_inbox_pull``,
``persona_config_sync``, ``profile_artifact_sync``) exists precisely because its
family is EXCLUDED from that loop (``_destination_for_sync_path`` → ``None``),
which means those families were never scanned on the way in. The persona-config
lane grew its own inline scan at ``c905569c1``; the others had none. This module
is that scan, lifted to one place so there is a single authority rather than
four drifting copies.

Two deliberate posture rules, both learned the hard way in this subsystem:

1. **Secrets are scanned everywhere.** The regex, the secret-ish path markers
   and the hard-excluded path parts are ``realm_sync``'s (imported lazily — this
   module never defines a second copy). A pulled entity carrying a secret-shaped
   assignment is refused at the door. This is strictly *less* destructive than
   the generic loop's behaviour, which raises and aborts the whole pull: here one
   bad entity is refused and the pull continues (per-entity isolation).

2. **Portability is scanned only over WIRING, never over prose.**
   ``realm_sync._assert_portable_artifacts`` already documents why: a skill's
   ``SKILL.md``, a profile ``MEMORY.md``, an ``AGENTS.md`` or a board card
   description legitimately mentions absolute paths as English, and "a refusal
   that bricks a publish over a false positive" is a failure class this
   subsystem has already paid for. So free-text keys (:data:`PROSE_KEYS`) are
   pruned before the portability walk, and file BODIES are never portability
   -scanned at all — only structured payloads and path components are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Free-text keys pruned before the portability walk, at ANY depth. These carry
#: operator/agent English (board card titles + descriptions + checklist text,
#: column titles, office folder names, actor display names), where an absolute
#: path is a legitimate mention and never live wiring.
PROSE_KEYS: frozenset[str] = frozenset(
    {
        "checklist",
        "description",
        "display_name",
        "folder",
        "folders",
        "name",
        "notes",
        "text",
        "title",
    }
)

#: Path components that can never appear in a pulled relative path. ``..``/``.``
#: are traversal; empty is malformed. Absolute/drive-letter/UNC shapes are
#: rejected by :func:`path_refusal` structurally rather than by name.
_UNSAFE_COMPONENTS = frozenset({"", ".", ".."})


@dataclass(frozen=True, slots=True)
class Refusal:
    """One refused entity. Mirrors ``persona_config_sync._refusal``'s row shape
    (``{key, code, message}``) so every lane's ``refused`` list reads the same
    in a pull result and in Mission Control."""

    key: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "code": self.code, "message": self.message}


def _secret_assignment_re():
    from .realm_sync import SECRET_ASSIGNMENT_RE  # lazy: avoid import cycle/weight

    return SECRET_ASSIGNMENT_RE


def path_refusal(rel: str) -> tuple[str, str] | None:
    """Typed refusal for an untrusted RELATIVE path from a pulled subtree.

    ``None`` means admissible. Covers traversal, absolute/drive-letter/UNC
    shapes, Windows reserved device names (a ``con``/``nul`` component crashes
    the write on Windows — a pull DoS the skill mirror already learned about),
    and the shared secret-ish / hard-excluded path markers.
    """

    from .realm_sync import _is_hard_excluded_path, _is_secretish_path
    from .skill_promotion import is_windows_reserved_component

    text = str(rel or "").replace("\\", "/")
    if not text.strip():
        return ("unsafe_path", "empty relative path")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return ("unsafe_path", f"absolute path is never a pull destination: {text}")
    parts = tuple(part for part in text.split("/"))
    for part in parts:
        if part in _UNSAFE_COMPONENTS:
            return ("unsafe_path", f"unsafe path component in {text!r}")
        if is_windows_reserved_component(part):
            return ("reserved_path_component", f"reserved device name component in {text!r}")
    if _is_secretish_path(text):
        return ("secretish_path", f"path matches a secret marker: {text}")
    if _is_hard_excluded_path(text):
        return ("hard_excluded_path", f"path is hard-excluded from realm sync: {text}")
    return None


def content_refusal(data: bytes) -> tuple[str, str] | None:
    """Typed refusal for a pulled FILE BODY. Secret-shaped assignments only —
    deliberately no portability walk (see the module docstring: file bodies are
    prose)."""

    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 — an undecodable blob carries no assignment
        return None
    if _secret_assignment_re().search(text):
        return ("secret_shaped_value", "file content carries a secret-shaped assignment")
    return None


def prune_prose(value: Any, prose_keys: frozenset[str] = PROSE_KEYS) -> Any:
    """Drop every ``prose_keys`` branch so the portability walk only ever sees
    wiring. Recursive; containers are rebuilt, scalars pass through. Pass an
    empty set for a payload that is 100% wiring (a persona definition, whose keys
    are themselves an allowlist) so nothing is exempted."""

    if isinstance(value, dict):
        return {
            key: prune_prose(item, prose_keys)
            for key, item in value.items()
            if str(key) not in prose_keys
        }
    if isinstance(value, (list, tuple)):
        return [prune_prose(item, prose_keys) for item in value]
    return value


def _flatten_assignments(value: Any, *, out: list[str] | None = None) -> str:
    """Render a structure as unquoted ``key=value`` lines for the assignment
    scanner. ``json.dumps`` writes ``"token": "…"``; the closing quote on the key
    defeats ``\\btoken\\b\\s*[:=]``, so a secret carried as a FIELD would slip
    through a raw-dump scan. Rendering it as ``token=…`` closes that."""

    rows = [] if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list, tuple)):
                _flatten_assignments(item, out=rows)
            else:
                rows.append(f"{key}={item}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten_assignments(item, out=rows)
    return "\n".join(rows)


def payload_refusal(
    payload: Any,
    *,
    prefix: str = "",
    check_portability: bool = True,
    prose_keys: frozenset[str] = PROSE_KEYS,
) -> tuple[str, str] | None:
    """Typed refusal for a pulled STRUCTURED entity (a board card, an office
    actor, a persona definition).

    Runs the secret-assignment scan over the whole payload and — when
    ``check_portability`` — the shared machine-shaped-value walk over the
    prose-pruned payload. ALL portability offenders are named in one message
    (the "name ALL offenders" precedent) so an operator sees the full picture.
    """

    from .persona_config_sync import find_nonportable_values

    try:
        encoded = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = str(payload)
    # Two passes, because the scanner is an ASSIGNMENT regex: the raw dump catches
    # a secret embedded inside a value (``"display_name": "api_key: sk-…"`` — the
    # persona-config lane's shape), while the flattened pass catches a secret
    # carried as a FIELD, which JSON quoting (``"token": "…"``) otherwise hides
    # from ``\btoken\b\s*[:=]``.
    if _secret_assignment_re().search(encoded) or _secret_assignment_re().search(_flatten_assignments(payload)):
        return ("secret_shaped_value", "entity carries a secret-shaped assignment")
    if check_portability:
        offenders = find_nonportable_values(prune_prose(payload, prose_keys), prefix=prefix)
        if offenders:
            return (
                "nonportable_path",
                "machine-shaped value(s): " + ", ".join(row["key"] for row in offenders),
            )
    return None


def refuse_entity(
    key: str,
    *,
    relative_paths: tuple[str, ...] = (),
    payload: Any = None,
    blobs: tuple[bytes, ...] = (),
    check_portability: bool = True,
    prose_keys: frozenset[str] = PROSE_KEYS,
    prefix: str = "",
) -> Refusal | None:
    """One admission decision for one entity across all of its evidence.

    Returns the FIRST refusal (path → payload → body) or ``None``. Callers keep
    per-entity isolation: refuse this entity, record the row, keep pulling.
    """

    for rel in relative_paths:
        found = path_refusal(rel)
        if found is not None:
            return Refusal(key, found[0], found[1])
    if payload is not None:
        found = payload_refusal(
            payload,
            prefix=prefix or key,
            check_portability=check_portability,
            prose_keys=prose_keys,
        )
        if found is not None:
            return Refusal(key, found[0], found[1])
    for blob in blobs:
        found = content_refusal(blob)
        if found is not None:
            return Refusal(key, found[0], found[1])
    return None


def refuse_package(key: str, root: Path) -> Refusal | None:
    """Admission decision for a whole multi-file package (a pulled skill).

    Every file's relative path and body is scanned; portability is NOT — a
    skill's documentation legitimately names absolute paths. ``root`` missing is
    admissible (nothing to admit).
    """

    if not root.is_dir():
        return None
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        found = path_refusal(f"{key}/{rel}")
        if found is not None:
            return Refusal(key, found[0], found[1])
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > 1_000_000:
            continue  # matches ``_file_contains_secret_assignment``'s size bound
        found = content_refusal(data)
        if found is not None:
            return Refusal(key, found[0], f"{found[1]} ({rel})")
    return None
