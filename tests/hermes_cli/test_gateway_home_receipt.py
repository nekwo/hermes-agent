"""B-1: the gateway states which HERMES_HOME it booted under, and why.

ANTI-VACUITY NOTE (read before adding a case here).

The obvious way to test "profile resolution" is to set ``HERMES_HOME`` in a
fixture and then assert that something reports that value. That proves nothing:
the fixture wrote the answer. Every assertion below is therefore pinned to a
field the mutation under test does NOT also write:

* the ladder tests run ``_apply_profile_override`` in a SUBPROCESS with a
  conflicting flag and env, and read back ``HERMES_PROFILE_RESOLUTION`` — a
  variable no fixture here ever sets, and whose two candidate values
  (``flag`` / ``env_profile_dir``) are produced by two DIFFERENT branches. A
  mutant that reports the env home instead of the flag-resolved one must change
  which branch ran, so it cannot also produce ``flag``.
* the receipt tests probe ``telegram_configured`` and ``env_key_count``, which
  are derived from ``.env`` FILE CONTENT on disk, not from any environment
  variable the test set.
* the leak test seeds a sentinel VALUE and greps the whole emitted line; a
  mutant that widens the receipt to include values must surface the sentinel,
  which nothing else in the line can produce.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.gateway_home_receipt import (
    RESOLUTION_ACTIVE_PROFILE_MARKER,
    RESOLUTION_DEFAULT,
    RESOLUTION_ENV_PROFILE_DIR,
    RESOLUTION_FLAG,
    build_gateway_home_receipt,
    env_key_names,
    profile_name_of,
    suspicious_home_row,
    wrapper_profiles,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Ladder instrumentation (hermes_cli._profile_bootstrap) — run out of process,
# because the pre-parse
# mutates os.environ and sys.argv of whatever interpreter runs it.
# ---------------------------------------------------------------------------

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    sys.argv = json.loads(os.environ.pop("PROBE_ARGV"))
    sys.path.insert(0, os.environ["PROBE_ROOT"])
    # Executing main.py end-to-end would start the CLI, and only the pre-parse
    # is under test. It lives in its own import-safe module now
    # (hermes_cli._profile_bootstrap), so this imports the real thing instead of
    # exec'ing a slice of main.py's source between two spellings — a boundary
    # any reformat of that file could move without reddening anything.
    from hermes_cli._profile_bootstrap import apply_profile_override
    apply_profile_override()
    print(json.dumps({
        "resolution": os.environ.get("HERMES_PROFILE_RESOLUTION"),
        "hermes_home": os.environ.get("HERMES_HOME"),
        "argv": sys.argv,
    }))
    """
)


def _make_root(tmp_path: Path, profiles: dict[str, str]) -> Path:
    """A hermes root with ``profiles/<name>/.env`` for each entry."""
    root = tmp_path / "hermesroot"
    (root / "profiles").mkdir(parents=True)
    for name, env_text in profiles.items():
        profile = root / "profiles" / name
        profile.mkdir()
        (profile / ".env").write_text(env_text, encoding="utf-8")
    return root


def _run_preparse(root: Path, argv: list[str], env_home: str | None) -> dict:
    env = dict(os.environ)
    env["PROBE_ROOT"] = str(PROJECT_ROOT)
    env["PROBE_ARGV"] = json.dumps(["hermes", *argv])
    if env_home is None:
        env.pop("HERMES_HOME", None)
    else:
        env["HERMES_HOME"] = env_home
    # A stale inherited answer must not be able to masquerade as this run's.
    env["HERMES_PROFILE_RESOLUTION"] = "STALE_INHERITED"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_flag_beats_a_conflicting_profile_shaped_env(tmp_path):
    """resolution=flag when --profile is given DESPITE a conflicting env home.

    Kill-mutation: report the env home instead of the flag-resolved one (i.e.
    move the rung-2 early return above the flag scan). The probe then reports
    ``env_profile_dir``, and ``hermes_home`` stays on base.

    Anti-vacuity: the fixture sets ``HERMES_HOME`` to the BASE profile and the
    flag names ALICE. The two rungs disagree by construction, so an assertion on
    which one won cannot be satisfied by the fixture's own write.
    """
    root = _make_root(
        tmp_path, {"base": "A=1\n", "alice": "TELEGRAM_BOT_TOKEN=x\n"}
    )
    out = _run_preparse(
        root,
        ["--profile", "alice", "gateway", "run"],
        env_home=str(root / "profiles" / "base"),
    )
    assert out["resolution"] == RESOLUTION_FLAG
    assert Path(out["hermes_home"]).name == "alice"
    # And the flag is stripped, so argv alone could never have told us this.
    assert "--profile" not in out["argv"]


