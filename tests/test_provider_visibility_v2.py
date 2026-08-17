"""provider_visibility v2 (transport plan W4): the typed fields that retire
the launcher's ◆-box status scrape — model/provider, API-key presence, OAuth
login state — each failure-isolated so the credential payload (which the
launcher's model switcher depends on) can never be broken by a status probe.
"""

from __future__ import annotations

import pytest

import hermes_cli.harness as harness


def test_v2_schema_and_credential_payload_intact(monkeypatch):
    payload = harness.build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v2"
    # The v1 contract the launcher's model switcher consumes is untouched.
    assert isinstance(payload["providers"], list)


def test_v2_environment_block_carries_model_and_provider():
    payload = harness.build_provider_visibility()
    environment = payload.get("environment")
    assert isinstance(environment, dict)
    assert "model" in environment
    assert "provider" in environment


def test_v2_api_keys_mirror_the_status_box_registry():
    from hermes_cli.status import STATUS_API_KEYS

    payload = harness.build_provider_visibility()
    api_keys = payload.get("api_keys")
    assert isinstance(api_keys, list)
    names = {row["name"] for row in api_keys}
    assert names == set(STATUS_API_KEYS.keys()), (
        "the typed contract reports the SAME registry the status box renders "
        "— hoisted, not copied, so drift is structurally impossible"
    )
    for row in api_keys:
        assert isinstance(row["configured"], bool)


def test_v2_auth_logins_report_the_oauth_lanes():
    payload = harness.build_provider_visibility()
    logins = payload.get("auth_logins")
    assert isinstance(logins, list)
    names = {row["name"] for row in logins}
    assert {"Nous Portal", "OpenAI Codex", "Qwen", "MiniMax"} <= names
    for row in logins:
        assert isinstance(row["logged_in"], bool)


def test_v2_blocks_are_failure_isolated(monkeypatch):
    """A broken status probe drops its block; it never breaks the credential
    payload — the launcher treats an absent block as 'fall back to the
    scrape', exactly like a v1 hermes."""

    def _boom() -> dict:
        raise RuntimeError("status probe broken")

    monkeypatch.setattr(harness, "_provider_visibility_environment", _boom)
    monkeypatch.setattr(harness, "_provider_visibility_api_keys", _boom)
    payload = harness.build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v2"
    assert "environment" not in payload
    assert "api_keys" not in payload
    assert isinstance(payload["providers"], list)
    # auth_logins was not sabotaged and still reports.
    assert isinstance(payload.get("auth_logins"), list)


# --- PL-1: the `catalog` block — what you COULD connect ----------------------
#
# The defect this block exists to kill: a roster built from credentials-present
# + logins-present can render "this key is dead" but cannot render "you could
# connect this", so a never-configured provider and a dead one both show up as
# an absence of usable models. Nothing else in the payload can answer it.


class _Entry:
    """One pooled credential. Attribute-only — `build_provider_visibility`
    reads it through getattr, never by type."""

    def __init__(self, label: str, token: str):
        self.id = f"cred-{label}"
        self.label = label
        self.auth_type = "api_key"
        self.source = f"env:{label}"
        self.access_token = token
        self.last_status = None


class _Pool:
    def __init__(self, entries):
        self._entries = entries

    def entries(self):
        return self._entries

    def peek(self):
        return None


def _plant_credential(monkeypatch, provider: str, entry) -> None:
    """Give exactly ONE provider a pooled credential.

    The pytest sandbox points HERMES_HOME at an empty tempdir, so without this
    every pool is empty and any assertion that loops over credential rows is
    silently vacuous. Planting for one provider only is what lets the
    never-configured assertions below be about a real difference rather than
    about an empty payload.
    """
    import agent.credential_pool as pool_mod

    def _load_pool(pid):
        return _Pool([entry] if pid == provider else [])

    monkeypatch.setattr(pool_mod, "load_pool", _load_pool)


def _catalog_rows() -> dict:
    payload = harness.build_provider_visibility()
    catalog = payload.get("catalog")
    assert isinstance(catalog, list), "the catalog block must be present"
    return {row["id"]: row for row in catalog}


def test_catalog_lists_a_provider_with_no_credentials(monkeypatch):
    """The whole point: a provider with NO credential is still nameable.

    Exactly one provider (`openrouter`) is given a credential; `fireworks` —
    an ordinary api_key provider — is given none. Both must appear in
    `catalog`; only openrouter may appear in `providers`.

    MUTATION (kill): derive the catalog from the credentialed set (e.g. filter
    `provider_login_catalog()` by `payload["providers"]`) — `fireworks`
    disappears and this goes red.

    Anti-vacuity: the probed field is membership of `catalog`, which the
    mutated path also writes — it just writes a strictly smaller set. And the
    fact that `fireworks` is credential-less is ASSERTED here, not assumed, so
    the test cannot pass because the environment happened to have no
    credentials at all (which is the pytest sandbox's default, and is exactly
    how this assertion would have gone vacuous).
    """
    _plant_credential(monkeypatch, "openrouter", _Entry("OPENROUTER_API_KEY", "x" * 20))

    payload = harness.build_provider_visibility()
    credentialed = {row["id"] for row in payload["providers"]}
    catalog = {row["id"] for row in payload["catalog"]}

    assert credentialed == {"openrouter"}, credentialed
    assert "fireworks" not in credentialed
    assert "fireworks" in catalog
    assert "openrouter" in catalog


