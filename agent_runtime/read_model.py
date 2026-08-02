from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

from hermes_time import now

from . import paths
from .parity import events_watermark
from .resolution import resolve_runtime
from .serde import to_jsonable


def _rows(value: Any) -> list:
    """A snapshot section S4 emits as an id-keyed map, read as an ordered list
    of rows (map values). Accepts a plain list / ``None`` too."""

    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return list(value)
    return []


# 2 -> 3 (B4, 2026-07-31): ``apply_full_rebuild`` used to write the whole frame
# ONCE as the ``snapshot`` blob and then AGAIN as one ``projections_misc`` row
# per top-level section — every byte of the frame stored twice. The per-section
# rows are gone (``read_projection`` slices the blob instead), so a database
# written by the old code carries ~1.5x the bytes it needs and rows no writer
# maintains. Bumping the version makes ``_ensure_schema`` clear those DBs and
# reclaim the pages, rather than leaving stale duplicates to be read.
READ_MODEL_SCHEMA_VERSION = 3
ROW_TABLES = (
    "agent_instances",
    "operator_channels",
)
SNAPSHOT_PROJECTION = "snapshot"


class FrameSource(StrEnum):
    """Where the frame a caller is holding actually came from.

    The serve path used to answer this question with silence: a read model that
    had never been populated returned ``{}`` from ``render_snapshot()`` and
    ``harness snapshot --json`` printed the empty object as if it were the
    runtime. A typed source makes the degradation legible instead.
    """

    BUILT = "built"
    CACHE = "cache"
    CACHE_MISS_REBUILT = "cache_miss_rebuilt"


class ResolvedFrame(NamedTuple):
    frame: dict
    source: FrameSource


def snapshot_watermark(snapshot: Any, *, override: dict | None = None) -> dict:
    """THE watermark derivation for every read-model write site.

    Three call sites derived this independently and disagreed on the fallback:
    ``write_snapshot`` fell back to ``{}`` (an offset-0 watermark that makes the
    projector believe it is caught up at the head of the log), the projector fell
    back to ``events_watermark()``, and ``apply_full_rebuild`` patched missing
    keys back in from the frame. One helper, one fallback: the frame's own
    watermark when it has one, a freshly measured ``events_watermark()`` when it
    does not.
    """

    frame_watermark = ((snapshot or {}).get("parity") or {}).get("watermark") or {}
    resolved = dict(override) if override else dict(frame_watermark)
    if not resolved:
        resolved = events_watermark(last_event_ts=frame_watermark.get("last_event_ts"))
    resolved.setdefault("event_offset", frame_watermark.get("event_offset", 0))
    resolved.setdefault("last_event_ts", frame_watermark.get("last_event_ts"))
    resolved.setdefault("captured_at", now())
    return resolved


def resolve_snapshot_frame(
    *,
    prefer_cache: bool,
    read_model: "ReadModel | None" = None,
) -> ResolvedFrame:
    """Resolve the frame the snapshot serve path hands back, naming its source.

    The built frame is ALWAYS in hand (``write_snapshot`` keeps ``snapshot.json``
    the boot cache regardless of the read-model flags), so a cache miss degrades
    to it and reports ``cache_miss_rebuilt`` instead of serving an empty object.
    """

    from .snapshot import build_snapshot, write_snapshot

    built = write_snapshot(build_snapshot())
    if not prefer_cache:
        return _resolved(built, FrameSource.BUILT)
    cached = (read_model or ReadModel()).render_snapshot()
    if cached:
        return _resolved(cached, FrameSource.CACHE)
    return _resolved(built, FrameSource.CACHE_MISS_REBUILT)


def _resolved(frame: dict, source: FrameSource) -> ResolvedFrame:
    """Stamp the provenance onto the frame's parity envelope.

    ``parity`` is the frame's self-describing provenance block (``resolution``,
    ``runtime_root``, ``watermark``), so the serve source belongs there and
    nowhere else — one location, additive, no contract bump.
    """

    parity = frame.get("parity")
    if not isinstance(parity, dict):
        parity = {}
        frame["parity"] = parity
    parity["frame_source"] = str(source)
    return ResolvedFrame(frame, source)


