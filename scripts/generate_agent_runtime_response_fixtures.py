"""Generate deterministic CLI response envelopes for Launcher contract tests.

The production argparse tree and production handlers emit every fixture.  The
generator runs against an isolated empty runtime root, then normalizes only
the random error id added after the handler has classified the response.

Some envelopes only exist against runtime STATE (a realm whose sync remote is
gone; a realm whose credential belongs to somebody else).  Those cases name an
arrangement in :data:`FIXTURE_ARRANGEMENTS` — a callable that builds that state
in the isolated root just before the case runs.  An arrangement is deliberately
allowed to use the runtime API directly: it is the fixture's *given*, never the
thing under test, and the envelope is still produced by the real argparse tree
and the real handler.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "response_envelopes"

#: The realm ids the realm-sync arrangements mint.  FIXED, because the store
#: mints a uuid otherwise and the id reaches the envelope's ``id`` field.
UNREACHABLE_REALM_ID = "realm_fixture_unreachable"
DENIED_REALM_ID = "realm_fixture_denied"

FIXTURE_CASES = {
    "work_list_empty.json": ["harness", "work", "list", "--json"],
    "work_peek_not_found.json": [
        "harness",
        "work",
        "peek",
        "terminal:fixture-missing",
        "--json",
    ],
    # The realm-sync status family.  These three are the envelope the Launcher's
    # realm-sync sheet parses, produced by the producer instead of hand-authored
    # in the consumer's suite: the typed refusal, and the two ``remote_checked:
    # false`` codes the sheet CLASSIFIES (`RealmRemoteCheckFailure`).  A code
    # that stops being emitted here stops being mirrored, which is the drift the
    # launcher's vocabulary gate cannot see on its own.
    "realm_sync_status_not_found.json": [
        "harness",
        "realm",
        "sync",
        "status",
        "realm_fixture_missing",
        "--json",
    ],
    "realm_sync_status_remote_unreachable.json": [
        "harness",
        "realm",
        "sync",
        "status",
        UNREACHABLE_REALM_ID,
        "--json",
    ],
    "realm_sync_status_auth_denied.json": [
        "harness",
        "realm",
        "sync",
        "status",
        DENIED_REALM_ID,
        "--json",
    ],
}


def _arrange_unreachable_remote(root: Path, setenv: Callable[[str, str], None]) -> None:
    """A realm whose sync repo has an ``origin`` that is not there.

    ``realm_sync_status`` fetches first, ``_git`` fails, and the envelope
    degrades to local facts with ``remote_check_error: sync_remote_unreachable``.
    No network is involved: the remote is a path inside the isolated root that
    was never created.
    """

    from agent_runtime import realm_sync
    from agent_runtime.store import RealmStore

    realm = RealmStore().create(
        name="Fixture",
        realm_id=UNREACHABLE_REALM_ID,
        default_workspace_id="ws_fixture_unreachable",
        default_workspace_name="Fixture",
    )
    repo = realm_sync._sync_repo_path(realm)
    repo.mkdir(parents=True, exist_ok=True)
    _git_quiet("init", "-b", "main", str(repo))
    _git_quiet("-C", str(repo), "remote", "add", "origin", str(root / "no-such-remote"))


def _arrange_denied_credential(root: Path, setenv: Callable[[str, str], None]) -> None:
    """A server-bound realm read with a credential issued for another realm.

    This is the AUTHORIZATION half of the same honesty pair: the status verb
    catches the refusal, answers the local half, and reports the refusal's code
    on ``remote_check_error``.  The realm-id mismatch is decided before any
    backend request, so this arrangement is offline too.

    The credential travels by ``HERMES_REALM_SYNC_CREDENTIAL`` rather than
    ``--credential-file`` on purpose: the fixture records its own ``argv``, and
    a machine path in there would make the committed bytes machine-specific.
    """

    from agent_runtime import realm_sync
    from agent_runtime.realm_membership import CREDENTIAL_ENV_VAR, CREDENTIAL_SCHEMA_VERSION
    from agent_runtime.store import RealmStore

    store = RealmStore()
    realm = store.create(
        name="Fixture",
        realm_id=DENIED_REALM_ID,
        default_workspace_id="ws_fixture_denied",
        default_workspace_name="Fixture",
    )
    realm.server_id = "srv_fixture"
    realm.sync_manifest_ref = "https://fixture.invalid/realm.git"
    realm = store.save(realm)

    # The degrade has a FLOOR — no local repo, no degrade, the refusal is raised
    # whole.  A cloned-already repo is what puts this case on the degrade side.
    repo = realm_sync._sync_repo_path(realm)
    repo.mkdir(parents=True, exist_ok=True)
    _git_quiet("init", "-b", "main", str(repo))

    credential = root / "credential.json"
    credential.write_text(
        json.dumps(
            {
                "schema_version": CREDENTIAL_SCHEMA_VERSION,
                "realm_id": "realm_fixture_somebody_else",
                "api_base": "https://fixture.invalid/api",
                "api_token": "fixture-token",
                "git_url": "https://fixture.invalid/realm.git",
                "git_authorization": "Bearer fixture-token",
                # Far enough out that the fixture never expires into a different
                # code, and fixed so the bytes never move.
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    setenv(CREDENTIAL_ENV_VAR, str(credential))


#: Case name → the state that case needs, built in the isolated root just before
#: the case runs.  A case with no entry runs against the empty runtime.
FIXTURE_ARRANGEMENTS: dict[str, Callable[[Path, Callable[[str, str], None]], None]] = {
    "realm_sync_status_remote_unreachable.json": _arrange_unreachable_remote,
    "realm_sync_status_auth_denied.json": _arrange_denied_credential,
}


def _git_quiet(*args: str) -> None:
    subprocess.run(["git", *args], capture_output=True, check=True)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {str(key): _normalize(item) for key, item in value.items()}
        if "error_id" in normalized:
            normalized["error_id"] = "err_fixture"
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _parser() -> argparse.ArgumentParser:
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def _run(parser: argparse.ArgumentParser, argv: list[str]) -> dict[str, Any]:
    args = parser.parse_args(argv)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = args.func(args)
    return {
        "argv": argv,
        "exit_code": exit_code,
        "stdout": _normalize(json.loads(stdout.getvalue())),
    }


def _write(name: str, payload: dict[str, Any]) -> None:
    (FIXTURE_ROOT / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_manifest() -> None:
    lines = [
        f"{hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest()}  {name}"
        for name in FIXTURE_CASES
    ]
    (FIXTURE_ROOT / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def isolate(root: Path, *, setenv: Callable[[str, str], None] | None = None) -> None:
    """Point the runtime env at ``root`` — ONE definition, shared with the gate.

    ``tests/agent_runtime/test_response_contract_fixture.py`` re-runs the
    producer and compares; it has to isolate the runtime exactly the way this
    generator does or the comparison is between two different worlds.  It passes
    ``monkeypatch.setenv`` so its own writes are undone at teardown.

    The home directory is named ``hermes`` on purpose: ``running_work`` publishes
    the resolved home's BASENAME as ``ambient.home_name``, so the name reaches
    the committed bytes.
    """

    from agent_runtime.realm_membership import CREDENTIAL_ENV_VAR

    write = setenv if setenv is not None else os.environ.__setitem__
    # Cleared, not just unset-by-default: an arrangement points this at a
    # credential file, and a leftover value would silently re-authorize the
    # NEXT case. The loader reads empty as "no credential".
    write(CREDENTIAL_ENV_VAR, "")
    hermes_home = root / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    write("HERMES_HOME", str(hermes_home))
    write("HERMES_HEAD_HOME", str(hermes_home))
    write("HERMES_AGENT_RUNTIME_ROOT", str(root / "runtime"))
    write("LOCALAPPDATA", str(root / "local"))


def arrange(name: str, root: Path, *, setenv: Callable[[str, str], None] | None = None) -> None:
    """Build the state case ``name`` needs, if it names an arrangement."""

    build = FIXTURE_ARRANGEMENTS.get(name)
    if build is None:
        return
    build(root, setenv if setenv is not None else os.environ.__setitem__)


def main() -> int:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hermes-response-fixtures-", ignore_cleanup_errors=True
    ) as temp:
        parser = _parser()
        for index, (name, argv) in enumerate(FIXTURE_CASES.items()):
            # A root per case, so no case can observe the state another case's
            # arrangement left behind and the dict's order stays cosmetic.
            case_root = Path(temp) / f"case{index}"
            isolate(case_root)
            arrange(name, case_root)
            _write(name, _run(parser, argv))
        _write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
