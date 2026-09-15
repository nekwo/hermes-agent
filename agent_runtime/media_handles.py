"""Chat media as CONTENT HANDLES, so a remote client stops needing our paths.

Gateway Stage 8. Everything a paired device can already read about a chat turn
it can read except one thing: the pictures. An image reaches an operator as a
``MEDIA:<absolute path>`` line inside the message TEXT — there is no media field
on any chat record, no blob store under the runtime root, and no content address
for a binary file anywhere in this repo (the one CAS,
``prompt_observability_catalogs``, is JSON text). So the launcher renders a
message from a runtime on another machine, reaches for ``File(path)``, and
paints *"Not on this machine"*. This module is the other end of the fix.

The handle namespace, and why it is the whole design
----------------------------------------------------

A remote caller names an artifact by ``sha256:<64 hex>`` and by nothing else.
It never sends a path and :func:`resolve_handle` never accepts one — not
"sanitizes one", *accepts*: a path-shaped argument does not match
:data:`HANDLE_RE` and is refused :data:`REASON_HANDLE_INVALID` before any
filesystem call is made. There is therefore no traversal surface to get wrong,
because there is no step anywhere that turns caller input into a path. The
server turns a handle into a path only by LOOKING IT UP in a set it enumerated
itself (:func:`build_media_scope`), which can only ever answer with something
that was already in scope.

Content addressing buys the second property for free: a handle names BYTES, so a
client cache keyed on it is trivially immutable and needs no invalidation
protocol. That is why the digest is over the content and not over the path, and
it is also the reason the mint has to happen here rather than at the client —
the client holds the path and cannot know the bytes.

Where the scope comes from: DERIVED, never registered
------------------------------------------------------

There is no handle registry. A registry is a second copy of a fact, and a second
copy drifts — an artifact deleted, a log rotated, a message edited, and the
registry still promises bytes nobody can produce. So the scope is DERIVED at
read time from the store that already knows the artifacts: the chat live-log
mirror (``agent_runtime/chat_live_log.py``,
``<head-home>/chat_live_logs/<session>.jsonl``), which exists precisely because
somebody needed a greppable projection of the transcripts. Re-derivation is the
whole cache-invalidation story: the scope is rebuilt from the mirror, and the
per-file digest memo is keyed on ``(path, size, mtime_ns)`` so a file that
CHANGED gets a new handle rather than serving the old one's bytes.

**This is the reachability rule the authorization model already states**, one
layer out: a caller may fetch only artifacts reachable from data its tier
already lets it read. A console device may read chats; a chat's declared media
is therefore in scope; a file that appears in no transcript is in no scope and is
refused :data:`REASON_UNKNOWN_HANDLE` — which is also, and not by coincidence,
the answer a probing caller gets for a digest it guessed.

Two bounds that are not the reachability rule, and are still enforced
--------------------------------------------------------------------

**Images only.** ``MEDIA:`` is a line the MODEL writes, so the reachability rule
alone would make "whatever the model typed" fetchable, and a model that typed
``MEDIA:~/.ssh/id_rsa`` would have written an exfiltration primitive. The
extension allowlist (:data:`IMAGE_EXTENSIONS`, the set
``gateway/platforms/api_server.py`` already delivers inline) is what makes that
unrepresentable: a handle only ever exists for a file whose extension is in the
set, so the namespace cannot contain a credential. It is deliberately NOT the
gateway's ``validate_media_delivery_path`` — that validator's recency window
(600 s) and cache-root allowlist are right for its question (an agent handing an
unvetted path to an upload) and wrong for this one, where yesterday's Stage-C
proof under an artifact directory is exactly what the caller is asking for and
would be refused. The consequence is recorded rather than hidden: non-image
media — video, PDF, archives — is NOT in the handle namespace at all. The
largest ``MEDIA:``-delivered artifact on this machine is a 1.1 GB MP4, which is
the strongest argument available that this is a decision and not an omission.

**A size cap, and it is not a new number.** :data:`MAX_FETCH_BYTES` is 5 MiB,
which is ``gateway/platforms/api_server.py``'s ``_MEDIA_DATA_URL_MAX_BYTES``
reused rather than re-decided — that constant answers *the same question about
the same protocol* (inline a ``MEDIA:`` file, base64, for a client that cannot
read local paths), and a second number would be a second policy on one lane.
Measured on this machine before it was adopted: 428 Stage-C screenshots, median
351,423 B, largest 2,146,781 B; the whole 175-file image corpus under the hermes
homes tops out at 2,722,628 B. So 5 MiB clears every real image with room, and
refuses the video — which is the refusal being right, not the cap being small.
No ranging is built, because nothing this machine has produced needs it; a
client is told the cap by name (:data:`REASON_ARTIFACT_TOO_LARGE` carries
``cap_bytes`` and ``size_bytes``) rather than left to infer it.

Cross-install media: a SECOND row kind, and only one of them has a path
---------------------------------------------------------------------

Stage P4 (ruling R-P3). A cross-install dispatch's reply is forged into install
A's chat carrying a ``MEDIA:`` line that names a path on install **B**. A holds
no bytes for it and can never hash it, so A cannot mint that handle — B does, at
reply time (:func:`mint_reply_media`), and the ``reference → handle`` map rides
the dispatch completion home (``dispatch_store.record_completion``'s ``media``
argument). What lands in A's scope is therefore a :class:`RemoteMediaArtifact`:
the handle B minted, the reference the transcript already shows, the peer that
holds the bytes, and **no path** — because there is no file on this disk to name
and a nullable path field would be an invitation to open one.

Two dataclasses rather than one with ``path: Path | None`` is that argument
expressed in the type: :func:`read_artifact_bytes` takes the local kind and only
the local kind, so "read the bytes of a row that has none" is not a branch
anybody can forget — it does not typecheck. A remote row is served by
``serve_rpc``'s proxy arm instead, which dials the peer, verifies the returned
bytes against the handle (free, because the handle IS the digest) and caches
them here by content address (:func:`write_cached_bytes`). The cache needs no
invalidation protocol for the same reason the handle needs no version: it names
BYTES.

**Local wins on a collision.** The same file on both installs mints the same
handle — that is what content addressing means — and :meth:`MediaScope.get`
answers with the local row, so a fetch this disk can answer never spends a peer
dial.

**Both bounds apply on both hops, unchanged.** The extension allowlist is
enforced by B when it mints and again by A when it folds a stored map into its
scope, because A must never take B's word for what kind of file a handle names;
the 5 MiB cap is checked by B's ``runtime``-side read, by A's fold, and by the
proxy against the bytes that actually arrive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths

__all__ = [
    "HANDLE_PREFIX",
    "HANDLE_RE",
    "MAX_FETCH_BYTES",
    "MAX_SCOPE_ARTIFACTS",
    "MAX_SCOPE_LOGS",
    "MAX_REPLY_ARTIFACTS",
    "MAX_REMOTE_ARTIFACTS",
    "MAX_REMOTE_COMPLETIONS",
    "SCOPE_LOG_TAIL_BYTES",
    "IMAGE_EXTENSIONS",
    "MEDIA_TYPES",
    "MEDIA_DECLARATION_PREFIX",
    "REASON_HANDLE_INVALID",
    "REASON_UNKNOWN_HANDLE",
    "REASON_ARTIFACT_TOO_LARGE",
    "REASON_ARTIFACT_UNREADABLE",
    "MediaArtifact",
    "RemoteMediaArtifact",
    "MediaScope",
    "MediaRefusal",
    "handle_for_bytes",
    "media_declarations",
    "mint_reply_media",
    "parse_media_declaration",
    "build_media_scope",
    "remote_artifacts_from_completions",
    "resolve_handle",
    "read_artifact_bytes",
    "remote_cache_path",
    "read_cached_bytes",
    "write_cached_bytes",
    "reset_digest_memo",
]

#: The one handle grammar. A namespace with a prefix rather than a bare digest,
#: so the algorithm is on the wire: the day a second one is needed, a handle
#: says which it is instead of being 64 hex characters of unstated provenance.
HANDLE_PREFIX = "sha256:"
HANDLE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: See the module docstring: ``_MEDIA_DATA_URL_MAX_BYTES`` reused, not minted.
MAX_FETCH_BYTES = 5 * 1024 * 1024

#: Bounds on ONE scope derivation. The live corpus on this machine is 46 logs
#: totalling 138,622 bytes, so none of these bite today; they exist because a
#: mirror rotates at 10 MB (``chat_live_log.LIVE_LOG_ROTATE_BYTES``) and an
#: unbounded scan on an RPC that answers inline on the reader loop is a way to
#: stall every other client on the serve.
MAX_SCOPE_ARTIFACTS = 512
MAX_SCOPE_LOGS = 256
SCOPE_LOG_TAIL_BYTES = 2 * 1024 * 1024

#: How many artifacts ONE reply may mint handles for (Stage P4). Small on
#: purpose and not a guess: the mint hashes every file it accepts, it runs on
#: install B inside the turn the operator is waiting on, and the whole measured
#: corpus of ``MEDIA:``-carrying replies on this machine declares one image.
#: A reply that declares more than this keeps its extra references — they are
#: TEXT, and the launcher renders them exactly as it does an unfetchable one —
#: it simply mints no handles for them, which is the same "no handle for that
#: picture" state a local scope already has a word for.
MAX_REPLY_ARTIFACTS = 16

#: Bounds on the REMOTE half of one scope derivation: how many stored
#: cross-install completions are read, and how many rows they may contribute.
#: The local bounds' argument, applied to the second source — this derivation
#: also runs inline on the reader loop, and a sender that dispatched a thousand
#: times must not turn one ``runtime.media.index`` into a thousand-row reply.
MAX_REMOTE_COMPLETIONS = 128
MAX_REMOTE_ARTIFACTS = 256

#: The extension allowlist, and therefore the whole of what a handle can name.
#: Spelled here rather than imported: ``agent_runtime`` does not depend on the
#: ``gateway`` package on any hot path. The parity is a TEST
#: (``test_media_handles.py``) against ``api_server._MEDIA_IMG_EXT``, so drift is
#: caught rather than merely deprecated in a comment.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)

#: extension → media type, ``api_server._MEDIA_MIME``'s table, pinned by the same
#: parity test.
MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

#: The sentinel the runtime's own prompt teaches
#: (``persona_runtime.py``: "a ``MEDIA:`` line alone on its own line is a
#: DECLARATION"). Case-sensitive, exactly as the launcher's parser is
#: (``local_document_reference.dart``: ``kDeclaredMediaPrefix``) — lowercase is
#: prose, and a case-insensitive match here would put every sentence about the
#: protocol into the namespace.
MEDIA_DECLARATION_PREFIX = "MEDIA:"

#: The argument was not the handle grammar. This is the arm a path-shaped
#: argument lands in, which is why it is the one refusal that is checked before
#: anything touches a disk.
REASON_HANDLE_INVALID = "handle_invalid"
#: Well-formed, and in no scope this caller can reach. Deliberately the same
#: answer for "deleted since it was indexed", "never existed" and "guessed",
#: because distinguishing them would answer a probe.
REASON_UNKNOWN_HANDLE = "unknown_handle"
#: In scope, over :data:`MAX_FETCH_BYTES`. Carries the cap so the client learns
#: the number instead of inferring it from a silence.
REASON_ARTIFACT_TOO_LARGE = "artifact_too_large"
#: In scope, and the bytes could not be produced NOW — vanished between the
#: derivation and the read, or unreadable. A real state and not a 500.
REASON_ARTIFACT_UNREADABLE = "artifact_unreadable"

#: An absolute path, anchored the two ways this stack actually produces them: a
#: Windows drive (``X:\Eternia\...``, the whole corpus on this machine) and a
#: POSIX root. A RELATIVE path is not a media declaration — it would be resolved
#: against whatever directory the serve happens to be in, which is a different
#: file on every install.
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")

_digest_lock = threading.Lock()
#: (resolved path, size, mtime_ns) → digest. Keyed on the stat triple and not on
#: the path, so a REWRITTEN file mints a new handle rather than serving the old
#: bytes under the old name. Process-scoped and unbounded-in-principle; bounded
#: in practice by :data:`MAX_SCOPE_ARTIFACTS` being the only thing that fills it.
_digest_memo: dict[tuple[str, int, int], str] = {}


def reset_digest_memo() -> None:
    """Tests only. Production never needs this — see the memo's key."""

    with _digest_lock:
        _digest_memo.clear()