def test_catalog_row_shape_and_models_dev_mapping():
    rows = _catalog_rows()
    for row in rows.values():
        assert isinstance(row["id"], str) and row["id"]
        assert isinstance(row["name"], str) and row["name"]
        assert isinstance(row["flows"], list) and row["flows"]
        assert row["key_var"] is None or isinstance(row["key_var"], str)
        assert isinstance(row["models_dev_id"], str) and row["models_dev_id"]

    # The mapping the Launcher used to hardcode now lives server-side.
    # MUTATION (kill): make `models_dev_id_for` return the slug unconditionally.
    assert rows["openai-codex"]["models_dev_id"] == "openai"
    assert rows["opencode-zen"]["models_dev_id"] == "opencode"
    assert rows["qwen-oauth"]["models_dev_id"] == "alibaba"
    # A lane that is its own catalog id stays its own catalog id.
    assert rows["openrouter"]["models_dev_id"] == "openrouter"


def test_catalog_names_the_external_owner_disconnect_command():
    """External-tool-owned credentials get the documented command, never a
    silent delete. MUTATION (kill): return None from `disconnect_command_for`
    — red."""
    rows = _catalog_rows()
    assert rows["claude-code"]["disconnect_command"]
    assert "claude" in rows["claude-code"]["disconnect_command"]
    # A hermes-managed lane must NOT advertise an external disconnect command
    # (that would send the operator to a shell for something Settings owns).
    assert rows["openrouter"]["disconnect_command"] is None


def test_catalog_failure_is_isolated(monkeypatch):
    """A raising catalog builder drops its block and nothing else.

    MUTATION (kill): let the exception escape `build_provider_visibility` —
    the call raises and this goes red.
    """

    def _boom() -> list:
        raise RuntimeError("catalog builder broken")

    monkeypatch.setattr(harness, "_provider_visibility_catalog", _boom)
    payload = harness.build_provider_visibility()
    assert "catalog" not in payload
    assert isinstance(payload["providers"], list)
    assert isinstance(payload.get("environment"), dict)


def test_v2_consumers_see_an_unchanged_payload_minus_catalog(monkeypatch):
    """Additive means additive: strip the new keys and the v2 payload is what
    it always was.

    MUTATION (kill): rename any pre-existing field (e.g. `auth_type` →
    `authType`) or add a new one (e.g. `access_token`) to a credential row —
    red.

    Anti-vacuity: the pytest sandbox's HERMES_HOME is an empty tempdir, so
    `providers` is EMPTY by default and a bare loop over credential rows would
    assert nothing at all. A credential is planted and the row count asserted
    before the shape is checked.
    """
    _plant_credential(monkeypatch, "openrouter", _Entry("OPENROUTER_API_KEY", "x" * 20))

    payload = harness.build_provider_visibility()
    assert payload["schema"] == "hermes.provider_visibility/v2"
    assert set(payload) - {"catalog"} == {
        "schema",
        "providers",
        "environment",
        "api_keys",
        "auth_logins",
    }
    rows = [
        credential
        for provider in payload["providers"]
        for credential in provider["credentials"]
    ]
    assert len(rows) == 1, "no credential row was inspected — the shape check would be vacuous"
    for provider in payload["providers"]:
        assert set(provider) == {"id", "credentials"}
    for credential in rows:
        assert set(credential) - {"token_preview"} == {
            "index",
            "label",
            "auth_type",
            "source",
            "selected",
            "health",
        }


def test_no_credential_value_appears_in_the_payload(monkeypatch):
    """The secrecy gate. A seeded sentinel secret must not survive
    serialization anywhere — not in a credential row, not in the catalog, not
    in the environment block.

    MUTATION (kill): emit `access_token` on a credential row, or widen
    `_TOKEN_PREVIEW_CHARS` past the sentinel's length — red.

    Anti-vacuity: the sentinel is planted on a REAL pooled credential that the
    builder walks (verified by asserting the row for it is emitted at all), so
    "not found" cannot mean "nothing was searched".
    """
    import json as _json

    sentinel = "SENTINEL-DO-NOT-EMIT-abcdefghijklmnop"
    _plant_credential(monkeypatch, "openrouter", _Entry("SENTINEL_KEY", sentinel))

    payload = harness.build_provider_visibility()
    rendered = _json.dumps(payload)

    # The sentinel credential really was walked.
    labels = {
        credential["label"]
        for provider in payload["providers"]
        for credential in provider["credentials"]
    }
    assert "SENTINEL_KEY" in labels

    assert sentinel not in rendered
    # The preview is present, and is ONLY the tail.
    previews = {
        credential["token_preview"]
        for provider in payload["providers"]
        for credential in provider["credentials"]
    }
    assert "…mnop" in previews
    for preview in previews:
        if preview is None:
            continue
        assert len(preview) <= 5, preview


