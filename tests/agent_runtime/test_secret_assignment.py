"""The secret-shaped-assignment rule: one home, one separator, no blind spots.

THE DEFECT THESE TESTS PIN (2026-07-25). Every one of the runtime's twelve
independent spellings of "secret-ish key + separator + value" wrote the
separator as ``\\s*[:=]``. That cannot match JSON — in ``{"token": "…"}`` the
key's CLOSING QUOTE sits between the key word and the ``:``. Since EVERY
realm-sync artifact is JSON, ``_assert_no_secret_artifacts`` was blind across
the entire publish surface, and every redaction lane surfaced JSON-encoded
credentials verbatim.

The fix is :data:`agent_runtime.redaction.SECRET_KEY_SEPARATOR`, consumed by
all four live composed patterns. These tests hold three lines:

1. the publish gate actually REFUSES a JSON artifact carrying a secret
   (:func:`test_publish_gate_refuses_json_artifact_carrying_a_secret` — the one
   that matters; it is red on the pre-fix spelling),
2. nothing that used to be caught became silent, and prose stays clean,
3. the ``group(1)`` contract every ``f"{match.group(1)}=[redacted]"`` call site
   depends on is unshifted.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_runtime import realm_sync
from agent_runtime.redaction import (
    ALL_SECRET_ASSIGNMENT_PATTERNS,
    ENV_SECRET_ASSIGNMENT_RE,
    SECRET_ASSIGNMENT_RE,
    SECRET_KEY_SEPARATOR,
    TEXT_SECRET_ASSIGNMENT_RE,
    TEXT_SECRET_VALUE_ASSIGNMENT_RE,
)

# The exact spelling that shipped on main before this fix. Kept verbatim so the
# "previously published clean" half of every claim below is asserted, not
# asserted-by-memory.
PRE_FIX_GATE_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|client[_-]?secret|oauth[_-]?token"
    r"|password|private[_-]?key|secret|token)\b"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{16,}"
)

#: The four JSON shapes the gate published clean. Flat, nested, in-list, and
#: the ``api_key`` spelling — one per structural position a credential lands in
#: inside ``office.json`` / ``board.json`` / ``store/workspaces/*.json``.
JSON_SECRET_ARTIFACTS = {
    "flat": {"token": "ghp_abcdefghij1234567890"},
    "api_key": {"api_key": "sk-abcdefghij1234567890"},
    "nested": {"a": {"client_secret": "abcdefghij123456"}},
    "in_list": {"actors": [{"password": "abcdefghij123456"}]},
}

#: Prose that names a secret without assigning one. A gate false positive
#: hard-fails an operator's whole realm publish, so these must stay silent.
PROSE_NEGATIVES = (
    "the token is stored in your password manager",
    "Set your api_key in the settings screen",
    "[token](https://example.com/docs/tokens)",
    "Rotate the client_secret whenever a member leaves the realm.",
    '{"token_count": 20208}',
    '{"secret": false}',
    '{"password": null}',
    '{"token": "abc"}',
)


# ── 1. The publish gate ────────────────────────────────────────────────────


def _artifact(tmp_path, name: str, payload: dict):
    source = tmp_path / name
    source.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return realm_sync.RealmSyncArtifact(
        kind="workspace",
        source=source,
        relative_path=f"store/workspaces/{name}",
        destination=None,
    )


@pytest.mark.parametrize("shape", sorted(JSON_SECRET_ARTIFACTS))
def test_publish_gate_refuses_json_artifact_carrying_a_secret(tmp_path, shape):
    """THE test. A JSON artifact carrying a credential must be REFUSED.

    Every realm-sync artifact is JSON, so before the separator fix this
    published clean — the assertion on ``PRE_FIX_GATE_RE`` below is what makes
    that a proven regression test rather than a hopeful one.
    """

    payload = JSON_SECRET_ARTIFACTS[shape]
    encoded = json.dumps(payload, indent=2)

    # Previously: published clean.
    assert PRE_FIX_GATE_RE.search(encoded) is None

    artifact = _artifact(tmp_path, f"{shape}.json", payload)
    with pytest.raises(realm_sync.RealmSyncError) as excinfo:
        realm_sync._assert_no_secret_artifacts([artifact])
    assert excinfo.value.code == "sync_secret_excluded"
    assert excinfo.value.safe_details["paths"] == [f"store/workspaces/{shape}.json"]


@pytest.mark.parametrize("shape", sorted(JSON_SECRET_ARTIFACTS))
def test_publish_gate_refuses_synthesized_json_content_carrying_a_secret(tmp_path, shape):
    """Same, through the ``content=`` lane — a synthesized artifact is scanned
    by the bytes it PUBLISHES, not by its provenance file."""

    encoded = json.dumps(JSON_SECRET_ARTIFACTS[shape]).encode("utf-8")
    source = tmp_path / "provenance.yaml"
    source.write_text("personas: {}\n", encoding="utf-8")
    artifact = realm_sync.RealmSyncArtifact(
        kind="persona_config",
        source=source,
        relative_path=f"store/realms/{shape}.json",
        destination=None,
        content=encoded,
    )

    with pytest.raises(realm_sync.RealmSyncError) as excinfo:
        realm_sync._assert_no_secret_artifacts([artifact])
    assert excinfo.value.code == "sync_secret_excluded"


def test_publish_gate_still_refuses_the_flat_env_form(tmp_path):
    """Regression floor: the shape the gate already caught must still fire."""

    source = tmp_path / "config.yaml"
    source.write_text("token: ghp_abcdefghij1234567890\n", encoding="utf-8")
    artifact = realm_sync.RealmSyncArtifact(
        kind="persona_config",
        source=source,
        relative_path="store/persona_config.yaml",
        destination=None,
    )
    with pytest.raises(realm_sync.RealmSyncError) as excinfo:
        realm_sync._assert_no_secret_artifacts([artifact])
    assert excinfo.value.code == "sync_secret_excluded"


@pytest.mark.parametrize("text", PROSE_NEGATIVES)
def test_publish_gate_stays_silent_on_prose_and_short_values(tmp_path, text):
    """A gate false positive bricks a publish. Prose that merely NAMES a secret,
    and JSON whose value is too short/structural to be a credential, must pass."""

    source = tmp_path / "notes.md"
    source.write_text(text, encoding="utf-8")
    artifact = realm_sync.RealmSyncArtifact(
        kind="skill",
        source=source,
        relative_path="store/skills/notes.md",
        destination=None,
    )
    realm_sync._assert_no_secret_artifacts([artifact])  # must not raise


def test_office_display_name_write_gate_rejects_json_shaped_secret():
    """``office_store`` rejects secret-shaped display names at the WRITE
    chokepoint using the same shared rule, so one member's name can never
    hard-fail another member's publish."""

    from agent_runtime.office_store import _assert_display_name_publishable

    _assert_display_name_publishable("Alice")  # must not raise
    with pytest.raises(ValueError):
        _assert_display_name_publishable('{"token": "ghp_abcdefghij1234567890"}')


