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
   No second list free to drift. Those authorities are asked under a home this
   process resolved ONCE (:func:`resolved_fingerprint_home`) rather than under
   the ambient ``HERMES_HOME`` the build itself exports per persona — see that
   constant for why "one resolution" and not merely "the head home" is what
   makes the closure a function of the store.
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
A WRITE-BACK IS ONE UNIT (MCF-21)
=============================================================================

The cache is three files — the core, the sidecar that binds to its bytes, and
the stat set that makes a later miss diffable. They landed through three
independent ``os.replace`` calls: each atomic alone, the TRIO not. The property
"these three describe one build" was held up by two ad-hoc binding guards
(``core_sha256`` between core and sidecar, ``entries.fingerprint`` between
entries and sidecar) rather than by one rule, and a fourth file would have made
a third guard.

So the unit is now the GENERATION. Every write-back mints ``gen-<stamp>/`` under
:data:`CORE_CACHE_DIRNAME`, writes all three files into it while nothing points
at it, and lands by replacing ONE small pointer file naming it. Atomicity rides
that single replace. A crash or a disk failure at any earlier point leaves a
directory the pointer never named — invisible to every reader, reaped by the
next successful write-back.

Two consequences are worth stating where they can be read rather than derived:

* **The recorded target shape was not implementable and this is not it.** MC-3
  said "``os.replace`` the directory"; ``os.replace`` cannot replace a non-empty
  directory anywhere, and on Windows cannot replace a directory at all. The full
  argument, including why rename-away-then-rename-in is REFUSED, is at
  :func:`_live_generation_dir`.
* **The guards were re-aimed, not deleted.** A swap makes a TORN trio
  impossible. It does nothing about a tampered or hand-restored file inside a
  generation that is already published, which is what ``core_sha256`` convicts
  and what ``entries_unbound`` now convicts. Both stay, documented to their new
  reason. What DID retire is the partial-landing arm: a published pair with no
  entries file, and its ``entries=false reason=entries_io`` receipt, are
  unrepresentable and are gone from the table below.

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
| ``snapshot_core_cache generation_residue`` (WARNING) | none | ``present=`` ``bound=`` ``live=`` ``leftover=`` then ``generations=`` LAST (a variable-length list, so nothing after it can be field-parsed). **CENSUS RULE (MCF-54(ii)/MCF-59): this line is NOT a failed write-back.** It rides a write-back that already logged ``ok=true``, and reports that the best-effort reap left superseded generation directories on disk - a reader holding one open, or a permission the writer lacks. ``present=`` counts the live generation too; ``leftover=`` does not, and the names in ``generations=`` are leftovers only, oldest first, capped at eight with the true total always in ``leftover=``. The same name across consecutive builds is a permanently held handle; a different name each time is transient contention, and only the first is worth acting on |
| ``snapshot_core_cache core_source=cache`` (INFO) | the snapshot payload | ``parity.core_source == "cache"`` — SAME spelling, no split. The line also carries ``caller=`` ``inputs=`` ``fingerprint=`` ``offset=``, none of which reach the payload |
| ``snapshot_core_cache core_source=cache stale=true`` (INFO) | the snapshot payload | ``parity.core_stale == true`` AND ``parity.freshness.state == "stale"`` — the field the launcher's ``MissionSnapshotEnvelope`` already maps to ``MissionSnapshotHealth.stale``. RESIDUAL SPLIT, named rather than fixed: the log says ``stale=true``, the payload says ``parity.core_stale``/``parity.freshness.state``, and the payload spelling is a consumer contract that predates this lane |
| ``snapshot_core_cache core_source=rebuilt`` (INFO, ``_log_demote``) | the snapshot payload, PARTIALLY | ``parity.core_source == "rebuilt"`` carries THAT the cache was demoted; the ``reason=`` never leaves this logger, and ``CoreDecision.reason`` is read by no caller today. So a field census of WHY a cache demoted has exactly one source: this line. Reasons ``unreadable`` ``core_digest_mismatch`` ``fingerprint_unavailable`` ``fingerprint_mismatch`` ``build_stamp_unknown`` ``build_stamp_mismatch`` ``contract_mismatch`` ``runtime_root_mismatch`` ``home_mismatch``. **CENSUS RULE (MC-2): ``home_mismatch`` is not an ordinary miss.** The other reasons say the STORE moved, the install changed, or the pair is unbound — all facts about the thing being cached. This one says the persisted pair was keyed under a different Hermes home than the reading process resolved, i.e. the two runs asked different QUESTIONS, and it is emitted INSTEAD of ``fingerprint_mismatch`` so the distinction is countable rather than inferred. On a multi-home install (an operator who really does run two roots) it is ordinary. On a SINGLE-PROFILE operator boot it is evidence that a persona scope was live while a build stat'd — the capture in ``core_cache.resolved_fingerprint_home`` was taken too late — which is a defect to go fix, not noise to tune out. A pair carrying no ``sidecar.fingerprint_home`` at all (every one written before MC-2) is skipped rather than demoted, so this reason can never fire for an install that simply predates the field. ``absent`` is deliberately NOT logged (the ordinary cold start would print a line on every build in every process), so its only trace is the ABSENCE of a line and a census must not read "no demote line" as "no demote". **CENSUS RULE (MC-3): ``fingerprint_mismatch`` ALONE grows a tail**, and the tail is ``changed=`` then ``diff=`` LAST (paths may contain spaces, so nothing can be field-parsed after it; the tail is additive, so an existing ``reason=`` grep is unaffected). No other reason carries one, deliberately: a diff on a ``build_stamp_mismatch`` would name every file the operator's upgrade touched and read as store churn. **The scope is ``last_pair`` BY CONSTRUCTION and that caveat is the row's most important sentence:** a demote diff is the delta since the LAST WRITE-BACK, so on a busy store it legitimately names files that are simply moving, and the receipt is TRUE without naming a defect. It is self-perturbation evidence — the A1-b/A2 class worth acting on — ONLY when the named paths are ones the runtime itself writes (``dispatch_delivery_drain.json``, ``serve_socket.owner.json``, ``state.db-wal``, ``serve_socket.lock``); when they are store paths the operator's own writes touched, the miss is legitimate and the cache is working as designed. An arm that could not compute the diff says so in its own words rather than emitting an empty list, which would read as "we looked and nothing moved": ``diff_scope=none changed=0 diff_reason=`` ``no_entries`` (nothing persisted yet, or an install predating MC-3) / ``entries_unbound`` (the entries file in the live generation is not the one that write-back put there — MCF-21 made a torn trio unrepresentable, so this now reads as tampering or corruption rather than as a failed diagnostic write) / ``digest_without_entry_delta`` (the digests disagreed and no triple did), then ``diff=diff_unavailable`` |
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
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from utils import atomic_json_write

from .dispatch_delivery import DRAIN_STATE_FILENAME
from .paths import DELETED_ARCHIVE_DIRNAME, OFFICE_ARCHIVE_DIRNAME
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
#: The stat set the sidecar's digest SUMMARISES, kept beside it so a divergence
#: is diffable at all. Its own file rather than a field on the sidecar: the
#: sidecar is read on the boot path by every consult, and folding megabytes of
#: triples into it would make the cheap half of the judgement pay for the
#: diagnostic half. See :func:`entries_path` for the measured size.
ENTRIES_FILENAME = "entries.json"

#: The ONE file whose replacement publishes a write-back (MCF-21). It names the
#: generation directory holding the trio above; nothing else decides which trio
#: is live. See :func:`_live_generation_dir` for why a pointer and not a
#: directory rename.
POINTER_FILENAME = "live.json"

#: Every generation directory is named with this prefix, and :func:`_is_generation_name`
#: is the only reader of that fact. The prefix is what lets the reaper tell a
#: directory this module owns from anything else that ever lands beside it, and
#: it is what CONTAINS a pointer: a name that does not match is refused rather
#: than joined onto :func:`_cache_dir`, so a corrupt or hostile pointer cannot
#: resolve the live trio outside the cache's own directory.
_GENERATION_PREFIX = "gen-"

#: What :func:`core_path` / :func:`sidecar_path` / :func:`entries_path` resolve
#: to when NO generation is published — a cold store, or the first consult after
#: MCF-21 landed on a store still holding the flat trio.
#:
#: A stable placeholder rather than ``None`` or a raise, because those helpers
#: have dozens of callers that legitimately ask "where would it be" before
#: anything is there (``unlink(missing_ok=True)``, ``_stat_entry``'s
#: absent-is-a-fact triple). Nothing ever WRITES here: a write-back always mints
#: a fresh generation, so a read through this path is an ``OSError`` and the
#: judgement demotes ``absent``, which is the honest answer.
_NO_GENERATION_DIRNAME = f"{_GENERATION_PREFIX}none"

#: The flat trio this module wrote before MCF-21. Read by NOTHING — see
#: :func:`_live_generation_dir` for why a pointerless store demotes rather than
#: adopting these — and reaped by the first successful write-back after landing.
_LEGACY_FLAT_FILENAMES = (CORE_FILENAME, SIDECAR_FILENAME, ENTRIES_FILENAME)

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
#: The persisted pair was keyed under a DIFFERENT home than this process
#: resolved, so its digest answers a different QUESTION — it is not evidence that
#: the store moved. Its own reason because the two demand opposite responses: an
#: ordinary ``fingerprint_mismatch`` says go look at the store, and this says go
#: look at who asked. See the channel table row for what it means to a census.
DEMOTE_HOME_MISMATCH = "home_mismatch"

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

