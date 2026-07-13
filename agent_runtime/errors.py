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


class NotFound(AgentRuntimeError):
    """Raised when a persisted runtime entity cannot be found."""


class AlreadyExists(AgentRuntimeError):
    """Raised when creating an entity that already exists."""


class EventPayloadTooLarge(AgentRuntimeError):
    """Raised when an event payload exceeds the Stage 1 JSONL budget."""


class RuntimeRootMismatch(AgentRuntimeError):
    """Raised when resolved runtime root does not match a caller pin."""


class ProbeIsolationViolation(AgentRuntimeError):
    """Raised when a run that demanded an isolated probe root would touch the live store.

    Guards Stage-C / QA probe runs: with ``HERMES_REQUIRE_ISOLATED_ROOT`` set, the
    resolved runtime root must be a dedicated ``agent-runtime-probe-*`` temp dir won via
    the env layer, so a probe can never persist persona instances into the live store.
    """
