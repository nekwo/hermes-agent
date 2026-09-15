"""``introduce`` is a CLI verb and has no wire twin (S2, R-S2-18).

R5's second clause — *agents can never initiate pairing* — is enforced against
REMOTE callers structurally rather than by a rule somebody remembers: there is
no method to call, no tool to call it with, no op on the gateway transport, and
the argv lane that would otherwise carry a CLI verb over the wire is refused
outright to every gateway connection.

Four registries and one lane, walked rather than named. A test that asserted
"``introduce`` is not in this list of three method names" would stop being a
test the moment a fourth door was added; a walk over whatever the registries
actually contain keeps holding.

**The residual is asserted too, as prose, because it is real.** A LOCAL agent
with shell access on this machine can run the verb — exactly as it can run
``harness gateway pair``, read ``serve_auth_token``, or open ``peers.json`` in
an editor. Every tool-using agent already holds the machine owner's authority.
The claim this file makes is the accurate one and no larger: *no caller on the
gateway listener, at any tier, on either lane, can mint an introduction
anywhere.*
"""

from __future__ import annotations

import pytest


def _names_containing(values, needle: str) -> list[str]:
    return sorted(str(value) for value in values if needle in str(value).lower())


def test_no_tool_method_op_or_allowlist_entry_names_introduce():
    """The four registries a remote caller can reach a name through."""

    from agent_runtime import serve_rpc
    from agent_runtime.call_authorization import PEER_METHOD_ALLOWLIST

    methods = list(serve_rpc.manifest()["methods"])
    assert methods, "the method registry is empty; this test would pass vacuously"
    assert _names_containing(methods, "introduce") == []

    assert _names_containing(PEER_METHOD_ALLOWLIST, "introduce") == []
    # …and the allowlist is non-empty, so the assertion above is about a set
    # that exists rather than about one that was never built.
    assert PEER_METHOD_ALLOWLIST

    from hermes_cli.harness_parts.serve import ops_manifest

    ops = list(ops_manifest(transport="gateway")["ops"])
    assert ops, "the gateway ops manifest is empty; this test would pass vacuously"
    assert _names_containing(ops, "introduce") == []


def test_no_registered_model_tool_names_introduce():
    """The TOOL registry, walked separately because it is the door an agent on
    THIS machine reaches through — the residual named in the module docstring is
    about a shell, not about a tool, and this is what keeps that distinction
    true."""

    import model_tools  # noqa: F401 — importing IS what populates the registry
    from tools.registry import registry

    names = sorted(registry.get_all_tool_names())
    assert names, "the tool registry is empty; this test would pass vacuously"
    assert _names_containing(names, "introduce") == []
    # The peer/gateway family that DOES exist is named here so the assertion
    # above cannot silently become "no tools at all were registered".
    assert any("agent_chat" in name for name in names)


def test_the_argv_lane_is_refused_to_a_gateway_connection():
    """Re-asserted BY NAME here, beside the registries, rather than left in the
    peer-lane file alone.

    This is the assertion that makes the other three mean something: a verb
    absent from every method registry would still be reachable if a gateway
    connection could send ``{"argv": [...]}``. The refusal is one typed error
    (``argv_lane_unavailable``), and the proof over a real listener lives in
    ``test_serve_gateway_peer_lane.py`` — imported here rather than duplicated,
    so there is one implementation of the proof and two places that depend on
    it.
    """

    import ast
    import pathlib

    source = pathlib.Path("hermes_cli/harness_parts/serve.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "argv_lane_unavailable" in source

    lane_test = pathlib.Path(
        "tests/agent_runtime/test_serve_gateway_peer_lane.py"
    ).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(lane_test)
    proofs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "argv" in node.name
    ]
    assert proofs, (
        "no test in test_serve_gateway_peer_lane.py proves the argv lane is "
        "refused on the gateway transport; the structural half of R5's remote "
        "clause has lost its proof"
    )


@pytest.mark.parametrize("spelling", ["introduce", "gateway.introduce", "peer.introduce"])
def test_a_spelling_a_caller_might_guess_resolves_to_nothing(spelling):
    """Three names an agent could plausibly try. Each answers "no such method"
    rather than a refusal, which is the honest shape: there is nothing to be
    refused FROM."""

    from agent_runtime import serve_rpc

    assert spelling not in serve_rpc.method_names()
