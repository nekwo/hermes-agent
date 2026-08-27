"""Per-root install identity for the remote gateway. Minted at boot; presented,
never proven.

What this is, and what it is NOT
-------------------------------

``<store_root>/gateway/install.json`` names ONE runtime root so a human — and,
from Stage 4, an install picker on a phone — can tell two installs apart:

    {"install_id": "<uuid4>", "display_name": "workstation",
     "created_at": "2026-08-27T12:00:00.000000+00:00"}

**Nothing in this file is secret.** The id travels on the ``ready`` / ``hello_ok``
/ ``version`` greeting frames in the clear, and it authenticates nobody: proving
you may talk to this runtime is ``serve_auth``'s job today and the device/peer
credential tiers' job from Stage 1 (see
``docs/agent-runtime-harness/planned/remote-gateway.md``). An id that both names
and authorises is the shape where "I know your install id" becomes "I am you";
this module exists to keep those two facts apart from the first commit.

Why a THIRD install id (the Stage 0 inventory decision)
-------------------------------------------------------

hermes already ships two things called ``install_id``. Neither can carry this
one, and the argument is recorded here rather than in a commit message so a
future duplicate-implementation audit (``planned/duplicate-implementation-
retirement.md``) reads a decision instead of an accident:

* ``monitoring.install_id`` (``agent/monitoring/policy.py::ensure_install_id``)
  is a **config-backed, deliberately ROTATABLE pseudonym** — its own docstring
  says clearing the config key rotates it on the next gateway start — consumed
  as OTel ``service.instance.id`` by ``gateway_health_export.py`` and
  ``otlp_exporter.py``. Rotatability is the feature there and a **lockout** here:
  a device paired against install X finds install X gone the day an operator
  clears a monitoring key. It is also scoped to a ``config.yaml`` under a HERMES
  **home**, while a gateway addresses a **store root** — and those are provably
  different scopes on this machine (the launcher's serve spawns with
  ``HERMES_HOME=profiles/base`` against the shared ``agent-runtime`` root, so one
  monitoring id would span several roots and several roots would share one id).
  It carries no display name and is documented as "carries no account identity",
  i.e. as a thing NOT to show a human.
* The telemetry ``install_id`` (``hermes_cli/observability/shared_metrics.py``)
  is a random UUID in the shared-metrics sqlite ``telemetry_state`` table, minted
  to aggregate anonymous counters. Putting it on a wire frame would convert an
  anonymity primitive into a network address — the exact inversion telemetry ids
  are supposed to prevent.

So: **DISTINCT, on scope (store root, not home/telemetry db), on lifetime
(never rotates — rotation is a lockout, the same rule ``serve_auth`` states),
and on audience (operator-presentable, with a name a human chose).** What is
deliberately NOT duplicated is the mechanism: the mint-iff-absent, root-is-an-
input, never-raises contract below is ``serve_auth.py``'s, restated for a
non-secret.

Contract
--------

* **The root is an INPUT.** Every function takes ``store_root``; this module
  never resolves it and never reads ``HERMES_HOME``. Multiple runtime roots
  coexist (QA lanes, worktree roots) and each is its own install.
* **Mint iff absent.** An existing record's ``install_id`` is never rewritten —
  only ``display_name`` is, and only through :func:`set_display_name`.
* **Never raises.** A runtime that cannot mint must still boot and SAY so: the
  typed ``state`` (``loaded`` / ``minted`` / ``error:<reason>``) is the
  observability, on the same "stated either way, never inferred from absence"
  rule the ``auth`` and ``socket`` blocks on ``ready`` already follow.

Fingerprints
------------

``gateway/`` MUST NOT be added to serve's ``_FINGERPRINT_ROOT_FILES`` /
``_FINGERPRINT_STORE_DIRS`` or to ``stream._scope_fingerprint``: the directory
APPEARS at first boot, which inside a fingerprint would cold the read-model
cache exactly while a fresh runtime warms up and make the stream emit
``state.reconciled`` on every restart. Same standing precedent as
``serve_instances/``, ``serve_auth_token`` and
``dispatch_delivery.DRAIN_STATE_FILENAME``. Both lists are explicit
allow-lists, so this is a rule to keep rather than a change to make.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "GATEWAY_DIRNAME",
    "INSTALL_RECORD_FILENAME",
    "DISPLAY_NAME_MAX_CHARS",
    "InstallIdentity",
    "clean_display_name",
    "default_display_name",
    "ensure_install_identity",
    "gateway_dir",
    "install_record_path",
    "read_install_identity",
    "set_display_name",
]

#: Lives beside the other per-root runtime state, under ``<store_root>``. The
#: DIRECTORY (not just the file) is the unit: Stage 1 adds ``devices.json`` and
#: Stage 6 ``peers.json`` beside this record.
GATEWAY_DIRNAME = "gateway"
INSTALL_RECORD_FILENAME = "install.json"

#: A display name is chrome for a picker row, not a key. Capped so a pasted
#: paragraph cannot bloat every greeting frame on the wire.
DISPLAY_NAME_MAX_CHARS = 64

STATE_LOADED = "loaded"
STATE_MINTED = "minted"


@dataclass(frozen=True, slots=True)
class InstallIdentity:
    """What this root is called — and what happened when we went to find out."""

    #: ``loaded`` | ``minted`` | ``error:<reason>``
    state: str
    #: ``None`` only in an error state; never invented.
    install_id: str | None
    display_name: str | None
    created_at: str | None
    #: Where it lives, so an operator can inspect/rename by hand.
    path: str

    @property
    def ok(self) -> bool:
        return self.state in (STATE_LOADED, STATE_MINTED)

    def frame_payload(self) -> dict[str, Any]:
        """The ``install`` block on ``ready`` / ``hello_ok`` / ``version``.

        Two deliberate deviations from the primary plan's ``{install_id,
        display_name, build}``:

        * **no ``build``** — the greeting frames already carry a top-level
          ``build`` block, and a second copy nested here is a second authority
          that can disagree with the first;
        * **plus ``state``** — so a client can tell "this runtime has no gateway
          identity because it could not write one" from "…because it predates
          the lane", which absence alone cannot say.

        ``created_at`` stays out of the frame: it is provenance for an operator
        reading the file, and no client behaviour keys off it.
        """

        return {
            "install_id": self.install_id,
            "display_name": self.display_name,
            "state": self.state,
        }


def gateway_dir(store_root: Path | str) -> Path:
    return Path(store_root) / GATEWAY_DIRNAME


def install_record_path(store_root: Path | str) -> Path:
    return gateway_dir(store_root) / INSTALL_RECORD_FILENAME


def default_display_name(store_root: Path | str) -> str:
    """What an operator sees before they ever set a name.

    The hostname, because the fact a picker needs is "which MACHINE is this" and
    that is the fact the operator already has a word for — the plan's own
    ``@workstation/dev`` example is a hostname. Falls back to the store root's
    directory name (which distinguishes two roots on one machine) and then to a
    constant, so this never returns empty.
    """

    for candidate in (
        _safe_hostname(),
        Path(store_root).name,
    ):
        cleaned = clean_display_name(candidate)
        if cleaned:
            return cleaned
    return "hermes"


def read_install_identity(store_root: Path | str) -> InstallIdentity:
    """Read the record WITHOUT minting one. ``error:absent`` when there is none.

    The read-only half, for verbs and probes that must not have a side effect on
    a root they were only asked about.
    """

    path = install_record_path(store_root)
    try:
        raw = _read_raw(path)
    except OSError:
        return _error(path, "unreadable")
    if raw is None:
        return _error(path, "absent")
    return _decode(path, raw)


def ensure_install_identity(store_root: Path | str) -> InstallIdentity:
    """Load the record, minting one if absent. Never raises."""

    path = install_record_path(store_root)
    try:
        raw = _read_raw(path)
    except OSError:
        return _error(path, "unreadable")
    if raw is not None:
        return _decode(path, raw)

    record = {
        "install_id": str(uuid.uuid4()),
        "display_name": default_display_name(store_root),
        "created_at": _now(),
    }
    try:
        _mint(path, record)
    except FileExistsError:
        # Another serve for this root won the race. Adopt its record rather than
        # overwrite it: an install id that changes under a paired device is a
        # lockout, which is the same argument ``serve_auth.ensure_token`` makes
        # about the token beside it.
        #
        # Unless what beat us is EMPTY — a process killed between the O_EXCL
        # create and the write. Nobody can hold the id in a zero-byte file, so
        # replacing it orphans nobody, while leaving it wedges the root forever
        # (mint-iff-absent means every later boot takes this branch). So heal it.
        try:
            existing = _read_raw(path)
        except OSError:
            return _error(path, "unreadable")
        if existing is not None:
            return _decode(path, existing)
        try:
            _replace(path, record)
        except OSError as exc:
            return _error(path, _error_reason(exc))
        except Exception:  # pragma: no cover - defensive; boot must not fail
            return _error(path, "mint_failed")
        return _identity(STATE_MINTED, path, record)
    except OSError as exc:
        return _error(path, _error_reason(exc))
    except Exception:  # pragma: no cover - defensive; boot must not fail here
        return _error(path, "mint_failed")
    return _identity(STATE_MINTED, path, record)


def set_display_name(store_root: Path | str, name: str) -> InstallIdentity:
    """Rename this install, preserving ``install_id`` and ``created_at``.

    Mints first when the root has no record yet, so an operator can name an
    install before anything has ever booted against it. The id that comes back
    is always the id that is now on disk — never the one we hoped to write.
    """

    cleaned = clean_display_name(name)
    if not cleaned:
        return _error(install_record_path(store_root), "empty_display_name")
    current = ensure_install_identity(store_root)
    if not current.ok:
        return current
    path = install_record_path(store_root)
    record = {
        "install_id": current.install_id,
        "display_name": cleaned,
        "created_at": current.created_at or _now(),
    }
    try:
        # ``os.replace``, not O_EXCL: this one is a REWRITE of a record that
        # exists, and the value it preserves (the id) is read out of the file
        # first — so a racing rename loses a name, never an identity.
        _replace(path, record)
    except OSError as exc:
        return _error(path, _error_reason(exc))
    except Exception:  # pragma: no cover - defensive
        return _error(path, "rename_failed")
    # The state is the one the LOAD-OR-MINT above reported, not a constant
    # ``loaded``. A rename against a root that had no record created the install
    # — that is the fact ``minted`` exists to say, and it is the fact an operator
    # running ``harness gateway rename`` on a fresh root most needs told. Writing
    # ``loaded`` here would have this call report the opposite of what it did,
    # which is the "stated either way, never inferred" rule broken on its own
    # module.
    return _identity(current.state, path, record)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover - defensive; a nameless host is fine
        return ""


def clean_display_name(value: Any) -> str:
    """Printable, single-line, bounded. A name is chrome, not an identifier.

    PUBLIC because ``harness gateway rename --dry-run`` (Stage 0b) has to print
    the string that WOULD land, and the only way to get that without a second
    copy of this rule is to ask the rule. A preview that echoes the operator's
    raw argument is worse than no preview: a 200-character paste would preview
    at 200 and land at :data:`DISPLAY_NAME_MAX_CHARS`.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(" " if ch in "\r\n\t" else ch for ch in text if ch.isprintable() or ch in "\r\n\t")
    text = " ".join(text.split())
    return text[:DISPLAY_NAME_MAX_CHARS].strip()


