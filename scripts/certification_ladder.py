"""Drive Stage 61G certification cases until the 10/10 unattended gate is green.

Sequentially runs burn-in cases, auto-resuming a case while it stops on
max_actions (the sanctioned driver path, not a manual tick). Stops on the first
non-green case so the failure can be diagnosed instead of burning tokens.
"""

import json
import subprocess
import sys


def run_cli(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "harness", *args], capture_output=True, text=True)
    text = proc.stdout or ""
    start = text.find("{")
    if start < 0:
        return {"error": (proc.stderr or text)[:300]}
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return {"error": text[start : start + 300]}


def main() -> int:
    target = 10
    cases = ["backend-only-edit", "noop-orchestration"]
    case_index = 0
    while True:
        cert = (run_cli(["status", "--json"]).get("swarm") or {}).get("certification") or {}
        streak = int(cert.get("consecutive_green") or 0)
        if streak >= target or cert.get("state") == "green":
            print(f"GATE GREEN streak={streak}", flush=True)
            return 0
        case_id = cases[case_index % len(cases)]
        case_index += 1
        manifest = run_cli(["burn-in", "run", case_id, "--max-actions", "24", "--json"])
        burn_id = manifest.get("burn_id")
        resumes = 0
        while (manifest.get("unattended") or {}).get("failure_class") == "max_actions" and resumes < 8 and burn_id:
            resumes += 1
            manifest = run_cli(["burn-in", "run", case_id, "--burn-id", burn_id, "--max-actions", "24", "--json"])
        unattended = manifest.get("unattended") or {}
        cert = manifest.get("certification") or {}
        print(
            f"case={case_id} burn={burn_id} status={manifest.get('status')} green={unattended.get('green')} "
            f"failure={unattended.get('failure_class')} resumes={resumes} streak={cert.get('consecutive_green')}",
            flush=True,
        )
        if not unattended.get("green"):
            print(f"STREAK BROKEN failure={unattended.get('failure_class')} error={manifest.get('error', '')}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