def test_pull_admission_refuses_json_body_carrying_a_secret():
    """The pull-side per-entity guard shares the same object, so it closed with
    the gate."""

    from agent_runtime.sync_admission import content_refusal

    assert content_refusal(b'{"a": 1}') is None
    found = content_refusal(json.dumps(JSON_SECRET_ARTIFACTS["flat"]).encode("utf-8"))
    assert found is not None
    assert found[0] == "secret_shaped_value"


# ── 2. Coverage: JSON caught, env/yaml still caught, prose still silent ─────


@pytest.mark.parametrize(
    "text",
    (
        '{"token": "ghp_abcdefghij1234567890"}',
        '{"api_key": "sk-abcdefghij1234567890"}',
        '{"a": {"client_secret": "abcdefghij123456"}}',
        '{"a": [{"password": "abcdefghij123456"}]}',
        '{"token":"ghp_abcdefghij1234567890"}',
        "{'oauth_token': 'ghp_abcdefghij1234567890'}",
        '{"private_key" : "abcdefghij1234567"}',
    ),
)
def test_gate_catches_every_json_form(text):
    assert PRE_FIX_GATE_RE.search(text) is None, "premise: this used to be missed"
    assert SECRET_ASSIGNMENT_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    (
        "token: ghp_abcdefghij1234567890",
        "api_key: sk-abcdefghij1234567890",
        'secret = "abcdefghij1234567890"',
    ),
)
def test_gate_still_catches_the_env_and_yaml_forms(text):
    assert PRE_FIX_GATE_RE.search(text) is not None, "premise: this was already caught"
    assert SECRET_ASSIGNMENT_RE.search(text) is not None


@pytest.mark.parametrize("text", PROSE_NEGATIVES)
def test_gate_stays_silent_on_prose(text):
    assert SECRET_ASSIGNMENT_RE.search(text) is None


@pytest.mark.parametrize(
    "pattern",
    ALL_SECRET_ASSIGNMENT_PATTERNS,
    ids=lambda p: str(p.pattern)[:40],
)
def test_every_pattern_closes_the_json_blind_spot(pattern):
    """The separator fix is shared, so NO exported pattern may be JSON-blind."""

    assert pattern.search('{"api_key": "sk-abcdefghij1234567890"}') is not None