@dataclass(frozen=True, slots=True)
class MediaArtifact:
    """One artifact in scope: what it is called, where it is, and how big.

    ``path`` never crosses a wire. ``reference`` does, and it is the SAME string
    the caller already holds — it is the payload of the ``MEDIA:`` line inside a
    message the client has rendered — so returning it discloses nothing the
    transcript did not already, and it is the only join key a client has between
    the picture it is trying to paint and a handle it can fetch.
    """

    handle: str
    reference: str
    path: Path
    media_type: str
    size_bytes: int

    @property
    def fetchable(self) -> bool:
        return self.size_bytes <= MAX_FETCH_BYTES

    def describe(self) -> dict[str, Any]:
        """The artifact as one row of the index reply."""

        return {
            "handle": self.handle,
            "reference": self.reference,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "fetchable": self.fetchable,
            # STATED on every row rather than only on the remote ones, for
            # ``peer.ping``'s reason: a client must never have to read a fact
            # out of a key's ABSENCE. ``false`` here is what every row said
            # implicitly before Stage P4, said out loud.
            "remote": False,
        }


@dataclass(frozen=True, slots=True)
class RemoteMediaArtifact:
    """One artifact a PAIRED INSTALL holds, named by a handle it minted.

    The sibling of :class:`MediaArtifact` and deliberately not a mode of it: it
    has no ``path``, because there is no file on this disk, and giving it a
    nullable one would make every reader of the local kind responsible for
    remembering that. See the module docstring's cross-install section.

    ``size_bytes`` is what B reported when it minted. It is a HINT here and
    never a promise — the proxy re-checks the bytes that actually arrive
    against both the cap and the handle — but it is an honest one to publish,
    because it is what lets a client see ``fetchable: false`` for an over-cap
    remote artifact without spending a round trip to learn it.
    """

    handle: str
    reference: str
    peer_install_id: str
    media_type: str
    size_bytes: int

    @property
    def fetchable(self) -> bool:
        return self.size_bytes <= MAX_FETCH_BYTES

    def describe(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "reference": self.reference,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "fetchable": self.fetchable,
            "remote": True,
            # Which install to ask, disclosed because the client already knows
            # it: the dispatch it read the reply from names the same id, and a
            # surface that can say "this picture lives on <machine>" tells an
            # operator something true that a bare spinner does not.
            "peer_install_id": self.peer_install_id,
        }


