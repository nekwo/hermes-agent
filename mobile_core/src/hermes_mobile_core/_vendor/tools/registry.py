"""Fail-loud mobile stub for the desktop tool registry."""

from hermes_mobile_core.exceptions import MobileUnsupported


def __getattr__(name):
    raise MobileUnsupported(
        f"Desktop tool registry access is unavailable in hermes-mobile-core: {name}"
    )
