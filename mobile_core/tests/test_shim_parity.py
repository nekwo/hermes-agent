from __future__ import annotations


def test_developer_role_model_shim_matches_live_upstream() -> None:
    from agent.prompt_builder import DEVELOPER_ROLE_MODELS as live
    from hermes_mobile_core._vendor.agent.prompt_builder import (
        DEVELOPER_ROLE_MODELS as vendored,
    )

    assert vendored == live


def test_native_gemini_shim_is_fail_closed() -> None:
    from hermes_mobile_core._vendor.agent.gemini_native_adapter import (
        is_native_gemini_base_url,
    )

    assert is_native_gemini_base_url("https://generativelanguage.googleapis.com/v1beta") is False


def test_tool_registry_stub_fails_loudly() -> None:
    import pytest

    from hermes_mobile_core.exceptions import MobileUnsupported
    from hermes_mobile_core._vendor import tools

    with pytest.raises(MobileUnsupported):
        from hermes_mobile_core._vendor.tools import registry

        registry.any_desktop_registry_symbol
