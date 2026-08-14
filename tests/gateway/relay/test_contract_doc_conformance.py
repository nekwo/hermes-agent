"""Cross-repo contract conformance: docs/relay-connector-contract.md ⟷ Python.

The contract doc is the formal interface the connector repo
(NousResearch/gateway-gateway) implements against. The connector's TypeScript
structs are hand-mirrored from the doc, so if the Python source of truth drifts
from the doc, the two repos silently diverge and the handshake / session-keying
breaks only at integration time.

These tests make the doc ⟷ code relationship an enforced invariant:

  * Every ``CapabilityDescriptor`` field (§2 table) is documented with the
    correct required/optional flag, and the doc lists no fields the dataclass
    lacks.
  * Every ``SessionSource`` wire key (what ``to_dict()`` actually serializes)
    is named in the contract doc's §3 discriminator section, and every
    discriminator the doc calls out as a column header exists on the dataclass.
  * Conversely, fields deliberately kept OFF the wire stay off it, and every
    field §3 calls a session-key discriminator still has a per-platform column
    telling the connector how to fill it.

They are invariants, NOT change-detector snapshots: they assert the *relation*
between two artifacts that must move together, not a frozen list of names. Add
a field to the descriptor and the doc, and the test stays green; add it to only
one, and CI fails — which is exactly the lockstep guarantee the plan's
Cross-Repo Coordination Checklist calls for.
"""

from __future__ import annotations

import re
from pathlib import Path

from gateway.relay.descriptor import CapabilityDescriptor
from gateway.session import SessionSource

# Repo root: tests/gateway/relay/ -> repo root is parents[3]
_CONTRACT_DOC = (
    Path(__file__).resolve().parents[3] / "docs" / "relay-connector-contract.md"
)


def _doc_text() -> str:
    assert _CONTRACT_DOC.exists(), (
        f"Contract doc missing at {_CONTRACT_DOC}. It is the formal cross-repo "
        f"interface (Phase 1, Task 1.5) and must ship with the relay adapter."
    )
    return _CONTRACT_DOC.read_text(encoding="utf-8")


def _parse_descriptor_table(text: str) -> dict[str, bool]:
    """Parse §2's markdown table → {field_name: required}.

    Rows look like: ``| `field` | type | yes|no | meaning |``. Returns a map of
    field name to whether the Required column says "yes".
    """
    fields: dict[str, bool] = {}
    # Restrict to the §2 section so §3/§4 tables don't bleed in.
    section = text.split("## 2. CapabilityDescriptor", 1)[-1].split("## 3.", 1)[0]
    row_re = re.compile(r"^\|\s*`([a-z_]+)`\s*\|[^|]*\|\s*(yes|no)\s*\|", re.M)
    for name, required in row_re.findall(section):
        fields[name] = required.strip() == "yes"
    return fields


def test_descriptor_fields_match_contract_doc():
    """§2 table ⟷ CapabilityDescriptor dataclass, names + required/optional."""
    documented = _parse_descriptor_table(_doc_text())
    assert documented, "Failed to parse any descriptor fields from the §2 table."

    dc_fields = CapabilityDescriptor.__dataclass_fields__  # type: ignore[attr-defined]
    # A dataclass field is "required" iff it has no default and no default_factory.
    import dataclasses

    code_required = {
        name
        for name, f in dc_fields.items()
        if f.default is dataclasses.MISSING
        and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
    }
    code_names = set(dc_fields.keys())
    doc_names = set(documented.keys())

    missing_from_doc = code_names - doc_names
    assert not missing_from_doc, (
        f"CapabilityDescriptor fields missing from the §2 contract-doc table: "
        f"{sorted(missing_from_doc)}. Document them so the connector mirrors them."
    )
    extra_in_doc = doc_names - code_names
    assert not extra_in_doc, (
        f"Contract-doc §2 table documents fields the dataclass does not have: "
        f"{sorted(extra_in_doc)}. Remove them or add them to descriptor.py."
    )

    # Required/optional must agree, so the connector knows which fields it may omit.
    for name, doc_required in documented.items():
        assert doc_required == (name in code_required), (
            f"Field '{name}': contract doc says required={doc_required}, but the "
            f"dataclass says required={name in code_required}. Reconcile them."
        )


