"""Permanent compatibility import for the upstream profile endpoint."""

from __future__ import annotations

from agent_runtime.personas import promote_profile_to_persona

# ``promote_profile_to_persona`` now lives in ``agent_runtime.personas`` — it is
# persona lifecycle, not stage routing, and it
# has a live caller outside this package (mission-lane removal, S1).
#
# This re-export is NOT cosmetic. The promotion endpoint
# (``POST /api/profiles/{name}/promote``) does
# ``from agent_runtime.blueprints.resolve import promote_profile_to_persona``.
# Since the 2026-07-31 upstream sync the route lives in the fork-ported
# ``hermes_cli/web_routers/profiles.py`` (upstream extracted the profile router
# out of ``web_server.py``); the import path must keep resolving for as long as
# that endpoint exists. See the S1 report and doc 18's executed-merge record.
__all__ = ["promote_profile_to_persona"]
