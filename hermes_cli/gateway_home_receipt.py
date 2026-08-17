"""Typed receipt for "which HERMES_HOME did this gateway boot under, and why".

Fork-owned (the ``gateway/`` package is upstream; route around it). This module
answers the question the 2026-08-16 alice-gateway investigation could not answer
from logs: a gateway that comes up on the wrong profile home is
indistinguishable, after the fact, from one that came up on the right one — the
process just quietly has no ``TELEGRAM_*`` keys.

Two facts make that possible today:

1. ``hermes_cli/main.py:_apply_profile_override`` resolves the home through a
   FOUR-rung ladder (explicit flag → an inherited ``HERMES_HOME`` whose parent
   directory is literally named ``profiles`` → the sticky ``active_profile``
   marker → nothing), and rung 2 *returns early*, so the marker is never
   consulted. Nothing recorded which rung answered.
2. A profile's credentials live in ``<profile>/.env``. On the live box
   ``profiles/base/.env`` holds ONE key and ``profiles/alice/.env`` holds 18
   including the three ``TELEGRAM_*`` ones, so "which home" and "has a bot
   token" are the same question wearing two hats.

The receipt shape deliberately reuses ``ProfileContextRow.row()``'s
``{code, subject, summary, fix_hint}`` contract
(``agent_runtime/profile_context.py:65-74``) so operator surfaces need no new
case.

**Values are never read.** ``env_key_names`` parses key names and discards
everything right of the first ``=``. That is a security property, not a
convenience: the receipt is written to a log file the operator pastes into
chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

# Which rung of main.py's ladder answered. Exported so main.py and the tests
# agree on the spelling rather than each hard-coding a string literal.
RESOLUTION_FLAG = "flag"
RESOLUTION_ENV_PROFILE_DIR = "env_profile_dir"
RESOLUTION_ACTIVE_PROFILE_MARKER = "active_profile_marker"
RESOLUTION_DEFAULT = "default"

#: main.py writes the answering rung here; the gateway reads it back at boot.
#: An env var rather than a return value because the pre-parse runs before any
#: hermes module is importable — that is the whole point of the pre-parse.
RESOLUTION_ENV_VAR = "HERMES_PROFILE_RESOLUTION"

#: The rung that skips the sticky marker. A gateway that resolved this way took
#: its home from whoever spawned it.
TRAP_RESOLUTION = RESOLUTION_ENV_PROFILE_DIR

TELEGRAM_KEY_PREFIX = "TELEGRAM_"

RECEIPT_CODE = "gateway_home_resolution"
SUSPICIOUS_CODE = "gateway_home_suspicious"


def env_key_names(env_path: Path) -> list[str]:
    """Key names in a ``.env`` file. NEVER the values.

    Everything right of the first ``=`` is dropped before the caller can see
    it. ``export FOO=1`` and ``FOO=1`` both yield ``FOO``; comments and blank
    lines yield nothing. A missing or unreadable file yields ``[]`` — a receipt
    that cannot be produced must not take the gateway down with it.
    """
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            names.append(key)
    return names


def profile_name_of(hermes_home: Path) -> str | None:
    """The profile name a home encodes, or None for a non-profile home.

    Same shape test main.py's rung 2 uses (``parent.name == "profiles"``), so
    the receipt names the profile exactly when the ladder would have trusted it.
    """
    try:
        if hermes_home.parent.name == "profiles" and hermes_home.name:
            return hermes_home.name
    except (OSError, ValueError):
        return None
    return None


def wrapper_profiles(profiles_root: Path) -> list[str]:
    """Profiles that have an installed gateway service wrapper on disk.

    The wrapper is what an operator means by "the gateway is installed for
    alice". Used only to decide whether a token-less boot is *suspicious* — a
    base gateway on a box with no wrapper anywhere is perfectly ordinary.
    """
    found: list[str] = []
    try:
        entries = sorted(profiles_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            service_dir = entry / "gateway-service"
            if service_dir.is_dir() and any(service_dir.glob("*.cmd")):
                found.append(entry.name)
                continue
            if service_dir.is_dir() and any(service_dir.glob("*.vbs")):
                found.append(entry.name)
        except OSError:
            continue
    return found


def build_gateway_home_receipt(
    *,
    hermes_home: Path,
    resolution: str,
    env_keys: Sequence[str],
) -> dict:
    """The one typed line a gateway emits at boot.

    ``env_key_count``/``telegram_configured`` are derived from key NAMES only
    (see :func:`env_key_names`); no value reaches this function.
    """
    profile = profile_name_of(hermes_home)
    telegram_configured = any(
        str(key).startswith(TELEGRAM_KEY_PREFIX) for key in env_keys
    )
    summary = (
        f"gateway home {hermes_home} "
        f"(profile={profile or 'none'}, resolved by {resolution}), "
        f"{len(env_keys)} env key(s), "
        f"telegram_configured={str(telegram_configured).lower()}"
    )
    return {
        "code": RECEIPT_CODE,
        "subject": profile or str(hermes_home),
        "summary": summary,
        "fix_hint": "",
        "profile": profile,
        "hermes_home": str(hermes_home),
        "resolution": resolution,
        "env_key_count": len(env_keys),
        "telegram_configured": telegram_configured,
    }


def suspicious_home_row(
    receipt: dict,
    *,
    installed_wrapper_profiles: Iterable[str],
) -> dict | None:
    """A WARNING row when this boot looks like the step-1.5 trap, else None.

    All THREE conditions must hold, and each one independently spares:

    1. the answering rung is ``env_profile_dir`` — the gateway took its home
       from whoever spawned it and never consulted the sticky marker;
    2. the resolved home has no ``TELEGRAM_*`` key at all, so this gateway
       cannot serve Telegram no matter what else is true;
    3. some OTHER profile has an installed gateway wrapper, i.e. the operator
       has expressed "the gateway belongs to that profile".

    Condition 3 is what keeps this from crying wolf: a deliberate token-less
    gateway on a box with no wrapper is a legitimate configuration, and warning
    on it would train the operator to ignore the row. Warn, never refuse — a
    base gateway can be exactly what was wanted.
    """
    if receipt.get("resolution") != TRAP_RESOLUTION:
        return None
    if receipt.get("telegram_configured"):
        return None
    profile = receipt.get("profile")
    others = [name for name in installed_wrapper_profiles if name != profile]
    if not others:
        return None
    target = others[0]
    return {
        "code": SUSPICIOUS_CODE,
        "subject": profile or str(receipt.get("hermes_home", "")),
        "summary": (
            f"gateway booted on '{profile or receipt.get('hermes_home')}' with no "
            f"TELEGRAM_* key, taking its home from the inherited environment; "
            f"profile '{target}' has an installed gateway service wrapper"
        ),
        "fix_hint": f"run with --profile {target}",
        "resolution": receipt.get("resolution"),
        "suspected_profile": target,
    }