@dataclass(frozen=True, slots=True)
class MediaRefusal:
    """Why a handle produced no bytes, in the machine-readable form the
    dispatcher renders straight onto ``error.data``."""

    reason: str
    detail: dict[str, Any] | None = None

    def refusal_data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason}
        if self.detail:
            payload.update(self.detail)
        return payload


@dataclass(frozen=True, slots=True)
class MediaScope:
    """Everything a caller of this install may name, plus what it cost to say so.

    The counters are not decoration. ``truncated`` is the difference between "no
    handle for that picture" and "the scan stopped before it got there", and a
    client that could not tell those apart would retry forever on one and never
    on the other.
    """

    artifacts: dict[str, MediaArtifact]
    logs_scanned: int
    declarations_seen: int
    truncated: bool
    #: Stage P4. Artifacts a paired install holds, folded from stored
    #: cross-install completions. Defaulted so every existing construction of
    #: this class — tests included — keeps meaning "no remote rows" rather than
    #: having to be rewritten to say so.
    remote: dict[str, RemoteMediaArtifact] = field(default_factory=dict)
    #: How many stored cross-install completions the remote fold read.
    completions_scanned: int = 0

    def get(self, handle: str) -> MediaArtifact | RemoteMediaArtifact | None:
        """The artifact a handle names, LOCAL first.

        The ordering is the cheap answer winning: content addressing means the
        same file on both installs mints one handle, so a row present in both
        halves is the same bytes and the local one costs no peer dial.
        """

        local = self.artifacts.get(handle)
        if local is not None:
            return local
        return self.remote.get(handle)

    def rows(self) -> list[MediaArtifact | RemoteMediaArtifact]:
        """Every artifact in scope, local and remote, ordered by reference.

        One ordering over both halves rather than local-then-remote: a client
        joins on the reference and never on position, and a stable total order
        is what keeps two indexes of one unchanged scope byte-identical.
        """

        merged: dict[str, MediaArtifact | RemoteMediaArtifact] = dict(self.remote)
        merged.update(self.artifacts)
        return sorted(merged.values(), key=lambda item: (item.reference, item.handle))


