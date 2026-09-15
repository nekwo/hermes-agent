"""Redacted worker-crash evidence and surviving-sidecar discovery."""
from __future__ import annotations
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional
from hermes_cli import kanban_db as _kb

_CRASH_TAIL_BYTES = 8 * 1024

_CRASH_SIDECAR_MAX = 8

_CRASH_SIDECAR_FILE_MAX_BYTES = 32 * 1024

_CRASH_LOG_GROWING_WINDOW_SECONDS = 120

_REDACTION_MARK = "[REDACTED]"

def _build_redaction_patterns() -> "list[tuple[Any, str]]":
    """Compile the redaction regex table once at import time.

    Patterns cover (in order):
      * ``Authorization: Bearer <token>`` / ``Authorization: Basic <token>``
        and bare ``Bearer <token>`` occurrences in log lines.
      * Standalone JWTs (``eyJ...`` three-part dotted base64url tokens).
      * Common header-named API key carriers:
        ``X-Api-Key``, ``api-key``, ``apikey``, ``x-auth-token``,
        ``proxy-authorization``.
      * Set-Cookie / Cookie header values.
      * S3 / signed-URL query params: ``X-Amz-*``, ``Signature``, ``Expires``
        (the expiry doubles as a freshness oracle for the signature).
      * JSON-ish ``"api_key": "..."``, ``"token": "..."``, ``"secret": "..."``,
        ``"password": "..."`` value runs.
      * Common credential CLI args: ``--token=...``, ``--password=...``.
    """
    import re as _re
    pats: list[tuple[Any, str]] = []

    # Authorization headers + bare Bearer references.
    pats.append((
        _re.compile(
            r"(?i)(Authorization\s*:\s*)(Bearer|Basic|Digest|Token)\s+"
            r"[A-Za-z0-9._\-+/=~]+"
        ),
        rf"\1\2 {_REDACTION_MARK}",
    ))
    pats.append((
        _re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=~]{8,}"),
        f"Bearer {_REDACTION_MARK}",
    ))

    # Standalone JWTs (header.payload.signature, base64url).
    pats.append((
        _re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        _REDACTION_MARK,
    ))

    # Header-named secret carriers.
    pats.append((
        _re.compile(
            r"(?im)^("
            r"(?:x[-_])?(?:api[-_]?key|auth[-_]?token|session[-_]?token)"
            r"|proxy-authorization|set-cookie|cookie"
            r")\s*:\s*.+$"
        ),
        rf"\1: {_REDACTION_MARK}",
    ))

    # S3 / signed-URL query params (case-insensitive). Both ``X-Amz-Signature``
    # and the generic ``Signature`` get redacted, plus ``Expires`` because the
    # epoch ts is enough to bound a leaked signature's freshness window.
    pats.append((
        _re.compile(
            r"(?i)(X-Amz-(?:Signature|Credential|Security-Token|Date|SignedHeaders|Algorithm))="
            r"[^&\s'\"]+"
        ),
        rf"\1={_REDACTION_MARK}",
    ))
    pats.append((
        _re.compile(r"(?i)(\b(?:Signature|Expires))=[^&\s'\"]+"),
        rf"\1={_REDACTION_MARK}",
    ))

    # JSON-ish credential fields.
    pats.append((
        _re.compile(
            r"(?i)([\"'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"id[_-]?token|token|secret|password|client[_-]?secret)[\"']\s*:\s*)"
            r"[\"'][^\"']+[\"']"
        ),
        rf"\1\"{_REDACTION_MARK}\"",
    ))

    # CLI args: --token=foo, --password=bar, --secret=baz.
    pats.append((
        _re.compile(
            r"(?i)(--(?:token|password|secret|api[-_]?key)=)\S+"
        ),
        rf"\1{_REDACTION_MARK}",
    ))

    # ``sk-live-...`` / ``sk-test-...`` / ``sk-...`` style raw secret keys
    # (OpenAI, Stripe, many SaaS providers). Matches the prefix + ≥12 chars.
    pats.append((
        _re.compile(r"\b(?:sk|pk|rk|tok)[_-](?:live|test|prod)[_-][A-Za-z0-9]{8,}"),
        _REDACTION_MARK,
    ))
    pats.append((
        _re.compile(r"\b(?:sk|pk|rk|tok)-[A-Za-z0-9_\-]{16,}"),
        _REDACTION_MARK,
    ))

    return pats

_REDACTION_PATTERNS = _build_redaction_patterns()

def _redact_secrets(text: str) -> str:
    """Strip Bearer / JWT / signed-URL / cookie / API-key style secrets.

    Returns ``text`` with every match replaced by ``[REDACTED]`` (or the
    pattern-specific surrounding context preserved + value redacted). Best-
    effort: the goal is to keep crash artifacts safe-to-share, not to be a
    DLP solution. New patterns are cheap to add to
    :func:`_build_redaction_patterns`.
    """
    if not text:
        return text
    out = text
    for pat, repl in _REDACTION_PATTERNS:
        try:
            out = pat.sub(repl, out)
        except Exception:
            # A misbehaving pattern must never crash crash-reporting itself.
            continue
    return out