def test_bare_run_under_profile_shaped_env_reports_the_trap_rung(tmp_path):
    """resolution=env_profile_dir for a bare run under a profile-shaped env.

    Kill-mutation: hardcode ``resolution="flag"`` at the early return. The probe
    then reports ``flag`` for a run that had no flag at all.

    Anti-vacuity: ``HERMES_PROFILE_RESOLUTION`` is seeded to ``STALE_INHERITED``
    by the runner, so a mutant that simply fails to write anything is also
    caught — the assertion cannot pass by inheritance.
    """
    root = _make_root(tmp_path, {"base": "A=1\n"})
    out = _run_preparse(root, ["gateway", "run"], env_home=str(root / "profiles" / "base"))
    assert out["resolution"] == RESOLUTION_ENV_PROFILE_DIR
    assert Path(out["hermes_home"]).name == "base"


def test_sticky_marker_answers_when_the_env_is_not_profile_shaped(tmp_path):
    """resolution=active_profile_marker — the rung the trap skips.

    Kill-mutation: delete the ``resolution = "active_profile_marker"`` line; the
    probe then reports ``default`` (the pre-stamp) even though the marker
    demonstrably chose the home.

    Anti-vacuity: the assertion pairs the RUNG with the resolved home. The
    fixture sets ``HERMES_HOME`` to the ROOT (not a profile), so the only way
    ``hermes_home`` can end on ``alice`` is the marker rung actually running.
    """
    root = _make_root(tmp_path, {"alice": "TELEGRAM_BOT_TOKEN=x\n"})
    (root / "active_profile").write_text("alice", encoding="utf-8")
    out = _run_preparse(root, ["gateway", "run"], env_home=str(root))
    assert out["resolution"] == RESOLUTION_ACTIVE_PROFILE_MARKER
    assert Path(out["hermes_home"]).name == "alice"


def test_stale_inherited_resolution_never_survives(tmp_path):
    """A child's receipt describes the CHILD, not whatever spawned it.

    Kill-mutation: drop the unconditional ``= "default"`` pre-stamp. The probe
    then reports ``STALE_INHERITED`` — the parent's answer, presented as this
    process's.
    """
    root = _make_root(tmp_path, {"base": "A=1\n"})
    out = _run_preparse(root, ["status"], env_home=str(root))
    assert out["resolution"] == RESOLUTION_DEFAULT


# ---------------------------------------------------------------------------
# The receipt itself
# ---------------------------------------------------------------------------


def test_receipt_reports_telegram_false_and_the_key_count_for_a_bare_profile(tmp_path):
    """env_key_count / telegram_configured come from the .env FILE.

    Kill-mutation: hardcode ``telegram_configured=True``. This case goes red
    while the alice case below stays green, so the mutant cannot hide.

    Anti-vacuity: both probed fields are derived from file content written by
    this test, never from an environment variable — the resolution machinery
    cannot set them.
    """
    home = tmp_path / "profiles" / "base"
    home.mkdir(parents=True)
    (home / ".env").write_text("# comment\nHERMES_SOMETHING=1\n\n", encoding="utf-8")
    receipt = build_gateway_home_receipt(
        hermes_home=home,
        resolution=RESOLUTION_ENV_PROFILE_DIR,
        env_keys=env_key_names(home / ".env"),
    )
    assert receipt["profile"] == "base"
    assert receipt["env_key_count"] == 1
    assert receipt["telegram_configured"] is False
    assert receipt["resolution"] == RESOLUTION_ENV_PROFILE_DIR


def test_receipt_reports_telegram_true_when_a_telegram_key_is_present(tmp_path):
    """The paired half of the case above — kill: hardcode False."""
    home = tmp_path / "profiles" / "alice"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=1\nOTHER=2\n", encoding="utf-8"
    )
    receipt = build_gateway_home_receipt(
        hermes_home=home,
        resolution=RESOLUTION_FLAG,
        env_keys=env_key_names(home / ".env"),
    )
    assert receipt["env_key_count"] == 3
    assert receipt["telegram_configured"] is True