def test_token_preview_refuses_short_and_non_string_values():
    """A short key must not become its own preview, and the Entra-ID bearer
    CALLABLE must never be invoked.

    MUTATION (kill): drop the length floor (`"…c123"` appears for a 5-char
    key) or the isinstance check (the callable is called) — red.
    """
    from types import SimpleNamespace

    called = {"n": 0}

    def _bearer():
        called["n"] += 1
        return "minted-token"

    assert harness._credential_token_preview(SimpleNamespace(access_token="abc")) is None
    assert (
        harness._credential_token_preview(SimpleNamespace(access_token="abcdefgh"))
        == "…efgh"
    )
    assert harness._credential_token_preview(SimpleNamespace(access_token=_bearer)) is None
    assert called["n"] == 0
    assert harness._credential_token_preview(SimpleNamespace()) is None


# --- EG-6.1: an absent block stops meaning two things ------------------------
#
# The four v2 blocks are failure-isolated on purpose, and that stays. What
# changed is what the wire says afterwards. Before EG-6.1 each isolator was a
# bare `except Exception: pass`, so "block absent" meant EITHER "this hermes is
# too old to emit it" OR "this hermes tried and the builder threw" — and the
# `catalog` block, whose entire reason for existing is to separate "never
# configured" from "configured and dead", fell the client back into exactly the
# indistinguishable rendering it was added to end.
#
# Three wire states now, all distinguishable: block present; block absent with
# no `block_errors`; block absent and NAMED in `block_errors`.


class _CatalogProbeExploded(RuntimeError):
    """Injected fault #1. A distinct CLASS is the whole point of the pair."""


class _CatalogProbeTimedOut(TimeoutError):
    """Injected fault #2."""


@pytest.mark.parametrize("error_class", [_CatalogProbeExploded, _CatalogProbeTimedOut])
def test_a_thrown_catalog_builder_is_named_and_the_block_absent(
    monkeypatch, error_class
):
    """The block drops (isolation intact) AND `block_errors` names the class.

    MUTATION (proves non-vacuity): keep the silent `except Exception: pass`.
    The `catalog`-absent half still passes — it always did — but `block_errors`
    is never written and the naming half goes red.

    Driven with TWO distinct injected classes, so a constant cannot pass: a
    mutant that hardcodes any one class name fails the other parameter.

    The injected message is deliberately token- and URL-shaped: the recorded
    value is the class NAME only, never `str(exc)`, the same disclosure rule
    `_credential_token_preview` and `_usage_failure_reason` follow.
    """

    def _boom() -> list:
        raise error_class("Bearer sk-LEAKME from https://example.invalid/catalog")

    monkeypatch.setattr(harness, "_provider_visibility_catalog", _boom)
    payload = harness.build_provider_visibility()

    assert "catalog" not in payload
    assert payload["block_errors"]["catalog"] == error_class.__name__
    # Isolation unchanged: the credential payload and the sibling blocks built.
    assert isinstance(payload["providers"], list)
    assert isinstance(payload.get("environment"), dict)

    serialized = repr(payload)
    assert "sk-LEAKME" not in serialized
    assert "https://" not in serialized


def test_a_healthy_build_has_no_block_errors_entry():
    """The other half of the pair: nothing threw, so nothing is named.

    MUTATION (kill): write `block_errors` unconditionally (seed it to `{}` when
    the payload is created, or record a healthy sentinel per block) — red.
    Without this test the naming test above is satisfied by an always-write
    mutant, and "absent because old" collapses back into "absent because
    threw".

    Anti-vacuity: the four blocks are asserted PRESENT first, so this cannot
    pass because the build produced nothing to name.
    """
    payload = harness.build_provider_visibility()
    assert isinstance(payload.get("environment"), dict)
    assert isinstance(payload.get("api_keys"), list)
    assert isinstance(payload.get("auth_logins"), list)
    assert isinstance(payload.get("catalog"), list)
    assert "block_errors" not in payload


def test_block_errors_is_keyed_per_block_not_one_flag(monkeypatch):
    """Two blocks fail with two different classes; both are named, and the two
    healthy blocks are untouched.

    MUTATION (kill): make the record a single scalar (`payload["block_error"] =
    class_name`) or a boolean flag — the equality goes red. One slot cannot hold
    two classes, and a per-block map is what the console needs in order to say
    WHICH surface it may not trust.
    """

    def _boom_environment() -> dict:
        raise _CatalogProbeExploded("environment probe broken")

    def _boom_catalog() -> list:
        raise _CatalogProbeTimedOut("catalog probe broken")

    monkeypatch.setattr(harness, "_provider_visibility_environment", _boom_environment)
    monkeypatch.setattr(harness, "_provider_visibility_catalog", _boom_catalog)
    payload = harness.build_provider_visibility()

    assert payload["block_errors"] == {
        "environment": "_CatalogProbeExploded",
        "catalog": "_CatalogProbeTimedOut",
    }
    assert "environment" not in payload
    assert "catalog" not in payload
    # Not sabotaged, so still reported — the isolation is per block.
    assert isinstance(payload.get("api_keys"), list)
    assert isinstance(payload.get("auth_logins"), list)