def handle_for_bytes(data: bytes) -> str:
    """The handle a blob would have. The mint, spelled once."""

    return HANDLE_PREFIX + hashlib.sha256(data).hexdigest()


def parse_media_declaration(line: str) -> str | None:
    """The payload of one ``MEDIA:`` DECLARATION line, or ``None``.

    Mirrors the launcher's ``parseDeclaredMediaLine`` rather than re-deciding the
    grammar, because a producer and a consumer that disagree about what a
    declaration is are a picture that renders on one machine and not the other:
    the whole line must be the declaration, a whole-line backtick wrap
    un-declares it, and trailing sentence punctuation is tolerated.

    Then two things the launcher does not have to check and this must. The path
    must be ABSOLUTE (see :data:`_ABSOLUTE_PATH_RE`) and its extension must be in
    :data:`IMAGE_EXTENSIONS`. Those two together are what keeps the prose out —
    the runtime's own prompt and half the skill docs contain the literal string
    ``MEDIA:<absolute path>``, and it is neither absolute nor an image, so it
    never becomes a handle.
    """

    stripped = line.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) > 1:
        stripped = stripped[1:-1].strip()
    if not stripped.startswith(MEDIA_DECLARATION_PREFIX):
        return None
    payload = stripped[len(MEDIA_DECLARATION_PREFIX) :].strip()
    payload = payload.strip("`").strip()
    payload = payload.rstrip(".,;").strip()
    if payload.startswith(("'", '"')) and payload.endswith(("'", '"')) and len(payload) > 1:
        payload = payload[1:-1].strip()
    if not payload or not _ABSOLUTE_PATH_RE.match(payload):
        return None
    if _extension(payload) not in IMAGE_EXTENSIONS:
        return None
    return payload


