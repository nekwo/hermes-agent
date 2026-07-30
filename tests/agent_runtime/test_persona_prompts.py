from agent_runtime.persona_runtime import _safe_read_soul_overlay


def test_soul_overlay_rejects_absolute_or_secret_like_paths(tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("do not read", encoding="utf-8")

    assert _safe_read_soul_overlay(str(secret)) is None
    assert _safe_read_soul_overlay(".env") is None
    assert _safe_read_soul_overlay("auth/token.md") is None
    assert _safe_read_soul_overlay("config/profile.md") is None
