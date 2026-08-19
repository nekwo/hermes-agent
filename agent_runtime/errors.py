class AgentRuntimeError(Exception):
    """Base class for agent runtime harness errors."""


class InvalidTransition(AgentRuntimeError):
    """Raised when a task transition is not allowed by the transition table."""


class ProofMissing(AgentRuntimeError):
    """Raised when a proof gate cannot be satisfied."""


class StaleRun(AgentRuntimeError):
    """Raised when a run heartbeat is stale beyond its allowed TTL."""


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


class RuntimeRootMismatch(AgentRuntimeError):
    """Raised when resolved runtime root does not match a caller pin."""


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
    already hold one of these item ids?", and it answered that from
    ``list_actors``, which drops undecodable files on the floor
    (``OfficeStore._read_actor_dir`` counts them and moves on). So ONE
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


class CardsUnreadable(ArchiveUnreadable):
    """Raised when a board's order-key allocation cannot see the WHOLE column.

    The board twin of :class:`ActorsUnreadable`, and the same defect reached
    through a different guard. ``_list_active_cards`` drops a card file it
    cannot decode, and every order-key decision is computed from that list: the
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

    Homed here beside ``WorkspaceDeleteBlocked`` / ``StaleRevision`` /
    ``RuntimeRootMismatch``, with the same ``code`` + ``safe_details`` shape, so
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
