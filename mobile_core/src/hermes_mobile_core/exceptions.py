"""Public exception types for the mobile provider core."""


class MobileCoreError(Exception):
    """Base class for mobile-core contract errors."""


class MobileUnsupported(MobileCoreError):
    """Raised when a desktop-only Hermes capability is reached."""


class InvalidRequest(MobileCoreError):
    """Raised when a facade request violates the JSON contract."""


class MalformedSSE(MobileCoreError):
    """Raised when a provider stream is not valid chat-completions SSE."""