def _read_raw(path: Path) -> str | None:
    """The file's text, ``None`` when it is absent or holds only whitespace."""

    if not path.is_file():
        return None
    # read_bytes + decode, never read_text: the repo's standing EOL rule — a
    # record an operator saved with CRLF must still parse.
    value = path.read_bytes().decode("utf-8", errors="replace").strip()
    return value or None


def _decode(path: Path, raw: str) -> InstallIdentity:
    """Parse a record that EXISTS. A broken one is a typed error, not a re-mint.

    Deliberately asymmetric with the empty-file heal in
    :func:`ensure_install_identity`: a zero-byte file's id is held by nobody, but
    a file with bytes in it may well be a record whose id a paired device (Stage
    1) still names. Overwriting that to make a boot look tidy destroys the only
    copy of the join key. An operator can read the typed reason and fix the file;
    nobody can un-mint.
    """

    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _error(path, "malformed_record")
    if not isinstance(record, dict):
        return _error(path, "malformed_record")
    install_id = str(record.get("install_id") or "").strip()
    if not install_id:
        return _error(path, "record_without_id")
    display_name = clean_display_name(record.get("display_name"))
    created_at = str(record.get("created_at") or "").strip() or None
    return InstallIdentity(
        state=STATE_LOADED,
        install_id=install_id,
        # A record whose name was emptied by hand still has to present SOMETHING
        # in a picker; the default is derived, never written back here (this is
        # the read path, and a read must not mutate a root).
        display_name=display_name or default_display_name(path.parent.parent),
        created_at=created_at,
        path=str(path),
    )


