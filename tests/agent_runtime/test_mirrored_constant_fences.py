"""Equality fences for constants that are deliberately spelled in two places.

Some constants in this repo are re-spelled rather than single-homed, on purpose:
importing across the seam would invert a dependency or drag a heavy module into
a process that wants none of it. The repo's ruling on that shape is not "never
duplicate" — it is ``agent_create.py:523-533``'s precedent: **re-spell, then
fence**. ``tests/agent_runtime/test_agent_create_service.py`` pins the four
``ERR_*`` codes against ``serve_rpc``'s same-named ones, so a one-sided edit
goes red instead of silently forking a wire contract.

This module extends that precedent to the two mirror pairs that had the comment
but not the gate. A comment is not a gate: it does not fail. Each assertion below
names BOTH files, because the thing a red here has to tell you is which of the
two spellings someone moved.

Anti-vacuity note: these tests import the real modules and compare the real
attributes. There is no fixture, no monkeypatch and no seam to mock — a
mutation of either constant is the only way to make one fail, which is exactly
the mutation the fence exists to catch.
"""

from __future__ import annotations


def test_the_mission_chat_lane_token_is_one_spelling_in_two_files():
    """``"mission_chat"`` is a wire token: it rides admission decisions out of
    ``mcp_admission`` and it is the lane ``terminal_envelope`` governs. The two
    modules do not import each other (neither is on the other's import path), so
    only this fence stands between them and two different lanes wearing one name.
    """

    from agent_runtime import mcp_admission, terminal_envelope

    assert mcp_admission.LANE_MISSION_CHAT == terminal_envelope.LANE_MISSION_CHAT, (
        "LANE_MISSION_CHAT has drifted: agent_runtime/mcp_admission.py and "
        "agent_runtime/terminal_envelope.py must spell the mission-chat lane "
        "token identically. Admission stamps one value onto its decisions and "
        "the terminal slice gates on the other, so a one-sided edit here means "
        "a persona turn is admitted to a lane nothing governs."
    )


def test_the_dispatch_reply_bound_is_one_number_in_three_files():
    """The reply-truncation bound is spelled three times along one path: the
    relay tool truncates at ``_REPLY_LIMIT`` before handing the reply over
    (``tools/agent_chat_tool.py``), the store bounds the persisted row
    (``dispatch_store.py``), and the delivery lane bounds the text it carries
    into the forged turn (``dispatch_delivery.py``). Three bounds that disagree
    do not raise — they truncate an answer twice, at two different characters.
    """

    from agent_runtime import dispatch_delivery, dispatch_store
    from tools import agent_chat_tool

    assert dispatch_delivery.REPLY_LIMIT == dispatch_store.REPLY_LIMIT, (
        "REPLY_LIMIT has drifted: agent_runtime/dispatch_delivery.py and "
        "agent_runtime/dispatch_store.py must bound the reply at the same "
        "number. dispatch_store.py's own comment declares the mirror; this is "
        "the gate that comment cannot be."
    )
    assert dispatch_store.REPLY_LIMIT == agent_chat_tool._REPLY_LIMIT, (
        "REPLY_LIMIT has drifted: agent_runtime/dispatch_store.py's REPLY_LIMIT "
        "and tools/agent_chat_tool.py's _REPLY_LIMIT are the same bound under "
        "two spellings. The store's comment says the reply bound 'matches the "
        "relay tool's own _REPLY_LIMIT' — keep that true or change both."
    )
