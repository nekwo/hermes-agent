"""A claim can say which hosts it is about, and a run says when it skipped one.

W1-H3 slice 4. `hh6-posix-file-lock-ignores-its-deadline` mutates the POSIX arm
of `agent_runtime/locks.py`'s platform fork. On `ubuntu-latest` the
`mutation-claims` job kills it. On this Windows host the mutated line never
runs, the claimed test passes anyway, and the gate reported SURVIVED — measured
again while building this file, exit 1 against `36dc77c68d^`. So a hand-run on
the gate host was permanently one known survivor away from green, which is the
same "silence looks like success" the gate exists to prevent, wearing the other
face: noise that gets learned as background.

`platforms` is the narrowest thing that fixes it. Absent from 112 of 113 rows
and that is the right default — a claim that does not say otherwise is a claim
about behaviour, and behaviour is not supposed to have a platform.

Two properties beyond "it skips": the skip is REPORTED by name (an unreported
skip is the silence again), and it is never SURVIVED — the difference between
"this host cannot answer" and "this host answered wrong" is the entire point.

ANTI-VACUITY throughout: every case writes its own claim and target, and the
host is injected, so a skip here is the declared platform's doing and not the
machine the suite happens to run on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import changed_line_mutation_check as gate


TARGET = "value = 5\n"


def _claim(identifier: str, target: Path, **extra) -> dict:
    claim = {
        "id": identifier,
        "path": str(target),
        "symbol": "module",
        "operator": "flip-a-literal",
        "find": "value = 5",
        "replace": "value = 99",
        # Always-passing: a claim that reaches the mutate loop here reports
        # SURVIVED, which is exactly the outcome under test.
        "test": ["{python}", "-c", "raise SystemExit(0)"],
    }
    claim.update(extra)
    return claim


def _files(tmp_path: Path, claims: list[dict]) -> tuple[Path, Path]:
    target = tmp_path / "target.py"
    target.write_bytes(TARGET.encode("utf-8"))
    claims_path = tmp_path / "claims.json"
    claims_path.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    exemptions = tmp_path / "exemptions.yaml"
    exemptions.write_text(json.dumps({"exemptions": []}), encoding="utf-8")
    return claims_path, exemptions


@pytest.fixture
def host(monkeypatch):
    def _install(name: str) -> None:
        monkeypatch.setattr(gate, "_current_platform", lambda: name)

    return _install


@pytest.fixture
def touched(monkeypatch):
    def _install(lines: set[int]) -> None:
        monkeypatch.setattr(gate, "_changed_lines", lambda base, path: set(lines))

    return _install


def test_a_posix_claim_on_a_windows_host_skips_by_name_and_the_run_is_green(
    tmp_path, host, touched, capsys
):
    """The pin, and the whole row: SKIPPED, said out loud, exit 0.

    The claim's test always passes, so a claim that reached the mutate loop
    would report SURVIVED and exit 1 — which is what this host did before.
    """

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("posix-only", target, platforms=["posix"])])
    host("windows")
    touched({1})

    code = gate.run("BASE", claims, exemptions, max_candidates=12, list_only=False)
    captured = capsys.readouterr()

    assert code == 0
    assert "mutation candidates: 1 (cap 12)" in captured.out
    assert (
        "SKIPPED (platform): posix-only (declared posix; this host is windows)"
        in captured.out
    )
    assert "MUTATE" not in captured.out
    assert "BASELINE" not in captured.out
    assert "SURVIVED" not in captured.err


def test_the_same_claim_on_a_posix_host_runs_and_survives(
    tmp_path, host, touched, capsys
):
    """ANTI-VACUITY, and the half that keeps the gate honest: identical claim,
    identical diff, host says posix — and it runs and reports SURVIVED. So the
    skip above is the platform declaration and not the claim being inert, and
    CI still enforces what this host cannot."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("posix-only", target, platforms=["posix"])])
    host("posix")
    touched({1})

    code = gate.run("BASE", claims, exemptions, max_candidates=12, list_only=False)
    captured = capsys.readouterr()

    assert code == 1
    assert "SKIPPED (platform)" not in captured.out
    assert "MUTATE: posix-only" in captured.out
    assert "SURVIVED: posix-only" in captured.err