def media_declarations(text: Any) -> list[str]:
    """Every declared image path in one message body, in order, deduplicated."""

    if not isinstance(text, str) or MEDIA_DECLARATION_PREFIX not in text:
        return []
    seen: list[str] = []
    for line in text.splitlines():
        payload = parse_media_declaration(line)
        if payload is not None and payload not in seen:
            seen.append(payload)
    return seen


def mint_reply_media(
    text: Any, *, max_artifacts: int = MAX_REPLY_ARTIFACTS
) -> list[dict[str, Any]]:
    """Handles for the images ONE reply declares, minted on the install that
    holds them.

    Stage P4 / ruling R-P3, and the mint has to be here — on install B, inside
    the turn — because a handle is a digest of BYTES and only the machine with
    the file can compute one. Install A receives this list on the dispatch
    completion and folds it into its own scope
    (:func:`remote_artifacts_from_completions`); it could not have derived a
    single row of it from the reply text, which names paths on a disk it cannot
    read.

    Returns ``[{reference, handle, media_type, size_bytes}, …]``, deduplicated
    by reference, in declaration order, bounded by ``max_artifacts``.
    ``fetchable`` is deliberately NOT carried: it is a function of
    :data:`MAX_FETCH_BYTES` and the size, and the install that will actually
    serve the bytes must apply its OWN cap rather than inherit a verdict from
    the one that minted.

    Never raises. A reply is a turn's whole output and a hashing failure must
    not cost the operator that turn, so a file that cannot be read contributes
    no row — the same "declared and not on this disk" state
    :func:`build_media_scope` already has an answer for.
    """

    minted: list[dict[str, Any]] = []
    try:
        references = media_declarations(text)
    except Exception:  # pragma: no cover - defensive; the parser is pure
        return []
    for reference in references:
        if len(minted) >= max_artifacts:
            break
        try:
            artifact = _artifact_for(reference)
        except Exception:  # pragma: no cover - _artifact_for swallows its own
            continue
        if artifact is None:
            continue
        minted.append(
            {
                "reference": artifact.reference,
                "handle": artifact.handle,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
            }
        )
    return minted


def remote_artifacts_from_completions(
    completions: Iterable[Any], *, max_artifacts: int = MAX_REMOTE_ARTIFACTS
) -> tuple[dict[str, RemoteMediaArtifact], bool]:
    """Stored cross-install completions → the remote half of a scope.

    ``completions`` is an iterable of ``{peer_install_id, media: [...]}`` rows —
    :func:`agent_runtime.dispatch_store.remote_media_completions`'s shape, taken
    as an argument so this stays a pure function of its input and the store stays
    injectable in a test.

    **Every row is re-validated here, and that is not belt-and-braces.** The
    ``media`` list was written by ANOTHER INSTALL. Trusting its ``media_type``
    would let a paired install put ``text/plain`` — or an extension outside
    :data:`IMAGE_EXTENSIONS` — into this install's namespace and then serve
    whatever it liked under it, which is exactly the exfiltration the allowlist
    exists to make unrepresentable. So the handle must match :data:`HANDLE_RE`,
    the reference must be an absolute path with an allowlisted image extension,
    and the media type is RE-DERIVED from that extension rather than read off
    the row. A row that fails any of those is dropped silently: it is not an
    error on this install, and a peer that sends junk gets no handle rather than
    an argument.

    Returns ``(rows, truncated)``. First writer wins on a duplicate handle,
    which is the newest completion (the store hands them back newest-first) —
    same bytes either way, so the choice only decides which peer gets dialled.
    """

    rows: dict[str, RemoteMediaArtifact] = {}
    truncated = False
    for completion in completions or ():
        if not isinstance(completion, dict):
            continue
        peer_install_id = str(completion.get("peer_install_id") or "").strip()
        if not peer_install_id:
            # A media map with nobody to ask is not a fetchable artifact; it is
            # a row that would refuse on every fetch while claiming to be in
            # scope, which is worse than absent.
            continue
        entries = completion.get("media")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if len(rows) >= max_artifacts:
                truncated = True
                continue
            artifact = _remote_artifact_for(entry, peer_install_id)
            if artifact is None or artifact.handle in rows:
                continue
            rows[artifact.handle] = artifact
    return rows, truncated


