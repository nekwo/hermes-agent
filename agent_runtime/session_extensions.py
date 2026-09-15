"""Fork-specific native compression lineage deletion for SessionDB."""
import json
from pathlib import Path
from typing import List, Optional


class RuntimeSessionMixin:
    def delete_compression_lineage(
        self,
        root_session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> List[str]:
        """Delete a root and only its native compression continuation chain.

        Explicit branches, delegate sessions, and tool children are preserved
        and detached, matching ``delete_session``'s public child semantics.
        """

        removed: List[str] = []

        def _do(conn):
            rows = conn.execute(
                "SELECT id, parent_session_id, end_reason, model_config FROM sessions"
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            if root_session_id not in by_id:
                return []
            lineage = {root_session_id}
            changed = True
            while changed:
                changed = False
                for row in rows:
                    if row["id"] in lineage or row["parent_session_id"] not in lineage:
                        continue
                    parent = by_id.get(row["parent_session_id"])
                    try:
                        meta = json.loads(row["model_config"] or "{}")
                    except Exception:
                        meta = {}
                    if (
                        parent is not None
                        and parent["end_reason"] == "compression"
                        and not meta.get("_branched_from")
                        and not meta.get("_delegate_from")
                    ):
                        lineage.add(row["id"])
                        changed = True
            ids = sorted(lineage)
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders}) AND id NOT IN ({placeholders})",
                tuple(ids + ids),
            )
            conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", tuple(ids))
            conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", tuple(ids))
            removed.extend(ids)
            return ids

        self._execute_write(_do)
        for session_id in removed:
            self._remove_session_files(sessions_dir, session_id)
        return removed

