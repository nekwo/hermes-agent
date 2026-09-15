"""Portable persona-definition projection for realm sync.

Realm sync used to publish each bound profile's **raw** ``config.yaml`` as the
``persona_config`` artifact and overwrite the member's file wholesale on pull
(``_destination_for_sync_path`` → ``<profile_home>/config.yaml`` → generic
write loop). That made it the last blind last-write-wins lane in the sync
surface — boards and the Mission Office got a 3-way baseline merge, skills got
the guarded inbox door — and it leaked two classes of content that must never
leave a machine:

1. **The base seed.** A persona bound to ``hermes_profile: base`` published
   ``profiles/base/config.yaml``; a member's pull overwrote THEIR fork seed —
   the file every new profile on that machine is created from. The Office
   realm-sync plan §5.1
   (``docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md``,
   launcher repo) explicitly ruled the base seed must never sync, but the guard
   was written against the base *persona id*, so
   a persona merely *bound to* the base profile home walked straight past it.
2. **Machine/installation-shaped content.** ``mcp_servers`` commands/args/env,
   absolute Windows paths, venv/install pointers, auth-shaped values. On another
   member's machine (or any non-Windows machine) they resolve to nothing and the
   member silently inherits a dead MCP server.

The fix is not "stop publishing the config" — persona DEFINITIONS live inside
``config.yaml`` (``agent_runtime.personas.<id>``), so an Office placement or a
workspace roster would otherwise reference a persona no member can materialize.
The fix is to publish a **synthesized projection**: an explicit allowlist of
shareable persona-definition keys, pruned to the persona set this realm actually
publishes, deterministic (sorted keys, LF) so republish is a no-op, and merged
key-wise on the pull side against a never-synced baseline with the same
adopt / converge / hold vocabulary the board / office / skill lanes use.

**Which SOURCE a published body is built from** (2026-07-25 regression, fixed):
the first cut treated *presence in* ``agent_runtime.personas.<id>`` as
completeness and published the raw override alone whenever the id appeared
there. On the live machine every persona carried a ONE-key override
(``chat_lane_restore_toolsets``) next to a 24-key resolved store record, so the
published ``store/personas.yaml`` was 304 bytes of one-key bodies — and the
accounting reported no drops, nothing synthesized, nothing missing: a clean
publish. A member pulling it adopted ``chat_lane_restore_toolsets`` and nothing
else, leaving Office placements pointing at personas they could not materialize.

Every published body is now built from the RESOLVED PERSONA RECORD — what
``config.ensure_persisted_personas`` returns (``{**catalog, **stored}``, store
wins) — with the allowlisted raw override filling in only the keys a record
cannot carry (``chat_lane_restore_toolsets`` and ``skills_remove`` are not
``AgentPersona`` fields, so config is their only source). The record wins on
conflict because the record is what this machine's runtime actually runs;
publishing the config value instead would ship a definition nothing here uses.
Disagreements are ACCOUNTED (``config_shadowed_keys``), never silently resolved.

Everything in this module is pure with respect to its inputs (the raw config
mapping and the persona records are injectable) so the allowlist, the pruning,
the portability validator, and the merge decision table are unit-testable
without a git repo.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_config_path

from .sync_merge import PullAction, classify_three_way_pull

# --- projection contract ---------------------------------------------------

PROJECTION_KIND = "realm_persona_config"
PROJECTION_SCHEMA_VERSION = 1

#: Published relative path of the synthesized projection inside a realm subtree.
#: Deliberately NOT ``profiles/<name>/config.yaml``: an older hermes maps that
#: path to ``<profile_home>/config.yaml`` and would overwrite a member's real
#: config with this personas-only document. ``store/personas.yaml`` is an
#: unknown path to every older client (``_destination_for_sync_path`` returns
#: ``None`` → the artifact is skipped), so old members degrade to "no persona
#: definitions" instead of losing their config.
PROJECTION_RELATIVE_PATH = "store/personas.yaml"

#: The ONLY persona-definition keys that may leave this machine.
#:
#: Deliberately excluded, with the reason:
#: - ``repo_scope`` — an absolute filesystem path to a checkout that exists only
#:   here (``X:/Unreal Engine/...``). ``repo_scope_label`` carries the portable
#:   half of that intent and IS published.
#: - ``readiness`` — runtime/derived state, not authored definition.
#: - ``model_override_issued_at`` — a local write-ordering guard (supersede
#:   clock); travelling it would let a stale realm snapshot win a local race.
#: - anything unknown — new keys are opt-in, never opt-out. Unknown keys are
#:   ACCOUNTED (``PersonaConfigProjection.dropped_keys``), never silently eaten.
PERSONA_DEF_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "api_mode",
        "autonomy",
        "chat_lane_restore_toolsets",
        "display_name",
        "hermes_profile",
        "include_core_context_files",
        "include_profile_memory",
        "iteration_budget",
        "max_api_calls",
        "max_total_tokens",
        "max_wall_seconds",
        "model",
        "provider",
        "repo_scope_label",
        "required_mcp_servers",
        "role",
        "skills",
        "skills_remove",
        "soul_overlay_path",
        "system_prompt_path",
        "toolsets",
    }
)

#: Non-persona configuration that is safe to publish alongside the definitions.
#:
#: Deliberately EMPTY. Everything else in ``config.yaml`` today is one of:
#: machine-shaped (``mcp_servers`` commands/env, paths), account-shaped
#: (provider/model defaults, gateway credentials), install-shaped (venv/plugin
#: pointers), or a member-local preference (display/skin/logging). Adopting a
#: publisher's harness feature flags onto a member's runtime is exactly the
#: invasiveness §5.1 refused for the base seed. Kept as a named constant so
#: promoting a genuinely portable policy key later is a one-line, reviewable
#: change with a test — not a quiet widening of the projection.
PORTABLE_CONFIG_KEYS: frozenset[str] = frozenset()

#: Persona-record attributes that are structural identity/versioning rather than
#: authored definition content. Excluded from the projection AND from the drop
#: accounting: reporting ``personas.dev.id`` as a dropped key on every publish is
#: noise that buries the drops an operator must actually read.
RECORD_STRUCTURAL_FIELDS: frozenset[str] = frozenset({"id", "schema_version"})

#: Keys without which a pulled definition cannot be materialized into a usable
#: persona — an Office placement would reference a name and a role that are not
#: there. The projection cannot invent what neither source carries, so a body
#: missing one of these is ACCOUNTED (``PersonaConfigProjection.incomplete``)
#: rather than refused: refusing would brick the whole realm publish over one
#: under-declared persona. What it must never be again is SILENT.
PERSONA_DEF_REQUIRED_KEYS: frozenset[str] = frozenset({"display_name", "role"})

#: Persona ids are written back with ``atomic_roundtrip_yaml_update`` on a
#: DOTTED key path (``agent_runtime.personas.<id>``), so a ``.`` in an id would
#: silently nest a new mapping level. Also the traversal guard for an untrusted
#: remote id.
_PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


# --- portability validation -------------------------------------------------

# A Windows drive-letter path (``X:\...`` / ``x:/...``) ANYWHERE in a value. The
# negative lookbehind keeps URL schemes out: ``http://host`` contains ``p://``
# but its ``p`` is preceded by ``t``. Case-insensitive by construction.
_DRIVE_LETTER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")

# A UNC share (``\\host\share``). Written as two literal backslashes followed by
# a hostname component and a separator.
_UNC_RE = re.compile(r"\\\\[A-Za-z0-9_.-]+[\\/]")

# A POSIX-absolute path as the WHOLE value: leading ``/`` plus at least two
# whitespace-free segments (``/home/tony/repo``, ``/opt/hermes/bin``). Anchored
# and whitespace-free on purpose — an unanchored ``^/`` rule fires on prose like
# ``"/help /clear"`` and a refusal that bricks a publish over a display-name
# false positive is a failure class this file has already paid for. The known,
# accepted miss is a POSIX path containing spaces (``/home/my user/repo``);
# missing a rare path beats bricking every publish.
_POSIX_ABS_RE = re.compile(r"^/(?:[^/\s]+/)+[^/\s]*$")


def nonportable_reason(value: Any) -> str | None:
    """Return a typed reason when ``value`` is machine/installation-shaped.

    ``None`` means portable. Non-strings are always portable (numbers, bools).
    Whitespace cannot defeat the check: drive-letter/UNC are scanned anywhere in
    the raw string, and the POSIX rule runs on the stripped value.
    """

    if not isinstance(value, str):
        return None
    if _DRIVE_LETTER_RE.search(value):
        return "drive_letter_path"
    if _UNC_RE.search(value):
        return "unc_path"
    if _POSIX_ABS_RE.match(value.strip()):
        return "posix_absolute_path"
    return None


def find_nonportable_values(data: Any, *, prefix: str = "") -> list[dict[str, str]]:
    """Walk a projected structure and return EVERY machine-shaped leaf.

    Rows are ``{"key": <dotted path>, "reason": <typed>, "value": <preview>}``.
    All offenders are returned in one pass so the caller can name them in a
    single typed error (the "name ALL offenders" precedent) instead of making an
    operator re-run the publish once per bad key.
    """

    offenders: list[dict[str, str]] = []
    if isinstance(data, dict):
        for key in sorted(data, key=str):
            offenders.extend(find_nonportable_values(data[key], prefix=f"{prefix}.{key}" if prefix else str(key)))
        return offenders
    if isinstance(data, (list, tuple)):
        for index, item in enumerate(data):
            offenders.extend(find_nonportable_values(item, prefix=f"{prefix}[{index}]"))
        return offenders
    reason = nonportable_reason(data)
    if reason is not None:
        offenders.append({"key": prefix or "<root>", "reason": reason, "value": str(data)[:200]})
    return offenders


NONPORTABLE_HINT = (
    "Machine-shaped values cannot travel to another member (or another OS). "
    "Remove the absolute path from the persona definition, or express it "
    "portably — 'repo_scope' is already excluded from realm sync; use "
    "'repo_scope_label' for the human name and let each member bind their own "
    "checkout. MCP server commands/env are never published."
)


# --- projection --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonaConfigProjection:
    """The synthesized, publishable persona-definition document.

    ``personas`` is the pruned + allowlisted definition map. Everything else is
    accounting, and every key that was dropped, shadowed, borrowed from config,
    or absent has a row — a publish must never again be able to ship a partial
    definition and report a clean result.

    - ``dropped_keys`` — every key the allowlist removed, from EITHER source
      (record attributes such as ``repo_scope`` / ``readiness`` included).
    - ``synthesized`` — ids whose published body came entirely from the resolved
      persona record: ``config.yaml`` contributed no key to it. (Before
      2026-07-25 this meant "the id had no raw override at all"; the record is
      now the base for every body, so the useful signal is what config added.)
    - ``config_only`` — ids with a raw override but NO resolvable record. Their
      body is the raw declaration alone; nothing else exists to build from.
    - ``config_contributed_keys`` — dotted paths whose value came from the raw
      override because the record could not carry them
      (``chat_lane_restore_toolsets``, ``skills_remove``, or a key the record
      resolved empty).
    - ``config_shadowed_keys`` — dotted paths where ``config.yaml`` and the
      resolved record disagreed and the RECORD won. This is a config-vs-store
      divergence made visible at publish time instead of at a member's pull.
    - ``incomplete`` — published bodies missing a
      :data:`PERSONA_DEF_REQUIRED_KEYS` key; a member can adopt them but cannot
      fully materialize them.
    - ``missing`` — wanted ids with neither a record nor a usable override.
    """

    personas: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped_keys: list[str] = field(default_factory=list)
    synthesized: list[str] = field(default_factory=list)
    config_only: list[str] = field(default_factory=list)
    config_contributed_keys: list[str] = field(default_factory=list)
    config_shadowed_keys: list[str] = field(default_factory=list)
    incomplete: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "kind": PROJECTION_KIND,
            "personas": self.personas,
            "schema_version": PROJECTION_SCHEMA_VERSION,
        }

    def to_bytes(self) -> bytes:
        """Deterministic bytes: sorted keys, block style, LF. Republishing an
        unchanged projection is a byte-for-byte no-op, so the publish
        change-detector (``_published_artifacts_differ``) stays honest."""

        text = yaml.safe_dump(
            self.document(),
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=4096,
        )
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    def hashes(self) -> dict[str, str]:
        return {persona_id: persona_def_hash(body) for persona_id, body in self.personas.items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "personas": sorted(self.personas),
            "dropped_keys": list(self.dropped_keys),
            "synthesized": list(self.synthesized),
            "config_only": list(self.config_only),
            "config_contributed_keys": list(self.config_contributed_keys),
            "config_shadowed_keys": list(self.config_shadowed_keys),
            "incomplete": [dict(row) for row in self.incomplete],
            "missing": list(self.missing),
        }


def persona_def_hash(body: Any) -> str:
    """Semantic content hash of one persona definition (key-order independent)."""

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """Coerce to plain YAML-safe scalars/containers, or raise ``TypeError``.

    Anything exotic (datetimes, dataclasses, ruamel nodes, sets) is refused so a
    published projection can never carry a type whose YAML round-trip differs
    between members — determinism is load-bearing for the change detector and
    the content hash.
    """

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    raise TypeError(type(value).__name__)


def _allowlist_persona_def(persona_id: str, raw: Any, dropped: list[str]) -> dict[str, Any]:
    """Project ONE persona definition through :data:`PERSONA_DEF_ALLOWED_KEYS`.

    Every removed key is appended to ``dropped`` as a dotted path so the publish
    result can account for it — nothing is silently eaten.
    """

    body: dict[str, Any] = {}
    if not isinstance(raw, dict):
        dropped.append(f"personas.{persona_id}")
        return body
    for key in sorted(raw, key=str):
        name = str(key)
        if name not in PERSONA_DEF_ALLOWED_KEYS:
            dropped.append(f"personas.{persona_id}.{name}")
            continue
        try:
            body[name] = _plain(raw[key])
        except TypeError:
            dropped.append(f"personas.{persona_id}.{name}")
    return body


def _record_field_names(record: Any) -> list[str]:
    """Definition-bearing attribute names carried by a resolved persona record.

    Dataclass fields when the record is one (``AgentPersona``), else its public
    attributes — the merge stays duck-typed so it is unit-testable without
    constructing a full store record. Deriving the name set FROM the record
    rather than hard-coding it is what keeps a newly added ``AgentPersona`` field
    accounted (as a drop) the day it lands instead of silently invisible: new
    keys are opt-in to the projection, never opt-out of the reporting.
    """

    try:
        names = [item.name for item in fields(record)]
    except TypeError:
        names = [name for name in dir(record) if not name.startswith("_")]
    return sorted(
        name
        for name in names
        if name not in RECORD_STRUCTURAL_FIELDS and not callable(getattr(record, name, None))
    )


def _record_to_def(persona_id: str, record: Any, dropped: list[str]) -> dict[str, Any]:
    """Project ONE resolved persona record through :data:`PERSONA_DEF_ALLOWED_KEYS`.

    This is the BASE of every published body (see the module docstring). The
    record is what ``config.ensure_persisted_personas`` resolves, so building
    from it is what keeps the projection in agreement with the runtime — and
    what retired the 2026-07-25 partial publish, where an id's mere presence in
    the raw override map was mistaken for a complete definition.

    Every attribute the allowlist excludes is appended to ``dropped``, so
    ``repo_scope`` (an absolute checkout path that exists only on this machine),
    ``readiness`` (derived runtime state) and ``model_override_issued_at`` (a
    local write-ordering clock) are reported rather than silently absent.
    """

    body: dict[str, Any] = {}
    for name in _record_field_names(record):
        if name not in PERSONA_DEF_ALLOWED_KEYS:
            dropped.append(f"personas.{persona_id}.{name}")
            continue
        value = getattr(record, name, None)
        if value is None or value == [] or value == {}:
            continue
        try:
            body[name] = _plain(value)
        except TypeError:
            dropped.append(f"personas.{persona_id}.{name}")
    return body


def raw_persona_overrides(config: Any) -> dict[str, Any]:
    """``agent_runtime.personas`` out of a parsed ``config.yaml`` mapping."""

    if not isinstance(config, dict):
        return {}
    runtime = config.get("agent_runtime")
    if not isinstance(runtime, dict):
        return {}
    personas = runtime.get("personas")
    return dict(personas) if isinstance(personas, dict) else {}


def load_raw_config(config_path: Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path is not None else get_config_path()
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def project_persona_definitions(
    persona_ids: list[str] | set[str],
    *,
    raw_config: dict[str, Any] | None = None,
    records: dict[str, Any] | None = None,
) -> PersonaConfigProjection:
    """Build the portable projection for exactly ``persona_ids``.

    Each body is the RESOLVED RECORD projected through the allowlist, with the
    allowlisted raw ``config.yaml`` override contributing ONLY the keys the
    record could not resolve. Presence in ``agent_runtime.personas.<id>`` is not
    completeness — treating it as completeness is exactly the defect this
    ordering retires (module docstring). Where both sources carry a key and
    disagree the record wins, because the record is what the runtime runs; the
    shadowed config key is reported, not silently discarded.

    Pruning to the wanted set is part of the contract: the projection must not
    leak definitions for personas this realm is not publishing
    (``resolve_realm_sync_artifacts`` already computes that set).
    """

    overrides = raw_persona_overrides(raw_config if raw_config is not None else load_raw_config())
    records = records or {}
    personas: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    synthesized: list[str] = []
    config_only: list[str] = []
    config_contributed: list[str] = []
    config_shadowed: list[str] = []
    incomplete: list[dict[str, Any]] = []
    missing: list[str] = []
    for persona_id in sorted({str(item) for item in persona_ids}):
        if not _PERSONA_ID_RE.match(persona_id):
            dropped.append(f"personas.{persona_id}")
            continue
        record = records.get(persona_id)
        raw_override = overrides[persona_id] if persona_id in overrides else None
        if record is None and raw_override is None:
            missing.append(persona_id)
            continue
        body = _record_to_def(persona_id, record, dropped) if record is not None else {}
        override_body = (
            _allowlist_persona_def(persona_id, raw_override, dropped) if raw_override is not None else {}
        )
        contributed = 0
        for name in sorted(override_body):
            if name in body:
                # Both sources carry it. The record is runtime truth; naming the
                # divergence is how a config-vs-store drift stops being invisible
                # until a member pulls a definition their publisher never read.
                if body[name] != override_body[name]:
                    config_shadowed.append(f"personas.{persona_id}.{name}")
                continue
            body[name] = override_body[name]
            config_contributed.append(f"personas.{persona_id}.{name}")
            contributed += 1
        if not body:
            missing.append(persona_id)
            continue
        personas[persona_id] = body
        if record is None:
            config_only.append(persona_id)
        elif not contributed:
            synthesized.append(persona_id)
        absent = sorted(PERSONA_DEF_REQUIRED_KEYS - set(body))
        if absent:
            incomplete.append({"persona_id": persona_id, "missing_keys": absent})
    # ``PORTABLE_CONFIG_KEYS`` is empty by design (see the constant). Promoting a
    # key means wiring it into ``document()`` AND the pull merge; fail loud rather
    # than let a widened allowlist look like it published something it did not.
    assert not PORTABLE_CONFIG_KEYS, "PORTABLE_CONFIG_KEYS has no projection wiring yet"
    return PersonaConfigProjection(
        personas=personas,
        dropped_keys=sorted(set(dropped)),
        synthesized=sorted(set(synthesized)),
        config_only=sorted(set(config_only)),
        config_contributed_keys=sorted(set(config_contributed)),
        config_shadowed_keys=sorted(set(config_shadowed)),
        incomplete=sorted(incomplete, key=lambda row: str(row.get("persona_id"))),
        missing=sorted(set(missing)),
    )


# --- baseline sidecar (never synced, never published) ------------------------


def read_persona_config_baseline(realm_id: str) -> dict[str, str]:
    from . import paths

    path = paths.persona_config_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_persona_config_baseline(realm_id: str, entries: dict[str, str]) -> None:
    from utils import atomic_json_write

    from . import paths

    atomic_json_write(
        paths.persona_config_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def update_persona_config_baseline_after_publish(realm_id: str, projection: PersonaConfigProjection) -> None:
    """Record the published definition hashes as the new baseline so the next
    pull sees local == baseline (no spurious conflict on my own publish)."""

    baseline = read_persona_config_baseline(realm_id)
    baseline.update(projection.hashes())
    write_persona_config_baseline(realm_id, baseline)


# --- pull -------------------------------------------------------------------


@dataclass(slots=True)
class PersonaConfigPullSummary:
    """Typed accounting for the persona-definition pull merge.

    Nothing is silently dropped and nothing is silently overwritten:

    - ``adopted`` — the member had no copy, or an unchanged copy the realm moved
      forward; written key-wise into ``agent_runtime.personas.<id>``.
    - ``converged`` — local already equals remote (no write).
    - ``kept_local`` — the member edited it and the realm did not; stays local
      and unpublished.
    - ``held`` — BOTH sides changed (or edit-vs-remove): the member's definition
      is left untouched and surfaced for an explicit resolve. Divergent content
      is never clobbered (skill-inbox precedent).
    - ``retained`` — the realm stopped publishing it. Unlike a board card, a
      persona definition is referenced by Office placements, persona instances,
      and running assignments, so it is NEVER removed from a member's config by
      a sync; it is kept and reported.
    - ``refused`` — a definition the guarded door would not admit (hostile id,
      machine-shaped value, secret-shaped value). Per-entity isolation: one bad
      definition can never abort the pull.
    """

    adopted: list[str] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    dropped_keys: list[str] = field(default_factory=list)
    source: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.adopted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": sorted(set(self.adopted)),
            "converged": sorted(set(self.converged)),
            "kept_local": sorted(set(self.kept_local)),
            "held": sorted(set(self.held)),
            "retained": sorted(set(self.retained)),
            "refused": list(self.refused),
            "dropped_keys": sorted(set(self.dropped_keys)),
            "source": self.source,
        }


def _projection_from_document(data: Any) -> tuple[dict[str, Any], list[str]] | None:
    """Parse the NEW ``store/personas.yaml`` document → (defs, dropped)."""

    if not isinstance(data, dict) or data.get("kind") != PROJECTION_KIND:
        return None
    personas = data.get("personas")
    if not isinstance(personas, dict):
        return {}, []
    dropped: list[str] = []
    defs: dict[str, Any] = {}
    for persona_id in sorted(personas, key=str):
        body = _allowlist_persona_def(str(persona_id), personas[persona_id], dropped)
        if body:
            defs[str(persona_id)] = body
    return defs, dropped


def read_remote_persona_defs(subtree: Path) -> tuple[dict[str, Any], list[str], str | None]:
    """Persona definitions carried by a pulled realm subtree.

    Bidirectional version tolerance lives here:

    - **New publisher** → ``store/personas.yaml`` (the projection). Re-projected
      through the same allowlist on ingest — a publisher is not trusted to have
      filtered correctly.
    - **Older publisher** → legacy raw ``profiles/<name>/config.yaml``. Their
      ``agent_runtime.personas`` map is projected on READ, so an old realm's
      persona definitions still travel while its ``mcp_servers`` / machine paths
      / venv pointers are dropped at the door and the member's own config is
      never overwritten wholesale.

    Returns ``(defs, dropped_keys, source)``; ``source`` is ``None`` when the
    subtree carries no persona definitions at all (nothing to reconcile).
    """

    projection_path = subtree.joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    if projection_path.is_file():
        try:
            data = yaml.safe_load(projection_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            data = None
        parsed = _projection_from_document(data)
        if parsed is not None:
            defs, dropped = parsed
            return defs, dropped, "projection"

    profiles_root = subtree / "profiles"
    if not profiles_root.is_dir():
        return {}, [], None
    defs: dict[str, Any] = {}
    dropped: list[str] = []
    found = False
    for profile_dir in sorted(p for p in profiles_root.iterdir() if p.is_dir()):
        legacy = profile_dir / "config.yaml"
        if not legacy.is_file():
            continue
        found = True
        try:
            data = yaml.safe_load(legacy.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        for persona_id, raw in sorted(raw_persona_overrides(data).items(), key=lambda kv: str(kv[0])):
            body = _allowlist_persona_def(str(persona_id), raw, dropped)
            if body:
                # A legacy realm can publish the same persona from several profile
                # configs; first (sorted) wins deterministically.
                defs.setdefault(str(persona_id), body)
    if not found:
        return {}, [], None
    return defs, dropped, "legacy_config"


def _refusal(persona_id: str, code: str, message: str) -> dict[str, str]:
    return {"persona_id": persona_id, "code": code, "message": message}


def merge_persona_def(local_raw: Any, remote_body: dict[str, Any]) -> dict[str, Any]:
    """The value written back for an adopted persona.

    The realm owns the SHARED surface (:data:`PERSONA_DEF_ALLOWED_KEYS`); the
    member keeps every other key they authored. Without this, adopting a realm
    definition would silently delete the member's own machine-shaped keys — the
    member's ``repo_scope`` pointing at THEIR checkout is the concrete case —
    which is the same clobber class this whole change exists to retire.
    """

    preserved = {
        str(key): value
        for key, value in (local_raw or {}).items()
        if isinstance(local_raw, dict) and str(key) not in PERSONA_DEF_ALLOWED_KEYS
    }
    return {**preserved, **remote_body}


def _admission_refusal(persona_id: str, remote_body: dict[str, Any]) -> dict[str, str] | None:
    """Per-definition admission through the SHARED guard.

    Was two inline scans here; ``sync_admission`` is now the one authority every
    specialized applier runs (defect (b) — board / office / skill had no scan at
    all because their families are excluded from the generic loop that carries
    ``_assert_no_secret_artifacts``). ``prose_keys=frozenset()`` keeps this lane
    scanning EVERY key: a persona definition is 100% wiring — its keys are
    themselves an allowlist — so nothing here is exempt prose.
    """

    from .sync_admission import refuse_entity

    refusal = refuse_entity(
        persona_id,
        payload=remote_body,
        prefix=f"personas.{persona_id}",
        prose_keys=frozenset(),
    )
    return None if refusal is None else _refusal(persona_id, refusal.code, refusal.message)


def apply_persona_config_pull(
    realm_id: str,
    subtree: Path,
    *,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> PersonaConfigPullSummary:
    """Merge pulled persona definitions into the member's config, key-wise.

    The member's own machine sections (``mcp_servers``, ``model``, ``display``,
    everything else) are untouched by construction: only
    ``agent_runtime.personas.<id>`` subtrees are written, one dotted key at a
    time through the existing comment-preserving chokepoint
    (``utils.atomic_roundtrip_yaml_update`` — the same writer ``save_config_value``
    uses). Decisions come from the SHARED ``sync_merge.classify_three_way_pull``
    against a never-synced baseline sidecar; there is no second merge engine.
    """

    from utils import atomic_roundtrip_yaml_update

    summary = PersonaConfigPullSummary()
    remote, dropped, source = read_remote_persona_defs(Path(subtree))
    summary.source = source
    summary.dropped_keys = sorted(set(dropped))
    if source is None:
        # An older/other realm that carries no persona definitions at all. Do not
        # touch the baseline — absence here is "not published", never "removed".
        return summary

    path = Path(config_path) if config_path is not None else get_config_path()
    local = raw_persona_overrides(load_raw_config(path))
    baseline = read_persona_config_baseline(realm_id)

    for persona_id in sorted(set(remote) | set(baseline)):
        remote_body = remote.get(persona_id)
        if not _PERSONA_ID_RE.match(persona_id):
            summary.refused.append(
                _refusal(persona_id, "invalid_persona_id", "persona id is not a safe config key")
            )
            continue
        if remote_body is not None:
            refusal = _admission_refusal(persona_id, remote_body)
            if refusal is not None:
                summary.refused.append(refusal)
                continue

        local_body = _allowlist_persona_def(persona_id, local.get(persona_id), []) if persona_id in local else None
        local_hash = persona_def_hash(local_body) if local_body else None
        remote_hash = persona_def_hash(remote_body) if remote_body else None
        decision = classify_three_way_pull(local_hash, remote_hash, baseline.get(persona_id))

        if decision.action == PullAction.NOOP:
            if remote_body is not None:
                summary.converged.append(persona_id)
            else:
                # ``absent_both``: neither side has it any more. Retire the stale
                # baseline entry so the sidecar never outlives what it tracks.
                baseline.pop(persona_id, None)
            continue
        if decision.action == PullAction.KEEP_LOCAL:
            summary.kept_local.append(persona_id)
            continue
        if decision.action == PullAction.ARCHIVE_LOCAL:
            # Remote stopped publishing it. A persona definition is referenced by
            # Office placements / instances / assignments — never delete it here.
            summary.retained.append(persona_id)
            baseline.pop(persona_id, None)
            continue
        if decision.action == PullAction.CONFLICT:
            summary.held.append(persona_id)
            continue
        if decision.action == PullAction.WRITE_REMOTE and remote_body is not None:
            if decision.reason == "converged":
                # Both sides moved to the SAME content: only the baseline needs
                # to catch up. Rewriting an identical definition would churn the
                # member's config file for nothing.
                summary.converged.append(persona_id)
            else:
                summary.adopted.append(persona_id)
                if not dry_run:
                    atomic_roundtrip_yaml_update(
                        path,
                        f"agent_runtime.personas.{persona_id}",
                        merge_persona_def(local.get(persona_id), remote_body),
                    )
            baseline[persona_id] = remote_hash or ""
            continue

    if not dry_run:
        write_persona_config_baseline(realm_id, baseline)
    return summary