def _tail_bytes_redacted(
    path: Path, *, limit: int = _CRASH_TAIL_BYTES,
) -> Optional[str]:
    """Return the last ``limit`` bytes of ``path`` as UTF-8, redacted.

    Returns None if the file does not exist or cannot be read. Skips a
    partial leading line so the result starts cleanly (unless the window
    has no newline at all). Decode errors fall back to ``replace`` so an
    arbitrary binary blob still yields *something* the operator can scan.
    """
    try:
        if not path.exists():
            return None
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
    except OSError:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    return _redact_secrets(text)

def _candidate_sidecar_dirs(workspace: Path) -> list[Path]:
    """Locations a worker is expected to drop sidecar pid manifests.

    Kept tiny and deterministic — sidecar discovery is a hint, not a
    contract. We look at the canonical ``<workspace>/.hermes/sidecars/``
    plus a couple of common alternates so workers/skills can use the
    convention they already follow without coordination.
    """
    return [
        workspace / ".hermes" / "sidecars",
        workspace / ".sidecars",
        workspace / "sidecars",
    ]

def _parse_sidecar_manifest(path: Path) -> Optional[dict]:
    """Parse a sidecar pid file. Returns None on any error.

    Accepts:
      * ``*.json`` — dict with at minimum ``pid`` and optionally ``cmd``,
        ``cwd``, ``log_path`` / ``log_paths``, ``name``.
      * Any other file — first non-blank line is parsed as an int pid.
    """
    try:
        if not path.is_file():
            return None
        st = path.stat()
        if st.st_size > _CRASH_SIDECAR_FILE_MAX_BYTES:
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name = path.stem
    if path.suffix.lower() == ".json":
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        try:
            pid = int(doc.get("pid"))
        except (TypeError, ValueError):
            return None
        cmd = doc.get("cmd") or doc.get("argv")
        if isinstance(cmd, str):
            cmd_out = [cmd]
        elif isinstance(cmd, list):
            cmd_out = [str(x) for x in cmd]
        else:
            cmd_out = None
        log_paths: list[str] = []
        single = doc.get("log_path") or doc.get("log")
        if isinstance(single, str) and single.strip():
            log_paths.append(single)
        multi = doc.get("log_paths") or doc.get("logs")
        if isinstance(multi, list):
            for entry in multi:
                if isinstance(entry, str) and entry.strip():
                    log_paths.append(entry)
        return {
            "name": str(doc.get("name") or name),
            "pid": pid,
            "cmd": cmd_out,
            "cwd": (str(doc["cwd"]) if isinstance(doc.get("cwd"), str) else None),
            "log_paths": log_paths,
            "source": str(path),
        }
    # Plain pid file.
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            pid = int(s)
        except ValueError:
            return None
        return {
            "name": name,
            "pid": pid,
            "cmd": None,
            "cwd": None,
            "log_paths": [],
            "source": str(path),
        }
    return None

def _discover_sidecars(workspace_path: Optional[str]) -> list[dict]:
    """Return at most ``_CRASH_SIDECAR_MAX`` sidecar entries for a workspace.

    Each entry is enriched with ``alive`` (current liveness probe via
    :func:`_pid_alive`) and a redacted, bounded ``log_tail`` for the first
    log file the manifest declares. Errors are swallowed — discovery must
    never raise back into the dispatcher.
    """
    if not workspace_path:
        return []
    try:
        ws = Path(workspace_path)
    except TypeError:
        return []
    out: list[dict] = []
    for d in _candidate_sidecar_dirs(ws):
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if len(out) >= _CRASH_SIDECAR_MAX:
                break
            parsed = _parse_sidecar_manifest(entry)
            if parsed is None:
                continue
            try:
                parsed["alive"] = _kb._pid_alive(parsed["pid"])
            except Exception:
                parsed["alive"] = False
            log_path = parsed["log_paths"][0] if parsed["log_paths"] else None
            if log_path:
                try:
                    parsed["log_path"] = log_path
                    parsed["log_tail"] = _tail_bytes_redacted(Path(log_path)) or ""
                except Exception:
                    parsed["log_tail"] = ""
            else:
                parsed["log_path"] = None
                parsed["log_tail"] = ""
            out.append(parsed)
        if len(out) >= _CRASH_SIDECAR_MAX:
            break
    return out

def _crashes_dir(board: Optional[str] = None) -> Path:
    """Return the durable crash-artifact directory for ``board``.

    Lives next to the per-task worker logs at ``<board logs>/crashes/`` so
    one ``hermes kanban gc`` sweep can age both off together. Created on
    demand; never raises if creation fails (we'll just fail to write the
    artifact and surface that as ``evidence_path = None``).
    """
    return _kb.worker_logs_dir(board=board) / "crashes"