def test_every_pattern_composes_the_shared_separator():
    """A thirteenth hand-rolled ``\\s*[:=]`` is a thirteenth blind spot. Pin the
    shared fragment into every exported pattern so a new one cannot skip it."""

    for pattern in ALL_SECRET_ASSIGNMENT_PATTERNS:
        assert SECRET_KEY_SEPARATOR in pattern.pattern


# ── 3. group(1) semantics — the trap ───────────────────────────────────────


@pytest.mark.parametrize(
    "pattern,expected_groups",
    (
        (SECRET_ASSIGNMENT_RE, 1),
        (ENV_SECRET_ASSIGNMENT_RE, 1),
        (TEXT_SECRET_ASSIGNMENT_RE, 2),
        (TEXT_SECRET_VALUE_ASSIGNMENT_RE, 2),
    ),
)
def test_group_count_is_pinned(pattern, expected_groups):
    """``SECRET_KEY_SEPARATOR`` is a character class + quantifier and
    non-capturing groups only. If a future edit introduces a CAPTURING group it
    shifts every ``group(1)`` and silently corrupts every redaction in the
    process — this is the tripwire."""

    assert pattern.groups == expected_groups


@pytest.mark.parametrize(
    "text,expected_key",
    (
        ("token: ghp_abcdefghij1234567890", "token"),
        ('{"token": "ghp_abcdefghij1234567890"}', "token"),
        ('{"api_key": "sk-abcdefghij1234567890"}', "api_key"),
        ('{"a": {"client_secret": "abcdefghij123456"}}', "client_secret"),
        ('{"a": [{"password": "abcdefghij123456"}]}', "password"),
        ("{'oauth_token': 'ghp_abcdefghij1234567890'}", "oauth_token"),
    ),
)
def test_gate_group_one_is_the_key_word(text, expected_key):
    match = SECRET_ASSIGNMENT_RE.search(text)
    assert match is not None
    assert match.group(1) == expected_key


def test_env_group_one_is_the_whole_key_including_its_prefix():
    match = ENV_SECRET_ASSIGNMENT_RE.search('{"HERMES_API_KEY": "sk-abcdefghij1234"}')
    assert match is not None
    assert match.group(1) == "HERMES_API_KEY"


def test_text_patterns_expose_key_then_value():
    match = TEXT_SECRET_VALUE_ASSIGNMENT_RE.search('{"password": "hunter2hunter2"}')
    assert match is not None
    assert match.group(1) == "password"
    assert match.lastindex >= 2, "prompt lanes branch on lastindex >= 2"


# ── 4. Redaction output on JSON — a deliberate choice, not an accident ──────


def test_realm_sync_redacts_a_json_payload_key_preserved_value_gone():
    """DECISION: redaction emits ``key=[redacted]``, which for ``{"token": "…"}``
    yields structurally-invalid JSON (``{"token=[redacted]"}``). That is
    ACCEPTED, not overlooked.

    ``_redact_text`` scrubs git stderr and diagnostic strings into
    ``safe_details`` for DISPLAY; nothing downstream re-parses it as JSON. The
    alternative — restructuring the pattern to capture and re-emit the quoting
    so the JSON stays well-formed — would add a capture group and shift
    ``group(1)`` at four call sites that rebuild output from it, trading a
    cosmetic win for the exact corruption :func:`test_group_count_is_pinned`
    guards. The invariant that matters is upheld: the secret does not survive,
    and the key stays legible so an operator can find what to rotate.
    """

    redacted = realm_sync._redact_text('fatal: remote rejected {"token": "ghp_abcdefghij1234567890"}')

    assert "ghp_abcdefghij1234567890" not in redacted
    assert "token=[redacted]" in redacted
    assert "fatal: remote rejected" in redacted


def test_realm_sync_redaction_survives_nested_and_multiple_secrets():
    redacted = realm_sync._redact_text(
        '{"api_key": "sk-abcdefghij1234567890", "a": {"client_secret": "abcdefghij123456"}}'
    )

    assert "sk-abcdefghij1234567890" not in redacted
    assert "abcdefghij123456" not in redacted
    assert "api_key=[redacted]" in redacted
    assert "client_secret=[redacted]" in redacted


def test_realm_sync_redaction_leaves_clean_text_untouched():
    assert realm_sync._redact_text("fatal: could not read Username") == (
        "fatal: could not read Username"
    )
