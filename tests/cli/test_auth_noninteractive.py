"""`hermes auth set-key` / `auth login` — the non-interactive credential verbs.

No network, no real provider, no live root: every test runs against a tmp
HERMES_HOME with a fake secret. The three properties under test are the three
the module exists for — the secret is never in argv, never echoed, and the ack
names the home it landed in.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from hermes_cli import auth_noninteractive as nia


SENTINEL = "sk-SENTINEL-DO-NOT-ECHO-0123456789"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME. The live operator root is never touched."""
    target = tmp_path / "hermes-home"
    target.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(target))
    return target


def _args(**kwargs):
    base = dict(provider="openrouter", label=None, stdin=True, profile=None, json=True)
    base.update(kwargs)
    return SimpleNamespace(**base)


def _feed_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(nia.sys, "stdin", io.StringIO(text))


# --- the parser contract: no value-bearing flag on set-key -------------------


def test_set_key_parser_has_no_value_bearing_secret_flag():
    """The secret cannot reach argv BY CONSTRUCTION — there is no flag to put
    it in.

    MUTATION (kill): add `auth_set_key.add_argument("--api-key", ...)` back in
    `hermes_cli/subcommands/auth.py` — red.

    Anti-vacuity note: the probed fact is the OPTION STRINGS of the `set-key`
    subparser specifically, read off the built parser rather than grepped. The
    sibling `auth add` verb legitimately HAS `--api-key` (it is the operator's
    terminal flow), so a whole-parser or whole-file assertion would either be
    permanently red or would have to special-case its way into vacuity. This
    scopes to the one verb whose argv a GUI controls.
    """
    import argparse

    from hermes_cli.subcommands.auth import build_auth_parser

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    build_auth_parser(subparsers, cmd_auth=lambda args: None)

    auth_parser = subparsers.choices["auth"]
    auth_sub = next(
        action
        for action in auth_parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    set_key = auth_sub.choices["set-key"]
    options = {
        option for action in set_key._actions for option in action.option_strings
    }
    assert "--stdin" in options, "the stdin path must exist (guards vacuity)"
    for forbidden in ("--api-key", "--key", "--secret", "--token", "--value"):
        assert forbidden not in options, forbidden

    # The sibling verb keeps its operator-facing flag — this asserts the check
    # above is scoped, not accidentally global.
    assert "--api-key" in {
        option
        for action in auth_sub.choices["add"]._actions
        for option in action.option_strings
    }


# --- the write lands through the lifecycle choke point ----------------------


def test_set_key_stores_via_the_lifecycle_choke_point(home, monkeypatch):
    """The save must reconcile .env AND lift any env-source suppression — i.e.
    it must be the choke point, not a bare .env write.

    MUTATION (kill): replace `save_provider_env_credential(key_var, secret)`
    with `hermes_cli.config.save_env_value(key_var, secret)` — the .env row
    still appears (so a .env-only assertion would pass), but
    `unsuppress_credential_source` is never called and this goes red.

    Anti-vacuity: the probed field is the RECORDED CALL to the choke point plus
    the resulting .env row. A bare-.env mutation writes the .env row too, which
    is precisely why the .env row alone is not the guard — the recorded
    choke-point call is.
    """
    seen = {}

    import hermes_cli.credential_lifecycle as lifecycle

    real = lifecycle.save_provider_env_credential

    def recording(env_var, value):
        seen["env_var"] = env_var
        seen["value_len"] = len(value)
        return real(env_var, value)

    monkeypatch.setattr(lifecycle, "save_provider_env_credential", recording)
    _feed_stdin(monkeypatch, SENTINEL + "\n")

    rc = nia.auth_set_key_command(_args(provider="openrouter"))
    assert rc == 0
    assert seen["env_var"] == "OPENROUTER_API_KEY"
    assert seen["value_len"] == len(SENTINEL)

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY" in env_text


def test_set_key_never_echoes_the_secret(home, monkeypatch, capsys):
    """stdout + stderr are grepped for the sentinel.

    MUTATION (kill): include the value in the ack (`ack["value"] = secret`) or
    print it — red.

    Anti-vacuity: the ack is asserted non-empty and correct FIRST, so "the
    sentinel is absent" cannot be satisfied by the command having printed
    nothing at all.
    """
    _feed_stdin(monkeypatch, SENTINEL + "\n")
    rc = nia.auth_set_key_command(_args(provider="openrouter"))
    assert rc == 0

    captured = capsys.readouterr()
    ack = json.loads(captured.out)
    assert ack["ok"] is True
    assert ack["provider"] == "openrouter"
    assert ack["key_var"] == "OPENROUTER_API_KEY"

    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err
    # Not even a fragment long enough to matter.
    assert "SENTINEL" not in captured.out
    assert "SENTINEL" not in captured.err


def test_set_key_ack_names_the_home_it_wrote_to(home, monkeypatch, capsys):
    """A credential written to the wrong home reads exactly like one that was
    never written. The ack must name the world.

    MUTATION (kill): drop the `ack["home"] = home` line — red.
    """
    _feed_stdin(monkeypatch, SENTINEL + "\n")
    assert nia.auth_set_key_command(_args(provider="openrouter")) == 0

    ack = json.loads(capsys.readouterr().out)
    assert ack["home"] == str(home)
    assert ack["profile"] is None


def test_set_key_profile_flag_redirects_the_write_and_reports_it(
    tmp_path, monkeypatch, capsys
):
    """`--profile` must move the WRITE, not just the label.

    MUTATION (kill): report `applied_profile` in the ack but skip the
    `set_hermes_home_override` — the ack still names the profile home (so an
    ack-only assertion would pass), but the .env lands in the default home and
    the on-disk assertions below go red. The probed fact is therefore the FILE
    location, which the mutated path also writes — to a different place.
    """
    root = tmp_path / "root"
    default_home = root / "default"
    default_home.mkdir(parents=True)
    profile_home = root / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: root / "profiles" / name)
    _feed_stdin(monkeypatch, SENTINEL + "\n")

    assert nia.auth_set_key_command(_args(provider="openrouter", profile="alice")) == 0
    ack = json.loads(capsys.readouterr().out)
    assert ack["profile"] == "alice"
    assert ack["home"] == str(profile_home)

    assert (profile_home / ".env").exists()
    assert not (default_home / ".env").exists()


