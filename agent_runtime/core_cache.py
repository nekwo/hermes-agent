"""The persisted read-model core, validated by a stat fingerprint (Plan EG-3.1).

=============================================================================
WHY THIS EXISTS
=============================================================================

A serve child's first read-model core costs ~20 s of filesystem metadata work,
on EVERY boot — the build is per-process, so a warm machine pays it too
(measured 24.2 s warm, 2026-08-17). Doc 14's own numbers say the cost is not
bandwidth: serializing the core is ~5 ms. The 20 s IS validation, done by
reconstruction.

So make validation cost what validation costs. The core is persisted after
every successful default-store build, together with a sidecar carrying the
**fingerprint of every input the build read**. The next process stats those
inputs again: match → load the core and serve it authoritative in ~2 s;
mismatch → serve the persisted core immediately, LABELED STALE, while the full
build runs, then replace it and write back.

=============================================================================
WHY A STAT FINGERPRINT AND NOT AN EVENT OFFSET
=============================================================================

The refused design (Plan G BW-H1) keyed validity on the event log's offset plus
a tail replay. It stays refused, for cause:

* the events section is 3 ms of a 5,485 ms build — an offset-keyed cache buys
  almost nothing and can only go stale undetectably;
* two shipped incidents came from writers that mutate durable state with NO
  EventLog event (``running_work.py``'s checkpoint, ``board_sync``'s
  materialization), so an offset key cannot see them at all;
* a tail replay would be a SECOND validity authority beside the key, and the
  two would drift. Property 6 (one lane per question), applied to the cache
  itself.

**The fingerprint decides validity, full stop.** There is no event-tail replay
here and there must never be one. ``event_offset`` IS recorded in the sidecar —
as a diagnostic, so a divergence receipt can name the log position the core was
built at — and it is never read as an input to the match decision.

=============================================================================
THE SOUNDNESS GROUND
=============================================================================

A (path, mtime_ns, size) triple is only a change signal if every writer moves
mtime. In this runtime every durable write goes through
:func:`utils.atomic_json_write`, which stages a temp file and ``os.replace``s
it into position — a rename ALWAYS moves the target's mtime, including for a
rewrite that produces byte-identical content. That is what makes the cheap
signal sound here specifically, and it is why the enumeration is
DIRECTORY-LEVEL rather than a list of names: a file that did not exist at the
last build has no previous triple to compare, so the walk has to find it.

Two mtime-blind cases are covered explicitly rather than assumed:

* **SQLite.** A WAL commit that has not checkpointed leaves ``state.db``'s
  mtime untouched, so the ``-wal`` and ``-journal`` siblings are fingerprinted
  beside it — the WAL under a mask that stops READING the database from looking
  like writing it. See :data:`_DB_SIBLINGS` for the mask, its ground, and what
  it deliberately does not cover.
* **In-place rewrites inside a directory.** Replacing an existing entry does
  not move the CONTAINING directory's mtime on NTFS, which is why every file
  is stat'd individually instead of trusting its parent (the same reasoning as
  the boards-tree per-card stat pattern in ``harness_parts/serve.py``).

=============================================================================
THE INPUT CLOSURE — THE ONE THING THIS STAGE CAN GET WRONG
=============================================================================

Plan EG §6.1 names this the plan's single biggest bet: a MISSED input serves
unlabeled stale as authoritative, which is the failure class the plan exists to
end, inverted. Three mitigations are load-bearing, not decorative:

1. **The closure is derived from the build's own readers** — every class below
   resolves through the SAME path authority the projection reads through
   (``paths.store_root``, ``running_work_store_paths``,
   ``chat_session_db_path``, ``_get_profiles_root``, ``get_all_skills_dirs``).
   No second list free to drift.
2. **The equivalence golden** (``test_core_fingerprint_cache.py`` test 7) reds
   a gap inside the fixture matrix: for one fingerprint the cache-served core
   must equal the rebuilt core field-for-field.
3. **The shadow-validation window** reds it in the field: a cache-hit boot ALSO
   runs the full build in the background and compares; a divergence is a loud
   receipt naming the section AND the rebuilt core is adopted.

If a shadow receipt ever shows divergence, the fix is WIDENING the stat set —
never trusting the cache harder.

Two more receipts (ML-10) cover the ways this lane can fail QUIETLY rather than
wrongly, both on the same channel and countable by the same census:

* ``fingerprint_refused`` — a walk hit its entry bound, so the fingerprint is
  refused and the cache is off for this install. Unchanged as a decision; it was
  previously a WARNING sentence that did not even name the tree.
* ``never_converged`` — this process's consecutive write-backs never agreed, so
  no later process can be served the cache at all. It names the oscillating
  input paths, because the sanctioned response is again to widen the closure
  over a NAMED input.

=============================================================================
ONE AUTHORITY
=============================================================================

The store decides; the projection serves. A cached or stale-labeled core never
deletes, never refuses a write, and never wins a conflict on its own say-so —
the 2026-08-15 mass archive was a projection that had acquired store powers. A
stale-labeled core is marked ``parity.freshness.state = "stale"``, which is the
signal the launcher's existing stale-banner lane already reads
(``mission_control_snapshot.dart``: ``freshnessState == 'stale'`` →
``MissionSnapshotHealth.stale``), so a stale frame is never ``live`` and
therefore never authoritative. No write-lane predicate is reachable from
either field.

=============================================================================
THE RECEIPT CHANNEL TABLE (ML-14 / C22)
=============================================================================

Every receipt this lane emits rides ONE channel — this module's logger,
``agent_runtime.core_cache`` — and each line leads with a FAMILY token, then an
event token or ``key=value`` field. That is what makes a receipt countable: a
census greps the tokens, never the prose after them, and the prose is then free
to say whatever an operator needs to read.

**What this table retires is not a missing receipt, it is a missing index.**
Three vocabularies word themselves ``reason=`` on this one logger — the demote
reasons (``DEMOTE_*``), the write refusals, and ML-10's typed bound refusal —
and two spellings COLLIDE ACROSS TWO EVENTS: ``reason=fingerprint_unavailable``
and ``reason=build_stamp_unknown`` are emitted by the WRITE lane
(``snapshot_core_cache_write ok=false``) and by the READ lane's demote
(``snapshot_core_cache core_source=rebuilt``) alike. Grepping a reason without
its family token counts two different facts as one — the launcher-side class
ML-6 retired, where one refusal was worded ``patch_gap:`` on one channel and
``REFUSED gap:`` on the other and a census MEASURED a false zero. The family
token is the discriminator, which is why it leads every row below.

A "second channel" here means a surface OTHER than this logger that carries the
same fact. There is exactly one: the snapshot's own ``parity`` envelope, which
:func:`label_core` stamps. It is named per row rather than assumed.

| receipt (grep this) | second channel | what to grep there |
|---|---|---|
| ``snapshot_core_cache fingerprint_refused`` (WARNING) | none | fields ``reason=entries_exceeded`` ``scope=store_root``/``skill_root`` ``bound=`` ``root=``. The scope is load-bearing: the two bounds are different numbers over different trees |
| ``snapshot_core_cache never_converged`` (WARNING) | none | ``builds=`` then ``diff_scope=`` ``changed=`` ``diff=`` LAST (paths may contain spaces). **CENSUS RULE (C22(i)): read ``diff_scope=`` or the count over-reports.** ``every_pass`` = the inputs oscillate, i.e. self-perturbation, the A2 defect worth acting on; ``last_pair`` = a store that is simply moving, where the receipt is true (the cache IS buying nothing) but names no defect; ``none`` with ``diff=diff_unavailable`` and ``diff_reason=no_entries``/``digest_without_entry_delta`` = the diff could not be computed and says so in its own words |
| ``snapshot_core_cache core_source=cache`` (INFO) | the snapshot payload | ``parity.core_source == "cache"`` — SAME spelling, no split. The line also carries ``caller=`` ``inputs=`` ``fingerprint=`` ``offset=``, none of which reach the payload |
| ``snapshot_core_cache core_source=cache stale=true`` (INFO) | the snapshot payload | ``parity.core_stale == true`` AND ``parity.freshness.state == "stale"`` — the field the launcher's ``MissionSnapshotEnvelope`` already maps to ``MissionSnapshotHealth.stale``. RESIDUAL SPLIT, named rather than fixed: the log says ``stale=true``, the payload says ``parity.core_stale``/``parity.freshness.state``, and the payload spelling is a consumer contract that predates this lane |
| ``snapshot_core_cache core_source=rebuilt`` (INFO, ``_log_demote``) | the snapshot payload, PARTIALLY | ``parity.core_source == "rebuilt"`` carries THAT the cache was demoted; the ``reason=`` never leaves this logger, and ``CoreDecision.reason`` is read by no caller today. So a field census of WHY a cache demoted has exactly one source: this line. Reasons ``unreadable`` ``core_digest_mismatch`` ``fingerprint_unavailable`` ``fingerprint_mismatch`` ``build_stamp_unknown`` ``build_stamp_mismatch`` ``contract_mismatch`` ``runtime_root_mismatch``. ``absent`` is deliberately NOT logged (the ordinary cold start would print a line on every build in every process), so its only trace is the ABSENCE of a line and a census must not read "no demote line" as "no demote" |
| ``snapshot_core_cache_write ok=true`` (INFO) | none | ``inputs=`` ``fingerprint=`` ``offset=`` |
| ``snapshot_core_cache_write ok=false`` (INFO/WARNING) | none | reasons ``serialize`` ``build_stamp_unknown`` ``fingerprint_unavailable`` ``io``. **COLLISION:** ``build_stamp_unknown`` and ``fingerprint_unavailable`` are ALSO demote reasons on the row above. Grep the family token with them, never the reason alone |
| ``snapshot_core_shadow ok=true`` (INFO) | none | ``caller=`` ``divergence=none`` — the shadow build agreed with the cache |
| ``snapshot_core_shadow ok=false`` (WARNING) | none | ``caller=`` ``reason=build`` — the shadow build itself raised |
| ``snapshot_core_shadow_divergence`` (WARNING) | none | ``caller=`` ``section=`` (the first section that disagreed). Its own family token rather than a field on the row above, because retiring the shadow lane is keyed on counting exactly this |
| ``snapshot_core_shadow adopt failed`` (WARNING) | none | **NO EVENT TOKEN — the one uncountable line in this lane.** It is prose after the family token, so a census can only grep the sentence. Named here rather than quietly renamed: the rename is a one-line change with a consumer question attached, and this row is the record that it is owed |

Adding a receipt here means adding a ROW here.
``tests/agent_runtime/test_core_cache_channel_table.py`` drives both directions
— a token no row names, and a row naming a token no writer emits, each turn it
red — and separately drives the writers to prove the rendered line really
carries the spelling this table tells a census to grep.

**Scope.** This table covers the core-cache lane, which is the vocabulary C22
names. Three other things in ``agent_runtime`` are called receipts and are NOT
in it, deliberately, because they are different artifacts on different channels
rather than log lines: ``persona_chat_mints``' mint receipts (durable JSON files
under ``persona_chat_mint_receipt_path``), ``profile_runner``'s
``CHAT_COMPACTION_RECEIPT_KIND`` (a structured event payload) and
``snapshot.build_receipt_facts`` (a facts dict folded into the frame). Each
would need its own table keyed on its own channel; naming them here is what
stops this one from being read as the whole census.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from utils import atomic_json_write

from .dispatch_delivery import DRAIN_STATE_FILENAME
from .serve_auth import SERVE_AUTH_TOKEN_FILENAME
from .serve_registry import SERVE_INSTANCES_DIRNAME
from .serve_socket import SOCKET_LOCK_FILENAME, SOCKET_OWNER_FILENAME

logger = logging.getLogger(__name__)

#: The cache's own home under the agent-runtime store root. A DEDICATED
#: directory rather than the existing ``snapshot.json``: that file is
#: ``write_snapshot``'s boot cache and the launcher's cold-paint lane reads it,
#: so two writers with different provenance would share one path and neither
#: could say which one produced the bytes. It is also excluded from the
#: fingerprint below — a cache whose own writes flipped its key would
#: invalidate itself on every build.
CORE_CACHE_DIRNAME = "serve_read_model"
CORE_FILENAME = "core.json"
SIDECAR_FILENAME = "sidecar.json"

#: ``parity.core_source`` values.
CORE_SOURCE_CACHE = "cache"
CORE_SOURCE_REBUILT = "rebuilt"

#: Why a persisted core was NOT served. Every one of these rides the demote
#: receipt: "the cache did not answer" must never be a silent outcome.
DEMOTE_ABSENT = "absent"
DEMOTE_UNREADABLE = "unreadable"
DEMOTE_CORE_DIGEST_MISMATCH = "core_digest_mismatch"
DEMOTE_FINGERPRINT_UNAVAILABLE = "fingerprint_unavailable"
DEMOTE_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
DEMOTE_BUILD_STAMP_UNKNOWN = "build_stamp_unknown"
DEMOTE_BUILD_STAMP_MISMATCH = "build_stamp_mismatch"
DEMOTE_CONTRACT_MISMATCH = "contract_mismatch"
DEMOTE_RUNTIME_ROOT_MISMATCH = "runtime_root_mismatch"

# --------------------------------------------------------------------------- #
# The receipt vocabulary (ML-10)
# --------------------------------------------------------------------------- #
#: Every receipt this module emits rides ONE channel — this module's logger, in
#: the ``snapshot_core_cache`` / ``snapshot_core_shadow`` family — and each one
#: leads with an EVENT TOKEN in the first field after that prefix, exactly as
#: ``snapshot_core_shadow_divergence`` already does. That is what makes a receipt
#: countable: a census greps the token, never the prose after it, and the prose
#: is then free to say whatever an operator needs to read.
#:
#: The two tokens below are ML-10's. They exist because the facts they carry used
#: to be either a bare WARNING sentence (the bound refusal, which never named
#: WHICH root blew the bound) or nothing at all (a cache that never converges,
#: which was silent by construction — the process just kept buying nothing).
RECEIPT_FINGERPRINT_REFUSED = "fingerprint_refused"
RECEIPT_NEVER_CONVERGED = "never_converged"

#: The typed reason on a bound refusal, plus which walk refused. ``scope``
#: matters because the two bounds are different numbers over different trees, and
#: an operator handed only a count cannot tell which tree to go look at.
REFUSAL_ENTRIES_EXCEEDED = "entries_exceeded"
REFUSAL_SCOPE_STORE_ROOT = "store_root"
REFUSAL_SCOPE_SKILL_ROOT = "skill_root"

#: The never-converged receipt's diff arms. ``every_pass`` names the paths that
#: differed on EVERY pass of the streak — the oscillating inputs, which is the
#: fact worth acting on; ``last_pair`` is the honest fallback when no path
#: differed on all of them (a store that is simply moving, which reads
#: differently and must not borrow the oscillation sentence).
#:
#: The two ``diff_unavailable`` reasons are C16's lesson applied here: an arm that
#: could not compute the diff says SO, in its own words. Silently reusing another
#: arm's sentence is how a fail-quiet default gets read as a measurement.
DIFF_SCOPE_EVERY_PASS = "every_pass"
DIFF_SCOPE_LAST_PAIR = "last_pair"
DIFF_SCOPE_NONE = "none"
DIFF_UNAVAILABLE = "diff_unavailable"
DIFF_UNAVAILABLE_NO_ENTRIES = "no_entries"
DIFF_UNAVAILABLE_NO_ENTRY_DELTA = "digest_without_entry_delta"

#: Hard bound on the store-root walk. Reaching it is NOT a partial answer: the
#: fingerprint becomes ``None`` and the caller must treat that as "never
#: cache". A truncated stat set is exactly a missed input.
MAX_FINGERPRINT_ENTRIES = 200_000

#: Per-root bound on the skill-registry walk. Skill packages are small trees
#: (``<root>/<slug>/SKILL.md`` plus package files); a root that blows past this
#: is not a skill registry, and the same refusal applies.
MAX_SKILL_ENTRIES_PER_ROOT = 20_000

#: Store-root entries that are DELIBERATELY not fingerprinted, each because it
#: moves for reasons a read-model core does not depend on. Anything not named
#: here is fingerprinted, so the default posture is inclusion.
#:
#: **A name its writer owns as a constant is IMPORTED here, never spelled.**
#: That is not a style tightening; hand-spelling is the defect this set shipped
#: with. ``"drain_state.json"`` sat in it annotated "per
#: ``dispatch_delivery.DRAIN_STATE_FILENAME``" while that constant read
#: ``dispatch_delivery_drain.json`` — so the exclusion named a file that has
#: never existed, and the real drain mirror, rewritten every
#: ``dispatch_delivery.DRAIN_MIRROR_HEARTBEAT_SECONDS`` for the life of a serve,
#: stayed INSIDE the key. A comment naming a constant is not a reference to it;
#: an import is, and it is the only form the compiler checks.
#:
#: ``serve_socket``'s own module doctrine (its "Fingerprint exclusion" section)
#: already required both socket files to be out of every freshness fingerprint,
#: and cited the same standing precedent this set is built on. It enumerated the
#: ALLOWLIST fingerprints — serve's ``_FINGERPRINT_ROOT_FILES`` /
#: ``_FINGERPRINT_STORE_DIRS`` and ``stream._scope_fingerprint`` — and this walk
#: is a DENYLIST, so "not added" was true there and violated here. Two
#: fingerprint designs with opposite defaults need the doctrine written on both;
#: that paragraph now names this constant too.
#:
#: Measured consequence of the two holes together (2026-08-18): every serve boot
#: rewrote ``serve_socket.owner.json`` and the drain rewrote its mirror within
#: seconds of boot, so no boot's key could describe the store the NEXT boot
#: stat'd. The lane demoted ``fingerprint_mismatch`` on every same-commit boot
#: from the day it shipped.
#:
#: RESIDUAL, stated rather than discovered later. Four names below are still
#: literals because no writer module owns them as a constant: ``locks`` and
#: ``snapshot.json`` are spelled inline inside their own path helpers in
#: ``agent_runtime.paths``, and the ``read_model.db`` trio is a CONFIGURABLE
#: default (``runtime_config``'s ``read_model.db_filename``) — an install that
#: renames it re-opens exactly the hole this comment block is about, one config
#: key away. Both are the same class as the drain defect and neither is fixed
#: here; the gate below can only prove the names a WRITER produces, so it cannot
#: see them either.
_EXCLUDED_STORE_ENTRIES = frozenset(
    {
        # The cache's own home (see CORE_CACHE_DIRNAME).
        CORE_CACHE_DIRNAME,
        # Entries appear and vanish at every serve boot/exit, and the auth token
        # appears at first boot. The standing precedent is already recorded at
        # ``agent_runtime/serve_registry.py`` and ``agent_runtime/serve_auth.py``
        # and in the read-cache fingerprint's own comment block.
        SERVE_INSTANCES_DIRNAME,
        SERVE_AUTH_TOKEN_FILENAME,
        # The socket owner sidecar and the lock that elects it. Rewritten at
        # every socket boot and removed on every clean exit — the serve
        # registry's shape exactly, refused for the same reason, and required to
        # be refused by ``serve_socket``'s own doctrine.
        SOCKET_LOCK_FILENAME,
        SOCKET_OWNER_FILENAME,
        # Lock files are created and removed INSIDE a build; a lock in the stat
        # set would make a build's own locking flip the key it just wrote.
        "locks",
        # ``write_snapshot``'s boot cache and the projector's read model are
        # OUTPUTS of the projection, never inputs to it.
        "snapshot.json",
        "read_model.db",
        "read_model.db-wal",
        "read_model.db-shm",
        # The delivery drain's telemetry mirror: a 60-second oscillator that no
        # projection reads. Same rule as the serve registry.
        DRAIN_STATE_FILENAME,
    }
)

#: ``atomic_json_write`` stages ``.<stem>_*.tmp`` beside its target. A staged
#: temp file that a crash stranded is not an input; a live one belongs to a
#: write that will move the real file anyway.
_TMP_SUFFIX = ".tmp"

#: SQLite's mtime-blind siblings. A WAL commit that has not checkpointed leaves
#: the main database file untouched, so the journal files are stat'd beside it.
#:
#: ``-shm`` IS NOT HERE, and the ``-wal`` entry wears a mask. Both are the same
#: correction: the build OPENS these databases, and an open is not a write.
#:
#: What was measured (2026-08-18, live root and reproduced in the fixture): with
#: a connection held, opening the SessionDB a second time moves ``-wal``'s mtime
#: while leaving it at SIZE 0 — the closing connection checkpoints, so the file
#: is re-created empty at open time — and rewrites ``-shm``. Both moved DURING
#: the boot build that was reading them. The key is taken pre-build by design, so
#: the build's own read guaranteed the next process a ``fingerprint_mismatch``:
#: the cache could not converge inside one process, let alone across two.
#:
#: THE MASK, and why each half is sound:
#:
#: * ``-shm`` carries no content signal at all. SQLite documents it as a
#:   non-persistent shared-memory index, rebuilt from the WAL by whichever
#:   process opens the database, and deleted when the last connection closes. Its
#:   mtime is an open-time artefact of a READER. Anything it could indicate is
#:   already carried by ``-wal`` (the frames it indexes) or by the main file (the
#:   checkpoint that retired them). ``stream._scope_fingerprint`` — the other
#:   fingerprint in this runtime that stats these siblings — has always used
#:   ``("", "-wal", "-journal")``, so dropping it here CONVERGES the two
#:   conventions rather than forking them.
#: * ``-wal`` is keyed on ``(path, size, mtime_ns if size > 0 else 0)``. A
#:   zero-length WAL holds no frames — there is nothing in it for the projection
#:   to read, and its mtime says only when some process last opened or
#:   checkpointed the database. The instant an uncheckpointed commit lands the
#:   file is non-empty, and the mtime counts again in full: the WAL-commit signal
#:   EG-3.1 requires is untouched for every state the signal can actually be in.
#:   Absent (``-1``) and present-but-empty (``0``) stay distinguishable, because
#:   the mask moves the mtime and never the size.
#:
#: WHAT THE MASK DOES NOT COVER, stated rather than discovered later. A
#: checkpoint that truncates the WAL to zero AFTER writing its frames into
#: ``state.db`` leaves a zero-length WAL whose mtime this ignores — but that
#: checkpoint moved ``state.db``'s own mtime and size, and the main file is the
#: FIRST entry in this tuple, so the change is carried there. The uncovered case
#: would be a commit that is both invisible in the main file and invisible in the
#: WAL's size, which SQLite's own durability rules do not admit.
_DB_SIBLINGS = ("", "-wal", "-journal")

#: The sibling whose mtime is masked while it is empty. Named rather than
#: spelled at the call site so the mask and the enumeration cannot drift.
_WAL_SIBLING = "-wal"

#: A DIRECTORY contributes its PATH and nothing else — never a timestamp, never
#: a present/absent distinction.
#:
#: Not an optimization; a correctness requirement, and it cost a false demote to
#: learn. Two independent reasons, both measured on this runtime's platform:
#:
#: 1. **A directory's own signal is perturbed by the children this fingerprint
#:    deliberately excludes.** The cache writes ``serve_read_model/`` INTO the
#:    store root, so a root whose existence-or-mtime counted made the very write
#:    that persisted a core invalidate the key it had just persisted — a
#:    guaranteed miss on every boot. Same hole for ``locks/``, the serve
#:    registry, and ``atomic_json_write``'s staged temp files.
#: 2. **A directory's mtime is not a reliable add signal anyway.** Measured on
#:    NTFS: creating a FILE inside a directory left the directory's ``mtime_ns``
#:    unchanged, while a later ``mkdir`` moved it. So it is noise in one
#:    direction and silence in the other — the worst combination for a change
#:    key.
#:
#: Nothing is lost. The enumeration is directory-LEVEL: an added file arrives as
#: its own new triple and a removed one takes its triple with it, so the parent's
#: timestamp is strictly redundant with the walk that produced it. That is the
#: same reasoning as the boards-tree per-card stat pattern in
#: ``harness_parts/serve.py``, taken one step further.
_DIR_MARK = -2


class FingerprintEntry(NamedTuple):
    path: str
    mtime_ns: int
    size: int


class CoreFingerprint(NamedTuple):
    """The sorted stat set over every build input, plus its own digest.

    ``entries`` is kept (not just the digest) so a divergence investigation can
    diff two fingerprints and name the file that moved. ``digest`` is what the
    sidecar stores: the entry list on the live store is tens of thousands of
    triples and the sidecar is read on the boot path.
    """

    entries: tuple[FingerprintEntry, ...]
    digest: str

    @property
    def count(self) -> int:
        return len(self.entries)


def _stat_entry(path: Any) -> FingerprintEntry:
    """One (path, mtime_ns, size) triple. An ABSENT path is a stable signal.

    A missing file records ``-1/-1`` rather than being skipped: "this input does
    not exist" is a fact the next build must be able to disagree with. Skipping
    it would make an appearing file indistinguishable from an unchanged one.

    A DIRECTORY records ``_DIR_MARK`` for both numbers — see that constant for
    why its mtime is poison rather than signal.
    """

    text = str(path)
    try:
        st = os.stat(path)
    except OSError:
        return FingerprintEntry(text, -1, -1)
    if stat.S_ISDIR(st.st_mode):
        return FingerprintEntry(text, _DIR_MARK, _DIR_MARK)
    return FingerprintEntry(text, int(st.st_mtime_ns), int(st.st_size))


def _entry_triple(entry: os.DirEntry, is_dir: bool) -> FingerprintEntry:
    if is_dir:
        return FingerprintEntry(entry.path, _DIR_MARK, _DIR_MARK)
    try:
        st = entry.stat()
    except OSError:
        return FingerprintEntry(entry.path, -1, -1)
    return FingerprintEntry(entry.path, int(st.st_mtime_ns), int(st.st_size))


def _walk_tree(root: Path, out: list[FingerprintEntry], *, limit: int, exclude_top: frozenset[str] = frozenset()) -> bool:
    """Enumerate ``root`` and every descendant, bounded by ``limit``.

    Returns False when the bound was reached — the caller must then refuse to
    fingerprint at all rather than serve a truncated stat set.

    Every FILE contributes (path, mtime_ns, size); every DIRECTORY contributes
    its path alone. Files individually rather than by their parent's mtime
    because replacing an existing entry does not move the containing directory
    on NTFS (the in-place-rewrite case the boards-tree per-card stat pattern
    already exists for); directories by path alone for the reason at
    :data:`_DIR_MARK`.

    Symlinked directories ARE followed, and the choice is deliberate: treating
    one as a leaf would leave everything under it outside the closure, which is
    the failure mode that matters here. A symlink LOOP is therefore possible and
    is handled by the bound rather than by loop detection — hitting ``limit``
    refuses the whole fingerprint, and refusing means "never cache", which is
    safe. Detecting the loop and continuing would not be: it would produce a
    plausible key over an incomplete walk.

    That doctrine is UNCHANGED by ML-10 and is kept deliberately (A5). What
    changed is only that the caller's refusal now leaves a countable receipt
    naming the tree — :func:`_receipt_fingerprint_refused` — so a loop that
    disables the cache for a whole install is a census row rather than one
    WARNING sentence somebody has to already be reading.
    """

    # The tree root records its PATH only, never its existence-or-not: a root
    # that appears because the cache wrote its own directory into it (the store
    # root's first-ever write on a virgin install) must not flip the key, and a
    # root that genuinely gains content flips it through the content's own
    # triples.
    out.append(FingerprintEntry(str(root), _DIR_MARK, _DIR_MARK))
    try:
        top_level = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        # An unreadable root is itself a stable signal (recorded above). It is
        # NOT a bound failure: a store root that does not exist yet is the
        # ordinary cold-start shape.
        return True
    pending: list[os.DirEntry] = []
    for entry in top_level:
        if entry.name in exclude_top:
            continue
        pending.append(entry)
    while pending:
        if len(out) >= limit:
            return False
        entry = pending.pop()
        name = entry.name
        if name.endswith(_TMP_SUFFIX) and name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        out.append(_entry_triple(entry, is_dir))
        if not is_dir:
            continue
        try:
            pending.extend(sorted(os.scandir(entry.path), key=lambda item: item.name))
        except OSError:
            continue
    return len(out) < limit


def _empty_wal_is_content_free(entry: FingerprintEntry) -> FingerprintEntry:
    """Drop a ZERO-LENGTH WAL's mtime; keep everything else exactly as stat'd.

    The one line where "the build reads this database" stops being spelled the
    same way as "somebody wrote to this database". See :data:`_DB_SIBLINGS` for
    the ground under the mask.

    ``size`` is never touched, so absent (``-1``) and present-but-empty (``0``)
    remain different facts — the same rule :func:`_stat_entry` follows for a
    missing file.
    """

    if entry.size > 0:
        return entry
    return FingerprintEntry(entry.path, 0, entry.size)


def _db_entries(db_path: Any, out: list[FingerprintEntry]) -> None:
    text = str(db_path)
    for suffix in _DB_SIBLINGS:
        entry = _stat_entry(text + suffix)
        if suffix == _WAL_SIBLING:
            entry = _empty_wal_is_content_free(entry)
        out.append(entry)


def _receipt_fingerprint_refused(*, scope: str, root: Any, bound: int) -> None:
    """The countable artifact for a walk that hit its entry bound (A4).

    A bound refusal disables the cache for the whole install — every boot pays
    the full build, forever, and the only thing that said so was a WARNING
    sentence that did not even name which store root blew the bound. This is the
    same fact on the same channel the shadow lane reports divergence on, in the
    shape a census can count: ``reason`` types it, ``scope`` says WHICH walk
    refused (the two bounds are different numbers over different trees),
    ``root`` names the tree to go look at, ``bound`` says what it was measured
    against.

    **The refusal itself is untouched and stays untouched.** Reaching a bound
    still makes the fingerprint ``None``, and ``None`` still means never cache —
    see :func:`build_input_fingerprint`. This function adds a receipt and decides
    nothing.
    """

    logger.warning(
        "snapshot_core_cache %s reason=%s scope=%s bound=%d root=%s — the walk "
        "hit its bound, so the stat set would have been truncated; the "
        "fingerprint is refused outright and nothing may be served from the "
        "cache until the tree named here shrinks or the bound is re-measured. A "
        "truncated stat set is exactly a missed input.",
        RECEIPT_FINGERPRINT_REFUSED,
        REFUSAL_ENTRIES_EXCEEDED,
        scope,
        bound,
        root,
    )


def build_input_fingerprint() -> CoreFingerprint | None:
    """The stat set over EVERY input the read-model build reads.

    ``None`` means "I could not fingerprint the inputs" and every caller must
    read it as **never cache** — not as "nothing changed". A missing answer is a
    loud refusal here, because the alternative is serving unlabeled stale as
    authoritative.

    The seven input classes, each resolved through the authority the BUILD
    reads through (§6.1's first mitigation — one authority, no second list):

    1. the agent-runtime store root subtree — ``paths.store_root()``, walked
       recursively so an ADDED file flips the key (offices, boards, personas,
       assignments, the event log and its rotation manifest, prompt
       observability, realm-sync baselines: everything the projection reads
       from the store, without a name list to fall behind);
    2. the ``running_work`` durable stores — ``running_work_store_paths()``, the
       ONE authority for them (they hang off the HERMES home, not the store
       root, and both mutate with NO event);
    3. the chat SessionDB — ``chat_session_db_path()``, the database the CHAT
       LANE writes, plus its WAL siblings;
    4. the profile inputs ``agents_readiness`` reads — the profiles root and,
       per profile, ``profile.yaml`` + ``config.yaml``, plus the sticky
       ``active_profile`` pointer that decides which one a bare invocation
       resolves;
    5. the config inputs — the ambient ``get_config_path()`` and the ROOT
       ``harness_root_config_path()`` (two authorities in production because
       the CLI profile redirect makes them genuinely different files);
    6. the skill registries — ``get_all_skills_dirs()`` (local profile skills,
       the shared canonical root, configured external roots) walked per root,
       plus the in-repo harness-skill source root the hash comparison reads;
    7. the event-rotation lane — the manifest and the resolved LIVE slice.
       Under the store root today, so class 1 covers them; stat'd explicitly
       anyway because the resolution is free to move the live slice elsewhere
       and a frozen ``events.jsonl`` entry after a rotation is exactly the
       silent-staleness shape this whole module is against.
    """

    entries: list[FingerprintEntry] = []

    # 1 — the agent-runtime store root subtree.
    try:
        from . import paths as _paths

        root = _paths.store_root()
    except Exception:
        return None
    if not _walk_tree(root, entries, limit=MAX_FINGERPRINT_ENTRIES, exclude_top=_EXCLUDED_STORE_ENTRIES):
        _receipt_fingerprint_refused(
            scope=REFUSAL_SCOPE_STORE_ROOT,
            root=root,
            bound=MAX_FINGERPRINT_ENTRIES,
        )
        return None

    # 2 — the running_work durable stores.
    try:
        from .running_work import running_work_store_paths

        store_paths = running_work_store_paths()
    except Exception:
        return None
    if not store_paths:
        # The authority could not resolve a home. "I cannot fingerprint these"
        # is not "there is nothing to watch" — refuse.
        return None
    for path in store_paths:
        _db_entries(path, entries)

    # 3 — the chat SessionDB.
    try:
        from .chat_session_scope import chat_session_db_path

        _db_entries(chat_session_db_path(), entries)
    except Exception:
        return None

    # 4 — profile inputs + the sticky active-profile pointer.
    try:
        from hermes_cli.profiles import _get_default_hermes_home, _get_profiles_root

        profiles_root = _get_profiles_root()
        entries.append(_stat_entry(profiles_root))
        entries.append(_stat_entry(_get_default_hermes_home() / "active_profile"))
        try:
            profile_dirs = sorted(os.scandir(profiles_root), key=lambda item: item.name)
        except OSError:
            profile_dirs = []
        for entry in profile_dirs:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            entries.append(_stat_entry(entry.path))
            entries.append(_stat_entry(Path(entry.path) / "profile.yaml"))
            entries.append(_stat_entry(Path(entry.path) / "config.yaml"))
    except Exception:
        return None

    # 5 — the two config authorities.
    try:
        from hermes_constants import get_config_path

        from .config import harness_root_config_path

        entries.append(_stat_entry(get_config_path()))
        entries.append(_stat_entry(harness_root_config_path()))
    except Exception:
        return None

    # 6 — the skill registries.
    try:
        from agent.skill_utils import get_all_skills_dirs

        from .skill_install import harness_skill_source_root

        roots = [*get_all_skills_dirs(), harness_skill_source_root()]
    except Exception:
        return None
    seen_roots: set[str] = set()
    for skill_root in roots:
        key = str(skill_root)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        if not _walk_tree(Path(skill_root), entries, limit=len(entries) + MAX_SKILL_ENTRIES_PER_ROOT):
            _receipt_fingerprint_refused(
                scope=REFUSAL_SCOPE_SKILL_ROOT,
                root=key,
                bound=MAX_SKILL_ENTRIES_PER_ROOT,
            )
            return None

    # 7 — the event-rotation lane.
    try:
        from . import event_rotation as _event_rotation

        entries.append(_stat_entry(_event_rotation.manifest_path()))
        entries.append(_stat_entry(_event_rotation.live_path()))
    except Exception:
        return None

    ordered = tuple(sorted(set(entries)))
    digest = hashlib.sha256(
        "\n".join(f"{item.path}|{item.mtime_ns}|{item.size}" for item in ordered).encode(
            "utf-8", "surrogatepass"
        )
    ).hexdigest()
    return CoreFingerprint(ordered, digest)


def contract_versions() -> dict[str, int]:
    """The wire versions a persisted core was produced under.

    A core written by a build whose contract has since moved is a core a
    consumer would decode against the wrong shape. Compared as a whole dict, so
    ADDING a version to this set is itself a demote signal for every core
    written before it — which is the safe direction.
    """

    from .parity import PARITY_ENVELOPE_VERSION
    from .snapshot import SNAPSHOT_CONTRACT_VERSION
    from .stream import STREAM_SCHEMA_VERSION

    return {
        "snapshot_contract": int(SNAPSHOT_CONTRACT_VERSION),
        "parity_envelope": int(PARITY_ENVELOPE_VERSION),
        "stream_schema": int(STREAM_SCHEMA_VERSION),
    }


def build_stamp_token() -> str | None:
    """WHICH CODE built the persisted core, or ``None`` when unmeasurable.

    ``None`` refuses the cache. An install whose build cannot be measured — no
    repo, no baked sha, a hung ``git`` — cannot prove the persisted core was
    produced by the code now running, and property 5 says an upgrade must never
    be able to serve the old install's core. Refusing is loud (the demote
    receipt names ``build_stamp_unknown``) and it is the safe direction.

    ``dirty`` rides the token, so a clean → dirty transition demotes. The
    residual is stated rather than hidden: two different EDITS that both leave
    the checkout dirty produce the same token, so on a dirty tree the stamp
    cannot distinguish them. That window is exactly what the shadow-validation
    comparison covers in the field, and it does not exist on any install the
    operator ships from.
    """

    try:
        from .build_stamp import build_stamp

        stamp = build_stamp()
    except Exception:
        return None
    if stamp.commit is None:
        return None
    return f"{stamp.source}:{stamp.commit}:{'dirty' if stamp.dirty else 'clean' if stamp.dirty is not None else 'unknown'}"


def _cache_dir() -> Path:
    from . import paths as _paths

    return _paths.store_root() / CORE_CACHE_DIRNAME


def core_path() -> Path:
    return _cache_dir() / CORE_FILENAME


def sidecar_path() -> Path:
    return _cache_dir() / SIDECAR_FILENAME


def _core_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Write-back
# --------------------------------------------------------------------------- #
def write_back(core: dict, *, fingerprint: CoreFingerprint | None = None) -> bool:
    """Persist the core's WIRE form plus its sidecar. Best effort, by contract.

    A failed write logs and changes NOTHING about the build that produced the
    core — the build path is byte-identical whether this succeeds or fails,
    which is what makes the cache safe to add to a hot path (test 9's second
    half is the pin).

    The sidecar binds to the core BYTES via ``core_sha256``, not merely to the
    path. Two writers cannot own one file here, but a half-replaced pair, a
    hand-edited core, or a rollback that restored an older ``core.json`` beside
    a newer sidecar would otherwise be indistinguishable from a valid pair.

    **``fingerprint`` must be the caller's PRE-build stat set.** The direction of
    the error matters and only one direction is safe. A key stat'd AFTER the
    build would absorb any write that landed WHILE the build ran: the core does
    not contain that write, the key says the inputs are unchanged, and the next
    process serves a core missing a write as authoritative — precisely the
    failure this stage exists to prevent. A key stat'd BEFORE the build is at
    worst OLDER than the core, which demotes the next process to a rebuild it
    did not strictly need. ``build_snapshot`` therefore takes the stat
    immediately before ``_build_snapshot_uncoalesced`` and threads it here;
    computing one locally (the ``None`` default) is for callers that hold no
    build, and it accepts that same conservative loss.

    **Named consequence: a cold store converges in two builds, not one.** The
    build is not a pure reader — ``PersonaInstanceStore.ensure_for_personas``
    materializes missing instance rows, and the chat SessionDB is CREATED by the
    first process that opens it. On a store where neither has happened yet, the
    pre-build key describes inputs the build itself then changed, so the next
    process demotes once and rebuilds; its own write-back records the settled
    state and every later process matches. That is a property of the
    conservative direction, not a defect — the other direction converges one
    build sooner and can serve a lost write forever.

    **Cost, named rather than discovered.** EVERY successful default-store build
    writes here, and a live-store core is megabytes, so a serve process that
    demotes several delta batches in a minute writes that many times. Priced and
    accepted for this landing: the write is ~5 ms of serialize plus one fsync
    against a build that costs seconds, and the alternative — skipping the write
    when the persisted pair would already match — needs the FULL judgement (a
    sidecar-only check leaves a tampered core permanently unhealed, because the
    read path refuses it while the write path keeps declining to replace it). If
    receipts show the churn matters, that is the refinement, gated on
    :func:`read_persisted_core`, not a narrowing of which builds write.
    """

    from .serde import to_jsonable

    try:
        payload = to_jsonable(core)
    except Exception:
        logger.warning("snapshot_core_cache_write ok=false reason=serialize", exc_info=True)
        return False
    stamp = build_stamp_token()
    if stamp is None:
        logger.info("snapshot_core_cache_write ok=false reason=build_stamp_unknown")
        return False
    key = fingerprint if fingerprint is not None else build_input_fingerprint()
    if key is None:
        logger.info("snapshot_core_cache_write ok=false reason=fingerprint_unavailable")
        return False
    parity = payload.get("parity") if isinstance(payload.get("parity"), dict) else {}
    watermark = parity.get("watermark") if isinstance(parity.get("watermark"), dict) else {}
    sidecar = {
        "fingerprint": key.digest,
        "fingerprint_entries": key.count,
        "build_stamp": stamp,
        "contract_versions": contract_versions(),
        # DIAGNOSTIC ONLY. Recorded so a divergence receipt can name the log
        # position the core was built at. It is NEVER an input to the match
        # decision below — see the module header on why an offset key is
        # refused.
        "event_offset": watermark.get("event_offset"),
        "core_sha256": _core_digest(payload),
        "runtime_root": str(_runtime_root_for_sidecar(parity)),
        "generated_at": payload.get("generated_at"),
    }
    try:
        atomic_json_write(core_path(), payload, indent=None, separators=(",", ":"), sort_keys=True)
        atomic_json_write(sidecar_path(), sidecar, indent=None, sort_keys=True)
    except Exception:
        logger.warning("snapshot_core_cache_write ok=false reason=io", exc_info=True)
        return False
    logger.info(
        "snapshot_core_cache_write ok=true inputs=%d fingerprint=%s offset=%s",
        key.count,
        key.digest[:12],
        "unknown" if sidecar["event_offset"] is None else sidecar["event_offset"],
    )
    _note_written_key(key)
    return True


# --------------------------------------------------------------------------- #
# Convergence — whether this process's cache is buying anything (ML-10 / A2)
# --------------------------------------------------------------------------- #
# The cache can fail in a way that costs nothing and says nothing: if some input
# moves on EVERY build, no key a build writes can ever describe the store the
# next build stats, so every process demotes, every process rebuilds, and the
# whole lane silently buys nothing forever. Nothing above detects that — a
# demote is individually legitimate, and the write-back that follows it looks
# exactly like a healthy one.
#
# The measurement is free, because both halves already exist: every build hands
# ``write_back`` the key it would persist, so a process can simply notice that
# its own consecutive write-backs never agree. Past ``NEVER_CONVERGED_BUILDS``
# it says so and NAMES the paths, because the sanctioned response to this — the
# same one the shadow lane's divergence receipt asks for — is to widen the stat
# set's closure over a named input, never to trust the cache harder.
#
# WHAT IS RETAINED, AND WHY IT IS NOT A STAT SET PER PROCESS. The digest of the
# last key written is kept always (a string). The last key's ENTRIES are kept
# only while a streak is live and dropped the moment two write-backs agree, so a
# settled process — which is every healthy one — holds no second stat set for a
# diagnostic that is not going to fire. On a live store an entry list is tens of
# thousands of triples; that is worth one branch to not retain.
#
# This block decides NOTHING. It reads the key the build already computed, and
# every write-back returns exactly what it returned before.

#: How many consecutive write-backs may disagree with the one before them before
#: the cache says out loud that it is buying nothing.
#:
#: THREE, and it is the measured virgin-root convergence rather than a round
#: number. A cold store legitimately fails to settle for a build or two — the
#: build is not a pure reader (``PersonaInstanceStore.ensure_for_personas``
#: materializes instance rows; the chat SessionDB is CREATED by the first process
#: that opens it), and the key is taken pre-build on purpose, so the first
#: write-back describes inputs the build then moved. ``write_back``'s own
#: docstring names that consequence, and the test helper
#: ``converge_persisted_core`` measures it. A bound at the measured convergence
#: is the one number that cannot fire on the healthy shape and does fire on the
#: pathological one, where the disagreement never ends.
NEVER_CONVERGED_BUILDS = 3

#: How many oscillating paths the receipt names. ``changed=`` carries the full
#: count beside them, so the cap can never make a large drift read as a small one.
_NEVER_CONVERGED_DIFF_PATHS = 5

_convergence_lock = threading.Lock()
_last_written_digest: str | None = None
_streak_entries: tuple[FingerprintEntry, ...] = ()
_streak_length = 0
_streak_last_diff: tuple[str, ...] | None = None
_streak_common_diff: frozenset[str] | None = None
_never_converged_reported = False


def _reset_convergence_state() -> None:
    """Forget this process's convergence history, as a fresh process would."""

    global _last_written_digest, _streak_entries, _streak_length
    global _streak_last_diff, _streak_common_diff, _never_converged_reported
    with _convergence_lock:
        _last_written_digest = None
        _streak_entries = ()
        _streak_length = 0
        _streak_last_diff = None
        _streak_common_diff = None
        _never_converged_reported = False


