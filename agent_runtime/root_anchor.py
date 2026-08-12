"""Publish the machine root anchor: the fact "this machine's runtime root is X".

The defect class this retires (2026-08-12, the ambient chat-history incident)
------------------------------------------------------------------------------

``harness persona chat history --json`` returned ``ok: true, count: 0`` under
ambient environment but ``count: 4`` with ``HERMES_HOME`` pinned, for a session
whose rows were on disk. An ambient process resolves the Windows platform
default (``%LOCALAPPDATA%\\hermes``) — a real, populated SHADOW runtime — and
never reaches ``chat_head_home.json``, because that pointer lives at
``<store_root>/chat_head_home.json`` and ``store_root`` is derived from the
very ambient home the pointer exists to correct. The machine-level fact "this
operator's runtime root is elsewhere" was recorded nowhere the platform
default could see.

The fix mirrors :func:`agent_runtime.chat_session_scope.publish_chat_head_home`
one level down: ``resolve_runtime``'s ladder already has a config rung —
``agent_runtime.store_root`` in ``<home>/config.yaml``, above the
platform-default rung — that nothing ever wrote. At ``harness serve`` boot
(the one process that provably knows the real root, because the Launcher told
it), this module writes that key into the PLATFORM DEFAULT home's
``config.yaml``, if and only if it is absent. After that, an ambient process
resolves the real store root via the config rung, finds the chat-head pointer,
and chat reads resolve the operator head.

Publish contract (mirrors the pointer's):

* **Only the absent key is ever written.** An operator's existing
  ``agent_runtime.store_root`` — same or different — is never overwritten.
* **Best effort, but ACCOUNTED.** Publishing must never fail the process that
  knows the answer, so every exit is a typed :class:`RootAnchorOutcome` rather
  than an exception — a silent skip here would be the same false-all-clear
  class the anchor exists to retire.
* **Never a probe/test root.** Probe isolation, probe-prefixed roots, and
  roots that carry none of the store marker dirs are refused: anchoring the
  machine to a temp dir would be strictly worse than the shadow default.
* **Preserve the operator's file.** The write is text-level (create, append,
  or insert one line) and is verified by re-parsing: the new document must
  equal the old one plus exactly the anchor key, or nothing is written.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .resolution import (
    PROBE_ROOT_PREFIX,
    STORE_MARKER_DIRS,
    RuntimeResolution,
    _platform_default_hermes_home,
    probe_isolation_required,
    resolve_runtime,
)

__all__ = [
    "RootAnchorOutcome",
    "RootAnchorReport",
    "publish_store_root_anchor",
]


class RootAnchorOutcome(str, Enum):
    """Every way a publish attempt can end. Nothing here raises."""

    #: The key was absent and is now recorded in the platform default config.
    PUBLISHED = "published"
    #: The key already names this root — nothing to do.
    ALREADY_RECORDED = "already_recorded"
    #: The key already names a DIFFERENT root. The operator's value wins,
    #: always; the divergence is reported, never repaired.
    OPERATOR_VALUE_KEPT = "operator_value_kept"
    #: The resolved root IS what an ambient process would resolve anyway —
    #: there is no machine fact to record.
    AMBIENT_ROOT = "ambient_root"
    #: The resolved root carries none of the store marker dirs. A root the
    #: harness never wrote to must not be anchored machine-wide.
    ROOT_NOT_STORELIKE = "root_not_storelike"
    #: Probe isolation is demanded, or the root is a probe temp dir.
    PROBE_ISOLATED = "probe_isolated"
    #: The existing config could not be extended without risking the rest of
    #: its content (re-parse verification failed, ambiguous structure, ...).
    DECLINED_UNSAFE_MERGE = "declined_unsafe_merge"
    #: IO/parse failure reading or writing the config. Accounted, not raised.
    UNWRITABLE = "unwritable"


@dataclass(frozen=True, slots=True)
class RootAnchorReport:
    """What a publish attempt did, machine-readable for the serve boot frame."""

    outcome: RootAnchorOutcome
    store_root: str
    config_path: str
    detail: str = ""

    @property
    def published(self) -> bool:
        return self.outcome is RootAnchorOutcome.PUBLISHED

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "store_root": self.store_root,
            "config_path": self.config_path,
            "detail": self.detail,
        }


def publish_store_root_anchor(
    env: Mapping[str, str] | None = None,
    *,
    resolution: RuntimeResolution | None = None,
) -> RootAnchorReport:
    """Record this process's resolved runtime root for ambient processes.

    Called once from the ``harness serve`` boot (injected — see
    ``serve_loop``'s ``root_anchor`` parameter), with the same "off unless the
    real entry point turns it on" contract as ``snapshot_prewarm``, so no unit
    test of the loop can touch the machine-global platform-default config.
    """

    source = os.environ if env is None else env
    item = resolution if resolution is not None else resolve_runtime(env)
    root = item.store_root
    config_path = _platform_default_hermes_home(source) / "config.yaml"

    def report(outcome: RootAnchorOutcome, detail: str = "") -> RootAnchorReport:
        return RootAnchorReport(
            outcome=outcome,
            store_root=str(root),
            config_path=str(config_path),
            detail=detail,
        )

    try:
        return _publish(source, root, config_path, report)
    except Exception as exc:  # accounted, never raised — best effort by contract
        return report(RootAnchorOutcome.UNWRITABLE, type(exc).__name__)


def _publish(
    source: Mapping[str, str],
    root: Path,
    config_path: Path,
    report,
) -> RootAnchorReport:
    import yaml

    if probe_isolation_required(source) or root.name.startswith(PROBE_ROOT_PREFIX):
        return report(RootAnchorOutcome.PROBE_ISOLATED)

    ambient_default = config_path.parent / "agent-runtime"
    if _same_path(root, ambient_default):
        # An ambient process already resolves this root by default; recording
        # the default as config would add a rung that says nothing.
        return report(RootAnchorOutcome.AMBIENT_ROOT)

    if not any((root / marker).is_dir() for marker in STORE_MARKER_DIRS):
        return report(RootAnchorOutcome.ROOT_NOT_STORELIKE)

    original_text = ""
    parsed: Any = None
    if config_path.exists():
        try:
            # read_bytes + decode, NEVER read_text: text mode translates the
            # operator's CRLF endings to LF on read, so a later write would
            # silently flip the whole file's line endings (the repo's standing
            # EOL trap, read-side variant — caught by the CRLF test).
            original_text = config_path.read_bytes().decode("utf-8")
        except OSError:
            return report(RootAnchorOutcome.UNWRITABLE, "config_unreadable")
        except UnicodeDecodeError:
            return report(RootAnchorOutcome.UNWRITABLE, "config_not_utf8")
        try:
            parsed = yaml.safe_load(original_text)
        except yaml.YAMLError:
            # A file we cannot understand is a file we must not touch.
            return report(RootAnchorOutcome.UNWRITABLE, "config_unparseable")
        if parsed is not None and not isinstance(parsed, dict):
            return report(RootAnchorOutcome.UNWRITABLE, "config_not_a_mapping")

    base: dict = parsed if isinstance(parsed, dict) else {}
    block = base.get("agent_runtime")
    existing = ""
    if isinstance(block, dict):
        existing = str(block.get("store_root") or "").strip()
    elif block is not None:
        # `agent_runtime: <scalar/list>` — extending it is not a merge, it is
        # a rewrite of an operator value. Refuse.
        return report(RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "agent_runtime_not_a_mapping")
    if existing:
        if _same_path(Path(existing).expanduser(), root):
            return report(RootAnchorOutcome.ALREADY_RECORDED)
        return report(RootAnchorOutcome.OPERATOR_VALUE_KEPT, existing)

    new_text = _composed_text(original_text, base, root)
    if new_text is None:
        return report(RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "no_unambiguous_insertion_point")

    # The whole safety of the text-level edit lives in this verification: the
    # new document must parse to the old one plus exactly the anchor key.
    try:
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError:
        return report(RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "merged_text_unparseable")
    expected = {
        **base,
        "agent_runtime": {**(block if isinstance(block, dict) else {}), "store_root": str(root)},
    }
    if reparsed != expected:
        return report(RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "merged_text_diverged")

    if not _atomic_write_text(config_path, new_text):
        return report(RootAnchorOutcome.UNWRITABLE, "config_write_failed")
    return report(
        RootAnchorOutcome.PUBLISHED, "created" if not original_text else "extended"
    )


_ANCHOR_COMMENT = (
    "# Machine root anchor - published by `harness serve` (agent_runtime/root_anchor.py)\n"
    "# so ambient processes resolve the operator's real runtime root instead of the\n"
    "# platform-default shadow. Written only while absent; your edits here win.\n"
)


def _composed_text(original_text: str, base: dict, root: Path) -> str | None:
    """The new config text, or ``None`` when no unambiguous edit exists."""

    value_line = f"store_root: {_yaml_single_quoted(str(root))}"
    newline = "\r\n" if "\r\n" in original_text else "\n"
    if "agent_runtime" not in base:
        # Fresh file, comment-only file, or a mapping without the block:
        # append a whole block. The operator's bytes are untouched above it,
        # and the block uses the file's own line endings.
        prefix = original_text
        if prefix and not prefix.endswith("\n"):
            prefix += newline
        if prefix:
            prefix += newline
        block_text = (_ANCHOR_COMMENT + f"agent_runtime:\n  {value_line}\n").replace(
            "\n", newline
        )
        return prefix + block_text

    # An `agent_runtime:` block mapping exists without `store_root`: insert one
    # child line directly under its header, at the block's own child indent.
    lines = original_text.splitlines(keepends=True)
    header_indexes = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").split("#", 1)[0].rstrip() == "agent_runtime:"
        and not line[:1].isspace()
    ]
    if len(header_indexes) != 1:
        return None
    header = header_indexes[0]
    indent = "  "
    for line in lines[header + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = line[: len(line) - len(line.lstrip())]
        if leading:
            indent = leading.rstrip("\r\n")
        break
    newline = "\r\n" if lines[header].endswith("\r\n") else "\n"
    insertion = f"{indent}{value_line}{newline}"
    return "".join(lines[: header + 1]) + insertion + "".join(lines[header + 1 :])


def _yaml_single_quoted(text: str) -> str:
    # Single-quoted YAML scalars treat backslashes literally (Windows paths
    # survive un-escaped); only embedded single quotes need doubling.
    return "'" + text.replace("'", "''") + "'"


def _same_path(left: Path, right: Path) -> bool:
    try:
        return str(left.resolve(strict=False)).casefold() == str(
            right.resolve(strict=False)
        ).casefold()
    except OSError:  # pragma: no cover - defensive
        return str(left.absolute()).casefold() == str(right.absolute()).casefold()


def _atomic_write_text(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(text)
            os.replace(handle.name, path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
    except Exception:
        return False
    return True