def build_media_scope(
    *,
    root: Path | None = None,
    max_artifacts: int = MAX_SCOPE_ARTIFACTS,
    max_logs: int = MAX_SCOPE_LOGS,
    tail_bytes: int = SCOPE_LOG_TAIL_BYTES,
    remote_completions: Iterable[Any] | None = None,
    max_remote_artifacts: int = MAX_REMOTE_ARTIFACTS,
) -> MediaScope:
    """Derive, right now, the set of artifacts a chat-reading caller may fetch.

    ``root`` defaults to the live-log directory this process already captured
    (``chat_live_log.capture_chat_live_log_root``) — the SAME resolution the
    mirror writes through, deliberately, because a second ladder is how the
    ``HERMES_HOME`` bleed that module's docstring warns about gets rebuilt one
    door over.

    Never raises. A mirror that cannot be read yields an EMPTY scope, and an
    empty scope refuses every handle — the failure direction that turns a broken
    projection into "no pictures" rather than into "any bytes you like".

    Ordering is newest-log-first so that a truncating scan keeps what a client is
    most likely rendering. Within a log the order is the file's own.

    ``remote_completions`` (Stage P4) is the SECOND source: stored cross-install
    dispatch completions carrying the ``reference → handle`` maps paired
    installs minted. ``None`` reads the store; an explicit (possibly empty)
    iterable is the injection seam a test uses. It never raises either — a
    store that will not open contributes no remote rows, which degrades this
    install to exactly the scope it had before Stage P4.
    """

    remote_source = (
        _remote_media_completions() if remote_completions is None else remote_completions
    )
    remote_rows, remote_truncated = remote_artifacts_from_completions(
        remote_source, max_artifacts=max_remote_artifacts
    )
    completions_scanned = len(list(remote_source)) if isinstance(remote_source, list) else 0

    directory = root if root is not None else _live_log_root()
    if directory is None:
        return MediaScope({}, 0, 0, remote_truncated, remote_rows, completions_scanned)

    try:
        logs = [p for p in directory.glob("*.jsonl") if p.is_file()]
    except OSError:
        return MediaScope({}, 0, 0, remote_truncated, remote_rows, completions_scanned)
    logs.sort(key=paths.safe_mtime, reverse=True)
    truncated = remote_truncated or len(logs) > max_logs
    logs = logs[:max_logs]

    artifacts: dict[str, MediaArtifact] = {}
    by_reference: dict[str, str] = {}
    declarations = 0
    scanned = 0
    for log in logs:
        scanned += 1
        for reference in _declarations_in_log(log, tail_bytes=tail_bytes):
            declarations += 1
            if reference in by_reference:
                continue
            if len(artifacts) >= max_artifacts:
                truncated = True
                continue
            artifact = _artifact_for(reference)
            if artifact is None:
                # Declared and not on this disk — the drafts directory this
                # machine's charsheet lane sweeps is full of these. Not an
                # error and not a handle.
                by_reference[reference] = ""
                continue
            by_reference[reference] = artifact.handle
            artifacts[artifact.handle] = artifact
    return MediaScope(
        artifacts, scanned, declarations, truncated, remote_rows, completions_scanned
    )


def resolve_handle(
    handle: Any, scope: MediaScope
) -> MediaArtifact | RemoteMediaArtifact | MediaRefusal:
    """Turn a caller's argument into an artifact, or into a typed refusal.

    THE boundary. The grammar check runs first and on the RAW argument, so a
    caller that sent ``../../../etc/passwd``, ``C:\\Windows\\win.ini`` or the
    reference string off a message it is looking at is refused
    :data:`REASON_HANDLE_INVALID` with no ``stat``, no ``open`` and no
    ``Path()`` constructed anywhere in this process. That ordering is the reason
    there is no traversal surface to argue about.

    A well-formed handle resolves against BOTH halves of the scope (Stage P4),
    local first, and the cap is applied to a remote row from the size its
    minting install reported — so an over-cap artifact on another machine is
    refused here, before any dial, with the same
    :data:`REASON_ARTIFACT_TOO_LARGE` a local one gets.
    """

    if not isinstance(handle, str):
        return MediaRefusal(REASON_HANDLE_INVALID, {"handle_type": type(handle).__name__})
    token = handle.strip()
    if not HANDLE_RE.match(token):
        return MediaRefusal(REASON_HANDLE_INVALID)
    artifact = scope.get(token)
    if artifact is None:
        return MediaRefusal(REASON_UNKNOWN_HANDLE)
    if not artifact.fetchable:
        return MediaRefusal(
            REASON_ARTIFACT_TOO_LARGE,
            {"cap_bytes": MAX_FETCH_BYTES, "size_bytes": artifact.size_bytes},
        )
    return artifact