def _changed_paths(
    before: tuple[FingerprintEntry, ...], after: tuple[FingerprintEntry, ...]
) -> tuple[str, ...]:
    """Every PATH whose triple differs between two stat sets.

    By path rather than by triple: a file whose mtime moved would otherwise be
    named twice (its old triple and its new one) and read as two inputs. An added
    or removed path differs too — its triple is absent on one side — which is the
    same rule ``_stat_entry`` follows for a missing file.
    """

    left = {entry.path: (entry.mtime_ns, entry.size) for entry in before}
    right = {entry.path: (entry.mtime_ns, entry.size) for entry in after}
    return tuple(
        sorted(path for path in set(left) | set(right) if left.get(path) != right.get(path))
    )


def _note_written_key(key: CoreFingerprint) -> None:
    """Record what this write-back persisted, and report a lane that never settles.

    Called on SUCCESSFUL write-backs only: a write that did not land is already a
    receipt of its own (``snapshot_core_cache_write ok=false``) and a key that was
    never persisted is not a key any later process could have agreed with.
    """

    global _last_written_digest, _streak_entries, _streak_length
    global _streak_last_diff, _streak_common_diff, _never_converged_reported

    with _convergence_lock:
        previous_digest = _last_written_digest
        previous_entries = _streak_entries
        _last_written_digest = key.digest
        if previous_digest is None:
            # The first write-back of a process has nothing to agree with, and
            # "one build" is never evidence of non-convergence.
            return
        if previous_digest == key.digest:
            # Settled: two consecutive builds wrote the same key, so the store
            # the next process stats is the store this one described.
            _streak_entries = ()
            _streak_length = 0
            _streak_last_diff = None
            _streak_common_diff = None
            return
        _streak_length += 1
        _streak_entries = key.entries
        if previous_entries and key.entries:
            changed = _changed_paths(previous_entries, key.entries)
            _streak_last_diff = changed
            _streak_common_diff = (
                frozenset(changed)
                if _streak_common_diff is None
                else _streak_common_diff & frozenset(changed)
            )
        if _streak_length < NEVER_CONVERGED_BUILDS or _never_converged_reported:
            return
        # Once per process. The receipt names an input to go widen the closure
        # over; repeating it every build afterwards would bury that under its own
        # noise without adding a fact.
        _never_converged_reported = True
        builds = _streak_length
        common = _streak_common_diff
        last = _streak_last_diff
    _receipt_never_converged(builds=builds, common=common, last=last)


