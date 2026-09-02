class AgentRuntimeError(Exception):
    """Base class for agent runtime harness errors."""


class StoreCorrupt(AgentRuntimeError):
    """Raised when persisted JSON cannot be decoded into the expected model."""


class DefaultScopeReconciliationRequired(AgentRuntimeError):
    """Raised when startup cannot choose one local default scope safely.

    The exception is deliberately non-mutating: callers must surface the
    read-only migration preview and wait for an operator-approved reconciliation
    instead of guessing which persisted realm/workspace identity should win.
    """

    code = "default_scope_reconciliation_required"

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


# S54 removed ``LegacyOrchestratorRemoved``. S5 deleted the legacy orchestrator
# it was raised for; S41 removed the last binding that still imported the name.

class NotFound(AgentRuntimeError):
    """Raised when a persisted runtime entity cannot be found."""


class AlreadyExists(AgentRuntimeError):
    """Raised when creating an entity that already exists."""


class EventPayloadTooLarge(AgentRuntimeError):
    """Raised when an event payload exceeds the Stage 1 JSONL budget."""


class StaleRevision(AgentRuntimeError):
    """Raised when an optimistic ``--expect-revision`` check fails.

    The card was mutated since the caller read it; the caller should refresh and
    replay (or surface the conflict). Replaying the same ``--idempotency-key``
    never raises this — it returns the recorded result.
    """


class ArchiveUnreadable(AgentRuntimeError):
    """Raised when an actor's ARCHIVE copy exists but cannot be decoded.

    Narrower than :class:`StoreCorrupt` on purpose, because the archive copy is
    not merely data — it is where the revision guard's token LIVES between a
    remove and the re-add that follows it. ``upsert_actor`` bases the new
    revision on the archived one so a re-added key carries its history forward;
    swallowing a decode failure there made the base 0 and the new revision 1,
    silently handing every peer a token lower than the one they already hold.
    A write that cannot read the number it must bump is a fault, not a fresh
    start, and the launcher's guard is only as honest as this token.

    ``code`` rides the RPC error envelope verbatim, the same way
    :class:`WorkspaceDeleteBlocked`'s does.
    """

    code = "archive_unreadable"


class ActorsUnreadable(ArchiveUnreadable):
    """Raised when the class-key fence cannot see the WHOLE actor directory.

    The fence's ``duplicate_item_placement`` half answers "does a live sibling
    already hold one of these item ids?", and it answered that from the actor
    ROWS alone, which drop undecodable files on the floor
    (``office_store.read_actor_dir`` counts them and moves on). So ONE
    unreadable instance-keyed actor file made the honest answer "unknown" while
    the fence read it as "no" — for every writer at once — and the class-keyed
    write it was built to refuse landed beside the sibling nobody could decode.
    Exactly the identical-sibling-nobody-can-find defect, reached through the
    fence itself.

    A guard that cannot see its evidence REFUSES. Subclasses
    :class:`ArchiveUnreadable` so every translation arm that already learned the
    "a file the write depends on will not decode" condition covers this one
    without a new branch, and carries its OWN ``code`` so the operator's cure
    (repair or remove the unreadable ACTOR file, not the archive copy) is not
    mislabelled.
    """

    code = "actors_unreadable"


class ActorArchived(AgentRuntimeError):
    """Raised when an upsert would re-add an actor key that was DELETED.

    ``upsert_actor`` used to read any upsert of an archived key as operator
    intent to re-add, and acted on it: it cleared the resurrection-guard ledger
    entry AND unlinked the archive copy. Measured live on 2026-08-27 — a retire
    acked ``archived_actor_keys=[...]`` with no failures, and a launcher that
    had booted nineteen seconds earlier re-pushed the same actors as
    ``state: active, updated_by: operator``. Because the archive copy was then
    gone, the retire REPLAY could no longer find anything to report and answered
    ``already_retired: true, archived_actor_keys: []`` forever: a permanent
    wedge that only ``harness office actor-remove`` cleared.

    The intent reading was the defect. A blind re-push carries no intent at all,
    and the two are indistinguishable at the store — which is why the door now
    needs a key (``resurrect=True``) rather than being held open for whoever
    walks through it.

    NON-RETRYABLE. The cure is not "try again": the actor was deleted on the
    authority, so the client must drop its local row. Re-placing is a NEW create
    with a freshly minted id, never a resurrection of this key.
    """

    code = "actor_archived"

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})


class CardsUnreadable(ArchiveUnreadable):
    """Raised when a board's order-key allocation cannot see the WHOLE column.

    The board twin of :class:`ActorsUnreadable`, and the same defect reached
    through a different guard. A card-directory scan drops a card file it
    cannot decode, and every order-key decision is computed from that scan's
    result (``_ordering_cards``, the one read that refuses): the
    neighbour keys an insert brackets between, and the keys a column rebalance
    rewrites wholesale. A card the platform would not open is therefore a card
    the allocator places on top of, and a rebalance reassigns every key in the
    column around a row it never saw — so the invisible card's key lands in an
    order nothing agrees on, and the corruption is written, not merely read.

    A write that cannot read the evidence it allocates against REFUSES. Reads
    are untouched: the board still lists, and the count travels beside the rows
    (``CardScan``) so the projection can state it.
    """

    code = "cards_unreadable"