def test_a_claim_with_no_platforms_field_runs_everywhere(tmp_path, host, touched, capsys):
    """The default, pinned: 112 of 113 rows say nothing, and saying nothing has
    to keep meaning "every host" or this field would have quietly narrowed the
    whole registry."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("everywhere", target)])
    host("windows")
    touched({1})

    code = gate.run("BASE", claims, exemptions, max_candidates=12, list_only=False)

    assert code == 1
    assert "SURVIVED: everywhere" in capsys.readouterr().err


def test_a_skipped_claim_still_counts_toward_the_cap(tmp_path, host, touched, capsys):
    """The cap counts SELECTED claims, so the number a diff must answer for is
    the same on every host. A Windows run that squeaked under the cap only
    because half its claims were platform-skipped would hand CI a refusal
    nobody local could reproduce."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(
        tmp_path,
        [
            _claim("posix-a", target, platforms=["posix"]),
            _claim("posix-b", target, platforms=["posix"], find="value = 5\n"),
        ],
    )
    host("windows")
    touched({1})

    code = gate.run("BASE", claims, exemptions, max_candidates=1, list_only=True)
    out = capsys.readouterr().out

    assert code == 2
    assert "mutation candidates: 2 (cap 1)" in out


def test_the_list_lane_reports_the_skip_too(tmp_path, host, touched, capsys):
    """`--list` is the inventory, and "this host will not answer for it" is
    part of the inventory."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("posix-only", target, platforms=["posix"])])
    host("windows")
    touched({1})

    gate.run("BASE", claims, exemptions, max_candidates=12, list_only=True)

    assert "SKIPPED (platform): posix-only" in capsys.readouterr().out


@pytest.mark.parametrize(
    "value, message",
    [
        ("posix", "non-empty list of strings"),
        ([], "non-empty list of strings"),
        ([1], "non-empty list of strings"),
        (["linux"], "unknown platforms"),
    ],
)
def test_a_malformed_platforms_field_is_a_configuration_error(
    tmp_path, touched, value, message
):
    """A platform nobody implements is a claim that skips on every host — a
    registered guarantee that runs nowhere and says nothing, which is worse
    than an unregistered one because it looks covered."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("bad", target, platforms=value)])
    touched({1})

    with pytest.raises(RuntimeError, match=message):
        gate.run("BASE", claims, exemptions, max_candidates=12, list_only=True)


def test_an_unknown_claim_field_is_refused_rather_than_ignored(tmp_path, touched):
    """`platform: "posix"` for `platforms: ["posix"]` is the typo this schema
    invites. Ignoring it would mean the claim runs on every host while its
    author believes it is scoped — a SURVIVED waiting to be blamed on the
    code."""

    target = tmp_path / "target.py"
    claims, exemptions = _files(tmp_path, [_claim("typo", target, platform="posix")])
    touched({1})

    with pytest.raises(RuntimeError, match=r"unknown claim fields: \['platform'\]"):
        gate.run("BASE", claims, exemptions, max_candidates=12, list_only=True)


def test_the_registered_posix_claim_is_the_one_that_declares_it(tmp_path):
    """The registry half. `hh6-posix-file-lock-ignores-its-deadline` is the
    claim the row is about; if a second claim ever declares a platform, this
    says so, because "which guarantees does this host not check?" must have a
    short and readable answer."""

    rows = json.loads(
        (gate.REPO_ROOT / "tests" / "mutation_claims.json").read_text(encoding="utf-8")
    )["claims"]
    declared = {row["id"]: row["platforms"] for row in rows if "platforms" in row}

    assert declared == {"hh6-posix-file-lock-ignores-its-deadline": ["posix"]}