def _receipt_never_converged(
    *, builds: int, common: frozenset[str] | None, last: tuple[str, ...] | None
) -> None:
    """The A2 receipt: this process's cache has never agreed with itself.

    Rides the same channel and the same ``snapshot_core_cache`` family as the
    shadow lane's divergence receipt, and asks for the same response: widen the
    input closure over the paths named here.

    ``diff=`` goes LAST on purpose — it is a variable-length list and a path may
    contain spaces, so anything after it could not be field-parsed.
    """

    if last is None:
        detail = (
            f"diff_scope={DIFF_SCOPE_NONE} changed=0 "
            f"diff_reason={DIFF_UNAVAILABLE_NO_ENTRIES} diff={DIFF_UNAVAILABLE}"
        )
    elif common:
        detail = _diff_detail(DIFF_SCOPE_EVERY_PASS, sorted(common))
    elif last:
        detail = _diff_detail(DIFF_SCOPE_LAST_PAIR, list(last))
    else:
        detail = (
            f"diff_scope={DIFF_SCOPE_NONE} changed=0 "
            f"diff_reason={DIFF_UNAVAILABLE_NO_ENTRY_DELTA} diff={DIFF_UNAVAILABLE}"
        )
    logger.warning(
        "snapshot_core_cache %s builds=%d %s — %d consecutive write-backs each "
        "wrote a key that disagreed with the one before it, so no process can "
        "ever be served this cache: it is costing a write per build and buying "
        "nothing. Widen the fingerprint's input closure over the paths named "
        "here (agent_runtime/core_cache.py), never trust the cache harder.",
        RECEIPT_NEVER_CONVERGED,
        builds,
        detail,
        builds,
    )