class PersonaInstancesUnreadable(ArchiveUnreadable):
    """Raised when a persona-lane write cannot see the WHOLE instance directory.

    The persona twin of :class:`ActorsUnreadable`, and the third subsystem to
    reach the same defect through its own guard. ``PersonaInstanceStore.list_all``
    drops a row file it cannot decode, and the arms that DECIDE on that list are
    writers: the steering repair derives ``live_ids`` from it and strips every
    child edge naming a row that is merely unreadable — a delete-shaped write
    produced by a parse error — and the backlink release promises to have
    released EVERY child reference before an owner is archived, a completeness
    claim the short list cannot support.

    A write that cannot read the rows it decides against REFUSES. Reads are
    untouched: the store still lists, and the count travels beside the rows
    (:class:`~.persona_assignments.PersonaInstanceScan`) so a projection can
    state it rather than describe a short answer as complete.
    """

    code = "persona_instances_unreadable"


class IdempotentReplayUnresolved(AgentRuntimeError):
    """A receipt for this idempotency key EXISTS, but the row it names cannot
    be resolved — so the write must refuse rather than run a second time.

    The board's replay used to answer this by returning ``None``, which is the
    same value it returns for "no receipt at all". The caller cannot tell those
    apart, so it did the write again — and ``add_card`` mints a NEW card id per
    call, so a key whose entire purpose is to prevent a duplicate produced one,
    and the follow-up ``_record_idempotency`` then overwrote the receipt so the
    first card was orphaned and permanently unreachable by that key.

    Cards are never hard-deleted (``archive_card`` writes the archive copy
    before unlinking the active one; ``restore_card`` does the reverse), so
    "resolves to neither" is a truncated write or a corrupted file — never a
    routine purge. There is no retry this refusal can wrongly break.

    The honest-answer principle is ``agent_create``'s: a replay that cannot
    re-read the row it describes says so (``actor_fresh: false``) instead of
    presenting a stale or invented answer. This is that branch for a lane whose
    ack cannot be degraded, only refused.

    ``code`` rides the RPC error envelope verbatim.
    """

    code = "idempotent_replay_unresolved"


class SyncConflict(AgentRuntimeError):
    """Raised when a board card is under an unresolved realm-sync conflict, or a
    conflict-resolution verb targets a card that has none."""


class ProbeIsolationViolation(AgentRuntimeError):
    """Raised when a run that demanded an isolated probe root would touch the live store.

    Guards Stage-C / QA probe runs: with ``HERMES_REQUIRE_ISOLATED_ROOT`` set, the
    resolved runtime root must be a dedicated ``agent-runtime-probe-*`` temp dir won via
    the env layer, so a probe can never persist persona instances into the live store.
    """


class WorkspaceDeleteBlocked(AgentRuntimeError):
    """Raised when a workspace hard-delete is refused by a delete guard.

    ``code`` is the typed machine reason (``workspace_has_goals`` /
    ``realm_default_workspace``) and rides the CLI error envelope verbatim;
    ``safe_details`` carries operator-safe counts/hints only, never content.
    """

    def __init__(self, code: str, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.safe_details = dict(safe_details or {})


class SkillTombstoneRefused(AgentRuntimeError):
    """Raised when a shared-skill tombstone write is refused at the door.

    ``code`` is the typed machine reason and rides the CLI/RPC error envelope
    verbatim; ``safe_details`` carries the slug and operator-safe hints only.
    Same shape as :class:`WorkspaceDeleteBlocked` on purpose — one exception
    vocabulary for "a delete chokepoint refused", not two.

    - ``skill_installer_owned`` — the slug is one of
      ``hermes_constants.CANONICAL_SHARED_SKILL_IDS``, which every pull
      REINSTALLS from repo source (``realm_sync``'s ``install_harness_skills``).
      A ledger entry for such a slug is a fight the installer wins on every
      pull, so the door refuses instead of minting a tombstone that silently
      does nothing; the delete lane for those ids is the constant plus
      ``docs/agent-runtime-harness/harness-skills/``.
    - ``skill_slug_invalid`` — the slug is not a safe canonical slug shape
      (``skill_promotion.validate_skill_slug``'s reason travels in the message).
    """

    def __init__(self, code: str, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.safe_details = dict(safe_details or {})


class WorkspaceUnresolved(AgentRuntimeError):
    """Raised when an office write would AUTHOR a surface for an unresolvable id.

    ``OfficeStore.ensure_surface`` lazily created a default surface for ANY id
    that passed ``serde.safe_id``, with no existence check at all. The measured
    consequence (2026-08-17 / EG-0.1): a leaked test context minted a whole live
    office in the operator's runtime root — one ``office.surface.created``, 67
    actor upserts and a ``revision 67`` actor file — for a workspace id no verb
    ever authorised and no ``workspace.created`` event ever named. The parity
    warning could report the wreckage afterwards; nothing refused it at the door.

    THE ACCEPTED CONSEQUENCE, stated so a field report of it is not read as a
    regression: a machine whose workspace record has not synced yet now gets a
    VISIBLE typed refusal instead of silently minting an orphan. That is the
    ruling's intent, not a side effect of it.

    Scoped to CREATION. An existing surface whose workspace record has since gone
    is still read, projected and archivable — see ``ensure_surface``'s ordering
    note. Refusing reads would break every projection of an orphan the operator
    is trying to clean up.

    Homed here beside ``WorkspaceDeleteBlocked`` / ``StaleRevision``, with the
    same ``code`` + ``safe_details`` shape, so
    CLI and RPC envelopes carry a machine reason verbatim through the mapping
    they already have. B23(v) is the recorded hazard this avoids: two exception
    vocabularies for one contract, noticed only when a third site copies the
    wrong one.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "workspace_unresolved",
        safe_details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.safe_details = dict(safe_details or {})
