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

The second declaration: ``agent_runtime.head_home`` (2026-08-13)
----------------------------------------------------------------

The same treatment, one level up, for the OTHER machine fact this runtime knew
and recorded nowhere durable: which home holds the operator-visible chat
``state.db``. Until now the only authority that named it was the Launcher's
spawn environment (``HERMES_HEAD_HOME``, hardcoded in
``mission_control_settings.dart``) — an operator/machine fact living as a
string in a Dart client, learned only by launcher-spawned processes. Everything
else degraded down the ladder.

So when ``harness serve`` boots with an EXPLICIT head (env or relay — the
``explicitly_named`` posture of :mod:`agent_runtime.chat_session_scope`), it
declares that head in the same platform-default ``config.yaml`` under the same
write-only-while-absent contract. The chat-scope ladder grows a
``CONFIG_DECLARED`` rung that reads it, below the shared-root pointer and above
the ambient guess. The runtime then declares its own head, and the client's env
pin demotes to an override plus a consistency check.

Publish contract (mirrors the pointer's, and holds for BOTH keys):

* **Only the absent key is ever written.** An operator's existing
  ``agent_runtime.store_root`` / ``agent_runtime.head_home`` — same or
  different — is never overwritten.
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
from dataclasses import dataclass, replace
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
    "HeadAnchorReport",
    "RootAnchorOutcome",
    "RootAnchorReport",
    "publish_store_root_anchor",
]


class RootAnchorOutcome(str, Enum):
    """Every way a publish attempt can end. Nothing here raises.

    Shared by both declarations (``store_root`` and ``head_home``); the members
    that can only arise for one of them say so.
    """

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
    #: HEAD ONLY. This process named no head of its own (no ``HERMES_HEAD_HOME``,
    #: no relay context), so it has nothing to declare — a process that resolved
    #: its head from the pointer or the ambient guess must never launder that
    #: guess into a machine-wide declaration. ``detail`` carries the rung it
    #: actually resolved, so the skip is legible rather than silent.
    NO_EXPLICIT_HEAD = "no_explicit_head"
    #: HEAD ONLY. The explicitly named head home is not a directory. Declaring
    #: it machine-wide would point every ambient process at nothing.
    HEAD_HOME_MISSING = "head_home_missing"
    #: HEAD ONLY. The head IS the platform default home — what an ambient
    #: process resolves anyway. Recording it would add a rung that says nothing.
    AMBIENT_HEAD = "ambient_head"


@dataclass(frozen=True, slots=True)
class HeadAnchorReport:
    """What the ``agent_runtime.head_home`` declaration did.

    Its own report rather than extra fields on :class:`RootAnchorReport`,
    because the two declarations succeed and fail independently: a machine can
    legitimately anchor its store root while having no head to declare (a
    serve started with no explicit head), and an operator reading the boot
    frame must be able to tell which of the two happened.
    """

    outcome: RootAnchorOutcome
    head_home: str
    config_path: str
    detail: str = ""

    @property
    def declared(self) -> bool:
        return self.outcome is RootAnchorOutcome.PUBLISHED

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "head_home": self.head_home,
            "config_path": self.config_path,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RootAnchorReport:
    """What a publish attempt did, machine-readable for the serve boot frame.

    ``head`` is the head-home declaration's own outcome, attached by
    :func:`publish_store_root_anchor` and absent (``None``) on a report built
    by hand. The serve frame therefore stays byte-compatible for any consumer
    that predates the head declaration: the block simply appears.
    """

    outcome: RootAnchorOutcome
    store_root: str
    config_path: str
    detail: str = ""
    head: HeadAnchorReport | None = None

    @property
    def published(self) -> bool:
        return self.outcome is RootAnchorOutcome.PUBLISHED

    def payload(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "outcome": self.outcome.value,
            "store_root": self.store_root,
            "config_path": self.config_path,
            "detail": self.detail,
        }
        if self.head is not None:
            block["head"] = self.head.payload()
        return block


