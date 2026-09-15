"""Downstream terminal envelope and interactive-command policy."""
import json
import logging
import os
import re
from typing import Optional, Dict, Any, Tuple
from tools.terminal_tool_guards import _looks_like_help_or_version_command, _strip_quotes
logger = logging.getLogger("tools.terminal_tool")

_HARNESS_NETWORK_ALLOWLIST = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

_HARNESS_BLOCK_PATTERNS = (
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), "git_push_requires_operator_approval"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+reset\s+--(?:merge|keep)\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+clean\b[^\n;&|]*(?:-[^\s]*[xdf]|/[xdf])", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+checkout\b[^\n;&|]*\s(?:--force|-f)\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+checkout\b[^\n;&|]*(?:\s--(?:\s|$)|\s--(?:pathspec-from-file|pathspec-file-nul)\b)", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+checkout\b[^\n;&|]*\s(?:\.|:/|:\\)(?:\s|$)", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+switch\b[^\n;&|]*\s(?:--force|-f)\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+restore\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bgit\s+stash\s+(?:drop|clear)\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\brm\s+-[^\n;&|]*r[^\n;&|]*f\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\bRemove-Item\b[^\n;&|]*(?:-Recurse|-r)\b", re.IGNORECASE), "tree_wipe_blocked"),
    (re.compile(r"\b(?:cat|type|Get-Content)\b[^\n;&|]*(?:\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)", re.IGNORECASE), "credential_read_blocked"),
    (re.compile(r"\b(?:kubectl|helm)\s+(?:apply|delete|rollout|scale|patch)\b", re.IGNORECASE), "prod_operation_requires_operator_approval"),
    (re.compile(r"\bterraform\s+(?:apply|destroy)\b", re.IGNORECASE), "prod_operation_requires_operator_approval"),
)

_HERMES_INTERACTIVE_SUBCOMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*|&&\s*|\|\|\s*)"
    r"(?:\S*[\\/])?hermes(?:\.exe)?"
    r"(?:\s+(?:-p|--profile)\s+\S+)?"
    r"\s+(?:tools|setup)\b",
    re.IGNORECASE,
)


def _harness_safety_block(command: str) -> str | None:
    if not os.getenv("HERMES_AGENT_RUNTIME_ROOT", "").strip():
        return None
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return None
    for pattern, reason in _HARNESS_BLOCK_PATTERNS:
        if pattern.search(normalized):
            return reason
    network_reason = _harness_network_block_reason(normalized)
    if network_reason:
        return network_reason
    return None


def _harness_network_block_reason(command: str) -> str | None:
    lowered = command.lower()
    if not re.search(r"\b(curl|wget|iwr|Invoke-WebRequest|Invoke-RestMethod)\b", command, re.IGNORECASE):
        return None
    urls = re.findall(r"https?://([^/\s'\"`]+)", command, flags=re.IGNORECASE)
    if not urls:
        return "network_command_requires_allowlist"
    for host in urls:
        clean_host = host.split(":", 1)[0].strip("[]").lower()
        if clean_host not in _HARNESS_NETWORK_ALLOWLIST:
            return "network_command_requires_allowlist"
    if re.search(r"(secret|token|password|credential|api[_-]?key|authorization|bearer)", lowered):
        return "credential_exfil_blocked"
    return None