#: MCF-54(ii), ruled by MCF-59. The generation reap is BEST EFFORT and stays
#: that way - a reader holding files open on Windows makes a removal fail, and a
#: landed write-back must never report failure because its housekeeping did not.
#: What was missing was never strictness, it was ACCOUNTING: a store that kept
#: failing to reap grew generations with nothing counting them, so the failure
#: mode had no observable at all. This receipt is that observable, and per the
#: operator refinement it NAMES the leftover directories rather than merely
#: counting them - a count says a problem exists, the names say WHICH one, and
#: whether it is the same directory every time (a permanently held handle) or a
#: different one each time (transient contention).
RECEIPT_GENERATION_RESIDUE = "generation_residue"

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
#: The persisted entries exist but belong to a DIFFERENT write-back than the
#: sidecar being judged — see :func:`entries_path` for the binding rule. Its own
#: reason rather than ``no_entries`` because the two ask for opposite responses:
#: ``no_entries`` says the diagnostic has not been written yet (an install that
#: predates this stage, or a cold pair) and resolves itself on the next
#: write-back, while this one says the THREE files in the cache directory
#: disagree about which generation they describe, which is the shape a failed
#: entries write (``reason=entries_io``) leaves behind. Diffing across that
#: boundary would name paths from a generation nobody asked about, so it refuses.
DIFF_UNAVAILABLE_ENTRIES_UNBOUND = "entries_unbound"

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
#: RESIDUAL, stated rather than discovered later. FIVE names below are still
#: literals because no writer module owns them as a constant: ``locks`` and
#: ``snapshot.json`` are spelled inline inside their own path helpers in
#: ``agent_runtime.paths``, and the ``read_model.db`` trio is a CONFIGURABLE
#: default (``runtime_config``'s ``read_model.db_filename``) — an install that
#: renames it re-opens exactly the hole this comment block is about, one config
#: key away. Both are the same class as the drain defect and neither is fixed
#: here; the gate below can only prove the names a WRITER produces, so it cannot
#: see them either.
#:
#: (That count read "Four" until MC-8 and was simply wrong — ``locks`` plus
#: ``snapshot.json`` plus a trio is five. Corrected in passing rather than left,
#: because a residual paragraph exists to be counted against the set and one that
#: miscounts invites the reader to conclude a name has already been dealt with.)
#:
#: MC-8's ``deleted_archive`` addition did NOT extend that residual: it was the
#: same class — a name spelled inline in ``agent_runtime.paths`` — and was
#: promoted to ``paths.DELETED_ARCHIVE_DIRNAME`` and imported, rather than
#: re-typed here. That is the precedent for the two that remain; they were left
#: deliberately (out of MC-8's ruled scope), not overlooked.
#:
#: H2's ``office_archive`` addition did not extend it either, and was RE-COUNTED
#: rather than assumed: same class again, promoted to
#: ``paths.OFFICE_ARCHIVE_DIRNAME`` and imported, so the literals below are still
#: ``locks`` + ``snapshot.json`` + the ``read_model.db`` trio — FIVE, unchanged.
#: The count is restated on every addition because this paragraph exists to be
#: counted against the set, and it has been wrong once already.
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
        # The per-task compaction graveyard. NOT justified by "the runtime
        # rewrites it" — see the first block below, which is the argument rather
        # than a note about it.
        DELETED_ARCHIVE_DIRNAME,
        # The orphaned-office-surface graveyard. The runtime DOES write this one,
        # which is why its argument is a different (and easier) one — see the
        # second block below.
        OFFICE_ARCHIVE_DIRNAME,
    }
)

#: WHY ``deleted_archive/`` IS EXCLUDED, WRITTEN WHERE THE EXCLUSION LIVES.
#:
#: Every other name above earns its place the same way: the RUNTIME rewrites it
#: on a boot or a timer, so keeping it would guarantee a mismatch. This one is
#: different in kind and therefore has to carry its own argument — it is excluded
#: because **the projection does not read it**, which is a claim about the reader
#: set, and a claim about a reader set rots the moment someone adds a reader.
#: Stating it here, at the constant, is the whole of the P12 ruling; the audit
#: that comes after this one is meant to find this paragraph and be able to
#: re-run it.
#:
#: **THE GREP THAT ESTABLISHES IT** (re-run it; do not trust this transcript)::
#:
#:     grep -rn "deleted_archive" agent_runtime/ hermes_cli/ | grep -v tests
#:
#: 2026-08-18, five hits, and every one is accounted for:
#:   * ``paths.DELETED_ARCHIVE_DIRNAME`` / ``paths.deleted_archive_dir`` — the
#:     name and its helper;
#:   * ``paths.events_archive_dir``'s docstring — distinguishing prose only;
#:   * ``event_rotation``'s module docstring — prose only;
#:   * ``migrations.py``'s ``archive_batches`` counter — counts batch dirs for a
#:     migration STATUS payload; a reader, and not the projection;
#:   * ``events.py::_archived_event_slices`` — a genuine READER of
#:     ``deleted_archive/*/manifest.json``. See the exception below.
#:
#: **THE CLAIM IS REPO-SCOPED** (C14: a dead-symbol/no-reader claim from a
#: narrower grep is a claim about that scope and nothing more). It covers
#: ``agent_runtime/`` and ``hermes_cli/`` — this repo's own runtime and CLI. It
#: says nothing about a consumer outside this repo, and does not need to: the
#: fingerprint is an input closure for a build that lives HERE.
#:
#: **THE EXCEPTION, NAMED RATHER THAN OMITTED** (MCF-12 — the correction that had
#: to be made before this landed, because P12 was first written as "zero
#: readers" and that is false). The chain is::
#:
#:     harness_doctor.py::_event_log_report
#:         -> events.py::event_log_health
#:             -> events.py::_archived_event_slices
#:                 -> reads deleted_archive/*/manifest.json
#:
#: So the harness doctor DOES read these manifests, and a comment claiming "no
#: readers" would be found false by the next audit, which would then reasonably
#: conclude this exclusion is wrong. **It is not wrong, and the reason is the
#: word "projection".** This walk is the input closure of the READ-MODEL BUILD
#: (``snapshot.py``) — it exists to answer "may a previously-built core be served
#: as authoritative". ``harness_doctor`` is a separate, operator-invoked
#: diagnostic that builds no core and consults no cache; a file it reads is not
#: thereby an input to the projection, any more than a log file is. Fingerprinting
#: a tree because SOME code in the repo reads it would grow the closure without
#: bound and is exactly the "denylist by hand" instinct this module already paid
#: for once.
#:
#: **NO CURRENT CODE WRITES IT** — stronger than the ruling required, so it is
#: recorded. ``deleted_archive_dir()`` has exactly ONE non-doc caller in the
#: repo, and it is the reader above; the two archivers that used to fill the tree
#: (``archive_task_events`` / ``compact_archived_task_events``) were retired at
#: S54 along with their private helpers, as the tombstone comment above
#: ``_archived_event_slices`` records. What sits under ``deleted_archive/`` on a
#: live root is therefore a graveyard of a retired feature: immutable, and not
#: merely unread but unwritten. An immutable tree contributes a constant to every
#: key it appears in, which is the clearest possible statement that its 18,804
#: stat calls buy nothing.
#:
#: **WHAT IT COSTS TODAY, MEASURED** (2026-08-18, the operator's live root):
#: 18,804 of the store's 23,107 fingerprint entries — 81 % — are under this one
#: directory. That is ~250 ms of every ~300 ms warm walk, paid 4-5 times per boot
#: (once per rider consult plus the leader's pre-build key), and since MC-3 it is
#: also ~81 % of every ``entries.json`` write-back (~3.4 MiB -> ~0.7 MiB). It is
#: additionally the only part of the closure that GROWS without bound against
#: ``MAX_FINGERPRINT_ENTRIES``, which would eventually turn a cost into a refusal.
#:
#: **WHAT WOULD MAKE THIS WRONG, AND THE OBLIGATION THAT FOLLOWS.** If a future
#: projection section reads compaction batches — a snapshot block that surfaces
#: archived-task history, say — then this tree becomes a build input and a stale
#: core could be served across a change to it. **Whoever adds that reader must
#: remove this exclusion in the SAME commit**, and take the walk cost knowingly.
#: The reverse obligation is lighter but real: a new NON-projection reader (a
#: second doctor section, a census) changes nothing here and should not be read
#: as re-admitting the tree.

#: WHY ``office_archive/`` IS EXCLUDED — A DIFFERENT, AND EASIER, ARGUMENT.
#:
#: ``deleted_archive/`` above needed a reader-set claim because nothing writes it
#: any more. This one is the opposite shape and is settled by the ordinary rule
#: the rest of the set runs on: **the runtime WRITES this tree, and the projection
#: does not read it.** ``paths.office_surface_archive_root()`` is
#: ``store_root()/office_archive`` — the destination
#: ``office_store.archive_orphaned_surface`` RENAMES a whole orphaned office
#: surface into, via ``office_store._free_surface_archive_dir``, driven by the
#: operator verb ``harness office archive-surface``
#: (``hermes_cli/harness_parts/office.py::_cmd_office_archive_surface``). It grows
#: without bound against :data:`MAX_FINGERPRINT_ENTRIES` — the destination helper
#: appends ``-2``, ``-3`` … suffixes so a re-archived orphan lands beside the
#: previous one rather than refusing — which is the same unbounded-growth
#: objection the graveyard block raises, on a tree that is still being written.
#:
#: **THE NEAR-NAME TRAP, FIRST, because getting it wrong inverts the argument.**
#: ``paths.office_archive_dir(workspace_id)`` is ``office/<ws>/archive/``: a
#: DIFFERENT tree, per-workspace, holding archived ACTOR placements, and READ by
#: ``OfficeStore`` — ``_read_actor_dir`` on the actor-listing seam
#: (``scan_actors(include_archived=True)``), and ``office_archived_actor_path`` on
#: the archived-actor lookups that ``upsert_actor`` / ``remove_actor`` /
#: ``restore_actor`` and the class-key fence depend on. That tree is a projection
#: INPUT and **stays in the walk**. Only the store root's own ``office_archive``
#: entry is excluded, which is exactly what ``exclude_top`` filters and what the
#: nesting note below pins.
#:
#: **THE GREP THAT ESTABLISHES IT** (re-run it; do not trust this transcript)::
#:
#:     grep -rn "office_archive" agent_runtime/ hermes_cli/ | grep -v tests
#:     grep -rn "office_surface_archive_root\|office_archived_surface_dir" \
#:         agent_runtime/ hermes_cli/ | grep -v tests
#:
#: 2026-08-18. Every hit, and which of the two trees it names:
#:
#: THIS tree (``store/office_archive/``) — one writer chain and no reader:
#:   * ``paths.OFFICE_ARCHIVE_DIRNAME`` / ``paths.office_surface_archive_root`` /
#:     ``paths.office_archived_surface_dir`` — the name and its two helpers;
#:   * ``office_store._free_surface_archive_dir`` — the ONLY code that touches the
#:     tree at all: it ``.exists()``-probes for a free slot and returns the
#:     destination. A probe belonging to the writer, not a reader of content;
#:   * ``office_store.archive_orphaned_surface`` — the WRITER
#:     (``paths.office_dir(wsid).rename(destination)``), plus its docstring naming
#:     the helper, plus its ``AlreadyExists("office_archive:<ws>")`` — an error
#:     TOKEN, not a path;
#:   * ``hermes_cli/harness.py``'s four ``office_archive_surface`` lines — an
#:     argparse local variable holding the ``archive-surface`` subparser; no path;
#:   * ``hermes_cli/harness_parts/office.py`` — ``_cmd_office_archive_surface``
#:     (the verb), its docstring, and the ``"office_archived"`` envelope kind, a
#:     wire event name;
#:   * this comment.
#: THE NEAR NAME (``office/<ws>/archive/``), all genuine readers/writers of the
#: per-workspace actor archive, none of them this tree:
#:   * ``paths.office_archive_dir`` / ``paths.office_archived_actor_path``;
#:   * ``office_store`` at the actor-listing scan, the two archived-actor
#:     existence+revision lookups, ``remove_actor``'s archived read-back,
#:     ``restore_actor``'s source path, and ``_archive_actor_locked``'s write.
#:
#: **NOTHING PROJECTS IT.** ``snapshot._offices_summary`` reads through
#: ``OfficeStore.list_workspaces()``, which enumerates ``office_root()``'s
#: children by the presence of ``office.json`` — and
#: ``office_surface_archive_root()`` is deliberately a SIBLING of ``office_root()``
#: rather than a child, precisely so an archived surface stops being projected
#: (its own docstring records that as the load-bearing reason). A tree the
#: projection is designed not to see is not an input to it. There is no restore
#: verb either: recovery is an operator moving the directory back by hand, which
#: is a store change the walk sees on the ``office/`` side when it happens.
#:
#: **THE CLAIM IS REPO-SCOPED** (C14), covering ``agent_runtime/`` and
#: ``hermes_cli/`` — the same scope, and the same reasoning, as the block above.
#:
#: **THE HALF THIS DOES NOT RETIRE, STATED SO NOBODY READS IT AS A BUG.** The
#: archive gesture MOVES the surface OUT of ``office/<ws>/``, which IS
#: fingerprinted, so the gesture itself still flips the key exactly once. That is
#: a real input change and it should flip the key. What leaves the closure here is
#: the graveyard's CONTINUING contribution: its size, its walk cost, its unbounded
#: growth, and any churn inside it after the move.
#:
#: **WHAT WOULD MAKE THIS WRONG, AND THE OBLIGATION THAT FOLLOWS.** If a future
#: projection section reads archived surfaces — an "archived offices" block, an
#: operator restore lane that projects what is recoverable — this tree becomes a
#: build input and a stale core could be served across a change to it. **Whoever
#: adds that reader must remove this exclusion in the SAME commit.**

