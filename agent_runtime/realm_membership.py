"""Stage 43 — backend-authoritative realm sync membership + credential intake.

Fills the ``RealmMembershipProvider`` seam in ``realm_sync.py`` with the
Eternia backend permission route. Server-bound realms authorize every sync
action against ``GET {api_base}/realms/{realm_id}/sync/permission`` using a
launcher-brokered short-TTL credential; any transport, contract, or
credential failure FAILS CLOSED. Server-less realms (``server_id is None``)
keep the local allow stub and are byte-identical to pre-Stage-43 behavior.

Credential material (api_token / git_authorization) must never appear in
envelopes, events, safe_details, or logs — only expiry metadata is safe.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_time import now

from .errors import NotFound
from .events import EventLog
from .models import Event, Realm
from .realm_sync import MembershipDecision, RealmMembershipProvider, RealmSyncError
from .store import RealmStore, WorkspaceStore

CREDENTIAL_ENV_VAR = "HERMES_REALM_SYNC_CREDENTIAL"
CREDENTIAL_SCHEMA_VERSION = 1
SYNC_ACTIONS = frozenset({"pull", "publish", "status"})
_REQUIRED_TEXT_FIELDS = ("realm_id", "api_base", "api_token", "git_url", "git_authorization")
_DENY_CODES = {"membership_denied", "role_insufficient", "sync_auth_failed", "invalid_request"}
_HTTP_TIMEOUT_SECONDS = 10.0


def _user_agent() -> str:
    """Explicit UA for every realm-sync backend request. The stdlib
    default (``Python-urllib/3.x``) is bot-blocked by the Cloudflare
    edge in front of the production API — those requests die as bare
    403s that used to masquerade as ``membership_denied``."""
    try:
        from hermes_cli import __version__ as version
    except Exception:  # noqa: BLE001 — UA must never break a sync call
        version = "unknown"
    return f"Hermes-Agent/{version} (realm-sync)"


@dataclass(frozen=True, slots=True)
class RealmSyncCredential:
    """Parsed launcher-brokered sync credential (contract frozen, schema v1)."""

    schema_version: int
    realm_id: str
    api_base: str
    api_token: str
    git_url: str
    git_authorization: str
    expires_at: datetime

    @classmethod
    def parse(cls, raw: Any) -> "RealmSyncCredential":
        if not isinstance(raw, dict):
            raise RealmSyncError("sync_auth_failed", "Realm sync credential must be a JSON object.")
        if raw.get("schema_version") != CREDENTIAL_SCHEMA_VERSION:
            raise RealmSyncError("sync_auth_failed", "Realm sync credential has an unsupported schema_version.")
        values: dict[str, str] = {}
        for field_name in _REQUIRED_TEXT_FIELDS:
            text = str(raw.get(field_name) or "").strip()
            if not text:
                raise RealmSyncError("sync_auth_failed", f"Realm sync credential is missing required field: {field_name}.")
            values[field_name] = text
        expires_at = _parse_expiry(raw.get("expires_at"))
        credential = cls(schema_version=CREDENTIAL_SCHEMA_VERSION, expires_at=expires_at, **values)
        if credential.is_expired():
            raise RealmSyncError(
                "sync_auth_failed",
                "Realm sync credential is expired; request a fresh credential from the launcher.",
                safe_details={"expires_at": expires_at.isoformat()},
            )
        return credential

    @classmethod
    def load(cls, path: Path | str) -> "RealmSyncCredential":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RealmSyncError("sync_auth_failed", "Realm sync credential file could not be read.") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise RealmSyncError("sync_auth_failed", "Realm sync credential file is not valid JSON.") from exc
        return cls.parse(raw)

    def is_expired(self) -> bool:
        return now().astimezone(timezone.utc) >= self.expires_at

    def git_extra_config(self) -> list[str]:
        """Per-invocation git config — never written to .git/config, never logged."""
        return [f"http.extraHeader=Authorization: {self.git_authorization}"]

    def bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}", "Accept": "application/json"}


def load_realm_sync_credential(credential_file: str | None = None) -> RealmSyncCredential | None:
    """Resolve a credential from an explicit path or the env fallback.

    Explicit ``--credential-file`` wins over ``HERMES_REALM_SYNC_CREDENTIAL``.
    Returns ``None`` when neither is set; a set-but-invalid credential raises
    ``sync_auth_failed`` (fail closed — never silently degrade to the stub).
    """
    path = str(credential_file or "").strip() or os.environ.get(CREDENTIAL_ENV_VAR, "").strip()
    if not path:
        return None
    return RealmSyncCredential.load(path)


def select_membership_provider(realm: Realm, credential: RealmSyncCredential | None) -> RealmMembershipProvider:
    """Stage 43 provider selection (Decision 4 / spec C1).

    - ``server_id is None``  → local allow stub (pre-Stage-43 behavior, unchanged).
    - server-bound + credential → backend-authoritative provider.
    - server-bound + no credential → fail closed (never the allow stub).
    """
    if not realm.server_id:
        return RealmMembershipProvider()
    if credential is None:
        raise RealmSyncError(
            "sync_auth_failed",
            "Server-bound realms require a launcher-brokered sync credential; pass --credential-file or set HERMES_REALM_SYNC_CREDENTIAL.",
        )
    return BackendRealmMembershipProvider(credential)


class BackendRealmMembershipProvider(RealmMembershipProvider):
    """Backend-authoritative sync authorization via the Stage 43 permission route."""

    def __init__(self, credential: RealmSyncCredential):
        self._credential = credential

    def authorize(self, realm: Realm, action: str) -> MembershipDecision:
        if action not in SYNC_ACTIONS:
            return MembershipDecision(False, "invalid_request", f"unsupported sync action: {action}")
        credential = self._credential
        if credential.realm_id != realm.id:
            return MembershipDecision(False, "sync_auth_failed", "Realm sync credential was issued for a different realm.")
        if credential.is_expired():
            return MembershipDecision(False, "sync_auth_failed", "Realm sync credential is expired; request a fresh credential from the launcher.")
        url = (
            f"{credential.api_base.rstrip('/')}/realms/{urllib.parse.quote(realm.id, safe='')}"
            f"/sync/permission?action={urllib.parse.quote(action, safe='')}"
        )
        try:
            status, payload = _request_json("GET", url, headers=credential.bearer_headers())
        except RealmSyncError:
            return MembershipDecision(
                False,
                "sync_remote_unreachable",
                f"Realm sync authorization backend is unreachable; denying {action} (fail closed).",
            )
        return _decision_from_response(status, payload, action=action)


def adopt_realms(credential: RealmSyncCredential, *, server_id: str | None = None, dry_run: bool = False) -> list[Realm]:
    """Fetch granted realms from the backend and upsert local Realm records.

    Idempotent: an unchanged realm is not rewritten; existing ``workspace_ids``
    are always preserved on re-adopt. ``sync_manifest_ref`` is set from the
    backend-provided ``git_url`` so ``_ensure_sync_repo`` clones the brokered
    remote on first sync.
    """
    url = f"{credential.api_base.rstrip('/')}/realms"
    status, payload = _request_json("GET", url, headers=credential.bearer_headers())
    if status in {401, 403}:
        decision = _decision_from_response(status, payload, action="adopt")
        raise RealmSyncError(decision.code or "membership_denied", decision.message or "Realm adopt was denied by the backend.")
    if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RealmSyncError(
            "sync_remote_unreachable",
            "Realm adopt received an unexpected backend response.",
            retryable=True,
            safe_details={"status": status},
        )
    store = RealmStore()
    adopted: list[Realm] = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        realm_id = str(item.get("id") or "").strip()
        item_server_id = str(item.get("server_id") or "").strip() or None
        if not realm_id or not item_server_id:
            continue
        if server_id and item_server_id != server_id:
            continue
        git_url = str(item.get("git_url") or "").strip()
        if not git_url and realm_id == credential.realm_id:
            git_url = credential.git_url
        adopted.append(
            _upsert_realm(
                store,
                realm_id=realm_id,
                server_id=item_server_id,
                name=str(item.get("name") or "").strip() or realm_id,
                slug=str(item.get("slug") or "").strip(),
                git_url=git_url,
                default_workspace_id=str(item.get("default_workspace_id") or "").strip() or None,
                default_workspace_name=str(item.get("default_workspace_name") or "Default").strip() or "Default",
                default_workspace_version=_nonnegative_int(item.get("default_workspace_version")),
                dry_run=dry_run,
            )
        )
    if not dry_run:
        for realm in adopted:
            _append_realm_adopted_event(realm)
    return adopted


def _append_realm_adopted_event(realm: Realm) -> None:
    """Advance the EventLog watermark so stream/read-model consumers see
    the store mutation (event-less writes are invisible to them). Best
    effort: a broken event log must never fail the adopt itself."""
    try:
        EventLog().append(
            Event(
                now(),
                "realm.adopted",
                None,
                None,
                None,
                {
                    "realm_id": realm.id,
                    "name": realm.name,
                    "server_id": realm.server_id,
                    "default_workspace_id": realm.default_workspace_id,
                    "default_workspace_version": realm.default_workspace_version,
                },
            )
        )
    except Exception:  # noqa: BLE001 — evidence channel, not the mutation
        pass


def notify_realm_published(credential: RealmSyncCredential, realm_id: str, *, commit: str, artifact_counts: dict[str, int]) -> None:
    """POST the counts-only publish notification (spec C4). Raises on any failure;
    the caller downgrades that to a ``warnings[]`` entry — never a publish failure."""
    url = f"{credential.api_base.rstrip('/')}/realms/{urllib.parse.quote(realm_id, safe='')}/sync/published"
    body = {"commit": str(commit), "artifact_counts": {str(kind): int(count) for kind, count in (artifact_counts or {}).items()}}
    status, _payload = _request_json("POST", url, headers=credential.bearer_headers(), body=body)
    if status not in {200, 204}:
        raise RealmSyncError(
            "sync_remote_unreachable",
            "Realm publish notification was rejected by the backend.",
            retryable=True,
            safe_details={"status": status},
        )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _upsert_realm(
    store: RealmStore,
    *,
    realm_id: str,
    server_id: str,
    name: str,
    slug: str,
    git_url: str,
    default_workspace_id: str | None,
    default_workspace_name: str,
    default_workspace_version: int,
    dry_run: bool,
) -> Realm:
    workspace_store = WorkspaceStore()
    existing_default = None
    if default_workspace_id:
        try:
            existing_default = workspace_store.get(default_workspace_id)
        except NotFound:
            pass
        if existing_default is not None and existing_default.realm_id not in {None, realm_id}:
            raise RealmSyncError(
                "sync_conflict",
                "Realm default workspace identity is already owned by another realm; refusing to move it.",
                safe_details={"workspace_id": default_workspace_id, "realm_id": realm_id},
            )
    try:
        item = store.get(realm_id)
    except NotFound:
        item = None
    if item is None:
        if dry_run:
            ts = now()
            return Realm(
                id=realm_id,
                slug=slug or realm_id,
                name=name,
                created_at=ts,
                updated_at=ts,
                server_id=server_id,
                default_workspace_id=default_workspace_id,
                default_workspace_name=default_workspace_name,
                default_workspace_version=default_workspace_version,
                workspace_ids=[default_workspace_id] if default_workspace_id else [],
                sync_manifest_ref=git_url or None,
            )
        item = store.create(
            name=name,
            server_id=server_id,
            realm_id=realm_id,
            default_workspace_id=default_workspace_id,
            default_workspace_name=default_workspace_name,
            default_workspace_version=default_workspace_version,
        )
    changed = False
    if name and item.name != name:
        item.name = name
        changed = True
    if slug and item.slug != slug:
        item.slug = slug
        changed = True
    if item.server_id != server_id:
        item.server_id = server_id
        changed = True
    if git_url and (item.sync_manifest_ref or "") != git_url:
        item.sync_manifest_ref = git_url
        changed = True
    if item.default_workspace_id != default_workspace_id:
        item.default_workspace_id = default_workspace_id
        changed = True
    if item.default_workspace_name != default_workspace_name:
        item.default_workspace_name = default_workspace_name
        changed = True
    if item.default_workspace_version != default_workspace_version:
        item.default_workspace_version = default_workspace_version
        changed = True
    if default_workspace_id and default_workspace_id not in item.workspace_ids:
        item.workspace_ids.append(default_workspace_id)
        changed = True
    if default_workspace_id and not dry_run:
        if existing_default is None:
            workspace_store.create(
                name=default_workspace_name,
                realm_id=realm_id,
                workspace_id=default_workspace_id,
            )
        elif existing_default.realm_id is None:
            existing_default.realm_id = realm_id
            workspace_store.save(existing_default)
    if changed and not dry_run:
        # emit_event=False: adopt_realms appends its own richer realm.adopted
        # event for this same mutation; a generic realm.updated would be a
        # duplicate watermark bump (Stage 12).
        item = store.save(item, emit_event=False)  # workspace_ids untouched — preserved on re-adopt
    return item


def _decision_from_response(status: int, payload: Any, *, action: str) -> MembershipDecision:
    body = payload if isinstance(payload, dict) else {}
    message = str(body.get("message") or "").strip()
    code = str(body.get("code") or "").strip()
    # Django Ninja HttpError bodies carry the deny code as {"detail": …}
    # (no code/message keys) — honor it so real backend reasons
    # (role_insufficient, …) survive instead of collapsing to the
    # generic membership_denied string.
    detail = str(body.get("detail") or "").strip()
    if detail:
        if not code and detail in _DENY_CODES:
            code = detail
        elif not message:
            message = detail
    if status == 200 and "allowed" in body:
        if bool(body.get("allowed")):
            return MembershipDecision(True, code or None, message)
        return MembershipDecision(False, code if code in _DENY_CODES else "membership_denied", message or f"Realm membership does not allow {action}.")
    if status in {401, 403}:
        return MembershipDecision(
            False,
            code if code in _DENY_CODES else "membership_denied",
            message or f"Realm sync {action} was denied by the backend.",
        )
    # Anything else is a contract/transport failure — fail closed.
    return MembershipDecision(
        False,
        "sync_remote_unreachable",
        f"Realm sync authorization returned an unexpected response; denying {action} (fail closed).",
    )


def _parse_expiry(raw: Any) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise RealmSyncError("sync_auth_failed", "Realm sync credential is missing required field: expires_at.")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RealmSyncError("sync_auth_failed", "Realm sync credential has an invalid expires_at timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Minimal stdlib JSON transport (no new dependencies). Returns (status, parsed_body).

    HTTP error statuses are returned for decision mapping; transport-level
    failures raise ``sync_remote_unreachable`` so every caller fails closed.
    """
    data = None
    request_headers = dict(headers or {})
    request_headers.setdefault("User-Agent", _user_agent())
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - api_base comes from the brokered credential
            return int(getattr(response, "status", 0) or 0), _parse_json_bytes(response.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return int(exc.code), _parse_json_bytes(raw)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RealmSyncError("sync_remote_unreachable", "Realm sync backend request failed.", retryable=True) from exc


def _parse_json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