def _synthetic_field_value(name: str, annotation: object):
    """A truthy placeholder for one ``SessionSource`` field, chosen by its type.

    Deliberately type-driven rather than a hand-written kwargs list: a
    hand-written list silently goes stale the moment someone adds a field, and
    an under-populated source makes every ``if self.x:`` branch in ``to_dict``
    invisible — which is exactly how the wire surface drifted from the doc
    unnoticed. An annotation this cannot map raises instead of defaulting, so
    the gap is loud.
    """
    import enum
    import typing

    from gateway.config import Platform

    ann = annotation
    # ``gateway.session`` has no ``from __future__ import annotations``, so
    # ``field.type`` is the resolved object; tolerate the string form too in
    # case that changes.
    if isinstance(ann, str):
        ann = {"str": str, "bool": bool, "int": int, "Platform": Platform}.get(
            ann.replace("Optional[", "").rstrip("]").strip(), ann
        )
    if typing.get_origin(ann) is typing.Union:  # i.e. Optional[X]
        inner = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(inner) == 1:
            ann = inner[0]
    if isinstance(ann, type):
        if ann is Platform:
            return Platform.DISCORD
        if issubclass(ann, enum.Enum):
            return next(iter(ann))
        if ann is bool:  # before int: bool is a subclass of int
            return True
        if ann is int:
            return 7
        if ann is str:
            return f"wire-{name}"
    raise AssertionError(
        f"SessionSource.{name} has annotation {annotation!r}, which "
        f"_synthetic_field_value() cannot build a truthy placeholder for. Teach "
        f"it that type — otherwise this field's to_dict() branch is never "
        f"exercised and the contract-doc conformance tests silently under-check."
    )


def _session_source_wire_keys() -> set[str]:
    """Keys ``SessionSource.to_dict()`` can emit (the actual wire surface).

    Every field is populated with a truthy value so that conditionally-included
    keys (the ``if self.x:`` branches in ``to_dict``) all appear. Populating
    only *some* fields would make this helper assert a subset of the real wire
    surface, and undocumented keys would slip through green.
    """
    kwargs = {
        name: _synthetic_field_value(name, f.type)
        for name, f in SessionSource.__dataclass_fields__.items()  # type: ignore[attr-defined]
    }
    return set(SessionSource(**kwargs).to_dict().keys())


def _table_cells(line: str) -> list[str]:
    """Split one markdown table row into cells, honouring escaped pipes.

    Type cells like ``string\\|null`` contain an ESCAPED pipe, so a naive
    ``split("|")`` shifts every later cell left by one and silently reads the
    wrong column — which is how ``user_id`` and ``thread_id`` first went
    missing from the discriminator set here without any test going red.
    """
    return [c.strip().strip("*` ") for c in re.split(r"(?<!\\)\|", line.strip("|"))]


def _parse_session_source_table(text: str) -> set[str]:
    """Parse §3's "SessionSource fields (the wire surface)" table → field names."""
    section = text.split("### SessionSource fields (the wire surface)", 1)[-1]
    section = section.split("### SessionSource discriminators per platform", 1)[0]
    return set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))


def _parse_discriminator_columns(text: str) -> list[str]:
    """Parse the per-platform table's column headers (minus the ``Platform`` key).

    The headers ARE the doc's claim about which discriminators exist; reading
    them out of the doc (rather than hardcoding the five current ones) keeps
    this an invariant instead of a change-detector snapshot.
    """
    section = text.split("### SessionSource discriminators per platform", 1)[-1]
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("|"):
            return [c for c in _table_cells(line)[1:] if c]
    return []


