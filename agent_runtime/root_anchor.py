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
* **ONE read-modify-write for both keys, and the result is re-read from
  DISK.** Two serves booting against one machine is a real concurrency (it is
  the discovery slice's whole premise). The atomic rename makes each write
  all-or-nothing but does nothing about a LOST UPDATE: two sequential merges,
  each re-reading the file, could land runtime A's ``store_root`` beside
  runtime B's ``head_home`` with both processes reporting ``published``. So
  the two keys merge in a SINGLE pass, and after the rename the file is
  re-read: a value that is not ours on disk is reported as
  :attr:`RootAnchorOutcome.LOST_RACE`, never as a publish. The re-parse
  verification above checks the text we COMPOSED; only the post-write read
  checks what an ambient process will actually resolve.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
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
    #: The write went through, but the post-write re-read does NOT find our
    #: value: a concurrent publisher's whole-file rename landed after ours, or
    #: the file could not be read back to confirm at all (``detail`` says
    #: which — the winning value, or ``post_write_unverifiable``). Reported
    #: rather than retried: another runtime's declaration is not ours to
    #: overwrite, and a publish nobody verified must not read as ``published``.
    LOST_RACE = "lost_race"
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
    #: The head home THIS PROCESS is running with — for a launcher-spawned
    #: serve that is exactly the client's ``HERMES_HEAD_HOME`` pin, echoed
    #: back. It is therefore useless as a consistency check against that pin
    #: (the comparison is true by construction); see
    #: :attr:`recorded_head_home`, which is the machine's actual answer.
    head_home: str
    config_path: str
    detail: str = ""
    #: The head home the machine-wide config CARRIES after this attempt: our
    #: value when the declaration landed (``published`` / ``already_recorded``),
    #: the operator's divergent value when it did not (``operator_value_kept``),
    #: the winner's value on ``lost_race``. ``None`` whenever nothing landed and
    #: nothing was read — a refusal (probe isolation, missing head), an unsafe
    #: merge, or an IO failure.
    #:
    #: This field exists because the consistency check the Launcher runs against
    #: this block was VACUOUS without it: the client pins ``HERMES_HEAD_HOME``,
    #: the serve child resolves its head from that same pin, and so
    #: ``head_home`` above always equalled the pin no matter what the machine
    #: config actually said. A config declaring a SHADOW head therefore read as
    #: CONFIRMED (reproduced 2026-08-13). The recorded value is the only one
    #: that answers "what will a process the Launcher did not spawn resolve".
    recorded_head_home: str | None = None

    @property
    def declared(self) -> bool:
        return self.outcome is RootAnchorOutcome.PUBLISHED

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "head_home": self.head_home,
            "config_path": self.config_path,
            "detail": self.detail,
            "recorded_head_home": self.recorded_head_home,
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

    The two lanes DECIDE independently (:func:`_store_plan`, :func:`_head_plan`
    — each refuses for its own reasons) and then WRITE together: whatever both
    still want recorded goes into one :func:`_merge_keys` read-modify-write, so
    a concurrent publisher cannot interleave between them.
    """

    source = os.environ if env is None else env
    item = resolution if resolution is not None else resolve_runtime(env)
    root = item.store_root
    config_path = _platform_default_hermes_home(source) / "config.yaml"

    store_plan = _guarded_plan(_store_plan, source, root, config_path)
    # The head declaration is deliberately NOT conditional on the root one:
    # they answer different questions and fail for different reasons, and a
    # skipped-because-of-the-other-lane declaration would be exactly the
    # unaccounted silence this module exists to retire.
    head_plan = _guarded_plan(_head_plan, source, root, config_path, chat_scope)

    pending: dict[str, str] = {}
    if store_plan.value is not None:
        pending[STORE_ROOT_KEY] = store_plan.value
    if head_plan.value is not None:
        pending[HEAD_HOME_KEY] = head_plan.value
    merges: dict[str, _KeyMerge] = {}
    if pending:
        try:
            merges = _merge_keys(config_path, pending)
        except Exception as exc:  # accounted, never raised — best effort
            merges = {
                key: _KeyMerge(RootAnchorOutcome.UNWRITABLE, type(exc).__name__)
                for key in pending
            }

    store_merge = merges.get(STORE_ROOT_KEY)
    head_merge = merges.get(HEAD_HOME_KEY)
    return RootAnchorReport(
        outcome=store_plan.outcome
        if store_merge is None
        else store_merge.outcome,
        store_root=str(root),
        config_path=str(config_path),
        detail=store_plan.detail if store_merge is None else store_merge.detail,
        head=HeadAnchorReport(
            outcome=head_plan.outcome if head_merge is None else head_merge.outcome,
            head_home=head_plan.head,
            config_path=str(config_path),
            detail=head_plan.detail if head_merge is None else head_merge.detail,
            recorded_head_home=None if head_merge is None else head_merge.recorded,
        ),
    )


#: The two keys of the ``agent_runtime:`` block this module owns.
#: ``chat_session_scope.DECLARED_HEAD_HOME_KEY`` is the READER's name for the
#: second one; they are deliberately spelled in both places' own vocabulary
#: rather than imported across the seam, and pinned equal by a test.
STORE_ROOT_KEY = "store_root"
HEAD_HOME_KEY = "head_home"


@dataclass(frozen=True, slots=True)
class _LanePlan:
    """One lane's pre-merge decision: a refusal, or a value to record.

    Split out of the publish functions so both lanes can decide BEFORE either
    one writes — that is what lets the two keys share a single read-modify-write
    pass (see :func:`_merge_keys`).
    """

    #: The typed refusal, when this lane will not write. Meaningless (and
    #: overwritten by the merge result) when ``value`` is set.
    outcome: RootAnchorOutcome = RootAnchorOutcome.UNWRITABLE
    detail: str = ""
    #: The value this lane wants recorded, or ``None`` when it refused.
    value: str | None = None
    #: The head this lane is reporting on, verbatim (head lane only).
    head: str = ""


def _guarded_plan(builder, *args) -> _LanePlan:
    """Run a lane's planner; a raising planner becomes a typed refusal.

    Both lanes' "never raises" contract funnels through here rather than being
    hand-written twice.
    """

    try:
        return builder(*args)
    except Exception as exc:  # accounted, never raised — best effort by contract
        return _LanePlan(RootAnchorOutcome.UNWRITABLE, type(exc).__name__)


def _store_plan(
    source: Mapping[str, str],
    root: Path,
    config_path: Path,
) -> _LanePlan:
    if probe_isolation_required(source) or root.name.startswith(PROBE_ROOT_PREFIX):
        return _LanePlan(RootAnchorOutcome.PROBE_ISOLATED)

    ambient_default = config_path.parent / "agent-runtime"
    if _same_path(root, ambient_default):
        # An ambient process already resolves this root by default; recording
        # the default as config would add a rung that says nothing.
        return _LanePlan(RootAnchorOutcome.AMBIENT_ROOT)

    if not any((root / marker).is_dir() for marker in STORE_MARKER_DIRS):
        return _LanePlan(RootAnchorOutcome.ROOT_NOT_STORELIKE)

    return _LanePlan(value=str(root))


def _head_plan(
    source: Mapping[str, str],
    root: Path,
    config_path: Path,
    chat_scope: Any,
) -> _LanePlan:
    """Decide whether this process's EXPLICIT head home may be declared.

    Mirrors :func:`agent_runtime.chat_session_scope.publish_chat_head_home` one
    level down, exactly as ``_store_plan`` mirrors the pointer for the store
    root: the pointer lives at ``<store_root>/chat_head_home.json`` and can only
    be found by a process that already resolved the right store root, so the
    declaration has to live where a process with NO hermes environment at all
    still looks — the platform-default ``config.yaml``.

    Never raises (see :func:`_guarded_plan`); every exit is typed.
    """

    # The probe guard is the STORE ROOT's, mirrored exactly — this lane used to
    # check only ``probe_isolation_required``, so a serve on an isolated
    # ``agent-runtime-probe-*`` root that carried an explicit HERMES_HEAD_HOME
    # but no HERMES_REQUIRE_ISOLATED_ROOT wrote a QA/worktree head into the
    # OPERATOR's machine config, permanently (write-once). A probe root's head
    # is a probe's fact.
    #
    # Deliberately NOT mirrored: the reader's ``layer == "env"`` skip.
    # ``chat_session_scope.declared_chat_head_home`` steps aside when the READER
    # pinned its own ``HERMES_AGENT_RUNTIME_ROOT``, because a process that chose
    # an alternate root must not inherit a machine-default fact. The WRITER's
    # question is the opposite one — "does this process KNOW the machine's real
    # head?" — and the Launcher pins ``HERMES_AGENT_RUNTIME_ROOT`` on every
    # serve child it spawns (``mission_control_hermes_installer.dart``), so
    # refusing on layer ``env`` here would disable the declaration for the exact
    # process the slice exists to hear from, i.e. ship it dead. The probe guard
    # above is what actually excludes QA and worktree roots.
    if probe_isolation_required(source) or root.name.startswith(PROBE_ROOT_PREFIX):
        return _LanePlan(RootAnchorOutcome.PROBE_ISOLATED)

    scope = chat_scope if chat_scope is not None else _resolved_chat_scope()
    if scope is None:
        return _LanePlan(RootAnchorOutcome.UNWRITABLE, "chat_scope_unavailable")
    # Only an EXPLICITLY named head may be declared. A head resolved from
    # the pointer, from the config declaration itself, or from the ambient
    # guess is not this process's own fact — declaring it would let a guess
    # (or a stale declaration) re-affirm itself machine-wide forever, the
    # same laundering the INSTANCE_RECORDED rung refuses at bind time.
    if not scope.explicitly_named:
        return _LanePlan(
            RootAnchorOutcome.NO_EXPLICIT_HEAD,
            str(getattr(scope.source, "value", scope.source)),
            head=str(scope.head_home),
        )

    head = Path(scope.head_home)
    if _same_path(head, config_path.parent):
        return _LanePlan(RootAnchorOutcome.AMBIENT_HEAD, head=str(head))
    try:
        head_exists = head.is_dir()
    except OSError:  # pragma: no cover - defensive
        head_exists = False
    if not head_exists:
        return _LanePlan(RootAnchorOutcome.HEAD_HOME_MISSING, head=str(head))

    return _LanePlan(value=str(head), head=str(head))


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


@dataclass(frozen=True, slots=True)
class _KeyMerge:
    """What one key's merge did, and what the config CARRIES afterwards."""

    outcome: RootAnchorOutcome
    detail: str = ""
    #: The value the config holds for this key once the pass finished — ours on
    #: a publish, the operator's on ``operator_value_kept``, the winner's on
    #: ``lost_race``. ``None`` when nothing was written and nothing was read
    #: back (an unsafe merge, an IO failure). This is what makes the Launcher's
    #: consistency check non-vacuous; see :attr:`HeadAnchorReport.recorded_head_home`.
    recorded: str | None = None


def _merge_keys(
    config_path: Path, values: Mapping[str, str]
) -> dict[str, _KeyMerge]:
    """Write every absent ``agent_runtime.<key>: <value>`` in ONE pass.

    The one text-level editor behind both declarations: same read discipline,
    same never-overwrite rule, same re-parse verification, and — because it
    takes ALL the keys at once — one read and one rename for the whole boot.
    Two publishers doing this by hand would be two chances to get the
    operator's file wrong; two SEQUENTIAL passes were two chances to lose
    another runtime's concurrent update (finding 4, 2026-08-13).

    Returns one :class:`_KeyMerge` per requested key, always.
    """

    import yaml

    def every(outcome: RootAnchorOutcome, detail: str = "") -> dict[str, _KeyMerge]:
        return {key: _KeyMerge(outcome, detail) for key in values}

    with _config_write_lock(config_path):
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
                return every(RootAnchorOutcome.UNWRITABLE, "config_unreadable")
            except UnicodeDecodeError:
                return every(RootAnchorOutcome.UNWRITABLE, "config_not_utf8")
            try:
                parsed = yaml.safe_load(original_text)
            except yaml.YAMLError:
                # A file we cannot understand is a file we must not touch.
                return every(RootAnchorOutcome.UNWRITABLE, "config_unparseable")
            if parsed is not None and not isinstance(parsed, dict):
                return every(RootAnchorOutcome.UNWRITABLE, "config_not_a_mapping")

        base: dict = parsed if isinstance(parsed, dict) else {}
        block = base.get("agent_runtime")
        if block is not None and not isinstance(block, dict):
            # `agent_runtime: <scalar/list>` — extending it is not a merge, it
            # is a rewrite of an operator value. Refuse, for every key.
            return every(
                RootAnchorOutcome.DECLINED_UNSAFE_MERGE, "agent_runtime_not_a_mapping"
            )
        existing_block: dict = block if isinstance(block, dict) else {}

        results: dict[str, _KeyMerge] = {}
        pending: dict[str, str] = {}
        for key, value in values.items():
            existing = str(existing_block.get(key) or "").strip()
            if not existing:
                pending[key] = value
            elif _same_path(Path(existing).expanduser(), Path(value)):
                results[key] = _KeyMerge(RootAnchorOutcome.ALREADY_RECORDED, "", existing)
            else:
                # ``detail`` keeps carrying the operator's value: that is the
                # shipped frame contract, and the field older consumers read.
                results[key] = _KeyMerge(
                    RootAnchorOutcome.OPERATOR_VALUE_KEPT, existing, existing
                )
        if not pending:
            return results

        # One key at a time into the SAME text, so the second insertion sees
        # the block the first one created.
        text = original_text
        working_base: dict = base
        composed: dict[str, str] = {}
        for key, value in pending.items():
            candidate = _composed_text(text, working_base, key, value)
            if candidate is None:
                results[key] = _KeyMerge(
                    RootAnchorOutcome.DECLINED_UNSAFE_MERGE,
                    "no_unambiguous_insertion_point",
                )
                continue
            text = candidate
            composed[key] = value
            working_base = {
                **base,
                "agent_runtime": {**existing_block, **composed},
            }
        if not composed:
            return results

        # The whole safety of the text-level edit lives in this verification:
        # the new document must parse to the old one plus exactly the anchor
        # keys.
        expected = {**base, "agent_runtime": {**existing_block, **composed}}
        try:
            reparsed = yaml.safe_load(text)
        except yaml.YAMLError:
            reparsed = None
            detail = "merged_text_unparseable"
        else:
            detail = "merged_text_diverged"
        if reparsed != expected:
            results.update(
                {
                    key: _KeyMerge(RootAnchorOutcome.DECLINED_UNSAFE_MERGE, detail)
                    for key in composed
                }
            )
            return results

        if not _atomic_write_text(config_path, text):
            results.update(
                {
                    key: _KeyMerge(RootAnchorOutcome.UNWRITABLE, "config_write_failed")
                    for key in composed
                }
            )
            return results

        # Post-write verification. The rename was atomic, but a concurrent
        # publisher's rename can land after ours — and then the machine-wide
        # fact is THEIRS while we would have reported ``published``. Read the
        # file back and say what is actually there.
        landed = _recorded_values(config_path, composed)
        published_detail = "created" if not original_text else "extended"
        for key, value in composed.items():
            on_disk = landed.get(key)
            if on_disk is None:
                results[key] = _KeyMerge(
                    RootAnchorOutcome.LOST_RACE, "post_write_unverifiable"
                )
            elif _same_path(Path(on_disk).expanduser(), Path(value)):
                results[key] = _KeyMerge(
                    RootAnchorOutcome.PUBLISHED, published_detail, on_disk
                )
            else:
                results[key] = _KeyMerge(
                    RootAnchorOutcome.LOST_RACE, on_disk, on_disk
                )
        return results


def _recorded_values(config_path: Path, keys: Mapping[str, str]) -> dict[str, str]:
    """Re-read the config from DISK and return the ``agent_runtime`` values.

    Deliberately not memoized and not shared with the read above: the whole
    point is to observe the file AFTER our rename, including another process's
    rename that landed on top of it.
    """

    import yaml

    try:
        parsed = yaml.safe_load(config_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    block = parsed.get("agent_runtime") if isinstance(parsed, dict) else None
    if not isinstance(block, dict):
        return {}
    found: dict[str, str] = {}
    for key in keys:
        value = str(block.get(key) or "").strip()
        if value:
            found[key] = value
    return found


#: Beside the config, never inside it: a lock file is runtime state, and the
#: operator's document must not grow a key it did not ask for.
_CONFIG_LOCK_SUFFIX = ".anchor.lock"
#: A lock older than this is a crashed holder's, not a live one's. Generous
#: relative to the work it guards (one read, one compose, one rename).
_CONFIG_LOCK_STALE_SECONDS = 30.0
#: How long a publisher waits for the lock before proceeding WITHOUT it. Boot
#: must not block on a diagnostic, and the post-write verification is what
#: actually keeps the report honest — the lock only narrows the window.
_CONFIG_LOCK_WAIT_SECONDS = 2.0
_CONFIG_LOCK_POLL_SECONDS = 0.02


@contextmanager
def _config_write_lock(config_path: Path):
    """Best-effort inter-process exclusion around the read-modify-write.

    NOT the correctness mechanism — :func:`_merge_keys`' post-write re-read is.
    This only makes the lost-update window small enough that two serves booting
    together normally serialize instead of racing. It never raises, never
    blocks past :data:`_CONFIG_LOCK_WAIT_SECONDS`, and breaks a stale lock so a
    crashed publisher cannot wedge every later boot.
    """

    lock_path = config_path.with_name(config_path.name + _CONFIG_LOCK_SUFFIX)
    held = _acquire_config_lock(lock_path)
    try:
        yield
    finally:
        if held:
            try:
                lock_path.unlink()
            except OSError:  # pragma: no cover - defensive
                pass


def _acquire_config_lock(lock_path: Path) -> bool:
    deadline = time.monotonic() + _CONFIG_LOCK_WAIT_SECONDS
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            if _lock_is_stale(lock_path):
                try:
                    lock_path.unlink()
                except OSError:  # pragma: no cover - another breaker won
                    pass
                continue
        except OSError:
            return False
        else:
            try:
                os.write(handle, str(os.getpid()).encode("ascii"))
            except OSError:  # pragma: no cover - the lock is the file, not its body
                pass
            finally:
                os.close(handle)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_CONFIG_LOCK_POLL_SECONDS)


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:  # pragma: no cover - it vanished; the next attempt wins
        return False
    return age > _CONFIG_LOCK_STALE_SECONDS


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
