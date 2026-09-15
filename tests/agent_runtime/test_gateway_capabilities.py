"""R-IP16's four words, and the one helper that stamps them.

The vocabulary is a WIRE FACT: a launcher and a paired install both read it to
decide whether a row is "not supported here" or "broken here", so a word that
drifted between the module and the frame would turn a feature-detection answer
into a guess. Two things are pinned — the tuple itself, and that stamping it is
a copy rather than a mutation.
"""

from __future__ import annotations

from agent_runtime.gateway_capabilities import (
    GATEWAY_CAPABILITIES,
    GATEWAY_CAPABILITY_CONTRACT,
    with_capabilities,
)


def test_the_capabilities_list_is_exactly_r_ip16s_four_words_sorted():
    """Four words, sorted, and sorted is load-bearing rather than tidy: two
    builds that support the same set must publish the same bytes, so a client
    comparing greetings across a reboot sees equality instead of a reordering it
    has to normalise."""

    assert GATEWAY_CAPABILITIES == ("announce", "introduce", "roster", "thread_read")
    assert list(GATEWAY_CAPABILITIES) == sorted(GATEWAY_CAPABILITIES)
    assert GATEWAY_CAPABILITY_CONTRACT == 1


def test_with_capabilities_never_mutates_its_input_and_hands_back_a_fresh_list():
    """A copy, because the argument is ``start_gateway_listener``'s return value
    and is also what a test compares against a literal — a helper that edited it
    in place would make "what did the listener report" depend on whether this
    function had run yet. And a FRESH list per call, because the block is
    serialised into three frames and a sidecar: one shared mutable list on a
    long-lived dict is a single careless ``append`` away from a capability
    nobody declared."""

    original = {"outcome": "listening", "port": 8765}
    stamped = with_capabilities(original)

    assert original == {"outcome": "listening", "port": 8765}
    assert stamped == {
        "outcome": "listening",
        "port": 8765,
        "capabilities": list(GATEWAY_CAPABILITIES),
    }

    stamped["capabilities"].append("smuggled")
    assert with_capabilities({})["capabilities"] == list(GATEWAY_CAPABILITIES)
    assert GATEWAY_CAPABILITIES == ("announce", "introduce", "roster", "thread_read")


def test_a_non_dict_block_still_yields_a_capability_carrying_dict():
    """The listener's error arm returns a dict today, and this helper is called
    at two sites that a later edit could reach with ``None``. Answering with a
    capability-carrying dict rather than raising keeps the greeting's own
    guarantee — a block states its outcome rather than vanishing — from
    depending on an upstream branch nobody re-checked."""

    assert with_capabilities(None) == {"capabilities": list(GATEWAY_CAPABILITIES)}
