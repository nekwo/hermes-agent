"""Public API for the Hermes mobile provider core."""

from .core import HermesMobileCore
from .auth import MobileAuthManager, ProviderAuthError
from .events import SCHEMA_VERSION
from .exceptions import InvalidRequest, MalformedSSE, MobileCoreError, MobileUnsupported
from .providers import list_supported_providers

__all__ = [
    "HermesMobileCore",
    "MobileAuthManager",
    "ProviderAuthError",
    "InvalidRequest",
    "MalformedSSE",
    "MobileCoreError",
    "MobileUnsupported",
    "SCHEMA_VERSION",
    "list_supported_providers",
]

__version__ = "0.2.0"