def _diff_detail(scope: str, paths: list[str]) -> str:
    return "diff_scope={} changed={} diff={}".format(
        scope, len(paths), ",".join(paths[:_NEVER_CONVERGED_DIFF_PATHS])
    )


def _runtime_root_for_sidecar(parity: dict) -> Any:
    identity = parity.get("runtime_root") if isinstance(parity.get("runtime_root"), dict) else {}
    resolved = identity.get("resolved") or identity.get("path")
    if resolved:
        return resolved
    try:
        from . import paths as _paths

        return _paths.store_root()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
class CacheRead(NamedTuple):
    """What the persisted pair said, and why it was or was not usable.

    ``core`` is present whenever a decodable core was on disk — INCLUDING the
    mismatch cases, because a mismatch still has something honest to serve while
    the rebuild runs, provided it wears the stale label. ``matched`` is the only
    field that authorizes serving it as authoritative.
    """

    core: dict | None
    matched: bool
    reason: str
    fingerprint: CoreFingerprint | None
    sidecar: dict


def read_persisted_core(*, fingerprint: CoreFingerprint | None = None) -> CacheRead:
    """Load the persisted pair and judge it. Never raises.

    The judgement is a conjunction and each clause has its own demote reason on
    the receipt, because "the cache did not answer" is useless to an operator
    without WHY: bytes that do not match the sidecar, a fingerprint that moved,
    an install that changed, a contract that moved, a root that is not this one.

    The clauses are ordered CHEAPEST-FIRST, deliberately. The stat set is a walk
    of every build input; a process with no persisted core to judge — every cold
    CLI invocation, every test with a fresh root — must not pay for one to be
    told there is nothing to compare it against.

    **This primitive is never memoised.** Every call reads the pair and, unless
    handed a key, walks the store. The boot lane's shared answer lives behind
    :func:`_armed_window_read`, reached only through :func:`consult` and
    :func:`take_stale_first_core`; a caller that asks this function directly is
    asking for a fresh judgement and gets one.
    """

    pair = _read_pair()
    if pair is None:
        return CacheRead(None, False, DEMOTE_ABSENT, None, {})
    return _judge_persisted_pair(pair[0], pair[1], fingerprint=fingerprint)


