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
