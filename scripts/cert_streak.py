"""Record and validate the Stage 46 unattended certification streak.

The recorder is intentionally small: it delegates execution to the Harness
burn-in CLI, then writes one evidence row per manifest. A row is green only when
the Harness unattended summary says it is green; manual events are taken from
that event-derived summary, not from operator memory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_LAUNCHER_ROOT = Path(r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher")
DEFAULT_CERT_DOC = DEFAULT_LAUNCHER_ROOT / "docs" / "mission_control" / "cert_streak.md"
DEFAULT_CASE_POOL = [
    "noop-orchestration",
    "custom-backend-proof",
    "custom-launcher-proof",
    "custom-cross-stack-proof",
    "cross-stack-edit",
    "noop-orchestration",
    "custom-backend-proof",
    "custom-launcher-proof",
    "cross-stack-edit",
    "custom-cross-stack-proof",
]


def run_cli(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    text = proc.stdout or proc.stderr or ""
    start = text.find("{")
    if start < 0:
        return {"status": "blocked", "failure_class": "missing_json", "error": text[:1000], "returncode": proc.returncode}
    try:
        manifest = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        return {"status": "blocked", "failure_class": "bad_json", "error": f"{exc}: {text[start:start + 1000]}", "returncode": proc.returncode}
    manifest.setdefault("returncode", proc.returncode)
    return manifest


def read_manifest(burn_id: str) -> dict[str, Any]:
    from agent_runtime.burn_in import burn_in_dir

    path = burn_in_dir(burn_id) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def case_metadata(case_id: str | None) -> dict[str, Any]:
    from agent_runtime.burn_in import STAGE47_CASES

    case = STAGE47_CASES.get(str(case_id or ""), {})
    return {
        "class": "custom-blueprint" if case.get("custom_blueprint") else "default-burn-in",
        "custom": bool(case.get("custom_blueprint")),
        "blueprint": str(case.get("blueprint") or "neko_two_dev_default"),
    }


def append_row(doc: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    validate_manifest_shape(manifest)
    doc.parent.mkdir(parents=True, exist_ok=True)
    rows = existing_rows(doc)
    number = len(rows) + 1
    meta = case_metadata(manifest.get("case_id"))
    unattended = manifest.get("unattended") if isinstance(manifest.get("unattended"), dict) else {}
    manual = unattended.get("manual_intervention_counts") if isinstance(unattended.get("manual_intervention_counts"), dict) else {}
    started = str(manifest.get("started_at") or "")
    finished = str(manifest.get("finished_at") or "")
    row = {
        "number": number,
        "case_id": manifest.get("case_id"),
        "task_id": manifest.get("task_id"),
        "class": meta["class"],
        "custom": meta["custom"],
        "blueprint": meta["blueprint"],
        "status": manifest.get("status"),
        "green": bool(unattended.get("green")),
        "duration": duration_label(started, finished),
        "runs": len(manifest.get("actual_persona_sequence") or []),
        "proof_ids": ",".join(str(item) for item in (manifest.get("proof_ids") or [])),
        "archive": manifest.get("archive_dir") or manifest.get("archive_batch") or "",
        "manual": sum(int(value or 0) for value in manual.values()),
        "failure": unattended.get("failure_class") or manifest.get("failure_class") or "",
    }
    with doc.open("a", encoding="utf-8", newline="\n") as handle:
        if number == 1 and not rows:
            handle.write("# Stage 46 Certification Streak\n\n")
            handle.write("| # | case | task | class | custom | blueprint | status | green | duration | runs | proof ids | archive | manual events | failure |\n")
            handle.write("|---:|---|---|---|---:|---|---|---:|---:|---:|---|---|---:|---|\n")
        handle.write(
            "| {number} | {case_id} | {task_id} | {class} | {custom} | {blueprint} | {status} | {green} | {duration} | {runs} | {proof_ids} | {archive} | {manual} | {failure} |\n".format(
                **{key: md_cell(value) for key, value in row.items()}
            )
        )
    return row


def validate_manifest_shape(manifest: dict[str, Any]) -> None:
    missing = [
        key
        for key in ("case_id", "task_id", "status", "unattended")
        if not manifest.get(key)
    ]
    if missing:
        detail = manifest.get("error") or manifest.get("kind") or manifest.get("failure_class") or "unknown"
        raise ValueError(f"burn-in manifest is incomplete; missing={missing}; detail={detail}")


def existing_rows(doc: Path) -> list[str]:
    if not doc.exists():
        return []
    return [
        line
        for line in doc.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ") and not line.startswith("| # ") and not line.startswith("|---")
    ]


def duration_label(started: str, finished: str) -> str:
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except Exception:
        return ""
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m{seconds % 60:02d}s"


def md_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")[:600]


def validate_doc(doc: Path) -> dict[str, Any]:
    rows = existing_rows(doc)
    green_rows = [row for row in rows if cell(row, 7).lower() == "true"]
    custom_green_rows = [row for row in green_rows if cell(row, 4).lower() == "true"]
    return {
        "ok": len(green_rows) >= 10 and len(custom_green_rows) >= 3,
        "row_count": len(rows),
        "green_count": len(green_rows),
        "custom_green_count": len(custom_green_rows),
        "doc": str(doc),
    }


def cell(row: str, index: int) -> str:
    parts = [part.strip() for part in row.strip().strip("|").split("|")]
    return parts[index] if index < len(parts) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run/record Stage 46 unattended certification streak rows.")
    parser.add_argument("--doc", type=Path, default=DEFAULT_CERT_DOC)
    parser.add_argument("--case", action="append", choices=DEFAULT_CASE_POOL)
    parser.add_argument("--pool", action="store_true", help="Run the default 10-row Stage 46 case pool.")
    parser.add_argument("--record-burn-id", action="append", default=[])
    parser.add_argument("--max-actions", type=int, default=24)
    parser.add_argument("--check", action="store_true", help="Validate the cert table only.")
    args = parser.parse_args(argv)

    if args.check:
        result = validate_doc(args.doc)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    for burn_id in args.record_burn_id:
        if not process_manifest(args.doc, read_manifest(burn_id)):
            return 1
    cases = DEFAULT_CASE_POOL if args.pool else list(args.case or [])
    for case_id in cases:
        if not process_manifest(args.doc, run_cli(["burn-in", "run", case_id, "--max-actions", str(args.max_actions), "--json"])):
            return 1

    if not args.record_burn_id and not cases:
        parser.error("provide --pool, --case, --record-burn-id, or --check")
    return 0


def process_manifest(doc: Path, manifest: dict[str, Any]) -> bool:
    try:
        row = append_row(doc, manifest)
    except ValueError as exc:
        print(json.dumps({"streak_broken": True, "failure": "incomplete_manifest", "error": str(exc)}, indent=2), file=sys.stderr)
        return False
    print(json.dumps(row, indent=2, sort_keys=True))
    unattended = manifest.get("unattended") if isinstance(manifest.get("unattended"), dict) else {}
    if not unattended.get("green"):
        print(json.dumps({"streak_broken": True, "failure": row.get("failure"), "case_id": row.get("case_id")}, indent=2), file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