def _read_pair() -> tuple[str, str] | None:
    """The persisted BYTES, or ``None`` when there is no pair to judge.

    Its own function so the boot lane can count and memoise the READ separately
    from the judgement — and so a witness can prove one read happened rather than
    inferring it from a duration.
    """

    try:
        return (
            sidecar_path().read_text(encoding="utf-8"),
            core_path().read_text(encoding="utf-8"),
        )
    except OSError:
        return None


def _judge_persisted_pair(
    raw_sidecar: str, raw_core: str, *, fingerprint: CoreFingerprint | None
) -> CacheRead:
    """The conjunction above, over bytes that have already been read."""

    try:
        sidecar = json.loads(raw_sidecar)
        core = json.loads(raw_core)
    except Exception:
        return CacheRead(None, False, DEMOTE_UNREADABLE, None, {})
    if not isinstance(sidecar, dict) or not isinstance(core, dict):
        return CacheRead(None, False, DEMOTE_UNREADABLE, None, {})
    if _core_digest(core) != sidecar.get("core_sha256"):
        # The sidecar does not describe these bytes. Refuse the core outright
        # rather than serving it stale: an unbound core is not a projection this
        # module produced, so nothing here can say what it contains.
        return CacheRead(None, False, DEMOTE_CORE_DIGEST_MISMATCH, None, sidecar)
    key = fingerprint if fingerprint is not None else build_input_fingerprint()
    if key is None:
        return CacheRead(core, False, DEMOTE_FINGERPRINT_UNAVAILABLE, key, sidecar)
    stamp = build_stamp_token()
    if stamp is None:
        return CacheRead(core, False, DEMOTE_BUILD_STAMP_UNKNOWN, key, sidecar)
    if sidecar.get("build_stamp") != stamp:
        return CacheRead(core, False, DEMOTE_BUILD_STAMP_MISMATCH, key, sidecar)
    if sidecar.get("contract_versions") != contract_versions():
        return CacheRead(core, False, DEMOTE_CONTRACT_MISMATCH, key, sidecar)
    try:
        from . import paths as _paths

        current_root = str(_paths.store_root())
    except Exception:
        current_root = None
    recorded_root = sidecar.get("runtime_root")
    if current_root is not None and recorded_root and str(recorded_root) != current_root:
        return CacheRead(core, False, DEMOTE_RUNTIME_ROOT_MISMATCH, key, sidecar)
    if sidecar.get("fingerprint") != key.digest:
        return CacheRead(core, False, DEMOTE_FINGERPRINT_MISMATCH, key, sidecar)
    return CacheRead(core, True, "", key, sidecar)


