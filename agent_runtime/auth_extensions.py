"""Fork-owned credential selection state and read-only Codex readiness.

Credentials and locking remain owned by hermes_cli.auth. This module owns only
selection sidecars and the downstream profile-readiness policy.
"""
from __future__ import annotations
import json
import logging
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from hermes_cli.auth_constants import AuthError
from hermes_constants import secure_parent_dir
from utils import atomic_replace

logger = logging.getLogger(__name__)
ROTATION_STATE_VERSION = 1

def _rotation_state_path() -> Path:
    """Sidecar path for pool selection state, beside the active auth store.

    Derived from ``auth._auth_file_path()`` on purpose: it inherits that function's
    pytest seat belt, so a test that forgot to sandbox ``HERMES_HOME`` cannot
    write rotation state into the real user's Hermes root either.
    """
    # Import at use time: auth re-exports these downstream helpers.
    from hermes_cli import auth

    return auth._auth_file_path().with_name("credential_rotation.json")


def _empty_rotation_state() -> Dict[str, Any]:
    return {"version": ROTATION_STATE_VERSION, "providers": {}}


def _load_rotation_state() -> Dict[str, Any]:
    """Read the rotation sidecar. Never raises; a damaged file starts empty.

    Rotation state is reconstructible by definition — losing it costs one
    provider one selection slot, never a credential — so an unreadable sidecar
    degrades to "start from the first entry" instead of failing a runtime
    resolution.
    """
    path = _rotation_state_path()
    if not path.exists():
        return _empty_rotation_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "auth: failed to parse %s (%s) — restarting credential rotation "
            "from the first entry",
            path, exc,
        )
        return _empty_rotation_state()
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        return _empty_rotation_state()
    return raw


def read_pool_rotation_state(provider_id: str) -> Dict[str, Any]:
    """Return one provider's persisted selection state as a plain dict.

    No global-root fallback, deliberately: a profile that borrows another
    store's CREDENTIALS still rotates on its own. Sharing the cursor would let
    one profile consume another's rotation slots, and the fallback store is
    read-only for this process anyway.
    """
    providers = _load_rotation_state().get("providers")
    if not isinstance(providers, dict):
        return {}
    slice_ = providers.get((provider_id or "").strip().lower())
    return dict(slice_) if isinstance(slice_, dict) else {}


def _write_rotation_state_file(store: Dict[str, Any]) -> Path:
    """Atomic 0600 write of the whole sidecar, mirroring ``_save_auth_store``."""
    path = _rotation_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)
    payload = json.dumps(store, indent=2) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return path


def write_pool_rotation_state(provider_id: str, state: Dict[str, Any]) -> Path:
    """Persist one provider's selection state, leaving auth.json untouched.

    Takes the auth store lock so rotation writes serialize against credential
    writes under the existing lock ordering rather than introducing a second
    one. An empty ``state`` drops the provider's slice, so a pool that stops
    rotating does not leave a row behind forever.
    """
    # Import at use time: auth re-exports these downstream helpers.
    from hermes_cli import auth

    key = (provider_id or "").strip().lower()
    with auth._auth_store_lock():
        store = _load_rotation_state()
        providers = store.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            store["providers"] = providers
        if state:
            providers[key] = dict(state)
        else:
            providers.pop(key, None)
        store["version"] = ROTATION_STATE_VERSION
        store["updated_at"] = datetime.now(timezone.utc).isoformat()
        return _write_rotation_state_file(store)


def _read_global_codex_tokens_if_usable() -> Dict[str, Any] | None:
    """Return usable global-root Codex singleton tokens for profile fallback."""
    # Import at use time: auth re-exports these downstream helpers.
    from hermes_cli import auth

    try:
        global_store = auth._load_global_auth_store()
        providers = global_store.get("providers") if isinstance(global_store, dict) else None
        state = providers.get("openai-codex") if isinstance(providers, dict) else None
        tokens = state.get("tokens") if isinstance(state, dict) else None
        if not isinstance(tokens, dict):
            return None
        access_token = str(tokens.get("access_token", "") or "").strip()
        refresh_token = str(tokens.get("refresh_token", "") or "").strip()
        if not access_token or not refresh_token:
            return None
        return {"access_token": access_token, "refresh_token": refresh_token, "last_refresh": state.get("last_refresh")}
    except Exception:
        logger.debug("Global Codex singleton fallback lookup failed", exc_info=True)
        return None


def codex_auth_store_credentials_present() -> bool:
    """Would :func:`resolve_codex_runtime_credentials` find a credential to serve?

    A READ-ONLY mirror of that function's credential SOURCES, in its order,
    minus every branch that spends something: no token refresh, no network
    probe of the Codex usage endpoint, no CLI import, no write to ``auth.json``
    and no clearing of a pool cooldown. It answers only "is there a credential
    here that the run path would hand to a turn", which is the question a
    readiness pass is entitled to ask.

    Why it exists as its OWN function rather than as a keyword on the resolver:
    the resolver's write/network branches are reached exactly when the
    singleton is unusable — the case readiness cares most about — so
    ``refresh_if_expiring=False`` is not sufficient to make it a read, and a
    reader would have to prove that from three nested branches rather than
    from a name.

    An access token past its expiry with a refresh token beside it counts as
    PRESENT: the run path refreshes it and serves the turn, so reporting
    attention for it would be reporting a condition the runtime does not have.
    Whether that refresh succeeds is only knowable over the network, which this
    function is defined not to do.
    """

    # Import at use time: auth re-exports these downstream helpers.
    from hermes_cli import auth

    try:
        auth._read_codex_tokens()
        return True
    except AuthError:
        pass
    except Exception:
        logger.debug("Codex singleton readiness read failed", exc_info=True)
        return False

    # The resolver's global-root singleton fallback. Its OTHER fallback —
    # ``_pool_codex_access_token`` — is deliberately NOT mirrored here: it is a
    # second, looser read of the same credential pool the readiness caller has
    # already asked with the pool's own availability rules, and it accepts an
    # entry that those rules just refused (it consults only
    # ``last_error_reset_at``, never the ``last_status`` cooldown). Mirroring it
    # would mean readiness could never report attention while any pool row held
    # any token string, including a long-dead one — which would retire the true
    # positive along with the false one. Where the two pool reads disagree,
    # readiness follows the stricter, and says so rather than leaving a reader
    # to discover the divergence.
    return bool(_read_global_codex_tokens_if_usable())