def read_artifact_bytes(artifact: MediaArtifact) -> bytes | MediaRefusal:
    """The bytes, re-checked against the cap and against the handle itself.

    Both re-checks look redundant against :func:`resolve_handle` and neither is.
    The size on the scope is from a ``stat`` taken during the derivation, so a
    file that GREW since is caught here rather than read whole into this
    process's memory. And the digest is verified because a handle is a promise
    about BYTES: if the file changed, serving it under the old handle would poison
    every content-addressed cache downstream, forever, with no invalidation
    protocol able to reach it. A mismatch is :data:`REASON_UNKNOWN_HANDLE` — the
    handle names bytes this install no longer has, which is exactly what that
    reason says.
    """

    try:
        size = artifact.path.stat().st_size
    except OSError:
        return MediaRefusal(REASON_ARTIFACT_UNREADABLE)
    if size > MAX_FETCH_BYTES:
        return MediaRefusal(
            REASON_ARTIFACT_TOO_LARGE,
            {"cap_bytes": MAX_FETCH_BYTES, "size_bytes": size},
        )
    try:
        data = artifact.path.read_bytes()
    except OSError:
        return MediaRefusal(REASON_ARTIFACT_UNREADABLE)
    if handle_for_bytes(data) != artifact.handle:
        return MediaRefusal(REASON_UNKNOWN_HANDLE)
    return data


# ── the proxy cache (Stage P4) ───────────────────────────────────────────────
#
# A content-addressed cache is the one kind that needs no invalidation
# protocol: the key IS the digest, so an entry can never be stale — it is
# either the bytes the handle names or it is not an entry at all. That property
# is what makes the fetch lane's second hop free rather than merely cheap, and
# it is the whole reason Stage 8 minted handles over content instead of paths.


#: Where cached peer bytes live. Under the INSTALL's root, beside
#: ``gateway/peers.json``, and reached through ``peer_store_root()`` for that
#: function's own stated reason: ``persona_profile_context`` flips
#: ``HERMES_HOME`` process-globally during a persona turn, so the ambient
#: answer depends on which unrelated persona happened to be mid-turn. A cached
#: artifact belongs to the install, exactly as a peer edge does.
MEDIA_CACHE_DIRNAME = "media_cache"


def remote_cache_path(handle: str, *, root: Path | None = None) -> Path | None:
    """Where the bytes for ``handle`` would be cached, or ``None``.

    ``None`` for anything that is not a well-formed handle — which is also the
    reason this is safe to build a path from at all. The filename is the HEX
    of the digest and nothing else: 64 characters from ``[0-9a-f]``, so no
    caller-supplied string ever reaches a path segment and there is no
    traversal question to answer, only the grammar there already was.
    """

    if not isinstance(handle, str) or not HANDLE_RE.match(handle.strip()):
        return None
    digest = handle.strip()[len(HANDLE_PREFIX) :]
    try:
        base = root if root is not None else _peer_store_root()
    except Exception:  # pragma: no cover - defensive; a cache must never raise
        return None
    if base is None:
        return None
    return base / "gateway" / MEDIA_CACHE_DIRNAME / f"{digest}.bin"


def read_cached_bytes(handle: str, *, root: Path | None = None) -> bytes | None:
    """Cached bytes for ``handle``, VERIFIED, or ``None``.

    The verification is not paranoia about our own writes; it is what keeps the
    cache from being a way to launder bytes into a content-addressed namespace.
    The file sits on a disk other things can touch, and serving whatever is at
    ``<digest>.bin`` without re-hashing would mean the handle stopped naming
    bytes the moment anybody edited one. A mismatch DELETES the entry and
    answers ``None``, so the next fetch re-dials rather than serving a lie
    forever.
    """

    path = remote_cache_path(handle, root=root)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_FETCH_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if handle_for_bytes(data) != handle.strip():
        try:
            path.unlink()
        except OSError:  # pragma: no cover - best effort
            pass
        return None
    return data


def write_cached_bytes(handle: str, data: bytes, *, root: Path | None = None) -> bool:
    """Cache verified bytes under their handle. Best effort by contract.

    Verified HERE as well as by the caller, because this is the door: a write
    whose bytes do not hash to the name would poison every later read, and the
    check costs one pass over at most 5 MiB against a dial that costs a LAN
    round trip.

    ``False`` for anything not written — a bad handle, a mismatch, a disk that
    refused. A cache that cannot write is a fetch that costs a dial next time,
    never a fetch that fails, so no caller branches on this except to say so in
    a log.
    """

    path = remote_cache_path(handle, root=root)
    if path is None or not isinstance(data, bytes):
        return False
    if len(data) > MAX_FETCH_BYTES or handle_for_bytes(data) != handle.strip():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader that opens a half-written file would hash
        # it, find a mismatch, and DELETE the entry the writer is still filling.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.part")
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError:
        return False
    return True


