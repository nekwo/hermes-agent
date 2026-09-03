"""``@install/target`` — the ONE place a chat target is read as cross-install.

Gateway Stage 7, ruling R4 (adopted at its recommendation: ``@install_name/target``
rather than opaque handles minted at pair time). An agent on install A addresses
an agent on install B by writing the install in front of the target it already
knows how to write::

    dev                      → local, the persona's canonical primary
    @personainst_dev_agent_2 → local, that specific instance
    @workstation/dev         → install "workstation", its "dev" persona
    @workstation/@personainst_dev_agent_2  ← NOT the spelling; see below

**Unqualified is local, forever.** That sentence is the whole compatibility
story and it is a property of the grammar rather than of a default somebody
chose: a value with no ``/`` in it never reaches the peer resolver at all, so
every send written before this module existed means exactly what it meant then.
There is no config, no env var and no roster lookup that can make a bare
``dev`` mean another machine — which matters because the failure mode of
getting this wrong is a private briefing crossing a machine boundary.

Why the qualifier is a PREFIX and not a suffix
-----------------------------------------------
``dev@workstation`` reads more naturally to a human and is the wrong shape here.
The target half of this grammar already has a leading-``@`` spelling of its own
(``@personainst_*`` handles), so a suffix form would put two different meanings
on one character in one string and force every reader — human and parser — to
decide which ``@`` it is looking at by counting. A prefix puts the install
first, which is also the order the decision is made in: *which runtime*, then
*who on it*.

Inside the qualifier the target is spelled exactly as it would be locally, with
one deliberate exception: the ``@`` on an instance handle is dropped, because
``@workstation/@personainst_x`` puts the sigil twice in one target for no gain.
:func:`parse_install_target` therefore accepts both and normalises to the bare
handle, so a model that writes the sigil out of habit is not refused for it.

What this module does NOT do
-----------------------------
It does not dial, does not authorize, and does not decide whether the far side
knows the persona being named. Resolution stops at "which paired install row is
this, and is that row usable" — everything past that is a fact only install B
holds, and asking it is a network call this module deliberately does not make.
A refusal here is therefore always DETERMINISTIC (an unknown name, a revoked
row, an ambiguous name), which is exactly the class Stage 7's retry posture
fails fast on: no attempt is burned for an answer that cannot change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "INSTALL_QUALIFIER",
    "MAX_INSTALL_REF_CHARS",
    "MAX_TARGET_CHARS",
    "REASON_AMBIGUOUS_INSTALL",
    "REASON_EMPTY_INSTALL",
    "REASON_EMPTY_TARGET",
    "REASON_PEER_EXPIRED",
    "REASON_PEER_REVOKED",
    "REASON_PEER_REVOKED_YOU",
    "REASON_UNKNOWN_INSTALL",
    "InstallTarget",
    "ResolvedInstallTarget",
    "TargetRefusal",
    "format_install_target",
    "is_install_qualified",
    "parse_install_target",
    "peer_store_root",
    "resolve_install_ref",
    "resolve_install_target",
]

#: The character that opens an install qualifier. Shared with the instance-handle
#: spelling on purpose — see the module docstring — and disambiguated by the
#: ``/``, which an instance handle never contains.
INSTALL_QUALIFIER = "@"

#: Bounds on each half. Both arrive from a model's tool call, and both are
#: compared against stored strings, so they are fenced at the boundary rather
#: than wherever they happen to be used. The install bound matches
#: ``gateway_identity.DISPLAY_NAME_MAX_CHARS``'s order of magnitude plus room
#: for a raw ``install_id`` (a uuid4 hex, 32 characters, or the 128 the peer
#: store admits); the target bound matches the persona-id bound the chat lane
#: already enforces (``chat_turn.normalize_chat_message``, 200).
MAX_INSTALL_REF_CHARS = 200
MAX_TARGET_CHARS = 200

#: Deterministic refusal reasons. Values, not prose, because three surfaces
#: branch on them: the tool's typed refusal to the calling model, the dispatch
#: row's error text, and the acceptance test.
REASON_EMPTY_INSTALL = "install_qualifier_empty"
REASON_EMPTY_TARGET = "install_qualifier_target_empty"
REASON_UNKNOWN_INSTALL = "unknown_peer_install"
REASON_AMBIGUOUS_INSTALL = "ambiguous_peer_install"
REASON_PEER_REVOKED = "peer_revoked"
#: S2. The credential lapsed (R-IP15 as amended). Its own reason because the
#: operator's next move differs from a revocation's: nobody decided this, a
#: clock ran out, and the cure is a fresh introduction rather than an argument
#: about whether the edge should exist.
REASON_PEER_EXPIRED = "peer_expired"
#: S2c. The FAR operator cut the edge and told us so (``peer.announce``). A
#: third word rather than folding into ``peer_revoked``, because "you revoked
#: them" and "they revoked you" send an operator to different machines — and
#: before the announce edge existed this state was indistinguishable from the
#: far install being down.
REASON_PEER_REVOKED_YOU = "peer_revoked_you"


@dataclass(frozen=True, slots=True)
class InstallTarget:
    """A parsed ``@install/target``. Syntax only — nothing has been looked up."""

    install_ref: str
    target: str

    def spelling(self) -> str:
        return format_install_target(self.install_ref, self.target)


@dataclass(frozen=True, slots=True)
class ResolvedInstallTarget:
    """A parsed qualifier matched to a usable row in ``gateway/peers.json``.

    ``install_id`` is the discriminator and ``display_name`` is chrome — the
    split matters here rather than being pedantry, because Stage 6's own field
    notes record two installs on one machine both displaying the hostname. Every
    downstream use (the dial, the dispatch row, the refusal text) names the id;
    the name exists so an operator reading a log recognises the machine.
    """

    install_id: str
    display_name: str
    target: str
    #: The instance handle when the target named one, otherwise ``""``. Split
    #: here rather than at the far door for the local lane's own reason: a
    #: persona may run several instances, and a handle in the persona slot has
    #: to travel as an instance id or it collapses to the canonical primary.
    target_instance_id: str = ""

    def spelling(self) -> str:
        return format_install_target(self.display_name, self.target)


@dataclass(frozen=True, slots=True)
class TargetRefusal:
    """A deterministic no. ``reason`` is what a caller branches on."""

    reason: str
    message: str
    #: Present only for :data:`REASON_AMBIGUOUS_INSTALL` — the install ids that
    #: matched, so the calling agent can immediately retry against an exact one
    #: instead of guessing which of two identically-named machines was meant.
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return False


def peer_store_root() -> Path:
    """The runtime root whose ``gateway/peers.json`` this install's edges live in.

    NOT ``paths.store_root()``, and the difference is the same one
    ``hermes_constants.get_hermes_background_work_home`` exists to name.
    ``resolve_runtime`` consults ``HERMES_HOME``'s ``config.yaml`` whenever
    ``HERMES_AGENT_RUNTIME_ROOT`` is unset, and ``persona_profile_context``
    flips ``HERMES_HOME`` PROCESS-GLOBALLY for the length of every persona turn
    — which is exactly when a cross-install send is made and exactly where the
    supervisor thread that later dials the peer is running. The ambient answer
    would therefore depend on which unrelated persona happened to be mid-turn,
    and on the launcher's own layout (``HERMES_HOME=profiles/<profile>`` with
    ``HERMES_HEAD_HOME=profiles/base``) those are different directories.

    A peer edge belongs to the INSTALL, not to a persona's profile, so the home
    that answers is the head one — the same precedence the background-work
    stores already use, reached through the same resolver rather than a second
    one. Both the send (which resolves the name) and the supervisor (which
    dials) call this, so they cannot read two different stores.
    """

    from hermes_constants import get_hermes_background_work_home

    from .resolution import resolve_runtime

    env = dict(os.environ)
    env["HERMES_HOME"] = str(get_hermes_background_work_home())
    return resolve_runtime(env).store_root


def format_install_target(install_ref: str, target: str) -> str:
    return f"{INSTALL_QUALIFIER}{install_ref}/{target}"


def is_install_qualified(value: Any) -> bool:
    """Cheap syntactic test: does this target name another install at all?

    Deliberately NOT "is this a valid qualifier" — a malformed one still answers
    True here, because the alternative is that ``@/dev`` or ``@workstation/``
    falls through to the local lane and silently addresses something on THIS
    machine. A caller uses this to decide which lane to enter and
    :func:`parse_install_target` to find out whether the spelling is usable.
    """

    text = str(value or "").strip()
    return text.startswith(INSTALL_QUALIFIER) and "/" in text


def parse_install_target(value: Any) -> InstallTarget | TargetRefusal | None:
    """``@install/target`` → its two halves. ``None`` when it is not qualified.

    Three answers rather than two, and the third is the important one: ``None``
    means *this is a local target, carry on exactly as before*, which is how the
    unqualified-is-local rule stays a property of the parser instead of a check
    somebody has to remember at each call site.

    Split on the FIRST ``/`` only. A persona id contains no slash today, but a
    greedy split would make that a silent assumption about a value another
    module owns; splitting once means a target that grows a slash is passed
    through whole and refused (or not) by the door that owns targets, which is
    where that decision belongs.
    """

    text = str(value or "").strip()
    if not is_install_qualified(text):
        return None
    body = text[len(INSTALL_QUALIFIER) :]
    install_ref, _, target = body.partition("/")
    install_ref = install_ref.strip()
    target = target.strip()
    if not install_ref:
        return TargetRefusal(
            REASON_EMPTY_INSTALL,
            f"{text!r} names no install before the '/'. Write "
            "@install_name/persona_or_instance, or drop the qualifier to reach "
            "an agent on this install.",
        )
    if len(install_ref) > MAX_INSTALL_REF_CHARS:
        return TargetRefusal(
            REASON_EMPTY_INSTALL,
            f"the install name in {text[:80]!r} is longer than "
            f"{MAX_INSTALL_REF_CHARS} characters.",
        )
    # The sigil is dropped, never required and never refused — see the module
    # docstring. `@workstation/@personainst_x` and `@workstation/personainst_x`
    # are one address, and a model that writes either is right.
    if target.startswith(INSTALL_QUALIFIER):
        target = target[len(INSTALL_QUALIFIER) :].strip()
    if not target:
        return TargetRefusal(
            REASON_EMPTY_TARGET,
            f"{text!r} names an install but nobody on it. Write "
            "@install_name/persona_or_instance.",
        )
    if len(target) > MAX_TARGET_CHARS:
        return TargetRefusal(
            REASON_EMPTY_TARGET,
            f"the target in {text[:80]!r} is longer than {MAX_TARGET_CHARS} "
            "characters.",
        )
    return InstallTarget(install_ref=install_ref, target=target)


def resolve_install_target(
    store_root: Path | str, parsed: InstallTarget
) -> ResolvedInstallTarget | TargetRefusal:
    """Match the qualifier against THIS install's peer rows.

    Resolution order is **install id first, display name second**, and the order
    is the load-bearing part rather than an optimisation. A display name is
    operator chrome that two installs can legitimately share
    (``gateway rename`` is per-root and nothing prompts an operator to run it,
    which Stage 6's field notes recorded as a live condition on this very
    machine); an ``install_id`` is minted once at Stage 0 and is unique by
    construction. So an id always wins, and a name that matches two rows is
    REFUSED with both ids rather than resolved to whichever the store happened
    to iterate first — because "the message went to the other DESKTOP-QJ7DDV2"
    is not a failure an operator can debug after the fact.

    An UNUSABLE row refuses with its own reason rather than reading as unknown,
    and S2/S2c made that three reasons where it was one. The facts are
    different and an operator acts differently on each: an unknown name is a
    typo or a pairing that never happened; a revoked row is a ceremony to re-run
    at both machines; an expired one is a credential to renew; and
    ``peer_revoked_you`` is the far operator's decision, which no amount of work
    at this machine will fix. ``dial_peer`` refuses the first three too — this
    is not that check moved, it is the same verdict reached before any work
    starts, so an unusable edge costs no attempt.

    The matching half lives in :func:`resolve_install_ref` so the directory tool
    and the send path share one answer to "which machine is @mac".
    """

    from .persona_assignments import safe_assignment_token

    resolved = resolve_install_ref(store_root, parsed.install_ref)
    if isinstance(resolved, TargetRefusal):
        return resolved
    record = resolved
    handle = (
        parsed.target
        if safe_assignment_token(parsed.target).startswith("personainst_")
        else ""
    )
    return ResolvedInstallTarget(
        install_id=record.peer_install_id,
        display_name=record.display_name,
        target=parsed.target,
        target_instance_id=handle,
    )


def resolve_install_ref(store_root: Path | str, install_ref: str):
    """Match one install REF — a display name or an install id — to a usable row.

    Factored out of :func:`resolve_install_target` when S2b's
    ``agent_chat_installs`` needed the same matcher without a target half. Two
    matchers would be two answers to "which machine is @mac", and the second one
    would be discovered by an operator whose message went somewhere the roster
    said it would not.

    Returns a ``PeerRecord`` or a :class:`TargetRefusal`.

    **It matches against ``usable_peers`` and names the reason from the full
    list** (R-S2-16). The two halves are separate on purpose: what an address
    may RESOLVE to is the usable set, so no send can land on an edge the runtime
    has written off; but a refusal is allowed to know more than the resolver, so
    an operator whose message bounced learns *revoked*, *expired* or *they
    revoked you* instead of the flat "no such install" all three used to be.
    """

    from .gateway_peers import list_peers, usable_peers

    root = Path(store_root)
    ref = str(install_ref or "").strip()
    try:
        usable = usable_peers(root)
    except Exception as exc:  # pragma: no cover - defensive; an unreadable store
        return TargetRefusal(
            REASON_UNKNOWN_INSTALL,
            f"this install's peer store could not be read ({type(exc).__name__}), "
            "so no cross-install target can be resolved.",
        )

    folded = ref.casefold()
    by_id = [row.record for row in usable if row.record.peer_install_id == ref]
    matches = by_id or [
        row.record
        for row in usable
        if (row.record.display_name or "").casefold() == folded
    ]
    if len(matches) > 1:
        return TargetRefusal(
            REASON_AMBIGUOUS_INSTALL,
            f"{len(matches)} paired installs are called {ref!r}. Address one by "
            "its install id instead of its name.",
            candidates=tuple(sorted(row.peer_install_id for row in matches)),
        )
    if matches:
        return matches[0]

    # Nothing usable matched. The FULL list is consulted only now, and only to
    # name the reason — an unusable row can explain a refusal and can never
    # satisfy an address.
    return _refuse_unmatched(root, ref, usable, list_peers(root))


def _refuse_unmatched(root, ref: str, usable, every_row):
    """Why nothing usable matched *ref*, in the operator's own vocabulary."""

    from .gateway_peers import read_peer_cache

    folded = ref.casefold()
    unusable = [
        row
        for row in every_row
        if row.peer_install_id == ref or (row.display_name or "").casefold() == folded
    ]
    if unusable:
        record = unusable[0]
        if record.revoked:
            return TargetRefusal(
                REASON_PEER_REVOKED,
                f"the edge to {record.display_name!r} ({record.peer_install_id}) is "
                "revoked at this install; an operator has to re-run the pairing "
                "ceremony at both machines before it can carry anything.",
            )
        if record.expired:
            return TargetRefusal(
                REASON_PEER_EXPIRED,
                f"the credential for {record.display_name!r} "
                f"({record.peer_install_id}) expired at {record.expires_at}; an "
                "operator introduces the two installs again to renew it.",
            )
        cached = read_peer_cache(root).get(record.peer_install_id)
        if cached is not None and cached.revoked_you:
            return TargetRefusal(
                REASON_PEER_REVOKED_YOU,
                f"{record.display_name!r} ({record.peer_install_id}) revoked this "
                f"install at {cached.revoked_you_at}; the row here is intact but "
                "that machine will refuse us until an operator over there pairs "
                "again.",
            )

    # The hint lists USABLE refs only — the exact spellings an address would
    # resolve, so a suggestion the runtime prints is never one it would then
    # refuse as ambiguous or revoked.
    known = sorted({row.ref for row in usable if row.ref})
    hint = (
        f" Paired installs: {', '.join(known[:8])}."
        if known
        else " No install is paired with this one yet; an operator runs "
        "`harness gateway peers pair` here and `join` there."
    )
    return TargetRefusal(
        REASON_UNKNOWN_INSTALL,
        f"no paired install matches {ref!r}.{hint}",
    )
