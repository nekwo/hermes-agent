"""Permanent compatibility import for the upstream profile endpoint."""

from __future__ import annotations

from agent_runtime.personas import promote_profile_to_persona

# ``promote_profile_to_persona`` now lives in ``agent_runtime.personas`` — it is
# persona lifecycle, not stage routing, and it
# has a live caller outside this package (mission-lane removal, S1).
#
# This re-export is NOT cosmetic. Upstream-owned ``hermes_cli/web_server.py:12671``
# (``POST /api/profiles/{name}/promote``) does
# ``from agent_runtime.blueprints.resolve import promote_profile_to_persona``, and
# the fork may not edit upstream files, so this import path must keep resolving for
# as long as that endpoint exists. See the S1 report: whoever deletes
# ``agent_runtime/blueprints/`` in S7 must leave a shim behind or get the operator to
# accept an upstream edit.
__all__ = ["promote_profile_to_persona"]
