"""One deterministic, operator-governed decision point for envelope-gated commands.

Why this module exists
----------------------
The harness terminal safety envelope (``tools/terminal_tool.py`` —
``_HARNESS_BLOCK_PATTERNS`` / ``_harness_safety_block``, both fork-added) hard
blocks ``git push``, destructive git, recursive delete, credential reads,
production operations and non-localhost network egress. It activates on the mere
PRESENCE of ``HERMES_AGENT_RUNTIME_ROOT`` and reads no permission state at
all — see ``docs/agent-runtime-harness/mission-chat-lane-gap-audit.md`` G4/G5b.

That produced a lane that behaves two opposite ways for the same command, and
BOTH were observed live on 2026-07-26 on the mission-chat Dev lane:

* **fail-CLOSED** — a turn whose persona is bound to a Hermes profile runs
  inside :func:`agent_runtime.profile_context.persona_profile_context`, which
  exports ``HERMES_AGENT_RUNTIME_ROOT``. The envelope fires and the agent gets
  ``git_push_requires_operator_approval`` with no channel to obtain that
  approval.
* **fail-OPEN** — a turn whose persona binds NO Hermes profile takes the
  ``binding.profile_home is None`` early-``yield`` at
  ``profile_context.py:82-84``: ``HERMES_AGENT_RUNTIME_ROOT`` is never
  exported, so the envelope is INERT. The command then falls through to
  ``tools/approval.py``, where ``is_cli`` / ``is_gateway`` / ``is_ask`` are all
  false and no cron marker is set — the historical non-interactive fail-open
  default — and ``git push origin main`` executes ungated and unrecorded.

Same lane, same role, opposite outcomes — decided by ambient process
environment state that nothing in the policy layer controls. Profile binding is
the clearest path to "unset" (above), and process history is a second: some
harness command handlers ``os.environ.setdefault`` the variable and
``_cmd_mission_chat_message`` does not, so in a long-lived ``harness serve``
process the answer also depends on what ran before this turn. Either way it is
not a policy; it is a coin flip.

What this module does
---------------------
It is the ONE place that answers "may this command run on this lane?" for the
envelope-gated classes, and it answers deterministically:

* **Governed lane + gated class + explicit grant** ⇒ runs, and the grant
  provenance is recorded (:func:`record_envelope_decision`).
* **Governed lane + gated class + no grant** ⇒ a TYPED refusal carrying the
  command class, the exact ROOT-config key that would grant it, the lane and
  the role. Never a silent auto-approval; never an unexplained hard block.
* **Governed lane + ungated command** ⇒ allowed, exactly as before.
* **Ungoverned lane** (no governed scope bound: ``hermes chat``, cron,
  gateway, acp, plain CLI) ⇒ :func:`envelope_decision` returns
  ``None`` and the caller keeps the legacy behavior byte-for-byte. This slice
  changes ONE lane.

Because the decision is keyed on a bound :class:`TerminalEnvelopeScope` rather
than on ``HERMES_AGENT_RUNTIME_ROOT``, the fail-open branch is closed: a
mission-chat turn is governed whether or not its persona binds a profile.

Grants are ROOT-config only
---------------------------
Mirrors the ``agent_runtime/mcp_admission.py`` precedent (landed 2026-07-26)
in every load-bearing respect:

1. **Root config only.** Read through
   :func:`agent_runtime.config.load_root_runtime_config`, so a sticky-active
   profile's own ``config.yaml`` can never grant itself the right to push. A
   profile cannot self-grant.
2. **Deny by default, no wildcard, no inheritance.** ``grants.<role>.<lane>``
   with no entry admits nothing. There is no ``*`` role, no ``*`` lane, and a
   grant on one lane says nothing about another.
3. **A code-owned command taxonomy.** :data:`COMMAND_CLASSES` is the complete
   envelope surface. Every current class is grantable; adding a future class
   still requires an explicit code review before configuration can name it.
4. **Every step can only NARROW.** A malformed stanza, an unknown class, a
   non-list value — all collapse to "grants nothing" AND produce a typed
   config issue. A config fault never reads as "allow".

Root ``config.yaml`` shape::

    agent_runtime:
      terminal_envelope:
        grants:
          dev:
            mission_chat: [git_push]

Nothing here weakens the envelope for a class that has no grant.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Iterator, Mapping

from .permission_modes import permission_mode_is_unbounded

logger = logging.getLogger(__name__)


# ── the lane this slice governs ─────────────────────────────────────────────
#
# Same spelling as ``mcp_admission.LANE_MISSION_CHAT`` — the runtime surface a
# persona turn runs on, distinct from ``mcp_lane``'s entry-point lane.

LANE_MISSION_CHAT = "mission_chat"

#: Lanes whose gated classes an operator GRANT table can lift. Deliberately ONE
#: lane: the operator ruling makes mission-chat the primary work lane, and it is
#: the only lane where a config edit can allow a gated class. Widening this is a
#: product decision, not a config edit.
GOVERNED_LANES = frozenset({LANE_MISSION_CHAT})


# ── command-class taxonomy ──────────────────────────────────────────────────
#
# These are the classes the envelope ALREADY distinguishes. Nothing is invented
# here: every class below is one row (or one adjacent group of rows) of
# ``tools/terminal_tool.py::_HARNESS_BLOCK_PATTERNS``, and every class maps
# back to the exact legacy reason code that table returns, so the audit lane
# and every existing consumer keep reading the vocabulary they already read.

GIT_PUSH = "git_push"
DESTRUCTIVE_GIT = "destructive_git"
RECURSIVE_DELETE = "recursive_delete"
NETWORK_EGRESS = "network_egress"

COMMAND_CLASSES: frozenset[str] = frozenset(
    {
        GIT_PUSH,
        DESTRUCTIVE_GIT,
        RECURSIVE_DELETE,
        NETWORK_EGRESS,
    }
)

#: Classes an operator grant may cover. Ruling R-2 removed the former secret
#: read/exfiltration and production-operation floors from this taxonomy, so all
#: remaining envelope classes are grantable.
GRANTABLE_COMMAND_CLASSES: frozenset[str] = COMMAND_CLASSES

#: Class → the legacy ``_HARNESS_BLOCK_PATTERNS`` reason code. Preserved
#: verbatim so ``blocked_tool_attempts.jsonl`` readers, operator dashboards and
#: the existing envelope tests keep seeing the vocabulary they already parse.
#: ``destructive_git`` and ``recursive_delete`` intentionally SHARE
#: ``tree_wipe_blocked`` — the legacy table did not distinguish them, and the
#: finer split exists so a grant can name one without granting the other.
LEGACY_REASON_BY_CLASS: Mapping[str, str] = {
    GIT_PUSH: "git_push_requires_operator_approval",
    DESTRUCTIVE_GIT: "tree_wipe_blocked",
    RECURSIVE_DELETE: "tree_wipe_blocked",
    NETWORK_EGRESS: "network_command_requires_allowlist",
}

#: Human-facing one-liners for the refusal an AGENT reads.
CLASS_SUMMARY: Mapping[str, str] = {
    GIT_PUSH: "publishes commits to a remote",
    DESTRUCTIVE_GIT: "discards uncommitted or committed work in the git tree",
    RECURSIVE_DELETE: "recursively deletes a directory tree",
    NETWORK_EGRESS: "reaches a network host outside the loopback allowlist",
}

_NETWORK_ALLOWLIST = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

#: MIRROR of ``tools/terminal_tool.py::_HARNESS_BLOCK_PATTERNS``, re-keyed from
#: reason code to command class. We mirror rather than import because
#: ``tools.terminal_tool`` is an upstream module whose import pulls in the whole
#: terminal backend stack (docker/modal/singularity probes), and this module is
#: imported by config/policy readers that must stay cheap. Drift is guarded by
#: ``tests/agent_runtime/test_terminal_envelope_grants.py::
#: test_class_patterns_mirror_the_legacy_envelope_table``, which asserts both
#: tables reach the same verdict over a shared corpus.
_CLASS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), GIT_PUSH),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+reset\s+--(?:merge|keep)\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+clean\b[^\n;&|]*(?:-[^\s]*[xdf]|/[xdf])", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+checkout\b[^\n;&|]*\s(?:--force|-f)\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (
        re.compile(
            r"\bgit\s+checkout\b[^\n;&|]*(?:\s--(?:\s|$)|\s--(?:pathspec-from-file|pathspec-file-nul)\b)",
            re.IGNORECASE,
        ),
        DESTRUCTIVE_GIT,
    ),
    (re.compile(r"\bgit\s+checkout\b[^\n;&|]*\s(?:\.|:/|:\\)(?:\s|$)", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+switch\b[^\n;&|]*\s(?:--force|-f)\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+restore\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\bgit\s+stash\s+(?:drop|clear)\b", re.IGNORECASE), DESTRUCTIVE_GIT),
    (re.compile(r"\brm\s+-[^\n;&|]*r[^\n;&|]*f\b", re.IGNORECASE), RECURSIVE_DELETE),
    (re.compile(r"\bRemove-Item\b[^\n;&|]*(?:-Recurse|-r)\b", re.IGNORECASE), RECURSIVE_DELETE),
)

_NETWORK_TOOL_RE = re.compile(r"\b(curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod)\b", re.IGNORECASE)
_NETWORK_URL_RE = re.compile(r"https?://([^/\s'\"`]+)", re.IGNORECASE)


def classify_command(command: str) -> str | None:
    """The envelope-gated class this command falls in, or ``None``.

    Pure and side-effect free: same answer regardless of environment, lane or
    config. Whether a class is REFUSED is a separate question answered by
    :func:`envelope_decision`.
    """

    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return None
    for pattern, command_class in _CLASS_PATTERNS:
        if pattern.search(normalized):
            return command_class
    return _network_class(normalized)


def _network_class(command: str) -> str | None:
    if not _NETWORK_TOOL_RE.search(command):
        return None
    hosts = _NETWORK_URL_RE.findall(command)
    if not hosts:
        return NETWORK_EGRESS
    for host in hosts:
        clean = host.split(":", 1)[0].strip("[]").lower()
        if clean not in _NETWORK_ALLOWLIST:
            return NETWORK_EGRESS
    return None


def legacy_reason_for_class(command_class: str | None) -> str | None:
    """The ``_HARNESS_BLOCK_PATTERNS`` reason code a class maps back to."""

    if not command_class:
        return None
    return LEGACY_REASON_BY_CLASS.get(command_class)


# ── the per-run scope (what lane/role is this command running under) ────────


@dataclass(frozen=True, slots=True)
class TerminalEnvelopeScope:
    """Lane + role identity for the run whose tools are executing.

    Bound by ``profile_runner._execute_agent_run`` for the duration of a run,
    the same ContextVar ferry ``persona_chat_continuity.tool_execution_scope``
    already uses to reach the terminal tool (proving the value survives to the
    tool call). Unbound on every lane that does not construct one, which is how
    "no other lane changes" is enforced structurally rather than by convention.
    """

    lane: str
    role: str
    persona_id: str = ""
    session_id: str = ""
    runtime_root: str = ""
    #: The chat permission mode this run resolved (``tool_permissions.
    #: permission_options_for_chat``). Stamped by ``persona_runtime`` from the
    #: SAME resolve the turn already performs, so the schema plane and the
    #: execution plane cannot disagree about which mode the turn is running
    #: under. Empty on any caller that does not model a permission mode, which
    #: reads exactly as the pre-2026-08-09 behaviour (grants table only).
    permission_mode: str = ""

    @property
    def governed(self) -> bool:
        return self.lane in GOVERNED_LANES

    @property
    def unbounded(self) -> bool:
        return permission_mode_is_unbounded(self.permission_mode)


_ENVELOPE_SCOPE: ContextVar[TerminalEnvelopeScope | None] = ContextVar(
    "hermes_terminal_envelope_scope", default=None
)


@contextmanager
def terminal_envelope_scope(scope: TerminalEnvelopeScope | None) -> Iterator[None]:
    token = _ENVELOPE_SCOPE.set(scope)
    try:
        yield
    finally:
        _ENVELOPE_SCOPE.reset(token)


def current_terminal_envelope_scope() -> TerminalEnvelopeScope | None:
    return _ENVELOPE_SCOPE.get()


# ── typed outcomes ──────────────────────────────────────────────────────────

OUTCOME_ALLOW = "allow"
OUTCOME_GRANTED = "granted"
OUTCOME_REFUSE = "refuse"

#: The gated class has no grant for this role+lane. THE headline failure class.
ENVELOPE_COMMAND_REQUIRES_GRANT = "envelope_command_requires_grant"
#: Reserved for any future gated class kept outside
#: :data:`GRANTABLE_COMMAND_CLASSES`. Ruling R-2 leaves no current hard floors.
ENVELOPE_COMMAND_NOT_GRANTABLE = "envelope_command_not_grantable"
#: Why a granted command was allowed to run. Recorded on the receipt so an
#: operator reading ``terminal_envelope_decisions.jsonl`` can tell an explicit
#: per-role config grant from the standing permission-mode posture.
GRANT_SOURCE_CONFIG = "config_grant"
#: The run's permission mode granted it (``unbounded``). This is the detective
#: half of the 2026-08-09 ruling: the class is no longer refused by default, so
#: the receipt naming the MODE is the only record of why it ran.
GRANT_SOURCE_PERMISSION_MODE = "permission_mode"

#: ``grants`` config issue codes (never widen; always reported).
GRANT_UNKNOWN_COMMAND_CLASS = "envelope_grant_unknown_command_class"
GRANT_CLASS_NOT_GRANTABLE = "envelope_grant_class_not_grantable"
GRANT_MALFORMED = "envelope_grant_malformed"


@dataclass(frozen=True, slots=True)
class TerminalEnvelopeGrantIssue:
    """A typed config error in the grant table. Never a silent grant."""

    code: str
    subject: str
    summary: str
    fix_hint: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
        }


@dataclass(frozen=True, slots=True)
class TerminalEnvelopeGrants:
    """The resolved ``grants.<role>.<lane>`` answer, plus any config faults."""

    role: str
    lane: str
    classes: frozenset[str] = frozenset()
    issues: tuple[TerminalEnvelopeGrantIssue, ...] = ()

    @property
    def config_key(self) -> str:
        return grant_config_key(role=self.role, lane=self.lane)

    def issue_rows(self) -> list[dict[str, Any]]:
        return [issue.row() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class TerminalEnvelopeDecision:
    """The one answer for one command on one run. Pure; no side effects."""

    outcome: str
    lane: str
    role: str
    command_class: str | None = None
    #: Legacy ``_HARNESS_BLOCK_PATTERNS`` reason code, carried so the audit
    #: lane and existing consumers keep their vocabulary.
    reason: str | None = None
    failure_class: str | None = None
    config_key: str | None = None
    summary: str = ""
    fix_hint: str = ""
    persona_id: str = ""
    session_id: str = ""
    grant_issues: tuple[TerminalEnvelopeGrantIssue, ...] = ()
    #: The permission mode the run resolved, carried onto every receipt.
    permission_mode: str = ""
    #: :data:`GRANT_SOURCE_CONFIG` or :data:`GRANT_SOURCE_PERMISSION_MODE` on a
    #: grant; empty otherwise.
    grant_source: str = ""

    @property
    def refused(self) -> bool:
        return self.outcome == OUTCOME_REFUSE

    @property
    def granted(self) -> bool:
        return self.outcome == OUTCOME_GRANTED

    def row(self) -> dict[str, Any]:
        """``{code, subject, entry_point_lane, summary, fix_hint}`` — the same
        typed-row shape ``mcp_lane`` / ``mcp_admission`` / ``machine_roots``
        already emit, so operator surfaces need no new case."""

        return {
            "code": self.failure_class or self.outcome,
            "subject": self.command_class or "",
            "entry_point_lane": self.lane,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
        }

    @property
    def granted_by(self) -> str | None:
        """The provenance string a receipt records for a GRANT.

        A config grant names its ROOT-config key (unchanged). A permission-mode
        grant has no config key by construction — naming the mode is the whole
        point of the receipt, so it says ``permission_mode=unbounded`` rather
        than leaving the field null and losing why the command ran.
        """

        if not self.granted:
            return None
        if self.grant_source == GRANT_SOURCE_PERMISSION_MODE:
            return f"permission_mode={self.permission_mode}"
        return self.config_key

    def explain(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "lane": self.lane,
            "role": self.role,
            "persona_id": self.persona_id,
            "session_id": self.session_id,
            "command_class": self.command_class,
            "reason": self.reason,
            "failure_class": self.failure_class,
            "config_key": self.config_key,
            "permission_mode": self.permission_mode,
            "grant_source": self.grant_source,
            "granted_by": self.granted_by,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
            "grant_issues": [issue.row() for issue in self.grant_issues],
        }


# ── config (ROOT only) ──────────────────────────────────────────────────────

#: Role aliases accepted in the grant table. EXACTLY the S64-ruled pair:
#: ``alice_supervisor`` ⇄ ``neko_supervisor``, retained as wire/config
#: compatibility so persisted role-envelope keys and historical decision-contract
#: values still decode.
#:
#: S66 removed a THIRD entry, bare ``"neko" -> "alice_supervisor"``, which the
#: S64 ruling never covered. This table feeds a PERMISSION path
#: (``resolve_terminal_envelope_grants``), so an un-ruled alias here silently
#: widens who a grant applies to. It was inert — no live persona carries role
#: ``neko`` (the live roster is ``alice_supervisor`` / ``dev`` / ``profile`` /
#: ``qa``, and the configured grants are ``dev`` / ``backend_dev``) — which is
#: exactly why it could sit unnoticed. Do not re-add an alias to a permission
#: table without a ruling that names it.
_ROLE_ALIASES: Mapping[str, str] = {
    "neko_supervisor": "alice_supervisor",
}


def canonical_role(role: str) -> str:
    text = str(role or "").strip().lower()
    return _ROLE_ALIASES.get(text, text)


def grant_config_key(*, role: str, lane: str) -> str:
    return f"agent_runtime.terminal_envelope.grants.{canonical_role(role)}.{str(lane or '').strip()}"


def envelope_config(cfg: Any | None = None):
    """The ``agent_runtime.terminal_envelope`` block from the ROOT config.

    Harness-wide operator policy, so it loads through
    ``config.load_root_runtime_config`` — a sticky-active profile's own
    ``config.yaml`` must not be able to grant itself the right to push. Any
    load fault yields the empty (grants-nothing) config: a config fault can
    only ever narrow.
    """

    from .runtime_config import TerminalEnvelopeConfig

    if cfg is not None:
        resolved = getattr(cfg, "terminal_envelope", None)
        return resolved if resolved is not None else TerminalEnvelopeConfig()
    try:
        from .config import load_root_runtime_config

        return load_root_runtime_config().terminal_envelope
    except Exception:  # pragma: no cover - defensive; a config fault must not open the gate
        logger.debug("terminal envelope config load failed; granting nothing", exc_info=True)
        return TerminalEnvelopeConfig()


def _lanes_for_role(grants: Mapping[str, Any], canon_role: str) -> Any:
    """The lane map for a role, honoring :data:`_ROLE_ALIASES` on config keys.

    The canonical spelling wins outright when present; an alias is only
    consulted when the canonical key is absent. Never unions the two — a
    resolution rule that merged both spellings would widen a grant table
    depending on how many ways the operator happened to spell one role.
    """

    direct = grants.get(canon_role)
    if direct is not None:
        return direct
    for key, value in grants.items():
        if canonical_role(str(key)) == canon_role:
            return value
    return None


def resolve_terminal_envelope_grants(
    *, role: str, lane: str, cfg: Any | None = None
) -> TerminalEnvelopeGrants:
    """``grants.<role>.<lane>`` — deny-by-default, no wildcard, no inheritance.

    Every rejected entry produces a typed issue instead of being dropped
    silently: an operator who mistypes ``git-push`` must learn that from the
    refusal, not from a command that keeps failing for a reason the config
    appears to have fixed.
    """

    canon_role = canonical_role(role)
    lane_text = str(lane or "").strip()
    config = envelope_config(cfg)
    grants = getattr(config, "grants", None) or {}
    if not isinstance(grants, Mapping):
        return TerminalEnvelopeGrants(role=canon_role, lane=lane_text)

    lanes = _lanes_for_role(grants, canon_role)
    if not isinstance(lanes, Mapping):
        return TerminalEnvelopeGrants(role=canon_role, lane=lane_text)

    raw_classes = lanes.get(lane_text)
    if raw_classes is None:
        return TerminalEnvelopeGrants(role=canon_role, lane=lane_text)
    if not isinstance(raw_classes, (list, tuple, set, frozenset)):
        issue = TerminalEnvelopeGrantIssue(
            code=GRANT_MALFORMED,
            subject=grant_config_key(role=canon_role, lane=lane_text),
            summary=(
                f"{grant_config_key(role=canon_role, lane=lane_text)} is "
                f"{type(raw_classes).__name__}, not a list of command classes; nothing is granted."
            ),
            fix_hint="Write it as a YAML list, e.g. mission_chat: [git_push].",
        )
        return TerminalEnvelopeGrants(role=canon_role, lane=lane_text, issues=(issue,))

    granted: set[str] = set()
    issues: list[TerminalEnvelopeGrantIssue] = []
    for raw in raw_classes:
        name = str(raw or "").strip()
        if not name:
            continue
        if name not in COMMAND_CLASSES:
            issues.append(
                TerminalEnvelopeGrantIssue(
                    code=GRANT_UNKNOWN_COMMAND_CLASS,
                    subject=name,
                    summary=(
                        f"'{name}' is not a terminal-envelope command class, so it grants nothing."
                    ),
                    fix_hint=(
                        "Valid classes: " + ", ".join(sorted(COMMAND_CLASSES)) + "."
                    ),
                )
            )
            continue
        if name not in GRANTABLE_COMMAND_CLASSES:
            issues.append(
                TerminalEnvelopeGrantIssue(
                    code=GRANT_CLASS_NOT_GRANTABLE,
                    subject=name,
                    summary=(
                        f"'{name}' is a hard envelope floor and cannot be granted by config; "
                        "it grants nothing."
                    ),
                    fix_hint=(
                        "Grantable classes: " + ", ".join(sorted(GRANTABLE_COMMAND_CLASSES)) + "."
                    ),
                )
            )
            continue
        granted.add(name)

    return TerminalEnvelopeGrants(
        role=canon_role,
        lane=lane_text,
        classes=frozenset(granted),
        issues=tuple(issues),
    )


# ── the decision ────────────────────────────────────────────────────────────


def envelope_decision(
    command: str,
    *,
    scope: TerminalEnvelopeScope | None = None,
    cfg: Any | None = None,
) -> TerminalEnvelopeDecision | None:
    """Decide one command, or return ``None`` when this lane is not governed.

    ``None`` is load-bearing: it means "this module has no opinion, keep the
    legacy behavior" and is what every run that binds NO scope gets — i.e.
    everything that is not a harness-constructed run (``hermes chat``, cron,
    gateway, acp, plain CLI). Callers must treat ``None`` as "fall through",
    never as "allow".

    Mission-chat is the only producer of a governed scope. A stale or external
    scope carrying any other lane gets ``None`` and therefore cannot invent a
    second grant surface.
    """

    resolved = scope if scope is not None else current_terminal_envelope_scope()
    if resolved is None or not resolved.governed:
        return None

    permission_mode = str(resolved.permission_mode or "")
    command_class = classify_command(command)
    if command_class is None:
        return TerminalEnvelopeDecision(
            outcome=OUTCOME_ALLOW,
            lane=resolved.lane,
            role=resolved.role,
            persona_id=resolved.persona_id,
            session_id=resolved.session_id,
            permission_mode=permission_mode,
        )

    reason = legacy_reason_for_class(command_class)
    grants = resolve_terminal_envelope_grants(role=resolved.role, lane=resolved.lane, cfg=cfg)
    config_key = grants.config_key

    if command_class in grants.classes:
        return TerminalEnvelopeDecision(
            outcome=OUTCOME_GRANTED,
            lane=resolved.lane,
            role=resolved.role,
            command_class=command_class,
            reason=reason,
            config_key=config_key,
            summary=(
                f"'{command_class}' is granted for role '{grants.role}' on the "
                f"'{resolved.lane}' lane by {config_key}."
            ),
            persona_id=resolved.persona_id,
            session_id=resolved.session_id,
            grant_issues=grants.issues,
            permission_mode=permission_mode,
            grant_source=GRANT_SOURCE_CONFIG,
        )

    # Permission-mode grant (operator ruling 2026-08-09). An ``unbounded`` run
    # gets every GRANTABLE class without a per-role config stanza — schema-plane
    # "full tool access" was hollow while the execution plane still refused
    # ``git push``. Three properties are load-bearing and must not be softened:
    #
    # * it never touches the hard floor (``GRANTABLE_COMMAND_CLASSES`` is the
    #   bound, and the not-grantable branch below is unchanged),
    # * it never applies to an ungoverned lane (we returned ``None`` above), and
    # * it is RECEIPTED exactly like a config grant. This is the compensating
    #   control the ruling trades the refusal for: preventive → detective. If a
    #   future change makes a mode-granted command run without
    #   ``record_envelope_decision`` seeing it, the ruling's safety argument is
    #   gone, not merely weakened.
    if resolved.unbounded and command_class in GRANTABLE_COMMAND_CLASSES:
        return TerminalEnvelopeDecision(
            outcome=OUTCOME_GRANTED,
            lane=resolved.lane,
            role=resolved.role,
            command_class=command_class,
            reason=reason,
            # No config key: this grant did not come from the grants table, and
            # naming one would send an operator to a stanza that is not why the
            # command ran.
            config_key=None,
            summary=(
                f"'{command_class}' is granted by permission_mode="
                f"'{permission_mode}' (runtime default) for role '{grants.role}' on "
                f"the '{resolved.lane}' lane; the command is recorded in "
                f"{ENVELOPE_DECISION_LOG}."
            ),
            persona_id=resolved.persona_id,
            session_id=resolved.session_id,
            grant_issues=grants.issues,
            permission_mode=permission_mode,
            grant_source=GRANT_SOURCE_PERMISSION_MODE,
        )

    if command_class not in GRANTABLE_COMMAND_CLASSES:
        return TerminalEnvelopeDecision(
            outcome=OUTCOME_REFUSE,
            lane=resolved.lane,
            role=resolved.role,
            command_class=command_class,
            reason=reason,
            failure_class=ENVELOPE_COMMAND_NOT_GRANTABLE,
            config_key=None,
            summary=(
                f"This command {CLASS_SUMMARY.get(command_class, 'is envelope-gated')} "
                f"('{command_class}'), which is a hard floor of the Harness execution "
                "safety envelope. No configuration grants it."
            ),
            fix_hint=(
                "There is no config key for this class and no permission mode lifts it. "
                "Do not retry or reword the command — achieve the goal without it, or ask "
                "the operator to perform this step themselves."
            ),
            persona_id=resolved.persona_id,
            session_id=resolved.session_id,
            grant_issues=grants.issues,
            permission_mode=permission_mode,
        )

    return TerminalEnvelopeDecision(
        outcome=OUTCOME_REFUSE,
        lane=resolved.lane,
        role=resolved.role,
        command_class=command_class,
        reason=reason,
        failure_class=ENVELOPE_COMMAND_REQUIRES_GRANT,
        config_key=config_key,
        summary=(
            f"This command {CLASS_SUMMARY.get(command_class, 'is envelope-gated')} "
            f"('{command_class}'), and role '{grants.role}' holds no grant for it on the "
            f"'{resolved.lane}' lane."
        ),
        fix_hint=_grant_fix_hint(
            role=grants.role, lane=resolved.lane, command_class=command_class, grants=grants
        ),
        persona_id=resolved.persona_id,
        session_id=resolved.session_id,
        grant_issues=grants.issues,
        permission_mode=permission_mode,
    )


def _grant_fix_hint(
    *, role: str, lane: str, command_class: str, grants: TerminalEnvelopeGrants
) -> str:
    """The operator-actionable half of the refusal the AGENT reads.

    Names the exact ROOT-config key, shows the stanza, and states plainly that
    the agent cannot grant it — the audit's finding was that the lane's own
    operative-rules text tells the model "the operator's current permission
    grant is the only gate", which is false. A refusal that does not say where
    the real gate lives reproduces that lie one layer down.
    """

    lines = [
        "This session is running under a RESTRICTED permission mode — the runtime "
        "default grants every operator-grantable envelope class, so a refusal here "
        "means an operator narrowed this session (`hermes harness persona permission "
        "set --mode bounded|read_only`) or the runtime default was configured "
        "narrower. Ask the operator to lift the restriction, or:",
        "",
        "An OPERATOR can allow just this class by adding this to the ROOT config.yaml "
        "(not a profile config — a profile cannot grant itself this):",
        "",
        "  agent_runtime:",
        "    terminal_envelope:",
        "      grants:",
        f"        {role}:",
        f"          {lane}: [{command_class}]",
        "",
        f"Config key: {grant_config_key(role=role, lane=lane)}",
        "The grant is read at turn time from the root config; a running "
        "`hermes harness serve` picks it up on its next turn.",
        "Do NOT retry, reword, or split this command — every form of it resolves to the "
        f"same '{command_class}' class and will be refused identically. Report the refusal "
        "to the operator and continue with the work you can do.",
    ]
    if grants.issues:
        lines.append("")
        lines.append("The existing grant stanza has problems that grant nothing:")
        for issue in grants.issues:
            lines.append(f"  - [{issue.code}] {issue.summary}")
    return "\n".join(lines)


def refusal_text(decision: TerminalEnvelopeDecision) -> str:
    """The full refusal an agent sees, self-explanatory by construction."""

    head = f"BLOCKED by Harness execution safety envelope: {decision.reason or decision.failure_class}"
    parts = [head, decision.summary]
    if decision.fix_hint:
        parts.append(decision.fix_hint)
    return "\n".join(part for part in parts if part)


# ── provenance ──────────────────────────────────────────────────────────────

#: Every governed decision that mattered (granted or refused) lands here.
ENVELOPE_DECISION_LOG = "terminal_envelope_decisions.jsonl"
#: Refusals ALSO keep landing in the pre-existing envelope audit lane so
#: operators watching that file lose nothing.
BLOCKED_ATTEMPT_LOG = "blocked_tool_attempts.jsonl"


#: Where a receipt root came from. Recorded on the row so an operator reading
#: a receipt can tell a scope-carried root from a resolved one.
AUDIT_ROOT_SOURCE_SCOPE = "scope"
AUDIT_ROOT_SOURCE_ENV = "env"
AUDIT_ROOT_SOURCE_RESOLVER = "resolver"


def _audit_root(scope: TerminalEnvelopeScope | None) -> Path | None:
    """The directory a governed decision's receipt lands in — a full ladder.

    Three rungs, first non-empty wins: the bound scope's ``runtime_root``, then
    ``HERMES_AGENT_RUNTIME_ROOT``, then the canonical resolver
    (``paths.store_root`` → ``resolution.resolve_runtime``, which has its own
    env → config → default ladder and always answers).

    The third rung is the point. Rungs 1 and 2 can BOTH be empty — ``scope`` is
    optional and ``scope_for_persona``'s ``runtime_root`` argument defaults to
    ``None``, and a profile-less persona never exports the variable
    (``profile_context.py``'s ``profile_home is None`` early-yield). Without a
    resolver rung, that combination silently drops the receipt for a decision
    that was itself made deterministically — the SAME ambient-presence
    dependence this module was written to retire, one layer down: the decision
    became a policy, but whether anyone could later prove it happened was still
    decided by process ancestry.

    Never raises: the resolver can refuse (``ProbeIsolationViolation`` under
    ``HERMES_REQUIRE_ISOLATED_ROOT``), and auditability must not gate the
    answer. Returning ``None`` after all three rungs means "genuinely nowhere
    to write", not "nobody exported a variable".
    """

    root = ""
    if scope is not None:
        root = str(scope.runtime_root or "").strip()
    if not root:
        root = str(os.getenv("HERMES_AGENT_RUNTIME_ROOT", "") or "").strip()
    if not root:
        try:
            from .paths import store_root

            root = str(store_root() or "").strip()
        except Exception:
            # A refusing resolver (probe isolation) or an unimportable module
            # is a real "nowhere to write" — fall through to None rather than
            # inventing a path.
            logger.debug("Envelope audit root unresolvable", exc_info=True)
            root = ""
    if not root:
        return None
    try:
        return Path(root).expanduser()
    except Exception:  # pragma: no cover - defensive
        return None


def audit_root_source(scope: TerminalEnvelopeScope | None) -> str:
    """Which rung of :func:`_audit_root` answered. Pure; for receipts + tests."""

    if scope is not None and str(scope.runtime_root or "").strip():
        return AUDIT_ROOT_SOURCE_SCOPE
    if str(os.getenv("HERMES_AGENT_RUNTIME_ROOT", "") or "").strip():
        return AUDIT_ROOT_SOURCE_ENV
    return AUDIT_ROOT_SOURCE_RESOLVER


def _append_jsonl(root: Path, name: str, event: Mapping[str, Any]) -> None:
    try:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        logger.warning("Failed to write terminal-envelope audit row", exc_info=True)


def record_envelope_decision(
    decision: TerminalEnvelopeDecision,
    command: str,
    *,
    scope: TerminalEnvelopeScope | None = None,
) -> None:
    """Write the receipt. Never raises — auditability must not gate the answer.

    A granted command is recorded with its GRANT PROVENANCE (which config key
    allowed it, for which role and lane), because "it ran because the operator
    said so" is the only honest record of a command the envelope would
    otherwise have blocked.
    """

    if decision.outcome == OUTCOME_ALLOW:
        return
    resolved = scope if scope is not None else current_terminal_envelope_scope()
    root = _audit_root(resolved)
    if root is None:
        return
    preview = str(command or "")[:500]
    event: dict[str, Any] = {
        "ts": time.time(),
        "tool": "terminal",
        # Which rung answered, so a receipt is self-describing: an operator
        # reading a row never has to guess whether the root came from the run's
        # own scope or from ambient process state.
        "audit_root_source": audit_root_source(resolved),
        "decision": decision.outcome,
        "lane": decision.lane,
        "role": decision.role,
        "persona_id": decision.persona_id,
        "session_id": decision.session_id,
        "command_class": decision.command_class,
        "reason": decision.reason,
        "failure_class": decision.failure_class,
        # WHY it ran (or was refused under which posture). ``granted_by`` keeps
        # naming the ROOT-config key for a config grant and names the MODE for a
        # permission-mode grant, so no granted command lands in this log without
        # its provenance — the compensating control the 2026-08-09 ruling
        # substitutes for the refusal it removed.
        "granted_by": decision.granted_by,
        "grant_source": decision.grant_source or None,
        "permission_mode": decision.permission_mode or None,
        "command_preview": preview,
    }
    if decision.grant_issues:
        event["grant_issues"] = [issue.row() for issue in decision.grant_issues]
    _append_jsonl(root, ENVELOPE_DECISION_LOG, event)
    if decision.refused:
        _append_jsonl(
            root,
            BLOCKED_ATTEMPT_LOG,
            {
                "ts": event["ts"],
                "tool": "terminal",
                "reason": decision.reason or decision.failure_class,
                "command_preview": preview,
                "failure_class": decision.failure_class,
                "command_class": decision.command_class,
                "lane": decision.lane,
                "role": decision.role,
                "config_key": decision.config_key,
            },
        )


def record_legacy_block(
    command: str,
    reason: str,
    *,
    scope: TerminalEnvelopeScope | None = None,
) -> bool:
    """Write the receipt for a LEGACY (pattern-table) block. Audit Q3.

    ``tools/terminal_tool.py::_log_harness_blocked_attempt`` writes the same row
    but resolves its root as ``os.getenv("HERMES_AGENT_RUNTIME_ROOT")`` and
    **returns early when that is unset** — the command was blocked and nothing
    recorded it, silently. That is reader #4's bug one file over, and reader #4
    is the reason this module already owns a three-rung ladder (:func:`_audit_root`).

    This is the fork-owned replacement for that body. It exists on this side of
    the ``tools/`` boundary so the upstream change is a ONE-LINE delegation
    rather than a re-derivation:

    .. code-block:: diff

        -def _log_harness_blocked_attempt(command: str, reason: str) -> None:
        -    root = os.getenv("HERMES_AGENT_RUNTIME_ROOT", "").strip()
        -    if not root:
        -        return
        -    try:
        -        path = Path(root).expanduser() / "blocked_tool_attempts.jsonl"
        -        ...
        +def _log_harness_blocked_attempt(command: str, reason: str) -> None:
        +    from agent_runtime.terminal_envelope import record_legacy_block
        +
        +    record_legacy_block(command, reason)

    Until that lands, the legacy writer keeps its silent-drop branch on runs
    that bind NO envelope scope. Runs that DO bind one never reach it at all:
    :func:`envelope_decision` answers for every harness lane (Q2), so
    ``_harness_envelope_block`` returns before the legacy table is consulted and
    :func:`record_envelope_decision` writes the receipt through the same ladder.

    Row shape is the legacy one verbatim (``ts``/``tool``/``reason``/
    ``command_preview``) so existing ``blocked_tool_attempts.jsonl`` readers
    need no change, plus ``audit_root_source`` so a receipt says which rung
    resolved its root. Returns whether a row was written; never raises.
    """

    resolved = scope if scope is not None else current_terminal_envelope_scope()
    root = _audit_root(resolved)
    if root is None:
        return False
    _append_jsonl(
        root,
        BLOCKED_ATTEMPT_LOG,
        {
            "ts": time.time(),
            "tool": "terminal",
            "reason": str(reason or ""),
            "command_preview": str(command or "")[:500],
            "audit_root_source": audit_root_source(resolved),
        },
    )
    return True


def blocked_result(decision: TerminalEnvelopeDecision) -> dict[str, Any]:
    """The terminal-tool JSON payload for a refusal.

    Keeps every key the legacy hard block emitted (``status``, ``blocked_by``,
    ``block_reason``) so nothing downstream has to learn a new shape, and adds
    the typed fields that make the refusal self-explanatory.
    """

    return {
        "output": "",
        "exit_code": -1,
        "error": refusal_text(decision),
        "status": "blocked",
        "blocked_by": "harness_execution_safety",
        "block_reason": decision.reason or decision.failure_class,
        "failure_class": decision.failure_class,
        "command_class": decision.command_class,
        "lane": decision.lane,
        "role": decision.role,
        "config_key": decision.config_key,
        "requirement_failure": decision.row(),
    }


def scope_for_persona(
    persona: Any,
    *,
    lane: str = LANE_MISSION_CHAT,
    session_id: str | None = None,
    runtime_root: Any = None,
    permission_mode: str | None = None,
) -> TerminalEnvelopeScope:
    """Build the run scope from a persona. Never raises on a odd role value."""

    from .personas import role_from_persona

    try:
        role = str(role_from_persona(persona))
    except Exception:  # pragma: no cover - defensive
        role = str(getattr(persona, "role", "") or "")
    return TerminalEnvelopeScope(
        lane=str(lane or "").strip(),
        role=role,
        persona_id=str(getattr(persona, "id", "") or ""),
        session_id=str(session_id or ""),
        runtime_root=str(runtime_root or ""),
        permission_mode=str(permission_mode or ""),
    )


def explain_terminal_envelope(
    *,
    role: str,
    lane: str = LANE_MISSION_CHAT,
    cfg: Any | None = None,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    """Operator view: what this role may do on this lane, and why. No side effects.

    ``permission_mode`` makes the view match :func:`envelope_decision`: an
    ``unbounded`` run is granted every grantable class by MODE, so a view that
    only read the grants table would show classes as refused that the very next
    command would run. Left unset the answer is the config-grants-only view,
    byte-identical to the pre-2026-08-09 shape.
    """

    grants = resolve_terminal_envelope_grants(role=role, lane=lane, cfg=cfg)
    mode = str(permission_mode or "")
    granted_by_mode: frozenset[str] = frozenset()
    if lane in GOVERNED_LANES and permission_mode_is_unbounded(mode):
        granted_by_mode = GRANTABLE_COMMAND_CLASSES - grants.classes
    granted = grants.classes | granted_by_mode
    return {
        "lane": lane,
        "role": grants.role,
        "governed": lane in GOVERNED_LANES,
        "config_key": grants.config_key,
        "command_classes": sorted(COMMAND_CLASSES),
        "grantable_command_classes": sorted(GRANTABLE_COMMAND_CLASSES),
        "granted": sorted(granted),
        "granted_by_config": sorted(grants.classes),
        "granted_by_permission_mode": sorted(granted_by_mode),
        "permission_mode": mode,
        "refused": sorted(COMMAND_CLASSES - granted),
        "grant_issues": grants.issue_rows(),
    }


def hard_floor_command_classes() -> frozenset[str]:
    """The classes no configuration can grant (empty after ruling R-2).

    Read-only accessor over the two canonical sets above, added so operator
    surfaces (``harness persona tool-diff --explain-envelope``) can name the hard
    floor without re-deriving it from a hand-listed set of their own. The
    grantable/hard-floor split is exactly the distinction
    :data:`ENVELOPE_COMMAND_NOT_GRANTABLE` exists for: telling an agent — or an
    operator — to ask for a grant that cannot exist would be a new lie.
    """

    return frozenset(COMMAND_CLASSES - GRANTABLE_COMMAND_CLASSES)


__all__ = [
    "AUDIT_ROOT_SOURCE_ENV",
    "AUDIT_ROOT_SOURCE_RESOLVER",
    "AUDIT_ROOT_SOURCE_SCOPE",
    "BLOCKED_ATTEMPT_LOG",
    "CLASS_SUMMARY",
    "COMMAND_CLASSES",
    "DESTRUCTIVE_GIT",
    "ENVELOPE_COMMAND_NOT_GRANTABLE",
    "ENVELOPE_COMMAND_REQUIRES_GRANT",
    "ENVELOPE_DECISION_LOG",
    "GIT_PUSH",
    "GOVERNED_LANES",
    "GRANTABLE_COMMAND_CLASSES",
    "GRANT_CLASS_NOT_GRANTABLE",
    "GRANT_MALFORMED",
    "GRANT_SOURCE_CONFIG",
    "GRANT_SOURCE_PERMISSION_MODE",
    "GRANT_UNKNOWN_COMMAND_CLASS",
    "LANE_MISSION_CHAT",
    "LEGACY_REASON_BY_CLASS",
    "NETWORK_EGRESS",
    "OUTCOME_ALLOW",
    "OUTCOME_GRANTED",
    "OUTCOME_REFUSE",
    "RECURSIVE_DELETE",
    "TerminalEnvelopeDecision",
    "TerminalEnvelopeGrantIssue",
    "TerminalEnvelopeGrants",
    "TerminalEnvelopeScope",
    "audit_root_source",
    "blocked_result",
    "canonical_role",
    "classify_command",
    "current_terminal_envelope_scope",
    "envelope_config",
    "envelope_decision",
    "explain_terminal_envelope",
    "grant_config_key",
    "hard_floor_command_classes",
    "legacy_reason_for_class",
    "record_envelope_decision",
    "record_legacy_block",
    "refusal_text",
    "resolve_terminal_envelope_grants",
    "scope_for_persona",
    "terminal_envelope_scope",
]