# --------------------------------------------------------------------------- #
# Labelling
# --------------------------------------------------------------------------- #
def label_core(core: dict, *, source: str, stale: bool) -> dict:
    """Stamp provenance onto the core's parity envelope, in place.

    ``parity`` is the frame's self-describing provenance block, and
    ``read_model._resolved`` already stamps ``frame_source`` there for exactly
    this reason — one location, additive, no contract bump.

    ``core_source`` is emitted ONLY when a persisted core was available to
    decide between, which is why the committed fixtures do not move: a build in
    a root that has never held a persisted core answers no such question, and
    stamping ``rebuilt`` there would be answering a question nobody asked, on
    every golden. Same rule as the ``delta_patches`` hydrate marker, which is
    absent when the lane is off precisely so the flag-off golden stays
    byte-identical.

    A stale core additionally sets ``parity.freshness.state = "stale"``. That is
    the field the launcher's ``MissionSnapshotEnvelope`` already parses and maps
    to ``MissionSnapshotHealth.stale``, so a stale-labeled frame can never read
    ``live`` — it is non-authoritative by the consumer's existing predicate, not
    by a new one.
    """

    parity = core.get("parity")
    if not isinstance(parity, dict):
        parity = {}
        core["parity"] = parity
    parity["core_source"] = str(source)
    if stale:
        parity["core_stale"] = True
        freshness = parity.get("freshness")
        if not isinstance(freshness, dict):
            freshness = {}
            parity["freshness"] = freshness
        freshness["state"] = "stale"
    else:
        parity.pop("core_stale", None)
        freshness = parity.get("freshness")
        if isinstance(freshness, dict) and source == CORE_SOURCE_CACHE:
            # The fingerprint matched, so this projection is confirmed CURRENT
            # as of now — that, and not the original build's clock, is when its
            # freshness window starts. The build's own time stays on the core's
            # top-level ``generated_at``; nothing is overwritten, one anchor is
            # refreshed. Without this a cache hit would serve a core whose
            # 30-second freshness window expired before it was loaded.
            from hermes_time import now as _now

            from .serde import to_jsonable

            freshness["generated_at"] = to_jsonable(_now())
    return core


