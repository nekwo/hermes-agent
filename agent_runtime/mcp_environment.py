"""Downstream MCP machine-root and transient environment overlays."""
import logging
import os
import re
from typing import Dict
logger = logging.getLogger("tools.mcp_tool")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MCP_ENV_OVERRIDE_PREFIX = "HERMES_MCP_ENV_"


def _normalize_mcp_env_server_name(name: str) -> str:
    """Normalize a server name into an env-var-safe uppercase token.

    Non-alphanumeric runs collapse to single underscores so the
    ``HERMES_MCP_ENV_<NAME>_<KEY>`` shape is unambiguous shell syntax.
    Returns an empty string for empty/None input.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).upper().strip("_")


def _get_process_mcp_env_overrides(server_name: str) -> dict:
    """Extract ``HERMES_MCP_ENV_<SERVER>_<KEY>=VALUE`` entries for one server.

    Returns a mapping of child env names (the part after the prefix) to
    their string values. Names that fail ``_ENV_VAR_NAME_RE`` are skipped
    with a WARNING log. The warning intentionally logs only the full
    process-env key name (which the user typed) and never the value.
    """
    normalized = _normalize_mcp_env_server_name(server_name)
    if not normalized:
        return {}
    prefix = f"{_MCP_ENV_OVERRIDE_PREFIX}{normalized}_"
    overrides: dict = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        child_key = key[len(prefix):]
        if not _ENV_VAR_NAME_RE.match(child_key):
            logger.warning(
                "Ignoring MCP env override with invalid child name for "
                "server %r (process env key %r)",
                server_name,
                key,
            )
            continue
        overrides[child_key] = value
    return overrides


def _resolve_machine_root_tokens(servers: Dict[str, dict]) -> Dict[str, dict]:
    """Apply the machine-root / platform-gate chokepoint to an mcp_servers map.

    Fails OPEN on import error only (a checkout without ``agent_runtime``
    behaves exactly as before); it never invents a path. Resolution itself
    fails LOUD — unresolvable servers are dropped and logged with a typed code.
    """
    try:
        from agent_runtime.machine_roots import resolve_mcp_servers
    except Exception:  # pragma: no cover — agent_runtime is optional at this layer
        return servers
    return resolve_mcp_servers(servers)
