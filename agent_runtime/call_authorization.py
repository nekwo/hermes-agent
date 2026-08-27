"""WHO may call WHAT — the front-door authorization vocabulary for the method lane.

Ruling A (2026-08-27,
``docs/agent-runtime-harness/planned/authorization-chokepoint.md``) put the check
at the RPC dispatch layer, evaluated against what the TRANSPORT proved, and
reframed the policy: **the tier is client security auth.** A credential that
traces to account-authed pairing may run console verbs; one that does not, may
not. hermes never sees an Eternia account directly, so the binding happens where
credentials are minted — the install's own serve token is the machine owner and
is ``local_console``; a device credential's tier is fixed at a pairing ceremony
only an account-authed operator surface can run (gateway Stage 1 / Stage A5).

Two rules follow, and they are the whole module:

* **The gate reads the tier off the CALLER, never off the request.** Everything
  a caller can type — ``--requested-by``, a ``params`` key, a hello field — is an
  assertion. The predicate takes a caller that only the transport builder mints,
  and there is no argument by which a request can name its own tier. This is the
  property the pre-existing coordinator machinery lacked (that one takes both the
  identity and the grant off argv, which is why Stage A4 renames it to what it
  is: an advisory self-declared budget).
* **A caller nobody recognised is REFUSED, not defaulted through.** Absence of a
  decision is never an allow.

Why a module and not four constants in ``serve_rpc``
---------------------------------------------------

``serve_rpc`` is the DISPATCHER. It should ask "may this caller run this tier?"
and render the refusal it gets back; it should not also be the place the policy
lives, because Stage A5 grows that policy a device-record lookup and the CLI
mirror (Stage A4) has to evaluate the same predicate with no dispatcher in the
picture. One import direction — ``serve_rpc`` → here, ``persona_commands`` →
here, and nothing back — keeps the two doors provably on one predicate.

What is deliberately NOT decided here
-------------------------------------

**The tier vocabulary itself is R11's, not this file's.** Two tiers exist because
two are what the shipped surface needs a word for: the docstrings on
``agent_retire.perform_agent_retire`` and ``serve_rpc._runtime_agent_retire``
already said ``console``, and everything that is not a level mutation is
``read``. Whether an ``admin`` tier carves out the skills INSTALL sub-phase is
the gateway plan's R11 question and is answered there, not by adding a constant
here on the way past.

Stage A1 lands the vocabulary and the declaration. Nothing in this file refuses
anything yet — the predicate and the caller model are A2/A3.
"""

from __future__ import annotations

__all__ = [
    "TIER_READ",
    "TIER_CONSOLE",
    "TIERS",
]

#: Anything that neither writes store state, emits an event, nor mints an id.
TIER_READ = "read"
#: A level MUTATION — the tier both agent methods' docstrings already named
#: (placement plan §A.11, owner decision D10-iv).
TIER_CONSOLE = "console"

#: Ordered least-privileged first. A tier outside this set is a programming
#: error, not a policy question: the registry refuses one at registration time
#: rather than letting a typo widen a door.
TIERS: tuple[str, ...] = (TIER_READ, TIER_CONSOLE)
