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


class LegacyOrchestratorRemoved(AgentRuntimeError):
    """Typed refusal retained while task-era read surfaces still deserialize it.

    Refuse rather than guess: the same discipline as
    ``DefaultScopeReconciliationRequired`` (surface, do not pick) and
    ``ambiguous_window_match`` (refuse, do not rank). ``safe_details`` carries
    redaction-safe routing facts only, never mission content.
    """

    code = "legacy_orchestrator_removed"

    def __init__(self, message: str, *, safe_details: dict | None = None):
        super().__init__(message)
        self.safe_details = dict(safe_details or {})

    def read_surface_envelope(self) -> dict:
        """Redaction-safe projection for READ surfaces (status / snapshot).

        The dispatch path must keep raising — that refusal is what replaced the
        legacy guess. But a pure read surface (``hermes harness status --json``,
        the Mission Control snapshot) is exactly what an operator uses to
        *diagnose* the broken mission, so it must report this condition as typed
        data rather than die on it: one undispatchable mission must not blank the
        HUD for the whole runtime. Everything here comes from ``safe_details``,
        which carries routing facts only (ids, states, counts) and never mission
        title/description content.
        """

        return {"code": self.code, "message": str(self), **self.safe_details}


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
