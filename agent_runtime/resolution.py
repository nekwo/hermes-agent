from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from .parse_cache import cached_yaml_file
from .errors import ProbeIsolationViolation, RuntimeRootMismatch

# Env marker a probe run sets to demand hard isolation. When truthy, the runtime root
# MUST be a dedicated ``agent-runtime-probe-*`` temp dir won via the env layer, or every
# store access fails fast (see ``assert_probe_isolation`` + ``paths.store_root``).
PROBE_ISOLATION_ENV = "HERMES_REQUIRE_ISOLATED_ROOT"
PROBE_ROOT_PREFIX = "agent-runtime-probe"
_FALSEY = frozenset({"", "0", "false", "no", "off"})


@dataclass(slots=True, frozen=True)
class RuntimeResolution:
    store_root: Path
    layer: str
    hermes_home: str | None
    config_path: str
    trace: tuple[str, ...]


def resolve_runtime(env: Mapping[str, str] | None = None) -> RuntimeResolution:
    source = os.environ if env is None else env
    trace: list[str] = []
    hermes_home = _hermes_home(source)
    config_path = hermes_home / "config.yaml"

    env_root = str(source.get("HERMES_AGENT_RUNTIME_ROOT") or "").strip()
    if env_root:
        root = Path(env_root).expanduser()
        trace.append(f"env HERMES_AGENT_RUNTIME_ROOT won: {root}")
        trace.append(f"config {config_path} skipped: env won")
        trace.append("default skipped: env won")
        return RuntimeResolution(root, "env", str(hermes_home), str(config_path), tuple(trace))
    trace.append("env HERMES_AGENT_RUNTIME_ROOT skipped: unset")

    configured = _configured_store_root(config_path)
    if configured:
        root = Path(configured).expanduser()
        trace.append(f"config agent_runtime.store_root won: {root}")
        trace.append("default skipped: config won")
        return RuntimeResolution(root, "config", str(hermes_home), str(config_path), tuple(trace))
    trace.append(f"config agent_runtime.store_root skipped: unset ({config_path})")

    root = _default_hermes_root(source) / "agent-runtime"
    trace.append(f"default won: {root}")
    return RuntimeResolution(root, "default", str(hermes_home), str(config_path), tuple(trace))


def assert_pinned(resolution: RuntimeResolution, *, pinned_root: str) -> None:
    expected = _normalized_path(Path(pinned_root).expanduser())
    actual = _normalized_path(resolution.store_root)
    if actual != expected:
        raise RuntimeRootMismatch(
            f"Runtime root mismatch: resolved {resolution.store_root} via {resolution.layer}, pinned {pinned_root}"
        )


def probe_isolation_required(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(PROBE_ISOLATION_ENV) or "").strip().lower() not in _FALSEY


def assert_probe_isolation(
    resolution: RuntimeResolution | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail fast when a probe run would resolve the live/default store root.

    No-op unless ``HERMES_REQUIRE_ISOLATED_ROOT`` is truthy. When set, the resolution
    must have been won by the env layer AND point at an ``agent-runtime-probe-*`` root;
    anything else (config/default layer, or an env root without the probe prefix) means
    the probe would write into the live store, so raise ``ProbeIsolationViolation``.
    """
    source = os.environ if env is None else env
    if not probe_isolation_required(source):
        return
    item = resolution or resolve_runtime(source)
    basename = item.store_root.name
    if item.layer == "env" and basename.startswith(PROBE_ROOT_PREFIX):
        return
    raise ProbeIsolationViolation(
        f"{PROBE_ISOLATION_ENV} is set but the resolved runtime root is not an isolated "
        f"probe root: resolved {item.store_root} via '{item.layer}' layer. Set "
        f"HERMES_AGENT_RUNTIME_ROOT to a '{PROBE_ROOT_PREFIX}-*' temp dir so the probe "
        "cannot persist into the live store."
    )


def resolution_table(env: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    resolution = resolve_runtime(env)
    source = os.environ if env is None else env
    config_path = Path(resolution.config_path)
    configured = _configured_store_root(config_path)
    rows = [
        _table_row("env", source.get("HERMES_AGENT_RUNTIME_ROOT"), winner=resolution.layer == "env"),
        _table_row("config", configured, winner=resolution.layer == "config"),
        _table_row("default", str(_default_hermes_root(source) / "agent-runtime"), winner=resolution.layer == "default"),
    ]
    return rows


def resolution_payload(resolution: RuntimeResolution | None = None) -> dict[str, object]:
    item = resolution or resolve_runtime()
    return {
        "store_root": str(item.store_root),
        "layer": item.layer,
        "trace": list(item.trace),
    }


def suspect_default_root(resolution: RuntimeResolution | None = None) -> bool:
    item = resolution or resolve_runtime()
    return item.layer == "default" and not (item.store_root / "tasks").is_dir()


def _configured_store_root(config_path: Path) -> str:
    raw = cached_yaml_file(config_path, default=None)
    if isinstance(raw, dict):
        return str((raw.get("agent_runtime") or {}).get("store_root") or "").strip()
    return ""


def _table_row(layer: str, value: object, *, winner: bool) -> dict[str, object]:
    text = str(value or "").strip()
    path = Path(text).expanduser() if text else None
    return {
        "layer": layer,
        "value": str(path) if path is not None else None,
        "exists": path.exists() if path is not None else False,
        "winner": winner,
    }


def _hermes_home(env: Mapping[str, str]) -> Path:
    value = str(env.get("HERMES_HOME") or "").strip()
    return Path(value).expanduser() if value else _platform_default_hermes_home(env)


def _default_hermes_root(env: Mapping[str, str]) -> Path:
    native_home = _platform_default_hermes_home(env)
    env_home = str(env.get("HERMES_HOME") or "").strip()
    if not env_home:
        return native_home
    env_path = Path(env_home).expanduser()
    try:
        env_path.resolve().relative_to(native_home.resolve())
        return native_home
    except (OSError, ValueError):
        pass
    if env_path.parent.name == "profiles":
        return env_path.parent.parent
    return env_path


def _platform_default_hermes_home(env: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        local_appdata = str(env.get("LOCALAPPDATA") or "").strip()
        base = Path(local_appdata).expanduser() if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _normalized_path(path: Path) -> str:
    try:
        return str(path.resolve(strict=False)).casefold()
    except OSError:
        return str(path.absolute()).casefold()
