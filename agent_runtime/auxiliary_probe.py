"""Context-local availability probes and client construction accounting."""
import contextlib
import contextvars
import threading
from typing import Any

_capability_probe_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aux_capability_probe_active", default=False
)
_client_construction_count = 0
_client_construction_lock = threading.Lock()


def _note_client_construction() -> None:
    """Count one real provider-client construction (test/pin seam).

    Covers the auxiliary-client construction seams: every module-level
    ``OpenAI(...)`` (they all resolve through the proxy below), the async
    client, and the Anthropic adapter client.
    """

    global _client_construction_count
    with _client_construction_lock:
        _client_construction_count += 1


def client_construction_count() -> int:
    """How many provider clients this process has constructed so far.

    The invariant worth pinning is a DELTA of zero across a capability check —
    a wall-clock assertion would rot on the first slow CI box.
    """

    with _client_construction_lock:
        return _client_construction_count


class _CapabilityProbeClient:
    """Inert stand-in for a provider client, returned only inside a probe.

    Attribute access yields the stand-in again, because thin wrappers read
    identity fields while they construct (``CodexAuxiliaryClient`` reads
    ``.api_key`` / ``.base_url``) and a probe that raised there would answer
    "unavailable" for a backend that IS available — the exact false negative
    issue #31179 fixed. Calling anything raises: a probe must never reach a
    wire, and if one of these ever escapes into a real request path we want a
    loud RuntimeError naming the seam, not a silent hang.
    """

    __slots__ = ("api_key", "base_url")

    def __init__(self, api_key=None, base_url=None):
        self.api_key = self if api_key is None else api_key
        self.base_url = self if base_url is None else base_url

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "capability-probe placeholder client was used to make a request; "
            "resolve a real client outside capability_probe_scope()"
        )

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "<capability-probe placeholder client>"


_CAPABILITY_PROBE_CLIENT = _CapabilityProbeClient()


def capability_probe_active() -> bool:
    return _capability_probe_active.get()


def is_capability_probe_client(candidate: Any) -> bool:
    """True for the stand-in AND for a thin wrapper built around one.

    The resolver wraps what it builds (``CodexAuxiliaryClient`` /
    ``AnthropicAuxiliaryClient`` keep the client they were handed on
    ``_real_client``), so a probe's result is usually a real wrapper object
    holding an unusable core. Treating only the bare stand-in as "probe
    output" let such a wrapper reach the shared client cache, where the next
    REAL caller would have been handed it.
    """

    if isinstance(candidate, _CapabilityProbeClient):
        return True
    return isinstance(getattr(candidate, "_real_client", None), _CapabilityProbeClient)


@contextlib.contextmanager
def capability_probe_scope():
    """Resolve "is a client available?" without constructing one.

    Scope is per-context (contextvars), so a probe on one thread never turns
    another thread's real resolution into a stand-in.
    """

    token = _capability_probe_active.set(True)
    try:
        yield
    finally:
        _capability_probe_active.reset(token)