#: Exclusions are keyed to TOP-LEVEL names only — ``_walk_tree``'s ``exclude_top``
#: filters the store root's own entries and nothing deeper — so a directory that
#: happens to be called ``deleted_archive`` or ``office_archive`` NESTED inside
#: another store subtree still contributes in full. That is the correct reading of
#: every argument above (they are all about the one graveyard each at the store
#: root, which is the only place ``paths.deleted_archive_dir()`` and
#: ``paths.office_surface_archive_root()`` can put theirs) and both are pinned by
#: test, so the comment cannot quietly grow into a claim about the names
#: everywhere.

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
#: * ``-wal`` is keyed on ``(path, 0, 0)`` in EVERY state that holds no frames —
#:   absent and present-at-zero-length alike — and on its full stat'd triple the
#:   instant a frame lands. A frameless WAL holds nothing for the projection to
#:   read, and neither its mtime nor its existence says anything about content:
#:   both are artefacts of when some process last opened or closed the database.
#:   The instant an uncheckpointed commit lands the file is non-empty and the
#:   full triple counts again, so the WAL-commit signal EG-3.1 requires is
#:   untouched for every state the signal can actually be in. See
#:   :func:`_wal_without_frames_is_content_free` for why absence and emptiness
#:   are ONE fact rather than two — the half that took a second field
#:   investigation to see, after the first mask deliberately kept them apart.
#:
#: WHAT THE MASK DOES NOT COVER, stated rather than discovered later. A
#: checkpoint that truncates the WAL to zero AFTER writing its frames into
#: ``state.db`` leaves a zero-length WAL whose mtime this ignores — and a clean
#: last-close, which checkpoints and then UNLINKS the file, leaves no WAL at all.
#: Both are invisible here and both are carried anyway: the checkpoint moved
#: ``state.db``'s own mtime and size, and the main file is the FIRST entry in
#: this tuple. The uncovered case would be a commit that is invisible in the main
#: file AND invisible in the WAL's size, which SQLite's own durability rules do
#: not admit.
_DB_SIBLINGS = ("", "-wal", "-journal")

#: The sibling that is masked while it holds no frames. Named rather than
#: spelled at the call site so the mask and the enumeration cannot drift, and
#: named for the SIBLING rather than for the state so that widening the mask to
#: another suffix takes a deliberate edit here — the main file's absence and
#: ``-journal``'s absence are real information and must stay keyed.
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


def _wal_without_frames_is_content_free(entry: FingerprintEntry) -> FingerprintEntry:
    """A WAL with no frames keys as ``(path, 0, 0)`` — ABSENT OR EMPTY, one triple.

    The one line where "the build reads this database" stops being spelled the
    same way as "somebody wrote to this database". See :data:`_DB_SIBLINGS` for
    the ground under the mask.

    WHY ABSENT AND EMPTY ARE ONE FACT
    =================================

    SQLite deletes the WAL when the last connection closes cleanly, and
    re-creates it at zero length on the next open. So a quiescent database
    alternates between "no ``-wal`` on disk" and "a zero-length ``-wal`` on
    disk" for reasons that are entirely about CONNECTION LIFETIME and never
    about content: both states say *no uncheckpointed frames*, which is the only
    thing this sibling is stat'd to tell us. Keying them apart records the
    lifecycle of a reader as if it were a write.

    This does NOT generalise, and the distinction it drops is real everywhere
    else. :func:`_stat_entry` records a missing input as ``-1/-1`` precisely so
    that a file which APPEARS is not indistinguishable from one that never
    moved — an appearing config, an appearing store row, an appearing skill
    package are all content events. The WAL is the one input whose appearance is
    definitionally content-free, because it appears EMPTY: the appearance and
    the emptiness are the same open() call. The moment it carries a frame its
    size is non-zero and it is keyed like anything else, so the general rule is
    suspended only over the exact state in which it says nothing.

    (A DIRECTORY at this path — ``_DIR_MARK``, not a state SQLite can produce —
    collapses here too. A database whose WAL path is a directory cannot be
    opened at all, and would fail loudly at the open long before a stale key
    could matter.)

    WHAT THE FIELD SHOWED, AND THE CONSEQUENCE THAT WILL BE FORGOTTEN
    ================================================================

    Measured on the operator's machine, 2026-08-18: two boots recorded the SAME
    events offset and the SAME entry count, and the second demoted
    ``fingerprint_mismatch`` anyway. ``state.db-wal``'s NTFS creation time was
    4.15 s AFTER the consult that missed — boot A's clean exit had deleted it,
    boot B had not yet opened the database — while the sidecar, written
    mid-session by a later build, held it present-and-empty. One entry flipped;
    nothing else in the closure moved.

    The structural consequence is worse than the flip: **which of the two states
    the sidecar records depends on which build in the process wrote LAST.** A
    boot whose only build is the boot build writes a consult-time key (WAL
    absent, because the database has not been opened yet) and the next boot can
    match. Any later led build — the launcher's hydrate, any ``forceFresh``
    gesture — writes a mid-session key (WAL present) and the next boot is a
    GUARANTEED miss. That is deterministic given the build history, not a race.

    Perverse corollary, stated because it will mislead the first person who
    tests this: **a hard-killed serve leaves the WAL behind and converges; a
    clean exit deletes it and misses.** Under this mask neither shape is keyed
    differently from the other, which is the point.
    """

    if entry.size > 0:
        return entry
    return FingerprintEntry(entry.path, 0, 0)


def sqlite_fingerprint_triples(db_path: Any) -> tuple[tuple[str, int, int], ...]:
    """``(suffix, mtime_ns, size)`` per journal sibling, under the WAL mask.

    THE one authority for "how does a poll lane key a SQLite database", promoted
    out of this module on 2026-08-21 because it had exactly one caller and three
    lanes needed it. The other two keyed the same database by a raw stat triple
    over the same three siblings, so the connection-lifetime flip
    :func:`_wal_without_frames_is_content_free` was written to absorb — WAL
    absent after a clean last-close, WAL present-and-empty the moment anything
    opens the file — reached them undiminished:

    * ``stream._scope_fingerprint`` (the Stage 12 watchdog, ~5 s cadence). Each
      flip appended a synthetic ``state.reconciled``, which ``patch_coverage``
      classifies UNCOVERED, which demotes the batch to a full core rebuild.
      Measured on the operator's runtime over 22.16 h to 2026-08-21 09:06:
      **2 433 ``snapshot_build reason=demote`` against 35 hydrates, median
      build_ms 3 083, max 37 266 — 2.29 h of CPU**, while the event log took
      1 239 ``state.reconciled`` (96.9 % of all events in the window) at a
      median 9.0 s spacing. 3 338 DISTINCT fingerprints over 4 597 reconciles,
      against a recurring at-rest anchor: the signature of one entry alternating
      between a stable "absent" string and a fresh ``mtime_ns`` on every open.
    * ``harness_parts.serve._runtime_state_fingerprint`` (the read-model cache),
      where the same flip keeps the cache permanently cold — the defect a
      2026-08-09 analysis named and nobody propagated the mask to.

    Returned keyed by SUFFIX rather than by path so a caller can spell its own
    label (the stream lane keys by basename, the cache lane by full path)
    without a second stat list free to drift from :data:`_DB_SIBLINGS`.
    """

    text = str(db_path)
    triples: list[tuple[str, int, int]] = []
    for suffix in _DB_SIBLINGS:
        entry = _stat_entry(text + suffix)
        if suffix == _WAL_SIBLING:
            entry = _wal_without_frames_is_content_free(entry)
        triples.append((suffix, entry.mtime_ns, entry.size))
    return tuple(triples)


