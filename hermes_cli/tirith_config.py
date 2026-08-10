"""Single authority for resolving the ``security.tirith_*`` flags.

``hermes_cli/tips.py`` ships this promise to operators::

    TIRITH_FAIL_OPEN env var overrides the tirith_fail_open config — a quick
    toggle without editing config.yaml.

Five decision points read those flags, and before this module only one of
them (``tools.tirith_security._load_security_config``) honoured the override.
The other four read ``hermes_cli.config`` directly and could not see it, so a
documented toggle was inert at four of five sites:

* ``cli.py`` — the "tirith enabled but unavailable" startup notice.
* ``gateway/run.py`` — the "manual approvals with no risk assessor" warning.
* ``tools/approval.py`` — the cron-deny lane, on tirith ``ImportError``.
* ``tools/approval.py`` — the main flow, on tirith ``ImportError``.

This module states the default, the ``tirith_enabled`` interaction, and the
env override **once**, and all five sites read it.

Import weight is load-bearing, not incidental
---------------------------------------------
Two of the callers sit inside ``except ImportError: from tools.tirith_security
import ...`` — they run *precisely when that module failed to import*, so the
seam cannot live there. It equally must not drag ``hermes_cli.config`` in at
import time: that module costs ~200 transitive imports (urllib.request,
http.client, agent.*) and several subsystems load tools while it is still
initializing (see ``tools/__init__.py``). So the only import here is
``hermes_cli.config_defaults``, which is pure data, and the config load stays
lazy inside the resolvers — exactly where the old inline copies did it.
``tests/hermes_cli/test_tirith_config.py`` pins that property.
"""

import os

from hermes_cli.config_defaults import DEFAULT_CONFIG

#: The defaults, stated where ``config.yaml`` already states them. Sourcing
#: them from ``DEFAULT_CONFIG`` rather than re-typing ``True`` here is what
#: keeps this module an *accessor* and not a fourth copy of the defaults.
SECURITY_DEFAULTS = dict(DEFAULT_CONFIG.get("security", {}) or {})

ENABLED_ENV_VAR = "TIRITH_ENABLED"
FAIL_OPEN_ENV_VAR = "TIRITH_FAIL_OPEN"

#: Deliberately narrower than ``utils.TRUTHY_STRINGS`` (which also accepts
#: ``"on"``). This set is the one ``tools.tirith_security._env_bool`` has
#: always used, and it is the only place either override has ever bound, so
#: widening it here would silently change what an operator's existing
#: ``TIRITH_FAIL_OPEN`` value decides. Anything unrecognised reads as False —
#: e.g. ``TIRITH_FAIL_OPEN=on`` means fail-closed.
_TRUTHY = frozenset({"1", "true", "yes"})


def _env_bool(key: str, default: bool) -> bool:
    """Resolve one env override. Unset leaves *default* untouched."""
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in _TRUTHY


def load_config_or_empty() -> dict:
    """Load the merged config, or ``{}`` when it cannot be read.

    The import is deliberately function-local — see the module docstring.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def security_section(config: "dict | None" = None) -> dict:
    """Return the ``security`` mapping from *config*, loading it if omitted.

    Pass an already-loaded config (``self.config`` in the CLI, the gateway's
    ``_load_full_config()`` result) to avoid a second load on a startup or
    per-command hot path. Never raises: an unreadable or malformed config
    degrades to ``{}`` so the defaults below apply, which is the same
    "safe default if config is unreadable" posture the inline copies had.
    """
    if config is None:
        config = load_config_or_empty()
    try:
        return dict(config.get("security", {}) or {})
    except Exception:
        return {}


def _enabled_from_section(section: dict) -> bool:
    return _env_bool(
        ENABLED_ENV_VAR,
        section.get("tirith_enabled", SECURITY_DEFAULTS["tirith_enabled"]),
    )


def _fail_open_from_section(section: dict) -> bool:
    return _env_bool(
        FAIL_OPEN_ENV_VAR,
        section.get("tirith_fail_open", SECURITY_DEFAULTS["tirith_fail_open"]),
    )


def tirith_enabled(config: "dict | None" = None) -> bool:
    """Is pre-exec tirith scanning switched on?

    ``config.yaml``'s ``security.tirith_enabled`` (default ``True``),
    overridden by the ``TIRITH_ENABLED`` env var when it is set.
    """
    return _enabled_from_section(security_section(config))


def tirith_fail_open(config: "dict | None" = None) -> bool:
    """Should an *operational* tirith failure allow the command through?

    ``config.yaml``'s ``security.tirith_fail_open`` (default ``True``),
    overridden by the ``TIRITH_FAIL_OPEN`` env var when it is set. This is the
    raw flag: it does **not** fold in ``tirith_enabled``, because
    ``tools.tirith_security`` checks the two independently — it returns
    ``allow`` up front when scanning is off, and only consults this flag on
    paths where scanning was actually attempted.
    """
    return _fail_open_from_section(security_section(config))


def fail_open_when_scanner_unavailable(config: "dict | None" = None) -> bool:
    """May a command run when the tirith scanner cannot run *at all*?

    This is the question ``tools/approval.py`` asks in its two
    ``except ImportError`` branches, named once instead of re-derived twice.
    ``True`` means allow as before; ``False`` means the operator opted into
    fail-closed and the command must not be silently permitted.

    The ``tirith_enabled`` interaction, stated once: when scanning is switched
    off there is no scanner to fail against, so an unavailable scanner is not
    a reason to withhold a command. That mirrors what
    ``tools.tirith_security.check_command_security`` already does when tirith
    *is* importable but disabled (``if not cfg["tirith_enabled"]: return
    allow``), and it is the rule the inline copies encoded too — they just
    read ``tirith_enabled`` from config only.
    """
    section = security_section(config)
    if not _enabled_from_section(section):
        return True
    return _fail_open_from_section(section)