def publish_store_root_anchor(
    env: Mapping[str, str] | None = None,
    *,
    resolution: RuntimeResolution | None = None,
    chat_scope: Any = None,
) -> RootAnchorReport:
    """Record this machine's runtime root AND head home for ambient processes.

    Called once from the ``harness serve`` boot (injected — see
    ``serve_loop``'s ``root_anchor`` parameter), with the same "off unless the
    real entry point turns it on" contract as ``snapshot_prewarm``, so no unit
    test of the loop can touch the machine-global platform-default config.

    Two independent declarations, each fully accounted in the returned report:
    ``agent_runtime.store_root`` (the outer report) and
    ``agent_runtime.head_home`` (``report.head``). ``chat_scope`` is the
    injection seam for the head lane — the same role ``resolution`` plays for
    the root lane — defaulting to the ONE chat-scope authority.
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
        store_report = _publish(source, root, config_path, report)
    except Exception as exc:  # accounted, never raised — best effort by contract
        store_report = report(RootAnchorOutcome.UNWRITABLE, type(exc).__name__)
    # The head declaration is deliberately NOT conditional on the root one:
    # they answer different questions and fail for different reasons, and a
    # skipped-because-of-the-other-lane declaration would be exactly the
    # unaccounted silence this module exists to retire.
    return replace(store_report, head=_publish_head_home(source, config_path, chat_scope))


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

    outcome, detail = _merge_key(config_path, "store_root", str(root))
    return report(outcome, detail)


def _publish_head_home(
    source: Mapping[str, str],
    config_path: Path,
    chat_scope: Any,
) -> HeadAnchorReport:
    """Declare this process's EXPLICIT head home in the platform-default config.

    Mirrors :func:`agent_runtime.chat_session_scope.publish_chat_head_home` one
    level down, exactly as ``_publish`` mirrors the pointer for the store root:
    the pointer lives at ``<store_root>/chat_head_home.json`` and can only be
    found by a process that already resolved the right store root, so the
    declaration has to live where a process with NO hermes environment at all
    still looks — the platform-default ``config.yaml``.

    Never raises; every exit is a typed :class:`RootAnchorOutcome`.
    """

    def report(
        outcome: RootAnchorOutcome, head: Any = "", detail: str = ""
    ) -> HeadAnchorReport:
        return HeadAnchorReport(
            outcome=outcome,
            head_home=str(head),
            config_path=str(config_path),
            detail=detail,
        )

    try:
        if probe_isolation_required(source):
            return report(RootAnchorOutcome.PROBE_ISOLATED)

        scope = chat_scope if chat_scope is not None else _resolved_chat_scope()
        if scope is None:
            return report(RootAnchorOutcome.UNWRITABLE, detail="chat_scope_unavailable")
        # Only an EXPLICITLY named head may be declared. A head resolved from
        # the pointer, from the config declaration itself, or from the ambient
        # guess is not this process's own fact — declaring it would let a guess
        # (or a stale declaration) re-affirm itself machine-wide forever, the
        # same laundering the INSTANCE_RECORDED rung refuses at bind time.
        if not scope.explicitly_named:
            return report(
                RootAnchorOutcome.NO_EXPLICIT_HEAD,
                scope.head_home,
                detail=str(getattr(scope.source, "value", scope.source)),
            )

        head = Path(scope.head_home)
        if _same_path(head, config_path.parent):
            return report(RootAnchorOutcome.AMBIENT_HEAD, head)
        try:
            head_exists = head.is_dir()
        except OSError:  # pragma: no cover - defensive
            head_exists = False
        if not head_exists:
            return report(RootAnchorOutcome.HEAD_HOME_MISSING, head)

        outcome, detail = _merge_key(config_path, "head_home", str(head))
        return report(outcome, head, detail)
    except Exception as exc:  # accounted, never raised — best effort by contract
        return report(RootAnchorOutcome.UNWRITABLE, detail=type(exc).__name__)


def _resolved_chat_scope() -> Any:
    """The ONE chat-scope authority's PROCESS answer, or ``None``.

    Imported lazily and deliberately not re-derived here: this module must not
    become a second opinion on where the head home is (the whole point of the
    2026-08 root-authority work), and it must not read ``HERMES_HEAD_HOME``
    itself — the env gate allowlists exactly one module for that, and it is the
    authority, not this one.
    """

    try:
        from .chat_session_scope import resolve_process_chat_scope

        return resolve_process_chat_scope()
    except Exception:  # pragma: no cover - defensive
        return None


def _merge_key(config_path: Path, key: str, value: str) -> tuple[RootAnchorOutcome, str]:
    """Write ``agent_runtime.<key>: <value>`` iff absent. Returns (outcome, detail).

    The one text-level editor behind both declarations: same read discipline,
    same never-overwrite rule, same re-parse verification. Two publishers doing
    this by hand would be two chances to get the operator's file wrong.
    """

    import yaml

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
            return (RootAnchorOutcome.UNWRITABLE, "config_unreadable")
        except UnicodeDecodeError:
            return (RootAnchorOutcome.UNWRITABLE, "config_not_utf8")
        try:
            parsed = yaml.safe_load(original_text)
        except yaml.YAMLError:
            # A file we cannot understand is a file we must not touch.
            return (RootAnchorOutcome.UNWRITABLE, "config_unparseable")
        if parsed is not None and not isinstance(parsed, dict):
            return (RootAnchorOutcome.UNWRITABLE, "config_not_a_mapping")

    base: dict = parsed if isinstance(parsed, dict) else {}
    block = base.get("agent_runtime")
    existing = ""
    if isinstance(block, dict):
        existing = str(block.get(key) or "").strip()
    elif block is not None:
        # `agent_runtime: <scalar/list>` — extending it is not a merge, it is
        # a rewrite of an operator value. Refuse.
        return (RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "agent_runtime_not_a_mapping")
    if existing:
        if _same_path(Path(existing).expanduser(), Path(value)):
            return (RootAnchorOutcome.ALREADY_RECORDED, "")
        return (RootAnchorOutcome.OPERATOR_VALUE_KEPT, existing)

    new_text = _composed_text(original_text, base, key, value)
    if new_text is None:
        return (RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "no_unambiguous_insertion_point")

    # The whole safety of the text-level edit lives in this verification: the
    # new document must parse to the old one plus exactly the anchor key.
    try:
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError:
        return (RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "merged_text_unparseable")
    expected = {
        **base,
        "agent_runtime": {**(block if isinstance(block, dict) else {}), key: value},
    }
    if reparsed != expected:
        return (RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "merged_text_diverged")

    if not _atomic_write_text(config_path, new_text):
        return (RootAnchorOutcome.UNWRITABLE, "config_write_failed")
    return (
        RootAnchorOutcome.PUBLISHED,
        "created" if not original_text else "extended",
    )


_ANCHOR_COMMENT = (
    "# Machine root anchor - published by `harness serve` (agent_runtime/root_anchor.py)\n"
    "# so ambient processes resolve the operator's real runtime root and chat head home\n"
    "# instead of the platform-default shadow. Written only while absent; your edits win.\n"
)


def _composed_text(
    original_text: str, base: dict, key: str, value: str
) -> str | None:
    """The new config text, or ``None`` when no unambiguous edit exists."""

    value_line = f"{key}: {_yaml_single_quoted(value)}"
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

    # An `agent_runtime:` block mapping exists without this key: insert one
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