def test_no_env_value_appears_anywhere_in_the_emitted_line(tmp_path):
    """No ``.env`` VALUE reaches the receipt. Kill: include the resolved dict.

    Anti-vacuity: the sentinel is a high-entropy string that appears ONLY as a
    value in the seeded file. Nothing else in the receipt — path, profile name,
    rung, counts — can produce it, so a hit is proof of a leak rather than a
    coincidence. The whole serialized line is searched, not a chosen field.
    """
    sentinel = "zzq7-SENTINEL-SECRET-VALUE-4417"
    home = tmp_path / "profiles" / "alice"
    home.mkdir(parents=True)
    (home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={sentinel}\nOPENAI_API_KEY={sentinel}\n", encoding="utf-8"
    )
    keys = env_key_names(home / ".env")
    assert keys == ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"]
    receipt = build_gateway_home_receipt(
        hermes_home=home, resolution=RESOLUTION_FLAG, env_keys=keys
    )
    assert sentinel not in json.dumps(receipt)
    assert sentinel not in receipt["summary"]


def test_env_key_names_never_returns_a_value(tmp_path):
    """Direct guard on the parser. Kill: return the whole line instead of key."""
    path = tmp_path / ".env"
    path.write_text(
        "export FOO=bar\nBAZ = qux\n#COMMENT=1\nNOEQUALS\n", encoding="utf-8"
    )
    assert env_key_names(path) == ["FOO", "BAZ"]


def test_env_key_names_tolerates_a_missing_file(tmp_path):
    assert env_key_names(tmp_path / "absent.env") == []


def test_profile_name_of_only_answers_for_profile_shaped_homes(tmp_path):
    assert profile_name_of(tmp_path / "profiles" / "alice") == "alice"
    assert profile_name_of(tmp_path / "hermesroot") is None


# ---------------------------------------------------------------------------
# The suspicious-home warning
# ---------------------------------------------------------------------------


def _trap_receipt(tmp_path):
    home = tmp_path / "profiles" / "base"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("ONLY=1\n", encoding="utf-8")
    return build_gateway_home_receipt(
        hermes_home=home,
        resolution=RESOLUTION_ENV_PROFILE_DIR,
        env_keys=env_key_names(home / ".env"),
    )


def test_warning_fires_for_the_trap_shape(tmp_path):
    """Kill: invert the resolution check — it would then never fire."""
    row = suspicious_home_row(
        _trap_receipt(tmp_path), installed_wrapper_profiles=["alice"]
    )
    assert row is not None
    assert row["suspected_profile"] == "alice"
    assert row["fix_hint"] == "run with --profile alice"


def test_warning_is_silent_for_a_flag_pinned_run(tmp_path):
    """A flag-pinned boot is never suspicious, token-less or not.

    Kill-mutation: drop the ``resolution != TRAP_RESOLUTION`` early return. Then
    every deliberate flag-pinned base gateway warns.
    """
    home = tmp_path / "profiles" / "base"
    home.mkdir(parents=True)
    (home / ".env").write_text("ONLY=1\n", encoding="utf-8")
    receipt = build_gateway_home_receipt(
        hermes_home=home,
        resolution=RESOLUTION_FLAG,
        env_keys=env_key_names(home / ".env"),
    )
    assert suspicious_home_row(receipt, installed_wrapper_profiles=["alice"]) is None


def test_warning_is_silent_when_no_other_profile_has_a_wrapper(tmp_path):
    """THE false-alarm guard the plan calls out.

    Kill-mutation: drop the wrapper-exists condition. Every bare base gateway on
    a box with no installed service then warns — which is how a real warning
    gets trained into background noise.

    Anti-vacuity: the receipt here is byte-identical to the firing case above;
    ONLY the wrapper set differs, so this can only go green via the condition
    under test.
    """
    assert suspicious_home_row(_trap_receipt(tmp_path), installed_wrapper_profiles=[]) is None
    # ... and a wrapper for the SAME profile is not a mismatch either.
    assert (
        suspicious_home_row(_trap_receipt(tmp_path), installed_wrapper_profiles=["base"])
        is None
    )


def test_warning_is_silent_when_the_home_does_have_a_telegram_key(tmp_path):
    """Kill: drop the telegram_configured check — a working alice would warn."""
    home = tmp_path / "profiles" / "alice"
    home.mkdir(parents=True)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")
    receipt = build_gateway_home_receipt(
        hermes_home=home,
        resolution=RESOLUTION_ENV_PROFILE_DIR,
        env_keys=env_key_names(home / ".env"),
    )
    assert suspicious_home_row(receipt, installed_wrapper_profiles=["base"]) is None


