"""RL-26 across a process boundary: a spawned serve that can draw.

The whole reason the seam exists. ``characters turnaround|rows|auto`` are the
runtime's only long runs (``_LONG_RUN_COMMANDS``), every one of them is a
generation, and until this landed the only deterministic draftsman in the tree
was a ``monkeypatch`` no spawned child could reach — so the long-run acceptance
proof (RL-25) stopped at its own fixture gate rather than spend a provider call.

This file spawns ``python -m hermes_cli.main harness serve --ndjson --service``
with ``HERMES_CHARSHEET_DRAFTSMAN=fake`` in its environment and nothing else
unusual, drives a whole batch down the argv lane, and reads what the child
committed. Three independent claims:

* the batch RAN — a turnaround, three approved references, three row strips,
  and a ``status`` readback naming one attempt per row;
* the payloads SAY which door drew them (``"draftsman": "fake"`` on every
  ``--json`` result the child wrote);
* no provider was called. Two guards, because one of them is negative evidence:
  the child's environment carries no credential-shaped variable at all, and the
  bytes it committed for a row are BYTE-IDENTICAL to what this process draws
  with the same public function — a remote backend cannot answer with the local
  Pillow drawing.

Sandboxing is the shape ``test_serve_ended_sidecar_child_e2e.py`` established:
every ``HERMES_*`` root plus ``HOME``/``USERPROFILE``/``LOCALAPPDATA``/
``APPDATA`` under one temp tree, so nothing here can reach the operator's store.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT_SECONDS = 180.0
REQUEST_TIMEOUT_SECONDS = 180.0
E2E_TEST_TIMEOUT_SECONDS = 600

#: The spawn IS the claim — same bypass, same reason, as the sibling e2e files.
pytestmark = [
    pytest.mark.live_system_guard_bypass,
    pytest.mark.timeout(E2E_TEST_TIMEOUT_SECONDS),
]

pytest.importorskip("PIL")

#: Anything that could authenticate a paid backend. Popped from the child's
#: environment and then ASSERTED absent: "the fake drew it" is the claim, and a
#: child that could not have paid even if it wanted to is how it is bounded from
#: the other side.
_CREDENTIAL = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|OPENAI|ANTHROPIC|OPENROUTER|NOUS|KREA|FAL|XAI|DEEPINFRA)", re.I)

CONCEPT = "an arrow knight"
STATES = "idle:2"
DIRECTIONS = "4"  # authored: s, e, n
EXPECTED_ROWS = ["idle-e", "idle-n", "idle-s"]


def _sandbox_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    local = tmp_path / "localappdata"
    roaming = tmp_path / "appdata"
    head = tmp_path / "head"
    for path in (home, local, roaming, head, tmp_path / "runtime"):
        path.mkdir(parents=True, exist_ok=True)
    env = {name: value for name, value in os.environ.items() if not _CREDENTIAL.search(name)}
    env.pop("HERMES_SERVE_BOOT_FAULT", None)
    env.update(
        {
            "HERMES_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "HERMES_HOME": str(home),
            "HERMES_HEAD_HOME": str(head),
            "LOCALAPPDATA": str(local),
            "APPDATA": str(roaming),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HERMES_CHARSHEET_DRAFTSMAN": "fake",
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + str(env.get("PYTHONPATH") or ""),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


class _Serve:
    """A real serve child, driven over the stdio NDJSON lane."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.frames: list[dict] = []
        self._next = 0

    def boot(self) -> dict:
        return self._wait_for(lambda frame: frame.get("event") == "ready", BOOT_TIMEOUT_SECONDS)

    def _wait_for(self, matches, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise AssertionError(
                    f"the serve child ended early; frames so far: {[f.get('event') for f in self.frames]}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.frames.append(frame)
            if matches(frame):
                return frame
        raise AssertionError(f"no matching frame within {timeout}s")

    def dispatch(self, *argv: str) -> tuple[int, dict]:
        """One ``harness …`` request down the argv lane → (exit code, payload)."""

        self._next += 1
        request_id = f"req-{self._next}"
        self.process.stdin.write(json.dumps({"id": request_id, "argv": list(argv)}) + "\n")
        self.process.stdin.flush()
        lines: list[str] = []

        def _collect(frame: dict) -> bool:
            if frame.get("id") != request_id:
                return False
            if frame.get("event") == "line":
                lines.append(str(frame.get("line", "")))
            return frame.get("event") == "exit"

        exit_frame = self._wait_for(_collect, REQUEST_TIMEOUT_SECONDS)
        text = "\n".join(lines).strip()
        payload = json.loads(text) if text.startswith("{") else {"raw": text}
        return int(exit_frame.get("code", -1)), payload

    def shutdown(self) -> None:
        try:
            self.process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
            self.process.stdin.flush()
            self.process.wait(timeout=60)
        except Exception:  # noqa: BLE001 - teardown; the kill below is the fallback
            self.process.kill()
            self.process.wait(timeout=60)


@pytest.fixture
def serve(tmp_path):
    env = _sandbox_env(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "--ndjson", "--service"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    child = _Serve(process)
    child.env = env
    try:
        child.boot()
        yield child
    finally:
        child.shutdown()


def test_a_spawned_serve_runs_a_whole_batch_on_the_fake_draftsman(serve, tmp_path):
    from agent.charsheet.fake_draftsman import square_image, strip_image

    assert not [name for name in serve.env if _CREDENTIAL.search(name)], "the child could authenticate a provider"

    base = tmp_path / "base.png"
    square_image("s").save(base, format="PNG")

    code, started = serve.dispatch(
        "harness", "characters", "start",
        "--concept", CONCEPT, "--slug", "arrow-knight",
        "--states", STATES, "--directions", DIRECTIONS,
        "--base-image", str(base), "--json",
    )
    assert code == 0, started
    draft = started["draft"]
    assert started["draftsman"] == "fake"

    code, turnaround = serve.dispatch("harness", "characters", "turnaround", "--draft", draft, "--json")
    assert code == 0, turnaround
    assert turnaround["draftsman"] == "fake"
    assert sorted(turnaround["turnaround"]) == ["e", "n", "s"]

    code, approved = serve.dispatch(
        "harness", "characters", "approve-direction", "--draft", draft, "--all", "--json"
    )
    assert code == 0, approved
    assert approved["stage"] == "rows"

    code, rows = serve.dispatch("harness", "characters", "rows", "--draft", draft, "--json")
    assert code == 0, rows
    assert rows["draftsman"] == "fake"
    assert sorted(rows["rows"]) == EXPECTED_ROWS

    code, status = serve.dispatch("harness", "characters", "status", "--draft", draft, "--json")
    assert code == 0, status
    assert status["draftsman"] == "fake"
    committed = status["status"]["rows"]
    assert sorted(committed) == EXPECTED_ROWS
    # Committed ONCE each: a batch that retried a row would say 2 here, and the
    # long-run proof's "expected revisions committed once" is this readback.
    assert [committed[key]["attempts"] for key in EXPECTED_ROWS] == [1, 1, 1]
    assert [committed[key]["approved"] for key in EXPECTED_ROWS] == [0, 0, 0]
    assert status["status"]["pending"]["rows"] == []

    # No provider was called, positively: the bytes the child committed for a
    # row are the ones THIS process draws from the same public function. A
    # remote backend does not answer with the local Pillow drawing.
    drawn = tmp_path / "expected-idle-e.png"
    strip_image([("e", index, 2) for index in range(2)]).save(drawn, format="PNG")
    assert Path(committed["idle-e"]["current"]).read_bytes() == drawn.read_bytes()