# --------------------------------------------------------------------------- #
# The process-level lane
# --------------------------------------------------------------------------- #
#: The cache lane is ARMED until this process has completed a full default-store
#: build of its own. That is the honest generalization of the plan's "first build
#: of a process": a serve boot issues several builds (prewarm, hydrate, status
#: polls) within seconds of each other, and gating on the literal first
#: CONSULTATION would have served the prewarm from the cache and then made the
#: hydrate — the one the launcher is actually waiting on — pay the full build
#: anyway, buying nothing the operator can see.
#:
#: An armed lane is never a stale-serve window: it is "this process has not yet
#: built its own truth, and the store says the persisted one is still current".
#: The fingerprint behind that answer is computed ONCE per armed window rather
#: than once per asker — see :data:`_consult_memo` for the window, its
#: invalidation, and what the sharing does and does not widen.
#:
#: Disarming on the first completed build also means no test can accidentally be
#: served from a cache: a fresh isolated root has no persisted core, so the
#: first consult always demotes, and the build that follows it disarms the lane.
_LANE = threading.local()
_lane_lock = threading.Lock()
_lane_armed = True
_shadow_done = False
_stale_served = False


# --------------------------------------------------------------------------- #
# One consult per boot, not one per rider (MC-1 / P5)
# --------------------------------------------------------------------------- #
#: A serve boot asks this lane the SAME question four times within about a
#: second: the stream's stale-first read, then the prewarm, hub and cli riders'
#: consults, and then the build leader's pre-build key — five full store walks
#: (measured ~300–355 ms each warm on the operator's drive), four core reads and
#: four digests, all describing one moment. The count is identical on the HIT
#: path, where the answer is by definition the same for every asker.
#:
#: They are one question, so they get one answer. The memo holds the
#: ``CacheRead`` the first asker computed, keyed on the STAT TRIPLES of the
#: persisted pair itself, and is dropped when the lane disarms — that is, the
#: moment this process owns its own truth.
#:
#: WHAT THIS DOES NOT CHANGE. A hit still means the fingerprint matched the
#: sidecar; a demote still carries its own reason and its own receipt per caller.
#: Only the number of times the identical computation runs moves.
#:
#: WHAT IT DOES WIDEN, said plainly. The validity of one asker's answer now
#: extends to the other askers in the same window instead of each re-deciding.
#: If a write lands mid-window, a later rider is served the answer computed
#: before it, where today it would have walked again and demoted. Three things
#: bound that, and they are the reason this is sound rather than merely cheap:
#:
#: 1. the window is a BOOT — it ends at the first completed full build of the
#:    process, which is the same instant the lane closes;
#: 2. the askers were already disagreeing, which is worse. Today rider 1 can be
#:    served the cache while rider 2 walks, misses and pays a full build, on one
#:    store, in one process, seconds apart — the divergence the 2026-08-18
#:    investigation recorded as A1-c. One answer per window retires it;
#: 3. the shadow-validation window is UNTOUCHED. A cache-hit boot still runs the
#:    full build in the background and compares field-for-field, so a write this
#:    memo absorbed surfaces as a divergence receipt and the rebuilt core is
#:    adopted. That mitigation is load-bearing here, not decorative.
#:
#: The stat pair is re-taken on every ask (two stats), so a write-back — this
#: lane's own or another process's — invalidates the memo immediately: the pair
#: is written through ``atomic_json_write``, and a rename always moves mtime.
#:
#: The lock is held ACROSS the computation, deliberately. Riders arrive within
#: milliseconds of each other; a lock released before the walk would let all of
#: them start their own and the memo would record the last one to finish, buying
#: nothing. Blocking is the mechanism, not a side effect. Nothing inside the
#: computed region takes :data:`_lane_lock`, and no holder of the lane lock takes
#: this one — the two never nest.
class _ConsultMemo(NamedTuple):
    stamp: tuple[FingerprintEntry, FingerprintEntry]
    raw_core: str
    read: CacheRead


_memo_lock = threading.Lock()
_consult_memo: _ConsultMemo | None = None


def _pair_stamp() -> tuple[FingerprintEntry, FingerprintEntry]:
    """Two stats over the persisted pair — the memo's whole invalidation rule."""

    return (_stat_entry(sidecar_path()), _stat_entry(core_path()))


def _drop_consult_memo() -> None:
    global _consult_memo
    with _memo_lock:
        _consult_memo = None


def _armed_window_read() -> CacheRead:
    """The boot lane's shared judgement: computed once, answered many times.

    Every caller gets its OWN decoded core, re-parsed from the memoised bytes.
    The build coalescer already deep-copies its result for exactly this reason:
    :func:`label_core` stamps provenance IN PLACE, so one shared dict would let
    the third rider's label land on the first rider's already-emitted frame.
    """

    global _consult_memo
    stamp = _pair_stamp()
    with _memo_lock:
        memo = _consult_memo
        if memo is not None and memo.stamp == stamp:
            return memo.read._replace(core=json.loads(memo.raw_core))
        pair = _read_pair()
        if pair is None:
            _consult_memo = None
            return CacheRead(None, False, DEMOTE_ABSENT, None, {})
        read = _judge_persisted_pair(pair[0], pair[1], fingerprint=None)
        # A judgement that produced no core has nothing to re-parse and nothing
        # worth holding: the pair is unreadable or unbound, and the next asker
        # should see that for itself rather than inherit a refusal.
        _consult_memo = (
            _ConsultMemo(stamp, pair[1], read) if read.core is not None else None
        )
        return read


def pre_build_fingerprint() -> CoreFingerprint | None:
    """The key a build persists — the consult's, when the consult still stands.

    The leader used to take a SECOND full walk here, milliseconds after its own
    consult had taken one over the same store. Reusing the consult's key keeps
    the direction ``write_back`` requires: a key stat'd BEFORE the build is at
    worst OLDER than the core it describes, which can only cost the next process
    a rebuild it did not strictly need. The unsafe direction — a key stat'd after
    the build, absorbing a write the core does not contain — is not reachable
    from here, because the memo is filled before the build starts and dropped
    when it completes.

    Falls through to a full walk whenever there is no standing consult: a cold
    store (nothing to consult), a disarmed lane (every later build in the
    process), or a pair that moved since.
    """

    with _memo_lock:
        memo = _consult_memo
    if memo is not None and memo.read.fingerprint is not None and memo.stamp == _pair_stamp():
        return memo.read.fingerprint
    return build_input_fingerprint()


def reset_process_state() -> None:
    """Re-arm the lane, as a fresh process would. Tests only.

    Same shape and same reason as ``build_stamp.reset_build_stamp_cache``: a
    property of the PROCESS has to be resettable for a test to be able to
    exercise a second process's behaviour without spawning one.

    The convergence history (ML-10) is process state by the same definition and
    is reset here too — a case that left a streak behind would hand the next case
    a process that had already half-declared non-convergence. So is the boot
    lane's shared consult: a memo surviving into the next case would answer it
    with the previous case's store.
    """

    global _lane_armed, _shadow_done, _stale_served
    with _lane_lock:
        _lane_armed = True
        _shadow_done = False
        _stale_served = False
    _reset_convergence_state()
    _drop_consult_memo()


def lane_armed() -> bool:
    with _lane_lock:
        return _lane_armed


def note_full_build_completed() -> None:
    """The process now owns its own truth — the cache lane closes.

    A no-op inside a shadow build: that build is a VALIDATION of the cache, not
    the process's answer, and letting it disarm the lane would make the next
    boot caller pay a full build for the privilege of having validated the one
    it just avoided.

    The armed window's shared consult ends here with the lane. Dropped OUTSIDE
    the lane lock on purpose: the memo lock is taken while judging, and judging
    never takes the lane lock, so the two locks must never nest in the other
    order either.
    """

    if getattr(_LANE, "shadow", False):
        return
    global _lane_armed
    with _lane_lock:
        _lane_armed = False
    _drop_consult_memo()


class shadow_build_scope:
    """Marks the calling thread's build as the shadow validation build."""

    def __enter__(self) -> None:
        _LANE.shadow = True

    def __exit__(self, *exc: Any) -> None:
        _LANE.shadow = False


def _log_demote(*, caller: str, reason: str, key: CoreFingerprint | None) -> None:
    logger.info(
        "snapshot_core_cache core_source=%s caller=%s reason=%s inputs=%s",
        CORE_SOURCE_REBUILT,
        caller,
        reason,
        "unknown" if key is None else key.count,
    )