def test_session_source_wire_keys_documented_in_contract():
    """Every wire key SessionSource.to_dict() emits is a row in the §3 table.

    §3's field table advertises itself as "every key the gateway accepts on the
    wire", and the connector repo hand-mirrors it into TypeScript. A key the
    gateway emits but the doc never names is a key the connector author cannot
    know to populate or read — a silent cross-repo gap.
    """
    documented = _parse_session_source_table(_doc_text())
    assert documented, "Failed to parse any rows from the §3 SessionSource table."

    undocumented = sorted(_session_source_wire_keys() - documented)
    assert not undocumented, (
        f"SessionSource wire keys absent from the §3 contract-doc field table: "
        f"{undocumented}. The connector normalizes events into these keys; if the "
        f"doc doesn't name them the connector author can't know to populate them. "
        f"Add a row per key (or, if a key is deliberately gateway-internal, stop "
        f"emitting it from to_dict())."
    )


def test_internal_only_session_fields_stay_off_the_wire():
    """Guard the inverse: fields deliberately NOT serialized must not leak.

    ``is_bot`` is an internal author-classification flag. ``role_authorized``
    and ``delivered_via_upstream_relay`` are trust signals stamped LOCALLY —
    ``delivered_via_upstream_relay`` in particular is what authz keys the
    upstream-trust decision off, so if it ever became a wire key a peer could
    forge it. If serializing any of these is intentional, it must be a
    deliberate, documented, cross-repo change — not a silent one.
    """
    leaked = sorted(
        {"is_bot", "role_authorized", "delivered_via_upstream_relay"}
        & _session_source_wire_keys()
    )
    assert not leaked, (
        f"Fields now serialized by SessionSource.to_dict() that must not be: "
        f"{leaked}. These are internal/trust-local. If a wire key is genuinely "
        f"intended, add it to docs/relay-connector-contract.md §3 and the "
        f"connector's SessionSource interface, then update this guard."
    )


def test_discriminator_columns_exist_on_dataclass():
    """§3's per-platform table headers must exist as SessionSource fields.

    These columns drive build_session_key() and are the #1 High-severity risk
    surface (Discord scope collision). If the doc advertises a discriminator
    column the dataclass can't carry, the connector has nowhere to put it.
    """
    columns = _parse_discriminator_columns(_doc_text())
    assert columns, "Failed to parse the §3 per-platform discriminator table."

    dc_fields = SessionSource.__dataclass_fields__  # type: ignore[attr-defined]
    wire_keys = _session_source_wire_keys()
    for discriminator in columns:
        assert discriminator in dc_fields, (
            f"Contract doc §3 lists '{discriminator}' as a session discriminator "
            f"column, but SessionSource has no such field."
        )
        # And it must be reachable on the wire (chat_type is always emitted; the
        # rest are conditional but still possible keys).
        assert discriminator in wire_keys, (
            f"Discriminator '{discriminator}' never appears in "
            f"SessionSource.to_dict() output — the connector cannot transmit it."
        )


def test_session_key_discriminators_have_a_per_platform_column():
    """Rows the §3 table marks "Session-key discriminator" must have a column.

    The reverse of the check above, and the one that catches a DELETION: the
    per-platform table is what tells the connector how to fill each
    discriminator on each platform, so silently dropping a column (e.g.
    ``scope_id``, the Discord server-isolation key) leaves a field the table
    still calls key-forming with no per-platform guidance. Header-existence
    alone cannot see that — removing a header only makes the doc claim less.
    """
    text = _doc_text()
    section = text.split("### SessionSource fields (the wire surface)", 1)[-1]
    section = section.split("### SessionSource discriminators per platform", 1)[0]
    key_forming = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = _table_cells(line)
        # | field | type | always sent | meaning |
        if len(cells) >= 4 and re.fullmatch(r"[a-z_]+", cells[0]):
            if "Session-key discriminator" in cells[3]:
                key_forming.add(cells[0])
    assert key_forming, "Failed to parse any session-key discriminators from §3."

    columns = set(_parse_discriminator_columns(text))
    missing = sorted(key_forming - columns)
    assert not missing, (
        f"§3 marks {missing} as session-key discriminators, but the per-platform "
        f"discriminator table has no column for them. The connector needs the "
        f"per-platform row to know what to put there; get it wrong and two "
        f"scopes collide into one session."
    )