def _harness_envelope_gate(
    command: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """The single envelope decision for this command, as ``(block, provenance)``.

    ``block`` is the refusal payload, or ``None`` to proceed — the historical
    contract, preserved verbatim under :func:`_harness_envelope_block`.

    ``provenance`` is the second half, added 2026-08-09. It is non-``None`` only
    when the command was GRANTED (a formerly-refused gated class that ran
    because a config grant or the run's permission mode allowed it), and it
    carries the account of WHY into the tool result the agent actually reads.
    Without it the granted path was silent: the runtime recorded the receipt and
    the agent — the receipt's own subject — had no way to know it existed, so an
    agent asked to confirm the mechanism could only report it missing. Every
    other command (ungoverned lane, ungated class) gets ``None`` and pays
    nothing; see ``agent_runtime.terminal_envelope.envelope_provenance``.

    Two layers, in order:

    1. **The governed-lane policy** (``agent_runtime.terminal_envelope``). When
       a run has bound a :class:`TerminalEnvelopeScope` for a governed lane
       (today: mission-chat only), that module is the ONE decision point: an
       ungranted gated command is a typed refusal that names the ROOT-config
       key which would grant it, and a granted one runs with its provenance
       recorded. This layer does NOT consult ``HERMES_AGENT_RUNTIME_ROOT``,
       which is what closes the historical fail-open branch — a mission-chat
       persona that binds no Hermes profile never exported that variable, so
       the envelope was inert and ``tools/approval.py``'s non-interactive
       fail-open default let ``git push`` through, unrecorded (both branches
       observed live 2026-07-26; see the module docstring of
       ``agent_runtime/terminal_envelope.py``).
    2. **The legacy envelope** — unchanged, and reached on every ungoverned
       lane (worker ticks, free-chat, ``hermes chat``, cron, gateway, acp) plus
       whenever the policy module cannot be imported. Import failure falls back
       to the hard block: fail CLOSED, never open.
    """

    if not isinstance(command, str):
        # A non-string command has no class to classify and no envelope opinion.
        # It falls through to ``_terminal_tool_run``'s typed invalid-command
        # error, which is where it was answered before this gate moved ahead of
        # that check — the ordering is preserved deliberately, not incidentally.
        return None, None

    try:
        from agent_runtime.terminal_envelope import (
            blocked_result,
            envelope_decision,
            envelope_provenance,
            record_envelope_decision,
        )

        decision = envelope_decision(command)
    except Exception:
        # Fail closed: an unavailable policy module means the legacy envelope
        # below decides, exactly as it did before this seam existed.
        logger.debug("Terminal envelope policy unavailable; using legacy envelope", exc_info=True)
        decision = None
    else:
        if decision is not None:
            receipted = bool(record_envelope_decision(decision, command))
            if decision.refused:
                return blocked_result(decision), None
            # Granted, or not an envelope-gated command at all. Either way the
            # governed lane has spoken and the legacy pattern table must not
            # re-block what an operator grant just allowed. A GRANT also reports
            # its provenance upward; an ungated command resolves to ``None`` and
            # the result is byte-identical to before.
            return None, envelope_provenance(decision, receipted=receipted)

    reason = _harness_safety_block(command)
    if reason is None:
        return None, None
    _log_harness_blocked_attempt(command, reason)
    return {
        "output": "",
        "exit_code": -1,
        "error": f"BLOCKED by Harness execution safety envelope: {reason}",
        "status": "blocked",
        "blocked_by": "harness_execution_safety",
        "block_reason": reason,
    }, None


def _harness_envelope_block(command: str) -> Optional[Dict[str, Any]]:
    """The refusal half of :func:`_harness_envelope_gate`, unchanged.

    Kept as the narrow "may this run?" question for callers that do not compose
    a tool result — the decision and its receipt are identical either way.
    """

    return _harness_envelope_gate(command)[0]


def _with_envelope_provenance(
    result: str,
    provenance: Optional[Dict[str, Any]],
) -> str:
    """Merge a grant's provenance into the tool's JSON result string.

    Best-effort by construction and in that order of priority: observability
    must never damage the answer. Anything unexpected — a non-dict payload, a
    result that is not JSON at all — returns the ORIGINAL string untouched
    rather than raising or substituting a synthesized one. The agent losing a
    provenance block is a degraded turn; the agent losing its command output is
    a broken one.
    """

    if not provenance:
        return result
    try:
        payload = json.loads(result)
        if not isinstance(payload, dict):
            return result
        payload.update(provenance)
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        logger.debug("Could not attach envelope provenance to terminal result", exc_info=True)
        return result


def _log_harness_blocked_attempt(command: str, reason: str) -> None:
    try:
        from agent_runtime.terminal_envelope import record_legacy_block

        record_legacy_block(command, reason)
    except Exception:
        logger.warning("Failed to write Harness blocked-command audit", exc_info=True)


def _interactive_cli_guidance(command: str) -> str | None:
    """Block known interactive Hermes CLIs in non-PTY foreground tool calls.

    ``hermes tools`` and ``hermes setup`` launch menu UIs and wait for stdin.
    In gateway/Telegram turns that looks like Alice is stuck, especially if the
    command is piped through ``head`` and the subprocess tree keeps waiting.
    Prefer non-interactive config inspection or explicit PTY usage.
    """
    if _looks_like_help_or_version_command(command):
        return None
    if _HERMES_INTERACTIVE_SUBCOMMAND_RE.search(_strip_quotes(command)):
        return (
            "This command starts an interactive Hermes menu (for example "
            "`hermes tools` or `hermes setup`) and can hang in non-PTY gateway "
            "turns. Use non-interactive config/file inspection, add an explicit "
            "non-interactive subcommand/flag if available, or rerun with pty=true "
            "only when interactive input is actually required."
        )
    return None
