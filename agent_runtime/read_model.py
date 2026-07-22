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


READ_MODEL_SCHEMA_VERSION = 1
ROW_TABLES = (
    "goals",
    "stage_verification",
    "runs",
    "proofs",
    "incidents",
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
                    if key not in {"goals", "runs", "proofs", "incidents", "persona_instances", "operator_channels"}:
                        self._write_misc(conn, str(key), value)
                # S4: goals / runs / incidents / persona_instances /
                # operator_channels ship as id-keyed maps; the writers consume
                # ordered rows, so read the map values.
                self._write_goals(conn, _rows(payload.get("goals")))
                self._write_runs(conn, _rows(payload.get("runs")))
                self._write_proofs(conn, payload.get("proofs") or [])
                self._write_incidents(conn, _rows(payload.get("incidents")))
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
        schema = resources.files("agent_runtime").joinpath("read_model_schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)

    def _write_misc(self, conn: sqlite3.Connection, projection: str, payload: Any) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO projections_misc(projection, payload) VALUES(?, ?)",
            (projection, _json(payload)),
        )

    def _write_goals(self, conn: sqlite3.Connection, goals: list[Any]) -> None:
        for index, goal in enumerate(item for item in goals if isinstance(item, dict)):
            goal_id = _first_text(goal, "goal_id", "id", "task_id") or f"goal_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO goals(
                    id, state, title, workspace_id, realm_id, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    _first_text(goal, "state", "status") or "unknown",
                    _optional_text(goal.get("title")),
                    _optional_text(goal.get("workspace_id")),
                    _optional_text(goal.get("realm_id")),
                    _optional_text(goal.get("updated_at") or goal.get("last_activity_at")),
                    _json(goal),
                ),
            )
            self._write_stage_verification(conn, goal_id, goal)

    def _write_stage_verification(self, conn: sqlite3.Connection, goal_id: str, goal: dict[str, Any]) -> None:
        raw = goal.get("stage_verification")
        stages = (raw or {}).get("stages") if isinstance(raw, dict) else []
        if not isinstance(stages, list):
            return
        for index, stage in enumerate(item for item in stages if isinstance(item, dict)):
            stage_id = _first_text(stage, "stage_id", "id") or f"stage_{index}"
            observed = stage.get("observed") if isinstance(stage.get("observed"), dict) else {}
            authoritative = stage.get("authoritative") if isinstance(stage.get("authoritative"), dict) else {}
            conn.execute(
                """
                INSERT OR REPLACE INTO stage_verification(
                    goal_id, stage_id, owner, observed_status, observed_proof_count,
                    authoritative_status, authoritative_proof_count, tamper_flag, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    stage_id,
                    _optional_text(stage.get("owner")),
                    _first_text(observed, "status"),
                    _optional_int(observed.get("proof_count")),
                    _first_text(authoritative, "status"),
                    _optional_int(authoritative.get("proof_count")),
                    1 if bool(stage.get("tamper_flag") or stage.get("tampered")) else 0,
                    _json(stage),
                ),
            )

    def _write_runs(self, conn: sqlite3.Connection, runs: list[Any]) -> None:
        for index, run in enumerate(item for item in runs if isinstance(item, dict)):
            run_id = _first_text(run, "run_id", "id") or f"run_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO runs(
                    id, task_id, persona_id, state, stage_id, updated_at, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _optional_text(run.get("task_id")),
                    _optional_text(run.get("persona_id")),
                    _first_text(run, "state", "status") or "unknown",
                    _optional_text(run.get("stage_id")),
                    _optional_text(run.get("finished_at") or run.get("last_heartbeat_at") or run.get("started_at")),
                    _json(run),
                ),
            )

    def _write_proofs(self, conn: sqlite3.Connection, proofs: list[Any]) -> None:
        for index, proof in enumerate(item for item in proofs if isinstance(item, dict)):
            proof_id = _first_text(proof, "proof_id", "id") or f"proof_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO proofs(
                    id, task_id, stage_id, type, status, created_by, payload
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proof_id,
                    _optional_text(proof.get("task_id")),
                    _optional_text(proof.get("stage_id")),
                    _optional_text(proof.get("type")),
                    _optional_text(proof.get("status")),
                    _optional_text(proof.get("created_by")),
                    _json(proof),
                ),
            )

    def _write_incidents(self, conn: sqlite3.Connection, incidents: list[Any]) -> None:
        for index, incident in enumerate(item for item in incidents if isinstance(item, dict)):
            incident_id = _first_text(incident, "incident_id", "id") or f"incident_{index}"
            state = _first_text(incident, "state", "status")
            if state is None and "is_open" in incident:
                state = "open" if incident.get("is_open") else "closed"
            conn.execute(
                """
                INSERT OR REPLACE INTO incidents(
                    id, task_id, kind, state, payload
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    _optional_text(incident.get("task_id")),
                    _optional_text(incident.get("kind")),
                    state or "unknown",
                    _json(incident),
                ),
            )

    def _write_agent_instances(self, conn: sqlite3.Connection, instances: list[Any]) -> None:
        for index, instance in enumerate(item for item in instances if isinstance(item, dict)):
            instance_id = _first_text(instance, "persona_instance_id", "instance_id", "id") or f"instance_{index}"
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_instances(
                    instance_id, persona_id, status, task_id, payload
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    _optional_text(instance.get("persona_id")),
                    _first_text(instance, "status", "state") or "unknown",
                    _optional_text(instance.get("task_id")),
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
        from .config import load_agent_runtime_config

        cfg = load_agent_runtime_config()
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