class ReadModel:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else self._read_model_db_path()

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(conn)
        return conn

    def apply_full_rebuild(self, snapshot: dict, *, watermark: dict | None = None) -> None:
        payload = to_jsonable(snapshot)
        normalized_watermark = snapshot_watermark(payload, override=watermark)
        applied_at = normalized_watermark.get("captured_at") or now()
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table in ROW_TABLES:
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("DELETE FROM projections_misc")
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(READ_MODEL_SCHEMA_VERSION)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("resolved_root", str(resolve_runtime().store_root)),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO projection_watermarks(
                        projection, event_offset, last_event_ts, applied_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    (
                        SNAPSHOT_PROJECTION,
                        int(normalized_watermark.get("event_offset") or 0),
                        _optional_text(normalized_watermark.get("last_event_ts")),
                        str(applied_at),
                    ),
                )
                # ONE write of the frame. The per-section ``projections_misc``
                # rows this loop used to emit alongside the blob were a verbatim
                # second copy of the same bytes, read by nothing that could not
                # slice the blob — see ``read_projection``.
                self._write_misc(conn, SNAPSHOT_PROJECTION, payload)
                self._write_agent_instances(conn, _rows(payload.get("persona_instances")) or _rows(payload.get("agent_instances")))
                self._write_operator_channels(conn, _rows(payload.get("operator_channels")))
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def render_snapshot(self) -> dict | None:
        """The cached frame, or ``None`` when this database holds no frame.

        ``None`` is the honest answer for "never populated / cleared by a schema
        migration"; callers route it through ``resolve_snapshot_frame`` rather
        than serving an empty object as if it were the runtime.
        """

        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM projections_misc WHERE projection = ?",
                (SNAPSHOT_PROJECTION,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload"])
            return payload if isinstance(payload, dict) else None

    def projection_watermark(self, projection: str) -> dict | None:
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT projection, event_offset, last_event_ts, applied_at
                FROM projection_watermarks
                WHERE projection = ?
                """,
                (projection,),
            ).fetchone()
        return dict(row) if row is not None else None

    def read_projection(self, projection: str, *, since_offset: int | None = None) -> dict:
        watermark = self.projection_watermark(SNAPSHOT_PROJECTION)
        if since_offset is not None and watermark and int(watermark.get("event_offset") or 0) <= since_offset:
            return {"projection": projection, "watermark": watermark, "rows": []}
        if projection in ROW_TABLES:
            with closing(self.connect()) as conn:
                rows = [
                    json.loads(row["payload"])
                    for row in conn.execute(f"SELECT payload FROM {projection} ORDER BY 1")
                ]
            return {"projection": projection, "watermark": watermark, "rows": rows}
        # Every non-row-table projection is a SECTION of the stored frame, so it
        # is read as a slice of the one blob instead of from a duplicate row.
        # This also stops ``persona_instances`` / ``operator_channels`` sections
        # answering ``None`` just because the old writer skipped their misc rows.
        frame = self.render_snapshot()
        if frame is None:
            payload = None
        elif projection == SNAPSHOT_PROJECTION:
            payload = frame
        else:
            payload = frame.get(projection)
        return {"projection": projection, "watermark": watermark, "payload": payload}

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        stored_version = self._stored_schema_version(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_instances)")}
        if "task_id" in columns:
            conn.execute("DROP TABLE agent_instances")
        conn.executescript(
            """
            DROP TABLE IF EXISTS goals;
            DROP TABLE IF EXISTS stage_verification;
            DROP TABLE IF EXISTS runs;
            DROP TABLE IF EXISTS proofs;
            DROP TABLE IF EXISTS incidents;
            """
        )
        schema = resources.files("agent_runtime").joinpath("read_model_schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
        if stored_version is not None and stored_version != READ_MODEL_SCHEMA_VERSION:
            self._reset_for_schema_change(conn, stored_version)

    @staticmethod
    def _stored_schema_version(conn: sqlite3.Connection) -> int | None:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", ("schema_version",)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def _reset_for_schema_change(self, conn: sqlite3.Connection, stored_version: int) -> None:
        """Clear a database written by an older projection layout.

        Dropping the watermark is what makes the next ``render_snapshot`` report
        a miss, so the frame gets rebuilt by the normal path rather than left as
        stale rows that still read. (Until S46 this also named
        ``Projector.apply_pending``, whose no-watermark branch fell through to a
        full rebuild; that lane is retired, so the reported miss is now the
        whole mechanism.) ``VACUUM`` returns the freed pages to the filesystem —
        without it a deleted duplicate keeps costing disk.
        """

        for table in ROW_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM projections_misc")
        conn.execute("DELETE FROM projection_watermarks")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(READ_MODEL_SCHEMA_VERSION)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("schema_migrated_from", str(stored_version)),
        )
        conn.commit()
        try:
            conn.execute("VACUUM")
        except sqlite3.Error as exc:
            # A contended VACUUM costs disk, not correctness. Record why it was
            # skipped instead of dropping the fact.
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("schema_migration_vacuum", f"skipped: {exc}"),
            )
            conn.commit()

    def _write_misc(self, conn: sqlite3.Connection, projection: str, payload: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO projections_misc(projection, payload) VALUES(?, ?)",
            (projection, _json(payload)),
        )

    def _write_agent_instances(self, conn: sqlite3.Connection, instances: list[Any]) -> None:
        for index, instance in enumerate(item for item in instances if isinstance(item, dict)):
            instance_id = _first_text(instance, "persona_instance_id", "instance_id", "id") or f"instance_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_instances(
                    instance_id, persona_id, status, payload
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    instance_id,
                    _optional_text(instance.get("persona_id")),
                    _first_text(instance, "status", "state") or "unknown",
                    _json(instance),
                ),
            )

    def _write_operator_channels(self, conn: sqlite3.Connection, channels: list[Any]) -> None:
        for index, channel in enumerate(item for item in channels if isinstance(item, dict)):
            channel_id = _first_text(channel, "channel_id", "persona_instance_id", "persona_id", "id") or f"channel_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO operator_channels(
                    channel_id, persona_id, session_id, payload
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    channel_id,
                    _optional_text(channel.get("persona_id")),
                    _optional_text(channel.get("session_id")),
                    _json(channel),
                ),
            )

    @staticmethod
    def _read_model_db_path() -> Path:
        """The read model's own database path.

        Named for what it resolves rather than for being "the default": the
        old ``_default_db_path`` collided by name with hermes_state's
        unrelated ``_default_db_path`` / ``_resolve_default_db_path`` pair
        (the *session* store's), so grepping either one landed on both and
        every reader had to re-derive which store was meant.
        """

        from .config import load_root_runtime_config

        cfg = load_root_runtime_config()
        filename = str(getattr(getattr(cfg, "read_model", None), "db_filename", "read_model.db") or "read_model.db")
        return paths.store_root() / filename


def _json(payload: Any) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