def _build_crash_artifact(
    *,
    task_id: str,
    profile: Optional[str],
    board: Optional[str],
    run_id: Optional[int],
    worker_pid: int,
    claim_lock: Optional[str],
    workspace_path: Optional[str],
    exit_kind: str,
    exit_code: Optional[int],
    error_text: str,
    event_kind: str,
    worker_log_path: Optional[Path],
) -> dict:
    """Assemble the deterministic crash-evidence payload.

    All free-text fields are passed through :func:`_redact_secrets`.
    Returned dict is JSON-serialisable and intentionally has stable key
    ordering: ``json.dumps(..., sort_keys=True)`` produces byte-identical
    output for the same inputs, which makes operator diffs sane and lets
    future reconciliation tooling content-hash artifacts.
    """
    now = time.time()
    sidecars = _discover_sidecars(workspace_path)
    alive_sidecars = [s for s in sidecars if s.get("alive")]
    # Classification:
    #   * "supervisor_lost_child" — the supervisor pid is dead but at
    #     least one detached child it tracked is still running. Recovery
    #     can attach to that child (or wait for it) rather than treating
    #     the task as a clean failure.
    #   * "process_failed" — everything's dead, this is a real crash.
    classification = (
        "supervisor_lost_child" if alive_sidecars else "process_failed"
    )
    worker_log_str = str(worker_log_path) if worker_log_path else None
    worker_log_tail = (
        _tail_bytes_redacted(worker_log_path) if worker_log_path else None
    ) or ""
    log_growing = False
    if worker_log_path:
        try:
            mtime = worker_log_path.stat().st_mtime
            log_growing = (now - mtime) < _CRASH_LOG_GROWING_WINDOW_SECONDS
        except OSError:
            log_growing = False
    return {
        "schema_version": 1,
        "kind": "kanban_worker_crash",
        "classification": classification,
        "event_kind": event_kind,
        "task_id": task_id,
        "profile": profile,
        "board": board,
        "run_id": run_id,
        "worker_pid": int(worker_pid),
        "claimer": claim_lock,
        "exit_kind": exit_kind,
        "exit_code": exit_code,
        "error": _redact_secrets(error_text or "")[:1000],
        "workspace_path": workspace_path,
        "worker_log_path": worker_log_str,
        "worker_log_tail": worker_log_tail,
        "worker_log_growing": bool(log_growing),
        "sidecars": sidecars,
        "alive_sidecar_pids": [int(s["pid"]) for s in alive_sidecars],
        "captured_at_epoch": int(now),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }

def _write_crash_artifact(
    artifact: dict,
    *,
    board: Optional[str] = None,
) -> Optional[Path]:
    """Persist ``artifact`` as JSON. Returns the path, or None on failure.

    The filename is ``<task>-<epoch>-<rand4>.json`` so multiple crashes on
    the same task in the same second don't collide. The ``rand4`` token
    is from :mod:`secrets` so a malicious co-tenant can't predict-and-
    overwrite a peer's artifact (defense in depth; the directory is
    user-owned anyway). Writes via a temp file + ``rename`` so a partial
    write never leaves a corrupt JSON file behind.
    """
    try:
        d = _crashes_dir(board=board)
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    task_id = str(artifact.get("task_id") or "unknown")
    epoch = int(artifact.get("captured_at_epoch") or time.time())
    fname = f"{task_id}-{epoch}-{secrets.token_hex(2)}.json"
    final = d / fname
    tmp = d / f".{fname}.tmp"
    try:
        tmp.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, final)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return final

def _capture_crash_artifact(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    worker_pid: int,
    claim_lock: Optional[str],
    exit_kind: str,
    exit_code: Optional[int],
    error_text: str,
    event_kind: str,
) -> Optional[dict]:
    """Build + persist a crash artifact and return ``{path, classification,
    sidecar_pids}`` (or None on failure).

    Reads task metadata via a short read query so it can be called from
    inside the same write-txn that mutates the task row, but the file I/O
    itself happens after the txn closes (caller responsibility — see
    :func:`detect_crashed_workers`).
    """
    row = conn.execute(
        "SELECT t.id, t.assignee, t.workspace_path, t.current_run_id "
        "FROM tasks t WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    profile = row["assignee"]
    workspace_path = row["workspace_path"]
    run_id = (
        int(row["current_run_id"]) if row["current_run_id"] is not None else None
    )
    board_slug = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    if board_slug:
        try:
            board_slug = _kb._normalize_board_slug(board_slug) or board_slug
        except ValueError:
            board_slug = None
    try:
        log_path = _kb.worker_log_path(task_id, board=board_slug)
    except Exception:
        log_path = None
    artifact = _build_crash_artifact(
        task_id=task_id,
        profile=profile,
        board=board_slug,
        run_id=run_id,
        worker_pid=worker_pid,
        claim_lock=claim_lock,
        workspace_path=workspace_path,
        exit_kind=exit_kind,
        exit_code=exit_code,
        error_text=error_text,
        event_kind=event_kind,
        worker_log_path=log_path,
    )
    path = _write_crash_artifact(artifact, board=board_slug)
    if path is None:
        return None
    return {
        "path": str(path),
        "classification": artifact["classification"],
        "alive_sidecar_pids": artifact["alive_sidecar_pids"],
        "run_id": run_id,
    }