def _db_entries(db_path: Any, out: list[FingerprintEntry]) -> None:
    text = str(db_path)
    for suffix, mtime_ns, size in sqlite_fingerprint_triples(db_path):
        out.append(FingerprintEntry(text + suffix, mtime_ns, size))


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


# --------------------------------------------------------------------------- #
# The home this process fingerprints through (MC-2 / P3)
# --------------------------------------------------------------------------- #
#: Resolved ONCE per process, then pinned for the length of every walk.
#:
#: WHAT IT FIXES. Four of the seven classes below bottom out in
#: ``hermes_constants.get_hermes_home()``, whose ladder is the context-local
#: override → ``os.environ["HERMES_HOME"]`` → the platform default. The BUILD
#: ITSELF exports that variable: the ``agents_readiness`` section runs
#: ``profile_readiness.profile_readiness_for_persona`` inside
#: ``profile_context.persona_profile_context``, which sets the context-local
#: override AND writes ``os.environ["HERMES_HOME"]`` process-globally for the
#: length of a per-persona scope. A consult on ANOTHER THREAD — a one-shot
#: hydrate, a status probe, the hub — therefore computed its closure over
#: whichever profile happened to be exported at that instant. Measured in the
#: field 2026-08-18: ``inputs=24344`` and ``inputs=23107`` from ONE process, over
#: ONE store, thirteen seconds apart. A non-filesystem input inside the closure,
#: which is the thing §6.1's bet says must not exist.
#:
#: WHY "RESOLVE THROUGH THE HEAD" IS NOT ON ITS OWN THE FIX. This is the whole
#: soundness argument, so it lives here rather than in a commit message.
#: :func:`hermes_constants.get_hermes_head_home` FALLS BACK to
#: ``get_hermes_home()`` whenever no head authority is present — and under an
#: active persona override that fallback IS the override. Worse, the authority it
#: consults first is a ContextVar, and ContextVars do not cross a thread
#: boundary: the head ``persona_profile_context`` records is invisible to the
#: very thread the divergence was measured on, so there the head degenerates to
#: the flipped ambient home. Resolving through the head is NECESSARY and NOT
#: SUFFICIENT. What makes the closure pure is taking that resolution ONCE — at
#: the first fingerprint of the process, which on every real lane is the boot
#: consult, before any persona scope in this process can have run — and pinning
#: it for every walk afterwards.
#:
#: THE RESIDUAL, named rather than discovered later. A process whose FIRST
#: fingerprint is taken while a persona scope is already live captures that
#: scope's home and pins it. That is not silent: ``write_back`` records
#: ``fingerprint_home`` in the sidecar and a later boot judging against it
#: demotes ``home_mismatch`` (see the channel table), which is exactly the field
#: signal that a capture was taken too late.
_fingerprint_home_lock = threading.Lock()
_fingerprint_home: tuple[Path, bool] | None = None


def resolved_fingerprint_home() -> tuple[Path, bool]:
    """``(home, authoritative)`` for this process — captured once, then frozen.

    ``authoritative`` is :func:`hermes_constants.hermes_head_home_is_authoritative`
    as it read AT CAPTURE TIME. ``False`` means the head had degenerated to the
    ambient resolution, so the recorded home is only as good as the moment it was
    taken. That is a fact a demote should be able to name, which is why it is
    persisted beside the home instead of dropped.
    """

    global _fingerprint_home
    with _fingerprint_home_lock:
        if _fingerprint_home is None:
            from hermes_constants import (
                get_hermes_head_home,
                hermes_head_home_is_authoritative,
            )

            _fingerprint_home = (
                Path(get_hermes_head_home()),
                bool(hermes_head_home_is_authoritative()),
            )
        return _fingerprint_home


def reset_fingerprint_home() -> None:
    """Forget the captured home, as a fresh process would. Tests only.

    Its own function rather than only a line inside
    :func:`reset_process_state` because the per-test environment sandbox moves
    ``HERMES_HOME`` between cases, and a capture frozen from case 1 would answer
    case 2 with a directory pytest has already deleted — a fingerprint that is
    stable for the wrong reason. The ``tests/agent_runtime`` conftest drops it
    autouse, the same way it drops the profile-runner resolve memo.
    """

    global _fingerprint_home
    with _fingerprint_home_lock:
        _fingerprint_home = None