def test_wrapper_profiles_finds_installed_service_wrappers(tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / "alice" / "gateway-service").mkdir(parents=True)
    (profiles / "alice" / "gateway-service" / "Hermes_Gateway_alice.cmd").write_text(
        "@echo off\n", encoding="utf-8"
    )
    (profiles / "base").mkdir(parents=True)
    assert wrapper_profiles(profiles) == ["alice"]
    assert wrapper_profiles(tmp_path / "absent") == []


# ---------------------------------------------------------------------------
# Wrapper generator invariant
# ---------------------------------------------------------------------------


def test_wrapper_generator_refuses_a_single_pinned_named_profile(tmp_path):
    """Kill: remove the flag from the template (i.e. pass an empty profile_arg).

    Regenerating today's healthy double-pinned alice wrapper with an empty
    ``profile_arg`` is precisely the regression that would produce the
    trap-vulnerable single-pin form, so the generator must refuse rather than
    write it.
    """
    from hermes_cli.gateway_windows import (
        GatewayWrapperNotPinned,
        _assert_named_profile_wrapper_is_pinned,
    )

    home = str(tmp_path / "profiles" / "alice")
    with pytest.raises(GatewayWrapperNotPinned) as excinfo:
        _assert_named_profile_wrapper_is_pinned(home, "")
    assert "--profile alice" in str(excinfo.value)

    # A wrapper naming a DIFFERENT profile is just as wrong as none at all.
    with pytest.raises(GatewayWrapperNotPinned):
        _assert_named_profile_wrapper_is_pinned(home, "--profile base")

    # The correct double pin passes.
    _assert_named_profile_wrapper_is_pinned(home, "--profile alice")


def test_wrapper_generator_allows_a_non_profile_home(tmp_path):
    """The default/custom root has no name to carry. Kill: drop the early return."""
    from hermes_cli.gateway_windows import _assert_named_profile_wrapper_is_pinned

    _assert_named_profile_wrapper_is_pinned(str(tmp_path / "hermesroot"), "")



# ---------------------------------------------------------------------------
# The four resolution literals, on both sides of a seam nothing held together.
#
# `gateway_home_receipt` exports RESOLUTION_FLAG / _ENV_PROFILE_DIR /
# _ACTIVE_PROFILE_MARKER / _DEFAULT and its comment said they existed so
# "the pre-parse and the tests agree on the spelling rather than each
# hard-coding a string literal". The pre-parse does not import them — it writes
# all four inline, and always has. So the constants and the producer were two
# independent copies of one wire vocabulary with nothing comparing them, and a
# rename on either side would have gone unnoticed until a gateway read a rung
# name it did not recognise at boot.
#
# The pre-parse cannot simply import them: it runs before any hermes module is
# importable, which is the entire point of the pre-parse. So the seam stays, and
# this is the thing that holds it. It reads `_profile_bootstrap.py`, which is
# where the pre-parse lives since the entrypoint gate landed — the producer
# moved, the seam did not.
# ---------------------------------------------------------------------------


def _preparse_source() -> str:
    import pathlib

    import hermes_cli

    path = pathlib.Path(hermes_cli.__file__).with_name("_profile_bootstrap.py")
    text = path.read_text(encoding="utf-8", errors="replace")
    assert len(text) > 5_000, "_profile_bootstrap.py read came back too small - vacuous"
    return text


@pytest.mark.parametrize(
    "constant",
    [
        "RESOLUTION_FLAG",
        "RESOLUTION_ENV_PROFILE_DIR",
        "RESOLUTION_ACTIVE_PROFILE_MARKER",
        "RESOLUTION_DEFAULT",
    ],
)
def test_each_resolution_rung_is_the_literal_the_preparse_writes(constant):
    from hermes_cli import gateway_home_receipt

    value = getattr(gateway_home_receipt, constant)
    source = _preparse_source()
    assert f'"{value}"' in source or f"'{value}'" in source, (
        f"{constant} == {value!r}, and the pre-parse writes no such literal. It "
        "hard-codes the resolution rung it took into "
        f"{gateway_home_receipt.RESOLUTION_ENV_VAR}; the gateway reads it back "
        "through this constant. If one side was renamed, rename the other in "
        "the same commit."
    )


def test_the_preparse_writes_the_resolution_env_var_this_module_names():
    from hermes_cli import gateway_home_receipt

    assert gateway_home_receipt.RESOLUTION_ENV_VAR in _preparse_source(), (
        "the pre-parse no longer writes the env var this module reads the rung "
        "from"
    )
