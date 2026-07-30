from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from importlib import resources
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
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


READ_MODEL_SCHEMA_VERSION = 2
ROW_TABLES = (
    "agent_instances",
    "operator_channels",
)


class ReadModel:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else self._default_db_path()

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(conn)
        return conn

    def apply_full_rebuild(self, snapshot: dict, *, watermark: dict) -> None:
        payload = to_jsonable(snapshot)
        normalized_watermark = dict(watermark or {})
        normalized_watermark.setdefault(
            "event_offset",
            ((payload.get("parity") or {}).get("watermark") or {}).get("event_offset", 0),
        )
        normalized_watermark.setdefault(
            "last_event_ts",
            ((payload.get("parity") or {}).get("watermark") or {}).get("last_event_ts"),
        )
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
                        "snapshot",
                        int(normalized_watermark.get("event_offset") or 0),
                        _optional_text(normalized_watermark.get("last_event_ts")),
                        str(applied_at),
                    ),
                )
                self._write_misc(conn, "snapshot", payload)
                for key, value in payload.items():
                    if key not in {"persona_instances", "operator_channels"}:
                        self._write_misc(conn, str(key), value)
                self._write_agent_instances(conn, _rows(payload.get("persona_instances")) or _rows(payload.get("agent_instances")))
                self._write_operator_channels(conn, _rows(payload.get("operator_channels")))
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def render_snapshot(self) -> dict:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM projections_misc WHERE projection = ?",
                ("snapshot",),
            ).fetchone()
            if row is None:
                return {}
            payload = json.loads(row["payload"])
            return payload if isinstance(payload, dict) else {}

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
        watermark = self.projection_watermark("snapshot")
        if since_offset is not None and watermark and int(watermark.get("event_offset") or 0) <= since_offset:
            return {"projection": projection, "watermark": watermark, "rows": []}
        with closing(self.connect()) as conn:
            if projection in ROW_TABLES:
                rows = [
                    json.loads(row["payload"])
                    for row in conn.execute(f"SELECT payload FROM {projection} ORDER BY 1")
                ]
                return {"projection": projection, "watermark": watermark, "rows": rows}
            row = conn.execute(
                "SELECT payload FROM projections_misc WHERE projection = ?",
                (projection,),
            ).fetchone()
        payload = json.loads(row["payload"]) if row is not None else None
        return {"projection": projection, "watermark": watermark, "payload": payload}

    def integrity_check(self) -> str:
        with closing(self.connect()) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row is not None else "missing"

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
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
    def _default_db_path() -> Path:
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


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
