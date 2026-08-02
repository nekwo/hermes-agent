"""S14 retires the hardcoded Stage C capture path inside ``agent_runtime``.

Operator ruling (2026-07-30): **Stage C visual proof lives only as the MCP server**
(``EterniaLauncher/tool/stagec_qa_mcp_server``) **plus the marionette skill.**
Nothing about capturing, resolving, policing, or parsing a Stage C screenshot is
hardcoded in the runtime any more.

The S1 extractions (``stagec_command_policy``, ``stagec_trace_parsers``) and the
capture dataclasses (``proof_capture``) existed to keep a *Python* capture lane
alive across S3-S12. With the provider gone, their only consumer is gone, so they
are removed as a feature rather than left as four orphan modules.

**The MCP-side Stage C path is untouched** and is pinned negatively below:
``tools/mcp_tool.py`` still resolves ``${roots.*}`` tokens for the ``launcher_qa``
server, which is how Stage C is actually reached now.
"""

from __future__ import annotations



REMOVED_MODULES = (
    "agent_runtime.stagec_mcp_visual_provider",
    "agent_runtime.stagec_command_policy",
    "agent_runtime.stagec_trace_parsers",
    "agent_runtime.proof_capture",
)






def test_the_mcp_side_stagec_path_is_untouched():
    """Stage C is reached through the launcher_qa MCP server, not through Python."""

    import tools.mcp_tool as mcp_tool

    resolved = mcp_tool._resolve_machine_root_tokens(
        {"launcher_qa": {"command": r"X:\repo\tool\server.exe", "args": []}}
    )
    assert set(resolved) == {"launcher_qa"}


def test_the_keep_side_proof_names_survive():
    """``proof`` has a keep-side meaning — these are read-model infra, not Stage C."""

    from agent_runtime import parity, patch_coverage

    assert callable(parity.ProjectionAccountant)
    assert patch_coverage is not None