def _identity(state: str, path: Path, record: dict[str, Any]) -> InstallIdentity:
    return InstallIdentity(
        state=state,
        install_id=str(record["install_id"]),
        display_name=str(record["display_name"]),
        created_at=str(record["created_at"]),
        path=str(path),
    )


def _payload(record: dict[str, Any]) -> bytes:
    # LF-canonical, and byte-for-byte what ``serde.write_json_atomic`` would
    # render for the same record — so a minted file and a renamed one are the
    # same shape rather than two spellings of one record.
    text = json.dumps(record, ensure_ascii=False, default=str, indent=2) + "\n"
    return text.encode("utf-8")


def _mint(path: Path, record: dict[str, Any]) -> None:
    """Create the record EXCLUSIVELY, so a boot race adopts instead of clobbers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, _payload(record))
    finally:
        os.close(fd)


def _replace(path: Path, record: dict[str, Any]) -> None:
    """tmp + ``os.replace``, so a reader never sees a half-written record.

    Uses ``agent_runtime.serde.write_json_atomic`` — the LF-pinned writer the
    serve registry and the socket owner lock already use — rather than upstream's
    ``utils.atomic_json_write``, which opens its temp file in text mode and so
    writes CRLF on Windows. Nothing here is secret, so the fsync/mode-preserving
    guarantees of the other writer buy nothing.
    """

    from agent_runtime.serde import write_json_atomic

    write_json_atomic(path, record)


def _error(path: Path, reason: str) -> InstallIdentity:
    return InstallIdentity(
        state=f"error:{reason}",
        install_id=None,
        display_name=None,
        created_at=None,
        path=str(path),
    )


def _error_reason(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "root_missing"
    if isinstance(exc, NotADirectoryError):
        return "root_not_a_directory"
    return "unwritable"