@contextmanager
def _pinned_to_fingerprint_home() -> Iterator[None]:
    """Resolve inside this block through the captured home, not the ambient one.

    The mechanism is :func:`hermes_constants.set_hermes_home_override` — the same
    context-local override ``persona_profile_context`` installs, applied in the
    opposite direction and only for the length of a stat. Deliberately NOT a
    hand-composed ``home / "config.yaml"`` at each site: every class below keeps
    resolving through its OWN path authority, which is §6.1's first mitigation
    ("no second list free to drift") and is precisely the property a second copy
    of a path rule would give up. The override is context-local by construction,
    so pinning the walking thread cannot perturb a persona turn running beside
    it.

    Applied PER CLASS rather than around the whole walk, so each class states why
    it needs the pin and each is independently falsifiable: dropping the pin from
    one class reds that class's witness alone.
    """

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home, _authoritative = resolved_fingerprint_home()
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def build_input_fingerprint() -> CoreFingerprint | None:
    """The stat set over EVERY input the read-model build reads.

    ``None`` means "I could not fingerprint the inputs" and every caller must
    read it as **never cache** — not as "nothing changed". A missing answer is a
    loud refusal here, because the alternative is serving unlabeled stale as
    authoritative.

    The seven input classes, each resolved through the authority the BUILD
    reads through (§6.1's first mitigation — one authority, no second list).
    Four of them resolve HOME-RELATIVE and are therefore taken under
    :func:`_pinned_to_fingerprint_home`, so the answer is a function of the
    store and of the home this process resolved once, never of the
    ``HERMES_HOME`` the build itself exports mid-walk. Which classes are pinned
    and which are not is part of the specification, so it is stated per class
    rather than left to be re-derived:

    1. the agent-runtime store root subtree — ``paths.store_root()``, walked
       recursively so an ADDED file flips the key (offices, boards, personas,
       assignments, the event log and its rotation manifest, prompt
       observability, realm-sync baselines: everything the projection reads
       from the store, without a name list to fall behind). That "without a name
       list" is the class's design and it now has TWO stated exceptions, which is
       why the sentence no longer stands alone: ``deleted_archive/`` is excluded
       because the PROJECTION has no reader for it (the full argument — including
       the ``harness_doctor`` reader that does exist and why it does not re-admit
       the tree — is written at ``_EXCLUDED_STORE_ENTRIES``), and the orphaned-
       surface graveyard ``office_archive/`` is excluded because the runtime
       writes it, without bound, and the projection is deliberately built not to
       see it. Both arguments live at that constant. An exception with a reason at
       the constant is not the failure mode the sentence warns about; an unargued
       name list is. NOT pinned:
       ``resolve_runtime`` reads ``HERMES_AGENT_RUNTIME_ROOT`` and then the ROOT
       config, neither of which follows the profile home — measured unchanged
       across a persona flip on both the same thread and another one;
    2. the ``running_work`` durable stores — ``running_work_store_paths()``, the
       ONE authority for them (they hang off the HERMES home, not the store
       root, and both mutate with NO event). PINNED;
    3. the chat SessionDB — ``chat_session_db_path()``, the database the CHAT
       LANE writes, plus its WAL siblings. PINNED;
    4. the profile inputs ``agents_readiness`` reads — the profiles root and,
       per profile, ``profile.yaml`` + ``config.yaml``, plus the sticky
       ``active_profile`` pointer that decides which one a bare invocation
       resolves. NOT pinned: ``_get_profiles_root`` anchors to
       ``get_default_hermes_root()``, which maps ``<root>/profiles/<name>`` back
       to ``<root>``, so a profile flip resolves the SAME directory — measured;
    5. the config inputs — ``get_config_path()``, taken PINNED (it is literally
       ``get_hermes_home() / "config.yaml"``, so a persona scope swaps the file
       being stat'd), and the ROOT ``harness_root_config_path()`` left ambient
       because it anchors to the hermes root like class 4. Two authorities in
       production because the CLI profile redirect makes them genuinely
       different files;
    6. the skill registries — ``get_all_skills_dirs()`` (local profile skills,
       the shared canonical root, configured external roots) walked per root,
       plus the in-repo harness-skill source root the hash comparison reads.
       PINNED, and this is the class the measured 1,237-entry divergence came
       from: index 0 is the AMBIENT home's ``skills/``;
    7. the event-rotation lane — the manifest and the resolved LIVE slice.
       Under the store root today, so class 1 covers them; stat'd explicitly
       anyway because the resolution is free to move the live slice elsewhere
       and a frozen ``events.jsonl`` entry after a rotation is exactly the
       silent-staleness shape this whole module is against. NOT pinned: both
       resolve off the store root.
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

        # PINNED. ``running_work._head_home`` already asks the head authority,
        # and its docstring names this very incident class ("ambient
        # get_hermes_home() is not an option either: it is flipped
        # process-globally for the duration of a persona turn"). That is true and
        # still not enough HERE: the authority it consults is a ContextVar, so on
        # a thread that is not the one running the persona scope the recording is
        # invisible and the head degenerates to the flipped ambient home.
        # Measured with a scope held on another thread: the stores resolved to
        # the OTHER profile's ``processes.json`` and ``state.db``.
        with _pinned_to_fingerprint_home():
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

        # PINNED, for the same cross-thread reason as class 2: the scope ladder
        # asks ``hermes_head_home_is_authoritative()`` first — a ContextVar read
        # — and when no rung answers it bottoms out in the ambient home. Measured
        # with a scope held on another thread: the SessionDB resolved to the
        # OTHER profile's ``state.db``, which is a whole different chat history
        # inside the key.
        with _pinned_to_fingerprint_home():
            chat_db = chat_session_db_path()
        _db_entries(chat_db, entries)
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

        # PINNED: ``get_config_path()`` is ``get_hermes_home() / "config.yaml"``,
        # so a persona scope swaps the file being stat'd.
        #
        # NAMED RESIDUAL, measured rather than assumed. In the standard profile
        # layout this stat is REDUNDANT with class 4, which already enumerates
        # every ``<profiles>/<name>/config.yaml``, so unpinning it alone does not
        # move the digest there and no witness in this repo can kill it on its
        # own. It is pinned anyway, because a closure's specification must not
        # rest on one class accidentally covering another. The digest-visible
        # half of this class's exposure is SECOND-ORDER and lands in class 6:
        # ``agent.skill_utils.get_external_skills_dirs()`` reads this very file
        # to decide which external skill roots exist.
        with _pinned_to_fingerprint_home():
            entries.append(_stat_entry(get_config_path()))
        # NOT pinned: anchored to the hermes ROOT, like class 4.
        entries.append(_stat_entry(harness_root_config_path()))
    except Exception:
        return None

    # 6 — the skill registries.
    try:
        from agent.skill_utils import get_all_skills_dirs

        from .skill_install import harness_skill_source_root

        # PINNED, and this is the class the measured 1,237-entry divergence came
        # from. ``get_all_skills_dirs()`` puts ``get_skills_dir()`` — the AMBIENT
        # home's ``skills/`` — at index 0, so a walk taken while a persona scope
        # is exported enumerates ANOTHER PROFILE'S ENTIRE SKILLS TREE. The
        # external roots behind it are read out of the ambient ``config.yaml``
        # (class 5's file), so they flip with it.
        #
        # The pin is what keeps ``agent/skill_utils.py`` — upstream-owned — out
        # of this change: the resolver stays byte-identical and answers for the
        # home this process resolved, instead of core_cache growing a second copy
        # of its "local, then shared, then external, deduped" rule.
        with _pinned_to_fingerprint_home():
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


def pointer_path() -> Path:
    """The file whose replacement IS the write-back (MCF-21)."""

    return _cache_dir() / POINTER_FILENAME


def _is_generation_name(name: Any) -> bool:
    """Whether ``name`` is a generation directory this module could have minted.

    CONTAINMENT, not tidiness. The name comes off disk, out of a file any process
    on the machine can write, and it is about to be joined onto
    :func:`_cache_dir`. ``..`` or a separator would resolve the "live trio"
    anywhere on the filesystem, and the judgement downstream would then be asked
    to bless bytes this module never wrote. The charset admits exactly what
    :func:`_new_generation_name` mints and nothing else — no dots, no separators,
    no drive letters — so escaping is unrepresentable rather than filtered.
    """

    if not isinstance(name, str) or not name.startswith(_GENERATION_PREFIX):
        return False
    body = name[len(_GENERATION_PREFIX) :]
    return bool(body) and all(char in "0123456789abcdef-" for char in body)


def _new_generation_name() -> str:
    """A generation name no other write-back can collide with.

    The timestamp is for the OPERATOR — a directory listing of the cache sorts
    into the order the write-backs happened, which is what makes a stranded
    staging directory legible. It is NOT how the live generation is chosen: the
    pointer is the only authority, and a reader that preferred the newest name or
    mtime would resurrect a generation whose publish never completed. The random
    tail is what makes the name unique across two processes writing back inside
    the same nanosecond.
    """

    return f"{_GENERATION_PREFIX}{time.time_ns():x}-{uuid.uuid4().hex[:8]}"


def _live_generation_dir() -> Path:
    """The directory holding the trio the pointer names.

    =========================================================================
    WHY A POINTER AND NOT A DIRECTORY SWAP
    =========================================================================

    MC-3 recorded the target shape as "write ``serve_read_model.next/``,
    ``os.replace`` the directory". That is not implementable, and the reason is a
    platform fact rather than a preference: ``os.replace`` cannot replace a
    NON-EMPTY directory anywhere (POSIX ``rename`` answers ``ENOTEMPTY``), and on
    Windows — the primary platform — it cannot replace a directory AT ALL, empty
    or not (measured 2026-08-18: ``PermissionError`` / ``WinError 5`` for both).
    A directory can only be renamed onto a name that does not exist.

    The shape that follows from that is rename-away-then-rename-in, and it is
    REFUSED: between the two renames there is no live generation at all, so a
    concurrent consult is served ``absent`` — a window strictly worse than the
    torn trio the swap exists to retire.

    So the atomicity rides ONE small file instead. The complete trio is written
    into a fresh generation directory that nothing points at, and the write-back
    lands when — and only when — the pointer naming it is replaced through
    :func:`utils.atomic_json_write`, the same single atomic-write authority the
    rest of this module already uses. A crash before that leaves a directory the
    pointer never named, which serves nobody and is reaped by the next successful
    write-back.

    =========================================================================
    A POINTERLESS STORE DEMOTES — IT DOES NOT ADOPT THE FLAT TRIO
    =========================================================================

    Every store that held a cache before MCF-21 has ``core.json`` /
    ``sidecar.json`` / ``entries.json`` sitting flat in :func:`_cache_dir`, and
    adopting them as generation zero was the alternative on offer. It is refused,
    and NOT because the judgement could not vet them — it could; the full
    conjunction in :func:`_judge_persisted_pair` is exactly what a torn legacy
    trio fails. It is refused because keeping a second resolution alive forever
    means a pointer that is ever LOST — deleted, truncated, unparseable — silently
    falls back to whatever flat trio is on disk. That path can serve an arbitrarily
    old core as authoritative, which is the missed-input direction this module
    calls its worst failure, reached through the one code path nobody exercises.

    The cost of refusing is exactly one demote, on the first boot after this
    lands, on each store. A cache's cold start is its designed-for state.
    """

    name = _live_generation_name()
    return _cache_dir() / (name if name is not None else _NO_GENERATION_DIRNAME)


def _live_generation_name() -> str | None:
    """What the pointer says, or ``None`` when nothing usable does.

    Never raises, and every unusable shape answers the SAME way — no pointer, an
    unreadable one, a non-object, a missing field, a name that is not one this
    module mints. They are one fact ("no generation is published") and giving
    them one answer is what keeps the caller from growing a second judgement.
    """

    try:
        payload = json.loads(pointer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("generation")
    return name if _is_generation_name(name) else None


def core_path() -> Path:
    return _live_generation_dir() / CORE_FILENAME


def sidecar_path() -> Path:
    return _live_generation_dir() / SIDECAR_FILENAME


def entries_path() -> Path:
    """The stat set behind the sidecar's digest, so a miss can name a path.

    **The binding rule, RE-AIMED by MCF-21.** The payload is
    ``{"fingerprint": <digest>, "entries": [[path, mtime_ns, size], …]}`` and a
    reader compares ``entries.fingerprint`` against the sidecar's, refusing
    ``diff_reason=entries_unbound`` when they differ.

    The reason it was written is GONE, and saying so is the point. It used to
    guard POSITIONAL MIXING: three files landed through three independent
    ``os.replace`` calls, so any one of them failing left three separate
    generations in one directory, and a reader trusting position over provenance
    would diff the current store against some earlier one and name paths from a
    generation nobody asked about. The generation swap makes that unrepresentable
    — the three files are written into one directory that becomes live in a
    single pointer replace, so the entries file beside a sidecar is always that
    sidecar's own.

    **What it still defends, which is why it stays.** Provenance is not proved by
    position even inside a published generation: a hand-restored, truncated or
    tampered ``entries.json`` still reaches this reader, and the alternative to
    refusing is a diff computed against a stat set that is not this pair's — a
    receipt that NAMES FILES an operator will go and investigate. A diagnostic
    that cannot prove which store it is describing must refuse and say so, never
    pass on partial knowledge. That is the same rule as ``core_sha256`` one file
    further out, and it now has the same shape of reason: both convict bytes that
    are not the ones this module wrote, rather than binding files the swap
    already binds.

    **SIZE, PRICED RATHER THAN DISCOVERED** (the C-7 class). Measured
    2026-08-18 by walking the operator's live ``agent-runtime`` tree read-only
    and serialising this exact payload: **22,286 entries → 3,539,812 bytes
    (3.38 MiB)**, i.e. **159 bytes per entry** against a mean path of 127
    characters. Projected at the 23,107-entry key the field logs: **~3.5 MiB**.

    That measurement also settles the format question P4 left open, in the
    opposite direction to the guess. JSON is not the cost: a compact
    ``path|mtime|size`` text form would spend ~153 bytes per entry against JSON's
    159 — a ~4 % saving — because the PATHS dominate and backslash escaping adds
    only ~10 bytes to each. A second, text-shaped atomic writer (which does not
    exist today) would therefore buy nothing worth its own authority. The lever
    that actually moves this number is the SIZE OF THE CLOSURE, not its encoding:
    18,804 of the field's 23,107 entries were ``deleted_archive/``, which no
    projection reads.

    **That is now done** (MC-8 / P12): the graveyard is excluded from the walk at
    ``_EXCLUDED_STORE_ENTRIES``, where the reader argument is written. The
    measurement above was taken BEFORE it, and is left standing because it is what
    justified the exclusion; read it as the pre-P12 number. Expected after: ~4,300
    entries and ~0.7 MiB here, with the same ~159 bytes per entry — the per-entry
    cost was never the lever and did not move.
    """

    return _live_generation_dir() / ENTRIES_FILENAME


def _core_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Write-back
# --------------------------------------------------------------------------- #
def write_back(core: dict, *, fingerprint: CoreFingerprint | None = None) -> bool:
    """Persist the core, its sidecar and its stat set as ONE generation.

    A failed write logs and changes NOTHING about the build that produced the
    core — the build path is byte-identical whether this succeeds or fails,
    which is what makes the cache safe to add to a hot path (test 9's second
    half is the pin). That outer contract is unchanged by MCF-21.

    **ALL THREE FILES OR NONE (MCF-21).** This used to land three files through
    three independent ``os.replace`` calls: each was atomic alone and the TRIO
    was not, so the property "these three describe one build" was held up by two
    ad-hoc binding guards rather than by one rule — and a fourth file would have
    made a third guard. Now the trio is written into a fresh generation directory
    nothing points at, and the write-back LANDS when the pointer naming it is
    replaced. One atomic act publishes three files; a failure anywhere before it
    publishes nothing at all. See :func:`_live_generation_dir` for why a pointer
    rather than the directory swap MC-3 recorded, and why a store with no pointer
    demotes instead of adopting the flat trio it finds.

    **The arm that retires with it.** The entries write used to sit deliberately
    OUTSIDE the pair's ``try``, so a failed diagnostic left a usable cache behind
    and receipted itself as ``entries=false reason=entries_io``. A published
    generation missing one of its three files is now unrepresentable, so that
    state — and its receipt — are gone from the vocabulary and from the channel
    table. An entries failure aborts the generation and the write-back reports
    the one failure it had: ``ok=false reason=io``. The trade is named rather
    than discovered: the lane loses the ability to keep a cache whose diagnostic
    could not be written, and gains the property that anything published is
    whole. It is the right way round because the diagnostic exists to explain the
    cache, and a cache nobody can explain is the state MCF-14 spent a whole
    investigation in.

    The sidecar still binds to the core BYTES via ``core_sha256``, and that check
    is NOT retired with the torn pair. It convicts a different thing now: a
    hand-edited core, a rollback that dropped an older ``core.json`` into the
    live generation, or bytes that did not come from this module at all — none of
    which a swap can prevent, because they happen to a generation that is already
    published.

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
    fingerprint_home, home_authoritative = resolved_fingerprint_home()
    sidecar = {
        "fingerprint": key.digest,
        "fingerprint_entries": key.count,
        # WHICH QUESTION this key answers, not just what it answered. A digest is
        # only comparable between two processes that resolved the same home; a
        # pair written under one and judged under another is a DIFFERENT closure,
        # and ``_judge_persisted_pair`` demotes it as ``home_mismatch`` rather
        # than letting it wear the generic ``fingerprint_mismatch``. The
        # authoritative flag rides beside it because an unauthoritative head is a
        # fact a demote should be able to name — it means the home was the
        # ambient resolution at capture time, so it is only as good as the moment
        # it was taken.
        "fingerprint_home": str(fingerprint_home),
        "fingerprint_home_authoritative": home_authoritative,
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
    # BEFORE the writes, because the pair on disk is about to become this
    # process's own and the previous boot's answer would be unrecoverable after.
    # A process boundary is not a convergence event — see
    # :func:`_capture_boot_streak_seed`.
    _capture_boot_streak_seed(key)
    generation = _new_generation_name()
    staged = _cache_dir() / generation
    try:
        # The SAME atomic writer as everything else this module lands
        # (``utils.atomic_json_write``, compact separators for the two large
        # payloads), because one atomic-write authority is this module's rule. It
        # is not what makes the trio atomic — the pointer replace below is — but a
        # second staging convention inside one directory is how a half-written
        # file gets read as a whole one.
        atomic_json_write(
            staged / CORE_FILENAME, payload, indent=None, separators=(",", ":"), sort_keys=True
        )
        atomic_json_write(staged / SIDECAR_FILENAME, sidecar, indent=None, sort_keys=True)
        # The convergence authority runs BEFORE the entries write and hands it
        # the number, rather than the entries write deriving one of its own: the
        # streak is ``_note_written_key``'s to decide, and a second site computing
        # it from the same seed would be two rules for one question (property 6).
        # It sits INSIDE the staging block, after the two files that make a cache
        # exist — see that function's docstring for the one window in which it can
        # now advance for a generation that does not publish, and why that window
        # is narrower than it looks.
        streak = _note_written_key(key)
        atomic_json_write(
            staged / ENTRIES_FILENAME,
            _entries_payload(key, streak),
            indent=None,
            separators=(",", ":"),
            sort_keys=True,
        )
        # THE LANDING. Everything above wrote into a directory nothing points at;
        # this one replace is the write-back.
        atomic_json_write(
            pointer_path(), {"generation": generation}, indent=None, sort_keys=True
        )
    except Exception:
        logger.warning("snapshot_core_cache_write ok=false reason=io", exc_info=True)
        # The pointer never named it, so this is housekeeping and not a retraction
        # — the previous generation is still live and still whole. Best effort by
        # construction: if it fails, the directory is inert residue and the next
        # successful write-back reaps it.
        shutil.rmtree(staged, ignore_errors=True)
        return False
    logger.info(
        "snapshot_core_cache_write ok=true inputs=%d fingerprint=%s offset=%s",
        key.count,
        key.digest[:12],
        "unknown" if sidecar["event_offset"] is None else sidecar["event_offset"],
    )
    # AFTER the reap, and after the ok=true line above: housekeeping accounting
    # on a write-back that has already landed and already reported success.
    _receipt_generation_residue(_reap_superseded_generations(generation), generation)
    return True


def _entries_payload(key: CoreFingerprint, streak: int) -> dict:
    """The stat set behind the digest, bound to the digest — see :func:`entries_path`."""

    return {
        "fingerprint": key.digest,
        # The convergence streak this write-back left standing, so the NEXT
        # process can carry it instead of restarting from zero. It rides here
        # rather than on the sidecar for two reasons: the sidecar is read by every
        # consult on the boot path and must stay the cheap half of the judgement,
        # and this file already IS "what this write-back knew" — the streak is
        # that, not a property of the cached core. See
        # :func:`_capture_boot_streak_seed`.
        "streak": int(streak),
        "entries": [[entry.path, entry.mtime_ns, entry.size] for entry in key.entries],
    }


#: How many generation directories may sit in the cache before the reap says so.
#: THREE, counting the live one - so the healthy steady state (one live
#: generation, plus at most a couple a concurrent reader briefly held open) is
#: silent, and a store that is actually accumulating is not. Deliberately a
#: bound on the OBSERVATION and not on the removal: nothing here deletes harder
#: because the number is exceeded.
GENERATION_RESIDUE_BOUND = 3

#: How many leftover directories the receipt names. ``leftover=`` carries the
#: full count beside them, so the cap can never make a large residue read as a
#: small one. Oldest first: a generation name leads with a hex nanosecond stamp,
#: so sorting is chronological, and the oldest survivor is the one that has been
#: failing to reap the longest.
_GENERATION_RESIDUE_NAMES = 8


def _reap_superseded_generations(live: str) -> tuple[str, ...]:
    """Drop what the pointer no longer names. BEST EFFORT, and it must stay that way.

    Three things accumulate in :func:`_cache_dir` and all three are reaped by one
    rule — "keep the pointer and the generation it names":

    * the generations this write-back superseded;
    * staging directories stranded by a crash or a failed landing, which never
      served anybody because the pointer never named them;
    * the FLAT trio written before MCF-21, which :func:`_live_generation_dir`
      deliberately refuses to read. This is the only thing that ever removes it,
      and it is a one-time cleanup per store rather than a migration.

    **Why an individual failure is swallowed - and why the ACCUMULATION is not.**
    A reader in another process can be mid-read of a generation this call is
    removing; on Windows that makes the removal fail outright, which is exactly
    the right outcome. The cost of losing one reap is one directory the next
    write-back tries again on; the cost of letting it raise would be a landed
    write-back reporting failure. So the failure stays swallowed and
    ``ignore_errors`` stays on.

    What did NOT follow from that, and used to be claimed here, is that the
    outcome is not an event worth a line in a log an operator reads. A store that
    keeps failing to reap accumulates generations with NOTHING counting them - a
    silent drop with no accounting, which is the one thing this module refuses
    everywhere else (MCF-54(ii), ruled by MCF-59). This function therefore
    RETURNS what it left behind and :func:`write_back` hands that to
    :func:`_receipt_generation_residue`. Counting is not enforcement: the return
    value changes nothing about what was removed, and a write-back that has
    landed still reports success.

    A file it does not recognise is LEFT ALONE — including ``atomic_json_write``'s
    own ``.tmp`` staging files, which live in this directory while the pointer is
    being replaced. Reaping one of those would break a concurrent write-back for
    the sake of tidiness.
    """

    cache_dir = _cache_dir()
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return ()
    for name in names:
        if name == live:
            continue
        if _is_generation_name(name):
            shutil.rmtree(cache_dir / name, ignore_errors=True)
        elif name in _LEGACY_FLAT_FILENAMES:
            try:
                (cache_dir / name).unlink()
            except OSError:
                pass
    # RE-LISTED, not derived from the loop above: ``ignore_errors=True`` makes a
    # removal that failed indistinguishable from one that worked, so the only
    # honest survivor count is the one taken from disk AFTER the pass. A second
    # listdir is the price of the answer being true.
    try:
        survivors = os.listdir(cache_dir)
    except OSError:
        return ()
    return tuple(
        sorted(name for name in survivors if _is_generation_name(name) and name != live)
    )


def _receipt_generation_residue(leftover: tuple[str, ...], live: str) -> None:
    """Say, once per write-back, that the cache directory is not draining.

    NAMES the directories (the operator refinement recorded at MCF-59): a count
    sends them hunting, the names tell them exactly which directory to unlock or
    remove, and reading the same name across two builds is what separates a
    permanently held handle from transient contention.

    ``generations=`` goes LAST, matching :func:`_receipt_never_converged`'s
    ``diff=``, because it is a variable-length list and nothing after a
    variable-length list can be field-parsed.

    Reports, never enforces: this is accounting on a write-back that has already
    landed and already logged ``ok=true``.
    """

    if len(leftover) + 1 <= GENERATION_RESIDUE_BOUND:
        return
    logger.warning(
        "snapshot_core_cache %s present=%d bound=%d live=%s leftover=%d "
        "generations=%s - the reap left superseded generations behind (a reader "
        "holding one open, or a permission the writer does not have), so this "
        "store is accumulating whole cached cores on disk. The write-back "
        "itself SUCCEEDED and nothing was retracted. Unlock or remove the "
        "directories named here under the cache dir - never the live one.",
        RECEIPT_GENERATION_RESIDUE,
        len(leftover) + 1,
        GENERATION_RESIDUE_BOUND,
        live,
        len(leftover),
        ",".join(leftover[:_GENERATION_RESIDUE_NAMES]),
    )


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

class _StreakSeed(NamedTuple):
    """The previous write-back's answer, carried across a process boundary."""

    digest: str
    entries: tuple[FingerprintEntry, ...]
    streak: int


_convergence_lock = threading.Lock()
_last_written_digest: str | None = None
_streak_entries: tuple[FingerprintEntry, ...] = ()
_streak_length = 0
_streak_last_diff: tuple[str, ...] | None = None
_streak_common_diff: frozenset[str] | None = None
_never_converged_reported = False
_boot_streak_seed: _StreakSeed | None = None
_boot_streak_seed_taken = False
#: True when the streak this process is continuing began in an EARLIER one, so
#: some of its passes were never observed here. It exists to stop the receipt
#: over-claiming — see :func:`_note_written_key`.
_streak_seeded = False


def _reset_convergence_state() -> None:
    """Forget this process's convergence history, as a fresh process would.

    The seed is forgotten too, and it must be: a capture surviving into the next
    case would seed that case's streak from a store pytest has already deleted.
    """

    global _last_written_digest, _streak_entries, _streak_length
    global _streak_last_diff, _streak_common_diff, _never_converged_reported
    global _boot_streak_seed, _boot_streak_seed_taken, _streak_seeded
    with _convergence_lock:
        _last_written_digest = None
        _streak_entries = ()
        _streak_length = 0
        _streak_last_diff = None
        _streak_common_diff = None
        _never_converged_reported = False
        _boot_streak_seed = None
        _boot_streak_seed_taken = False
        _streak_seeded = False


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


def _capture_boot_streak_seed(key: CoreFingerprint) -> None:
    """Carry the PREVIOUS write-back's answer across the process boundary (A2).

    WHY THIS EXISTS, measured. ``_note_written_key`` used to return early on the
    first write-back of a process, so the receipt fired on the FOURTH consecutive
    disagreeing write-back of ONE process. Boots write back once or twice: the
    receipt was unreachable on every boot shape there is, and the 2026-08-18
    05:33 pair (``9772c7720bef`` → ``d525e554be44``) — which IS the
    self-perturbation the receipt was written to expose — could never be
    reported. A process boundary is not a convergence event, and treating it as
    one is what made the diagnostic dead on arrival.

    **SEED ONLY ON A FINGERPRINT DISAGREEMENT.** This is the correctness point,
    and it is not optional. A ``build_stamp_mismatch`` (the operator upgraded), a
    ``contract_mismatch`` (the schema moved), a ``runtime_root_mismatch`` (a
    different store) and a ``home_mismatch`` (a different question) are all
    LEGITIMATE non-agreements that say nothing whatever about convergence.
    Seeding on those would make a routine upgrade look like an oscillating store
    and fire a WARNING receipt at an operator with a healthy install — the
    expensive direction of error for a diagnostic whose whole value is that it
    only speaks when something is wrong. The judgement is asked of
    :func:`_sidecar_answers_a_different_question`, the read lane's own authority,
    so the two can never drift.

    **Cost, priced.** The sidecar is tiny and is always read. The entries file is
    megabytes and is read ONLY when the digests actually disagree — a converged
    boot, which is every healthy one, pays one small read and stops.
    """

    global _boot_streak_seed, _boot_streak_seed_taken

    with _convergence_lock:
        if _boot_streak_seed_taken or _last_written_digest is not None:
            return
        _boot_streak_seed_taken = True
    seed = _persisted_streak_seed(key)
    with _convergence_lock:
        _boot_streak_seed = seed


def _persisted_streak_seed(key: CoreFingerprint) -> _StreakSeed | None:
    """The persisted pair read as "what the last write-back concluded"."""

    try:
        sidecar = json.loads(sidecar_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(sidecar, dict):
        return None
    recorded_digest = sidecar.get("fingerprint")
    if not isinstance(recorded_digest, str) or not recorded_digest:
        return None
    if recorded_digest == key.digest:
        # The boots AGREE. Seeded with no entries and no streak on purpose: the
        # agreement branch below drops both anyway, and reading megabytes of
        # triples to discard them is exactly the cost a healthy boot must not pay.
        return _StreakSeed(recorded_digest, (), 0)
    if _sidecar_answers_a_different_question(sidecar):
        return None
    persisted, _unavailable = _persisted_entries(expect_digest=recorded_digest)
    if persisted is None:
        # The disagreement is real and seeds the streak; only the PATHS are
        # missing, and the receipt says so in its own words rather than
        # pretending the streak did not happen.
        return _StreakSeed(recorded_digest, (), 0)
    return _StreakSeed(recorded_digest, persisted.entries, persisted.streak)


def _note_written_key(key: CoreFingerprint) -> int:
    """Record what this write-back persisted, and report a lane that never settles.

    Returns the streak length this write-back leaves standing (``0`` when the
    lane settled), which ``write_back`` hands to the entries file so the NEXT
    process can continue it. This function stays the ONE authority for that
    number; the file only records what it decided.

    **When it is called, and the one window MCF-21 opened.** It runs once the core
    and sidecar have staged — i.e. once the write-back is going to land unless the
    disk fails twice — and before the entries file that RECORDS its answer, because
    the entries file is now inside the published unit and cannot be written after
    the landing. So the old sentence "called on successful write-backs only" is no
    longer exactly true and is not left standing as if it were: an entries-write or
    pointer-publish failure now advances this process's streak for a generation
    that did not publish.

    That window is narrower than the change makes it sound. Under the OLD ordering
    an entries failure already fired this, so the genuinely new case is a pointer
    replace that fails immediately after three files were written successfully into
    the same directory. And the consequence is bounded by what the streak MEASURES:
    whether consecutive BUILDS produce keys that agree — a property of the store's
    stability, not of what reached the disk. A write-back that failed leaves that
    answer just as true as one that landed.
    """

    global _last_written_digest, _streak_entries, _streak_length
    global _streak_last_diff, _streak_common_diff, _never_converged_reported
    global _streak_seeded

    with _convergence_lock:
        previous_digest = _last_written_digest
        previous_entries = _streak_entries
        if previous_digest is None:
            seed = _boot_streak_seed
            if seed is not None:
                # The first write-back of a process has nothing IN MEMORY to
                # agree with — but the previous boot left its answer on disk, and
                # that is what a store which never converges ACROSS boots
                # disagrees with. Continuing the count is the whole of A2's fix.
                previous_digest = seed.digest
                previous_entries = seed.entries
                _streak_length = seed.streak
                _streak_seeded = seed.streak > 0
        _last_written_digest = key.digest
        if previous_digest is None:
            # Nothing persisted and nothing in memory: "one build" is never
            # evidence of non-convergence.
            return 0
        if previous_digest == key.digest:
            # Settled: two consecutive write-backs wrote the same key, so the
            # store the next process stats is the store this one described.
            _streak_entries = ()
            _streak_length = 0
            _streak_last_diff = None
            _streak_common_diff = None
            _streak_seeded = False
            return 0
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
        streak = _streak_length
        if _streak_length < NEVER_CONVERGED_BUILDS or _never_converged_reported:
            return streak
        # Once per process. The receipt names an input to go widen the closure
        # over; repeating it every build afterwards would bury that under its own
        # noise without adding a fact.
        _never_converged_reported = True
        builds = _streak_length
        # ``every_pass`` claims a path differed on EVERY pass of the streak, and
        # a SEEDED streak began before this process did — the intersection here
        # spans only the passes observed here. Claiming it anyway would push a
        # cross-boot streak into the arm C22(i) reserves for self-perturbation,
        # inflating exactly the count an operator is meant to act on. A seeded
        # streak reports ``last_pair``, which is the strongest true thing it can
        # say about a diff it did not watch accumulate.
        common = None if _streak_seeded else _streak_common_diff
        last = _streak_last_diff
    _receipt_never_converged(builds=builds, common=common, last=last)
    return streak


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
        detail = _diff_unavailable_detail(DIFF_UNAVAILABLE_NO_ENTRIES)
    elif common:
        detail = _diff_detail(DIFF_SCOPE_EVERY_PASS, sorted(common))
    elif last:
        detail = _diff_detail(DIFF_SCOPE_LAST_PAIR, list(last))
    else:
        detail = _diff_unavailable_detail(DIFF_UNAVAILABLE_NO_ENTRY_DELTA)
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


def _diff_unavailable_detail(reason: str) -> str:
    """The one spelling for "a diff was owed here and could not be computed".

    Its own function so the two receipts that can owe a diff — the never-converged
    warning and the fingerprint demote — word the refusal IDENTICALLY. Two
    hand-written copies of a census instruction is the C22 defect itself, one
    level down: a census greps ``diff_reason=`` and a second spelling measures a
    false zero on whichever copy it did not know about.

    ``changed=0`` rides along rather than being omitted, so the field set is the
    same on every arm and a parser never has to branch on presence. ``diff=`` is
    LAST here for the same reason it is last on a computed diff.
    """

    return (
        f"diff_scope={DIFF_SCOPE_NONE} changed=0 "
        f"diff_reason={reason} diff={DIFF_UNAVAILABLE}"
    )


class PersistedEntries(NamedTuple):
    """What a write-back recorded beside its digest: the stat set, and the streak.

    Both are read together because they are read from one file in one parse. The
    streak is what lets a never-converged run survive a process boundary — see
    :func:`_capture_boot_streak_seed` — and it is recorded by the write-back
    rather than recomputed by the reader, so there is exactly one rule for the
    number.
    """

    entries: tuple[FingerprintEntry, ...]
    streak: int


def _persisted_entries(*, expect_digest: Any) -> tuple[PersistedEntries | None, str]:
    """The stat set behind a persisted digest, or a TYPED reason it is unusable.

    Returns ``(record, "")`` on success and ``(None, <diff_reason>)`` otherwise.
    Never raises: a diagnostic that could take down the lane it explains is worse
    than no diagnostic.

    ``expect_digest`` is the SIDECAR's fingerprint, and the comparison is the
    binding rule :func:`entries_path` documents — which MCF-21 re-aimed rather
    than retired: the swap makes cross-generation mixing unrepresentable, and this
    check now convicts an entries file inside the LIVE generation whose contents
    are not the ones the write-back put there. It is a required keyword rather
    than an optional check, because "read the entries" and "read the entries that
    belong to this pair" are different operations and only one of them is sound —
    an optional check is one call site away from being the unsound one.
    """

    try:
        raw = entries_path().read_text(encoding="utf-8")
    except OSError:
        return None, DIFF_UNAVAILABLE_NO_ENTRIES
    try:
        payload = json.loads(raw)
    except Exception:
        return None, DIFF_UNAVAILABLE_NO_ENTRIES
    if not isinstance(payload, dict):
        return None, DIFF_UNAVAILABLE_NO_ENTRIES
    rows = payload.get("entries")
    if not isinstance(rows, list):
        return None, DIFF_UNAVAILABLE_NO_ENTRIES
    if not expect_digest or payload.get("fingerprint") != expect_digest:
        return None, DIFF_UNAVAILABLE_ENTRIES_UNBOUND
    entries: list[FingerprintEntry] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            return None, DIFF_UNAVAILABLE_NO_ENTRIES
        path, mtime_ns, size = row
        try:
            entries.append(FingerprintEntry(str(path), int(mtime_ns), int(size)))
        except (TypeError, ValueError):
            return None, DIFF_UNAVAILABLE_NO_ENTRIES
    try:
        # Absent on every entries file written before the streak was persisted,
        # which is a legitimate shape and not a refusal: it means "this
        # write-back recorded no streak", i.e. zero.
        streak = int(payload.get("streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    return PersistedEntries(tuple(entries), streak), ""


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

    The generation is resolved ONCE and both files are read out of it, rather than
    asking :func:`sidecar_path` and :func:`core_path` in turn. Two resolutions
    could straddle a publish and read one file from each of two generations, which
    is precisely the torn read MCF-21 exists to end — reintroduced by the reader
    instead of the writer.
    """

    generation = _live_generation_dir()
    try:
        return (
            (generation / SIDECAR_FILENAME).read_text(encoding="utf-8"),
            (generation / CORE_FILENAME).read_text(encoding="utf-8"),
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
    different_question = _sidecar_answers_a_different_question(sidecar)
    if different_question:
        return CacheRead(core, False, different_question, key, sidecar)
    if sidecar.get("fingerprint") != key.digest:
        return CacheRead(core, False, DEMOTE_FINGERPRINT_MISMATCH, key, sidecar)
    return CacheRead(core, True, "", key, sidecar)


def _sidecar_answers_a_different_question(sidecar: dict) -> str:
    """The demote reason for "this pair is not comparable", or ``""``.

    Every clause here asks the same thing in a different dimension: is the
    persisted pair about the same CODE, the same CONTRACT, the same ROOT and the
    same HOME as the process judging it? None of them is about whether the store
    moved — that is the digest compare, and it is the only clause left outside.

    **Its own function because it has two callers and must never grow a second
    rule.** :func:`_judge_persisted_pair` asks it to demote; the cross-process
    convergence seed (:func:`_capture_boot_streak_seed`) asks it to decide whether
    a disagreement is EVIDENCE of non-convergence or an ordinary upgrade. A
    hand-copied second cascade there would drift from this one and the drift
    would surface as a WARNING receipt fired at an operator with a healthy
    install — the expensive direction.

    Clause ORDER is preserved from the conjunction it was lifted out of, and the
    ordering is load-bearing rather than incidental: each is a string compare
    against something already in hand, and the home clause sits BEFORE the digest
    compare (at the call site) because behind it the case it exists for is
    unreachable — a pair written under another home has a different digest by
    construction, so it would be swallowed as a generic ``fingerprint_mismatch``
    and the operator would read "the store moved" for something that is not about
    the store at all.
    """

    stamp = build_stamp_token()
    if stamp is None:
        return DEMOTE_BUILD_STAMP_UNKNOWN
    if sidecar.get("build_stamp") != stamp:
        return DEMOTE_BUILD_STAMP_MISMATCH
    if sidecar.get("contract_versions") != contract_versions():
        return DEMOTE_CONTRACT_MISMATCH
    try:
        from . import paths as _paths

        current_root = str(_paths.store_root())
    except Exception:
        current_root = None
    recorded_root = sidecar.get("runtime_root")
    if current_root is not None and recorded_root and str(recorded_root) != current_root:
        return DEMOTE_RUNTIME_ROOT_MISMATCH
    # ABSENT MUST NOT DEMOTE. Every sidecar written before MC-2 carries no
    # ``fingerprint_home``, and treating absent as mismatch would demote every
    # install a SECOND time for no information — once for the closure change that
    # stage already forced, then again for a field it could not have written.
    recorded_home = sidecar.get("fingerprint_home")
    if recorded_home and str(recorded_home) != str(resolved_fingerprint_home()[0]):
        return DEMOTE_HOME_MISMATCH
    return ""


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
    stamp: tuple[FingerprintEntry, ...]
    raw_core: str
    read: CacheRead


_memo_lock = threading.Lock()
_consult_memo: _ConsultMemo | None = None


def _pair_stamp() -> tuple[FingerprintEntry, ...]:
    """The pointer and the pair it names — the memo's whole invalidation rule.

    The POINTER is in the stamp, and it is the load-bearing third stat. A
    generation flip is already visible in the other two — a
    :class:`FingerprintEntry` carries its path and the generation name is in it —
    but that leaves the memo's invalidation depending on a naming convention. The
    pointer's own mtime moves on every publish by the same ``os.replace`` argument
    the whole module rests on, so the memo drops on a republish whatever the
    generations are called.
    """

    generation = _live_generation_dir()
    return (
        _stat_entry(pointer_path()),
        _stat_entry(generation / SIDECAR_FILENAME),
        _stat_entry(generation / CORE_FILENAME),
    )


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
    with the previous case's store. So is the captured fingerprint home (MC-2):
    a capture surviving into the next case would resolve its closure through the
    previous case's home, which the sandbox has already deleted.
    """

    global _lane_armed, _shadow_done
    with _lane_lock:
        _lane_armed = True
        _shadow_done = False
    _reset_convergence_state()
    _drop_consult_memo()
    reset_fingerprint_home()


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


def _demote_diff_detail(key: CoreFingerprint | None, sidecar: dict | None) -> str:
    """WHICH inputs moved between the persisted write-back and this walk.

    **The scope is ``last_pair`` by construction, and that is a census caveat, not
    a detail.** This is the delta since the LAST WRITE-BACK, so on a store that is
    simply busy it legitimately names files that are simply moving. It is
    self-perturbation evidence — the A1-b/A2 defect worth acting on — only when
    the named paths are ones the RUNTIME ITSELF writes. The channel table row
    carries the reading rule; the token on the line is the honest scope rather
    than a new one invented to flatter the diagnostic.

    Every arm that cannot answer says so in its OWN words through
    :func:`_diff_unavailable_detail`, never by returning an empty diff — an empty
    ``diff=`` reads to a census exactly like "we looked and nothing moved", which
    is C16's lesson and is the one way this receipt could mislead.
    """

    if key is None or not key.entries:
        return _diff_unavailable_detail(DIFF_UNAVAILABLE_NO_ENTRIES)
    persisted, unavailable = _persisted_entries(
        expect_digest=(sidecar or {}).get("fingerprint")
    )
    if persisted is None:
        return _diff_unavailable_detail(unavailable)
    changed = _changed_paths(persisted.entries, key.entries)
    if not changed:
        # The digests disagreed and no triple did. Nothing here can be named, and
        # borrowing the ``last_pair`` sentence for it would report a measurement
        # that was never taken.
        return _diff_unavailable_detail(DIFF_UNAVAILABLE_NO_ENTRY_DELTA)
    return _diff_detail(DIFF_SCOPE_LAST_PAIR, list(changed))


def _log_demote(
    *, caller: str, reason: str, key: CoreFingerprint | None, sidecar: dict | None = None
) -> None:
    """The demote receipt — and, on a fingerprint miss ONLY, what moved (A1-b).

    Before this, a read-miss said ``reason=fingerprint_mismatch inputs=23107``
    and nothing else, so the operator saw the same line on every same-commit boot
    with nothing to act on, and no process — not the serve, not a read-only
    investigation — could name the file that moved. The absence was the finding.

    TWO things keep the tail honest, and both are load-bearing:

    * **It is computed LAZILY and only for ``fingerprint_mismatch``.** The hit
      path pays nothing — it never reaches here — and the other demote reasons
      are not "an input moved": a diff on a ``build_stamp_mismatch`` would name
      every file the operator's upgrade touched and read to a census as store
      churn, which is a measurement that would be true of the wrong thing.
    * **``diff=`` goes LAST on the line**, after ``changed=``, for the reason
      already written at :func:`_receipt_never_converged`: it is a
      variable-length list and a path may contain spaces, so nothing can be
      field-parsed after it. The tail is purely ADDITIVE — an existing
      ``reason=`` grep is unaffected, which is what made this approvable as a
      change to production log text.

    The diff is worded by :func:`_changed_paths` and :func:`_diff_detail`, the
    never-converged receipt's own helpers, so the two receipts spell ONE
    vocabulary and are told apart by their family/event token exactly as the C22
    table teaches — never by two spellings of one fact.
    """

    detail = (
        " " + _demote_diff_detail(key, sidecar)
        if reason == DEMOTE_FINGERPRINT_MISMATCH
        else ""
    )
    logger.info(
        "snapshot_core_cache core_source=%s caller=%s reason=%s inputs=%s%s",
        CORE_SOURCE_REBUILT,
        caller,
        reason,
        "unknown" if key is None else key.count,
        detail,
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
            # The sidecar rides along so a fingerprint miss can be diffed against
            # the entries THAT pair persisted — the binding rule at
            # ``entries_path``. Handing the judgement's own sidecar rather than
            # re-reading one is what keeps the diff about the generation that was
            # actually judged.
            _log_demote(
                caller=caller,
                reason=read.reason,
                key=read.fingerprint,
                sidecar=read.sidecar,
            )
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

    **The one-shot is the SUBSCRIBER's, not the process's (MC-4 / P6).** It used
    to be a module-global ``_stale_served``, and that made the stale paint a race
    rather than a delivery: a boot starts TWO ``stream_frames`` generators — the
    hub producer, which the office ``runtime.office.subscribe`` attaches 0.1–0.2s
    before the launcher asks for anything, and the launcher's own argv stream —
    and whichever reached this function first consumed the process's single
    allowance. Measured 2026-08-18: it went to ``caller=hub`` on two of three
    boots, where ``serve_office_subscriptions.office_patch_sink`` discards every
    row that is not an ``office_actor`` — i.e. the one stale paint the design
    exists to deliver was thrown away, and the operator watched an empty canvas
    for the length of a full build. The rule is now stated where the room is
    known (``stream_frames``' ``wants_stale_first``, derived at producer-build
    time by ``serve.py::_room_wants_stale_first``), and the one-shot is
    structural: :func:`agent_runtime.stream.stream_frames` asks this ONCE, at its
    head, before its tail loop.

    **What still bounds it, and why that bound is the sound one.** Only while the
    lane is armed. The lane disarms at :func:`note_full_build_completed`, so the
    window is the BOOT — the span in which this process has not yet built its own
    truth — not the session. A resubscribe long after that can never re-paint an
    old projection, which is the property the process-global flag was reaching
    for and got by over-tightening: it also refused the SECOND generator of a
    boot, which is the one the launcher is usually on.
    """

    with _lane_lock:
        if not _lane_armed:
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
