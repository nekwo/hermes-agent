"""What THIS hermes build can do on the peer/gateway lane, as four words.

Ruling R-IP16, as consumed by S2: **feature-detect via
``hello_ok.gateway.capabilities``; a missing capability is a row state, never a
refusal.** A launcher, or a paired install, that predates a verb must be able to
render "this machine cannot be introduced yet" rather than dial a method that
answers ``-32601`` and call it an outage. The alternative — probing each verb
and reading the error — makes "old build" and "broken build" the same
observation.

Why a MODULE of its own, importing nothing
-------------------------------------------

Two readers that must never disagree sit on opposite sides of a process
boundary: the serve stamps the list onto its ``gateway`` block (so it rides
``ready`` / ``hello_ok`` / ``version``), and ``harness gateway id`` prints the
same list from a cold CLI process with no listener anywhere. If the vocabulary
lived in ``serve.py`` the CLI would have to import the serve loop to read four
strings; if it lived in ``gateway_commands.py`` the serve would have to import
the CLI. So it lives here, with no imports at all, and both sides read the same
tuple.

Why the list rides EVERY outcome, including ``disabled``
---------------------------------------------------------

"Does this hermes know the verb" and "is the LAN door open" are different
questions and the block must answer both separately. S3's request loop runs
``introduce`` against its OWN serve over the loopback argv lane, on a machine
whose ``remote_gateway.listen`` may legitimately be off; a capabilities list
that appeared only when a listener bound would tell that caller the build is
old when the build is merely quiet. So :func:`with_capabilities` is applied
where ``gateway_block`` is initialised AND where the listener's own block is
adopted, and ``{"outcome": "disabled"}`` carries the list too.

What a word means, and what it does NOT
----------------------------------------

A word here is a claim about THIS build's surface: the verb exists, the method
is registered, the door is wired. It is not a claim that a caller is allowed
through it (the allowlist and the tier answer that), nor that the far side has
anything to say (a roster can be empty). Keeping it to "the name resolves" is
what makes the list cheap to compute and impossible to get wrong: nothing here
reads a store, a config or a clock.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GATEWAY_CAPABILITIES",
    "GATEWAY_CAPABILITY_CONTRACT",
    "with_capabilities",
]

#: R-IP16's four words, sorted. Sorted rather than "in the order they shipped"
#: because two builds that support the same set must publish the same bytes —
#: a launcher comparing greetings across a re-boot should see equality, not a
#: reordering it has to normalise.
#:
#: * ``announce`` — this install answers ``peer.announce`` and sends one
#:   (S2c's cache-only push edge).
#: * ``introduce`` — ``harness gateway introduce`` exists, so a launcher may
#:   fulfil a backend pair-grant against this machine (S2).
#: * ``roster`` — ``peer.roster.list`` is registered, so a paired install may
#:   ask who is addressable here (S2b).
#: * ``thread_read`` — ``peer.thread.read`` is registered, so a paired install
#:   may read the tail of a thread it was handed the session id for (S2b).
GATEWAY_CAPABILITIES: tuple[str, ...] = (
    "announce",
    "introduce",
    "roster",
    "thread_read",
)

#: The shape number of the ``capabilities`` key itself, not of the words in it.
#: A word is added or removed by editing the tuple above — that is the whole
#: negotiation, and it needs no version. This number moves only if the KEY ever
#: stops being "a sorted list of strings on the gateway block", which is the one
#: change a reader could not absorb by ignoring an unknown word.
GATEWAY_CAPABILITY_CONTRACT = 1


def with_capabilities(block: Any) -> dict[str, Any]:
    """A COPY of *block* carrying :data:`GATEWAY_CAPABILITIES`.

    A copy and never a mutation, because the caller's dict is the return value
    of ``start_gateway_listener`` and is also the thing a test compares against
    a literal; a helper that edited it in place would make "what did the
    listener report" depend on whether this function had run yet.

    A fresh ``list`` per call for the same reason the tuple is module-level and
    frozen: the block is serialised into three frames and a sidecar, and a
    shared mutable list on a long-lived dict is one careless ``.append`` away
    from a capability nobody declared.
    """

    row = dict(block) if isinstance(block, dict) else {}
    row["capabilities"] = list(GATEWAY_CAPABILITIES)
    return row