# ── internals ───────────────────────────────────────────────────────────────


def _peer_store_root() -> Path | None:
    try:
        from .gateway_targets import peer_store_root

        return peer_store_root()
    except Exception:  # pragma: no cover - defensive; the cache must never raise
        return None


def _remote_media_completions() -> list[dict[str, Any]]:
    """Stored cross-install completions carrying a media map, newest first.

    Function-local import and total by construction, for
    :func:`build_media_scope`'s reason: a scope derivation that raised would
    turn a store hiccup into a refused fetch of a LOCAL picture, and the honest
    failure direction here is "no remote pictures", never "no pictures".
    """

    try:
        from . import dispatch_store

        return dispatch_store.remote_media_completions(limit=MAX_REMOTE_COMPLETIONS)
    except Exception:  # pragma: no cover - defensive
        return []


def _remote_artifact_for(
    entry: dict[str, Any], peer_install_id: str
) -> RemoteMediaArtifact | None:
    """One row of a peer's media map → a remote artifact, or ``None``.

    See :func:`remote_artifacts_from_completions` for why every field is
    re-derived rather than trusted.
    """

    handle = entry.get("handle")
    if not isinstance(handle, str) or not HANDLE_RE.match(handle.strip()):
        return None
    reference = entry.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        return None
    reference = reference.strip()
    if not _ABSOLUTE_PATH_RE.match(reference):
        return None
    media_type = MEDIA_TYPES.get(_extension(reference))
    if media_type is None:
        return None
    size = entry.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return RemoteMediaArtifact(
        handle=handle.strip(),
        reference=reference,
        peer_install_id=peer_install_id,
        media_type=media_type,
        size_bytes=size,
    )


def _live_log_root() -> Path | None:
    try:
        from .chat_live_log import capture_chat_live_log_root

        return capture_chat_live_log_root()
    except Exception:  # pragma: no cover - defensive; a scope must never raise
        return None


def _extension(reference: str) -> str:
    dot = reference.rfind(".")
    slash = max(reference.rfind("/"), reference.rfind("\\"))
    if dot <= slash:
        return ""
    return reference[dot:].lower()


def _declarations_in_log(log: Path, *, tail_bytes: int) -> Iterable[str]:
    """Declared image paths in one mirror file, reading at most its tail.

    A partial first line after a tail seek is DROPPED rather than repaired: the
    mirror is NDJSON, so a truncated line is not decodable, and a decoder that
    guessed would be inventing a message nobody wrote.
    """

    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()
            else:
                handle.seek(0)
            raw = handle.read()
    except OSError:
        return []

    found: list[str] = []
    for line in raw.split(b"\n"):
        if MEDIA_DECLARATION_PREFIX.encode("utf-8") not in line:
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        found.extend(media_declarations(row.get("text")))
    return found


def _artifact_for(reference: str) -> MediaArtifact | None:
    """One declared path → an artifact, or ``None`` when this disk cannot back it.

    ``resolve()`` is called for the STAT and for the memo key only. It is not a
    containment check and there is nothing here to contain: the reference did not
    come from the caller, it came from a transcript this install wrote, and the
    caller never sees a path it can bend.
    """

    try:
        path = Path(reference)
        if not path.is_file():
            return None
        resolved = path.resolve()
        stat = resolved.stat()
    except (OSError, ValueError):
        return None
    media_type = MEDIA_TYPES.get(_extension(reference))
    if media_type is None:  # pragma: no cover - the parser already gated this
        return None
    handle = _digest_for(resolved, stat.st_size, stat.st_mtime_ns)
    if handle is None:
        return None
    return MediaArtifact(
        handle=handle,
        reference=reference,
        path=resolved,
        media_type=media_type,
        size_bytes=stat.st_size,
    )


def _digest_for(path: Path, size: int, mtime_ns: int) -> str | None:
    key = (str(path), int(size), int(mtime_ns))
    with _digest_lock:
        cached = _digest_memo.get(key)
    if cached is not None:
        return cached
    if size > MAX_FETCH_BYTES:
        # Hashing a file nobody may fetch spends the whole read to learn a name
        # that only ever appears beside ``fetchable: false``. The size is the
        # honest thing to report, so the artifact is minted with a handle over
        # the STAT TRIPLE instead — it is stable, it is not a content address,
        # and it is never served, which is why the prefix still says what it is.
        return HANDLE_PREFIX + hashlib.sha256(
            f"oversize\x00{path}\x00{size}\x00{mtime_ns}".encode("utf-8")
        ).hexdigest()
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = HANDLE_PREFIX + digest.hexdigest()
    with _digest_lock:
        _digest_memo[key] = value
    return value