def test_set_key_requires_stdin_flag(home, monkeypatch, capsys):
    """No prompt fallback. MUTATION (kill): fall through to
    `masked_secret_prompt` when `--stdin` is absent — the command blocks or
    prompts and this goes red."""
    _feed_stdin(monkeypatch, SENTINEL + "\n")
    rc = nia.auth_set_key_command(_args(stdin=False))
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "stdin_required"


def test_set_key_empty_stdin_is_a_named_error(home, monkeypatch, capsys):
    _feed_stdin(monkeypatch, "")
    rc = nia.auth_set_key_command(_args())
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["code"] == "empty_stdin"


def test_set_key_strips_a_windows_carriage_return(home, monkeypatch):
    """A `\\r\\n`-terminated pipe must not store a credential with a CR welded
    on — that presents as an unexplained 401, i.e. the exact defect class.

    MUTATION (kill): drop the `.rstrip("\\r")` — red.
    """
    seen = {}
    import hermes_cli.credential_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "save_provider_env_credential",
        lambda env_var, value: seen.update(value_repr=repr(value)) or {"ok": True},
    )
    _feed_stdin(monkeypatch, SENTINEL + "\r\n")
    assert nia.auth_set_key_command(_args(provider="openrouter")) == 0
    assert "\\r" not in seen["value_repr"]
    assert seen["value_repr"] == repr(SENTINEL)


def test_set_key_internal_failure_reports_class_name_only(home, monkeypatch, capsys):
    """An exception raised with the secret in scope must not have its message
    printed.

    MUTATION (kill): print `str(exc)` in the generic handler — red.
    """
    import hermes_cli.credential_lifecycle as lifecycle

    def boom(env_var, value):
        raise RuntimeError(f"failed writing {value}")

    monkeypatch.setattr(lifecycle, "save_provider_env_credential", boom)
    _feed_stdin(monkeypatch, SENTINEL + "\n")

    rc = nia.auth_set_key_command(_args(provider="openrouter"))
    assert rc == 1
    out = capsys.readouterr().out
    assert SENTINEL not in out
    payload = json.loads(out)
    assert payload["error"] == "set-key failed (RuntimeError)"


# --- auth login: the contract lands, the flows have not ---------------------


def test_auth_login_reports_an_unwrapped_flow_with_the_command_to_run(
    home, capsys
):
    """Honest degradation, not a fake success. The event must name the flow and
    hand back a runnable command.

    MUTATION (kill): emit `{"event": "done", "ok": true}` — red.
    """
    rc = nia.auth_login_command(
        SimpleNamespace(provider="openai-codex", json=True, profile=None)
    )
    assert rc == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event["event"] == "error"
    assert event["code"] == "flow_not_wrapped"
    assert event["flow"] == "device_code"
    assert event["cli_command"] == "hermes auth add openai-codex"
    assert event["home"] == str(home)


def test_auth_login_unknown_provider(home, capsys):
    rc = nia.auth_login_command(
        SimpleNamespace(provider="definitely-not-a-provider", json=True, profile=None)
    )
    assert rc == 1
    event = json.loads(capsys.readouterr().out.strip())
    assert event["code"] == "unknown_provider"