class CoreDecision(NamedTuple):
    """What the cache lane decided, and whether there was a question at all.

    ``demoted`` is the distinction that keeps the committed producer fixtures
    byte-identical. A build in a root that has never held a persisted core
    answers no question about provenance, so nothing is stamped; a build that
    ran BECAUSE a persisted core was rejected answers one, and stamps
    ``core_source=rebuilt``. See :func:`label_core`.
    """

    core: dict | None
    demoted: bool
    reason: str


def consult(*, caller: str, fingerprint: CoreFingerprint | None = None) -> CoreDecision:
    """The read half of the stage: serve the persisted core, or say why not.

    Runs on the default-store path only, while the lane is armed. On a match it
    emits its OWN receipt (``snapshot_core_cache core_source=cache``) and
    deliberately does NOT emit ``snapshot_build_core role=led``: there was no
    build, and a receipt claiming one would put the log back in the state EG-2.1
    just took it out of, where a wait and a build are indistinguishable.

    Every demote is logged with its reason — except ``absent``, which is the
    ordinary cold-start shape and would otherwise print a line on every build in
    every process that has no cache to consult.

    The riders of one boot share ONE judgement (:data:`_consult_memo`) and each
    still emits its OWN receipt: the log stays a per-caller account of what each
    asker was told, while the store is walked once. A caller that hands in its
    own ``fingerprint`` is answered from that key alone and never touches the
    shared window.
    """

    if not lane_armed():
        return CoreDecision(None, False, "")
    read = (
        _armed_window_read()
        if fingerprint is None
        else read_persisted_core(fingerprint=fingerprint)
    )
    if not read.matched or read.core is None:
        if read.reason != DEMOTE_ABSENT:
            _log_demote(caller=caller, reason=read.reason, key=read.fingerprint)
        return CoreDecision(None, read.reason != DEMOTE_ABSENT, read.reason)
    core = label_core(read.core, source=CORE_SOURCE_CACHE, stale=False)
    logger.info(
        "snapshot_core_cache core_source=%s caller=%s inputs=%d fingerprint=%s offset=%s",
        CORE_SOURCE_CACHE,
        caller,
        read.fingerprint.count if read.fingerprint else -1,
        read.fingerprint.digest[:12] if read.fingerprint else "unknown",
        read.sidecar.get("event_offset", "unknown"),
    )
    return CoreDecision(core, False, "")


def take_stale_first_core(*, caller: str) -> dict | None:
    """A persisted core to paint IMMEDIATELY, LABELED stale — or ``None``.

    The mismatch half of the design: rather than showing the operator nothing
    for the length of a full build, serve what the store last projected and say
    out loud that it is not validated. The replacement arrives on the next frame
    when the build completes.

    One-shot per process, and only while the lane is armed, so a resubscribe
    long after the process has built its own truth can never re-paint an old
    projection.
    """

    global _stale_served
    with _lane_lock:
        if not _lane_armed or _stale_served:
            return None
    # The same shared judgement the riders will get. This read is FIRST in the
    # boot, so on the ordinary shape it is the one that pays for the walk and
    # every consult behind it is answered for free.
    read = _armed_window_read()
    if read.core is None or read.matched:
        # Nothing to paint, or the core MATCHES — in which case the ordinary
        # cache-hit path above will serve it authoritative and painting a stale
        # copy first would be a lie in the pessimistic direction.
        return None
    with _lane_lock:
        if _stale_served:
            return None
        _stale_served = True
    logger.info(
        "snapshot_core_cache core_source=%s stale=true caller=%s reason=%s",
        CORE_SOURCE_CACHE,
        caller,
        read.reason,
    )
    return label_core(read.core, source=CORE_SOURCE_CACHE, stale=True)


# --------------------------------------------------------------------------- #
# Shadow validation
# --------------------------------------------------------------------------- #
#: The comparison ignores exactly the fields that describe THIS build rather
#: than the state it projected. Everything else — every section, every row,
#: every count — must agree, because a difference in any of them is the
#: input-closure gap §6.1 is about.
_SHADOW_IGNORED_PARITY_KEYS = frozenset(
    {
        "build_ms",
        "sections_ms",
        "generated_at",
        "projection_age_ms",
        "core_source",
        "core_stale",
        "freshness",
        "snapshot_bytes",
    }
)
_SHADOW_IGNORED_TOP_KEYS = frozenset({"generated_at", "parity", "runtime_paths_diagnostic"})

#: ``parity.watermark`` is compared, minus the clock that says WHEN it was
#: measured. The reason to compare it at all is ``event_offset``: two cores at
#: different log positions ARE a divergence, and one of the most informative
#: kinds. ``captured_at`` is the measurement's own timestamp — it moves on every
#: read by construction, so leaving it in would make every comparison diverge and
#: the whole window would report noise until somebody switched it off.
_SHADOW_IGNORED_WATERMARK_KEYS = frozenset({"captured_at"})


def compare_cores(cached: dict, rebuilt: dict) -> str | None:
    """The first section on which a cache-served core and a rebuild disagree.

    ``None`` means they agree. The NAME is the whole point of the return value:
    a boolean divergence receipt would tell an operator the cache is wrong
    without telling them which input class to widen.
    """

    from .serde import to_jsonable

    left = to_jsonable(cached)
    right = to_jsonable(rebuilt)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return "core"
    keys = sorted(set(left) | set(right))
    for key in keys:
        if key in _SHADOW_IGNORED_TOP_KEYS:
            continue
        if left.get(key) != right.get(key):
            return key
    left_parity = left.get("parity") if isinstance(left.get("parity"), dict) else {}
    right_parity = right.get("parity") if isinstance(right.get("parity"), dict) else {}
    for key in sorted(set(left_parity) | set(right_parity)):
        if key in _SHADOW_IGNORED_PARITY_KEYS:
            continue
        if key == "watermark":
            if _stripped(left_parity.get(key), _SHADOW_IGNORED_WATERMARK_KEYS) != _stripped(
                right_parity.get(key), _SHADOW_IGNORED_WATERMARK_KEYS
            ):
                return "parity.watermark"
            continue
        if left_parity.get(key) != right_parity.get(key):
            return f"parity.{key}"
    return None


def _stripped(value: Any, drop: frozenset[str]) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key not in drop}


def shadow_validate(cached: dict, *, caller: str, build: Callable[[], dict], adopt: Callable[[dict], None] | None = None) -> str | None:
    """Rebuild in full, compare against the core we just served, report.

    The UP-4 pattern applied to this cache, and the mitigation that converts
    §6.1's input-closure risk into receipts. A divergence is LOUD (a warning
    naming the section) and the rebuilt core is ADOPTED — written back, so the
    next boot cannot be served the divergent copy, and handed to ``adopt`` so
    the lane that already painted can replace what it painted.

    Retirement is receipts-based and is NOT this stage's call: zero divergence
    receipts across the agreed window (TC-3's shape).
    """

    try:
        with shadow_build_scope():
            rebuilt = build()
    except Exception:
        logger.warning("snapshot_core_shadow ok=false caller=%s reason=build", caller, exc_info=True)
        return None
    section = compare_cores(cached, rebuilt)
    if section is None:
        logger.info("snapshot_core_shadow ok=true caller=%s divergence=none", caller)
        return None
    # ADOPTION, in the order that matters: the divergent copy stops being
    # servable to the NEXT process first (the write-back), then this process
    # stops serving it (the lane closes), then whoever already painted is told
    # to replace what it painted. A receipt without adoption would leave the
    # operator reading about a canvas that is still wrong.
    write_back(rebuilt)
    logger.warning(
        "snapshot_core_shadow_divergence caller=%s section=%s — the persisted core "
        "disagreed with a full rebuild; the rebuilt core is adopted. Widen the "
        "fingerprint's input closure (agent_runtime/core_cache.py), never trust "
        "the cache harder.",
        caller,
        section,
    )
    note_full_build_completed()
    if adopt is not None:
        try:
            adopt(rebuilt)
        except Exception:  # pragma: no cover - an instrument must not fail a lane
            logger.warning("snapshot_core_shadow adopt failed", exc_info=True)
    return section


def claim_shadow_slot() -> bool:
    """Take the process's ONE shadow-validation slot, or report it taken.

    Once, not per cache hit: a boot issues several builds and spawning a full
    build behind each of them would cost the process more than the cache saved —
    four boot hits would buy four rebuilds.

    Separated from the thread start so the claim is testable as a claim. A gate
    whose only witness has to observe a background thread is a gate tested
    through a race.
    """

    global _shadow_done
    with _lane_lock:
        if _shadow_done:
            return False
        _shadow_done = True
    return True


def maybe_start_shadow_validation(cached: dict, *, caller: str, build: Callable[[], dict], adopt: Callable[[dict], None] | None = None) -> bool:
    """Start the shadow build on a daemon thread, if this process's slot is free."""

    if not claim_shadow_slot():
        return False
    thread = threading.Thread(
        target=lambda: shadow_validate(cached, caller=caller, build=build, adopt=adopt),
        name="harness-core-shadow",
        daemon=True,
    )
    thread.start()
    return True


def iter_fingerprint_paths(fingerprint: CoreFingerprint) -> Iterator[str]:
    """Every path in a fingerprint — the §6.1 audit surface, enumerable."""

    for entry in fingerprint.entries:
        yield entry.path
