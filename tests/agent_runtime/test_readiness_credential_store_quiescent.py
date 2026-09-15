"""The snapshot's readiness pass READS credentials; it must not rewrite them.

MCF-16. ``build_input_fingerprint`` stats ``profile.yaml`` / ``config.yaml`` /
per-profile dirs — ``auth.json`` is NOT in the closure. The readiness pass
(``agents_readiness``, the largest section of the build) nevertheless resolved a
runtime provider per persona, and ``CredentialPool._select_unlocked`` under
``round_robin`` persisted the rotated cursor by rewriting the whole credential
store. ``hermes_cli.auth::_save_auth_store`` stamps ``updated_at`` on every call
with no content comparison anywhere, so two logically identical writes produce
different bytes.

Consequence of the pair: a credential change on an otherwise-quiescent store
lands as a **false cache HIT** — the build's own pass moved the file, so a real
change is indistinguishable from the build's self-perturbation, and cached
tool-visibility/readiness is served as authoritative. That is the missed-input
direction the cache module itself calls its worst failure.

The four gates below are one assertion each, each with its own killing mutation:

1. the readiness pass leaves the store byte-identical (and is not vacuous — the
   pool selection is counted, so a probe that silently stopped reaching the pool
   cannot pass by doing nothing);
2. round-robin still rotates — in memory for the probe, and across a fresh
   ``load_pool`` for a real consumer, which is what the write-back is FOR;
3. a genuine credential change still persists;
4. a real write still moves ``updated_at`` — i.e. ``_save_auth_store`` was NOT
   made change-only. That is a separate shape, deliberately not taken here, and
   this gate is what keeps the two changes from silently merging.

CREDENTIAL HYGIENE: every token in this module is an inert marker string under
pytest's per-test ``HERMES_HOME`` tempdir. Nothing here is, or resembles, real
credential material.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_runtime.models import AgentPersona


PROVIDER = "deepseek"
#: Inert markers. Non-empty is the only property the pool cares about
#: (``_available_entries`` skips an api_key entry with no runtime key).
MARKER_A = "inert-marker-alpha-not-a-credential"
MARKER_B = "inert-marker-bravo-not-a-credential"


def _entry(idx: int, marker: str) -> dict:
    return {
        "id": f"cred-{idx}",
        "label": f"slot-{idx}",
        "auth_type": "api_key",
        "priority": idx,
        "source": "manual",
        "access_token": marker,
    }


def _hermes_home() -> Path:
    return Path(os.environ["HERMES_HOME"])


def _auth_path() -> Path:
    return _hermes_home() / "auth.json"


def _seed_round_robin_store() -> Path:
    """A quiescent two-credential round-robin store under the sandboxed home.

    Quiescent on purpose: no entry carries a cooldown, so ``clear_expired`` has
    nothing to clear and no refresh is due. That is the field condition MCF-16
    describes — the ONLY thing that could move these bytes is the rotation.
    """
    home = _hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "credential_pool_strategies:\n"
        f"  {PROVIDER}: round_robin\n",
        encoding="utf-8",
    )
    auth_path = _auth_path()
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    PROVIDER: [_entry(0, MARKER_A), _entry(1, MARKER_B)]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return auth_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_pool_selections(monkeypatch) -> dict[str, int]:
    """Count both selection entry points on the REAL pool class.

    This is the anti-vacuity pin for gate 1. "The store did not move" is
    trivially true for a probe that never reaches a credential pool at all, so
    the byte assertion is only evidence when paired with a counted selection.
    """
    from agent.credential_pool import CredentialPool

    counts = {"persisting": 0, "non_persisting": 0}
    real_select = CredentialPool.select
    real_probe = CredentialPool.select_without_persisting_rotation

    def counted_select(self):
        counts["persisting"] += 1
        return real_select(self)

    def counted_probe(self):
        counts["non_persisting"] += 1
        return real_probe(self)

    monkeypatch.setattr(CredentialPool, "select", counted_select)
    monkeypatch.setattr(
        CredentialPool, "select_without_persisting_rotation", counted_probe
    )
    return counts


def _payload_digest_without_updated_at(payload: dict) -> str:
    """Digest of a store payload with ``updated_at`` removed.

    A digest rather than the payload itself so an assertion that fails cannot
    print store content.
    """
    body = {k: v for k, v in payload.items() if k != "updated_at"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _selected_id(entry) -> str:
    """Reduce a selection to its id BEFORE it can reach an assertion.

    ``PooledCredential``'s repr carries its token fields. Comparing entries
    directly would put those fields one failed assertion away from a test log,
    so every assertion in this module compares ids only.
    """
    assert entry is not None, "the pool returned no credential at all"
    return str(entry.id)


def _persona() -> AgentPersona:
    # ``hermes_profile`` unset ⇒ the binding carries no profile home, so the
    # readiness pass reads the ambient (sandboxed) home rather than diverting
    # into a per-profile one. Same code path, one less moving part.
    return AgentPersona(
        id="probe-persona",
        display_name="Probe",
        role="dev",
        model="deepseek-chat",
        provider=PROVIDER,
        api_mode=None,
        toolsets=[],
        system_prompt_path="personas/probe/system.md",
    )


@pytest.fixture(autouse=True)
def _clear_provider_issue_memo():
    from agent_runtime import profile_readiness

    profile_readiness._provider_issue_cache_clear()
    yield
    profile_readiness._provider_issue_cache_clear()


# ── Gate 1 ──────────────────────────────────────────────────────────────────


def test_readiness_pass_leaves_credential_store_byte_identical(monkeypatch):
    """A readiness pass performs NO write to the credential store.

    Killing mutation: restore the persisting selection in
    ``profile_readiness::_compute_provider_issue`` (drop the
    ``resolver = probe_runtime_provider`` line) — the round-robin cursor is
    written back and the digest moves.
    """
    from agent_runtime.profile_readiness import profile_readiness_for_persona

    auth_path = _seed_round_robin_store()
    counts = _count_pool_selections(monkeypatch)
    before = _digest(auth_path)

    profile_readiness_for_persona(_persona())

    # The guarantee first, so a regression reds on the CONSEQUENCE rather than
    # on the instrument.
    assert _digest(auth_path) == before, (
        "the readiness pass rewrote auth.json with no credential change — the "
        "build is perturbing an input it does not declare, so a REAL "
        "credential change on a quiescent store is served as a false cache HIT "
        f"(counts={counts})"
    )
    # ...then the instrument's own liveness. Killed by anything that stops the
    # readiness pass reaching a credential pool, which would make the digest
    # assertion above true for the wrong reason.
    assert counts == {"persisting": 0, "non_persisting": 1}, (
        "vacuous gate: the readiness pass did not take exactly one "
        f"non-persisting pool selection (counts={counts}), so 'the store did "
        "not move' proves nothing"
    )


# ── Gate 2 ──────────────────────────────────────────────────────────────────


def test_non_persisting_selection_still_rotates_in_memory():
    """The probe must not silently degrade round-robin to 'always the first'.

    Killing mutation: make ``select_without_persisting_rotation`` skip the
    rotation as well as the write (e.g. return ``available[0]`` unrotated) —
    both calls hand back the same id.
    """
    _seed_round_robin_store()
    from agent.credential_pool import load_pool

    pool = load_pool(PROVIDER)
    # Only the ids cross into the assertions: a ``PooledCredential`` repr
    # carries its token fields, and no assertion in this suite may be one
    # failure away from printing credential-shaped material.
    first = _selected_id(pool.select_without_persisting_rotation())
    second = _selected_id(pool.select_without_persisting_rotation())

    assert first != second, (
        "the non-persisting selection pinned one credential: rotation was "
        "suppressed along with the write, which is a behaviour regression, "
        "not a fix"
    )


def test_persisting_selection_still_rotates_across_a_fresh_pool_load():
    """Real consumers rotate — and the write-back is what makes that true.

    ``load_pool()`` re-reads from disk on every call, so round-robin's cursor
    IS the persisted priority order. Killing mutation: gate the round-robin
    ``self._persist()`` off unconditionally — the second load returns the same
    id as the first.
    """
    _seed_round_robin_store()
    from agent.credential_pool import load_pool

    first = _selected_id(load_pool(PROVIDER).select())
    second = _selected_id(load_pool(PROVIDER).select())

    assert first != second, (
        "a fresh load handed back the same credential: the rotation cursor is "
        "no longer reaching disk for consumers that legitimately rotate"
    )


# ── Gate 3 ──────────────────────────────────────────────────────────────────


def test_genuine_credential_state_change_still_persists():
    """The change-gated callers are untouched by this change.

    Killing mutation: make ``CredentialPool._persist`` a no-op — the reloaded
    entry comes back without its exhausted status.
    """
    _seed_round_robin_store()
    from agent.credential_pool import load_pool

    pool = load_pool(PROVIDER)
    selected_id = _selected_id(pool.select())
    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"reason": "rate_limited"},
        credential_id=selected_id,
    )

    reloaded = {
        entry.id: entry.last_status for entry in load_pool(PROVIDER).entries()
    }
    assert reloaded.get(selected_id) == "exhausted", (
        "a real credential state change did not reach disk: suppressing the "
        "rotation write also suppressed the writes that carry actual state"
    )


# ── Gate 4 ──────────────────────────────────────────────────────────────────


def test_a_real_write_still_stamps_a_fresh_updated_at():
    """``_save_auth_store`` is NOT change-only — that is a different change.

    Making the credential write boundary compare content (and skip when equal)
    would retire this class for every caller, but it belongs in its own change
    with its own mutation evidence. This gate exists so the two cannot merge
    silently: it reds the moment ``_save_auth_store`` grows a content
    comparison.

    Killing mutation: add a payload comparison (minus ``updated_at``) in
    ``_save_auth_store`` that returns early when equal — the second write
    leaves ``updated_at`` where it was.
    """
    _seed_round_robin_store()
    from hermes_cli.auth import write_credential_pool

    identical_entries = [_entry(0, MARKER_A), _entry(1, MARKER_B)]
    write_credential_pool(PROVIDER, identical_entries)
    first = json.loads(_auth_path().read_text(encoding="utf-8"))

    write_credential_pool(PROVIDER, identical_entries)
    second = json.loads(_auth_path().read_text(encoding="utf-8"))

    assert second["updated_at"] != first["updated_at"], (
        "two logically identical writes produced the same updated_at: "
        "_save_auth_store became change-only, which is a separate shape that "
        "must land with its own evidence, not as a side effect of this one"
    )
    # Compared as digests, not as dicts: a dict comparison that fails prints the
    # whole store, and no assertion in this module may put store content in a
    # test log even when the content is this module's own inert markers.
    assert _payload_digest_without_updated_at(
        second
    ) == _payload_digest_without_updated_at(first), (
        "the two writes were not logically identical, so this gate is not "
        "measuring what it claims"
    )
