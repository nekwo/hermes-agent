"""THE contract-version authority — one literal, and a gate that keeps it one.

=============================================================================
WHY THIS FILE EXISTS
=============================================================================

The snapshot wire contract version is a **cross-repo lockstep**. hermes emits
it at ``parity.contract_version``; the Launcher pins
``kSupportedMissionContractVersion`` and its ``MissionSnapshotEnvelope.health()``
requires EXACT equality, so a frame one version either side of the pin degrades
to ``stale`` rather than being best-effort parsed. Moving the number is
therefore a deliberate two-repo change, never a side effect of a refactor.

It had no owner. The number was restated in eight places in this repo, and the
restatements rotted on a schedule:

* the 52 -> 53 landing moved two of six test literals. The other four —
  ``test_office_store``, ``test_s57_unruled_config_debt_removal``,
  ``test_stage19_visibility``, and ``test_s47_wire_constant_field_removal``
  (which called itself, in a comment, "the only live pin") — went red on
  ``main`` and stayed red for five days;
* the 53 -> 54 sweep then found ELEVEN more sites still pinned at 52, every one
  of them verified red on a pristine checkout;
* three independent agents rediscovered the same defect in one night.

The failure mode is worse than the red tests. Every one of those assertions was
a *negative* gate — "the cut this file records did NOT move the contract" —
written as an absolute number. That statement stays true forever, but an
absolute literal makes it go red on every unrelated bump. So the signal
inverted: a contract-version failure came to mean "someone bumped the contract",
i.e. routine noise to be re-baselined, rather than "a consumer is out of step".
A gate readers are trained to re-baseline is not a gate.

=============================================================================
THE SHAPE
=============================================================================

One producer-side constant, :data:`agent_runtime.snapshot.SNAPSHOT_CONTRACT_VERSION`,
which production emits and every consumer imports:

* **exactly one test states the literal** — :func:`test_the_contract_version_literal`
  below. It is a change-detector ON PURPOSE, and it is the only one that should
  exist: its whole job is to make a bump a reviewed edit with the cross-repo
  checklist attached;
* **every other test asserts RELATIVE to it** (``== SNAPSHOT_CONTRACT_VERSION``,
  ``- 1`` for a skew fixture, ``+ 1`` for a newer-contract fixture), so a bump
  carries them for free and a genuine failure means what it says;
* :func:`test_no_other_module_states_the_contract_version` is the structural
  pin. It walks the AST of every production and test module and fails if any
  of them binds an integer literal to the contract version. Substring matching
  would not do: the version appears in prose in a dozen docstrings and in
  ``'"contract_version": 46,' not in src`` assertions that are themselves
  gates. This follows the repo rule that structural gates are AST/token based,
  never text based (see ``test_s55_registered_events_have_emitters``).

Deleting the pin below does not make the gate pass — the gate requires the pin
to exist and to be in this file.

RED-PROOF. Three, run before this file landed:

1. ``SNAPSHOT_CONTRACT_VERSION = 55`` in ``snapshot.py`` (producer moved,
   consumers left alone): :func:`test_the_contract_version_literal` alone goes
   red, naming the cross-repo checklist. Every other consumer travels silently
   — which is the entire point. Reverted.
2. Restoring ``assert frame["parity"]["contract_version"] == 52`` in
   ``test_stage19_visibility``: :func:`test_no_other_module_states_the_contract_version`
   goes red naming that file and line. Reverted.
3. :func:`test_the_restatement_detector_is_not_vacuous` runs permanently. It
   feeds the detector five synthetic modules — one per restatement idiom — and
   requires it to flag all five, plus three derived spellings it must NOT flag.
   A detector that silently resolved nothing would pass this file forever
   otherwise, which is the exact way the fixture gate in
   ``test_response_contract_fixture`` used to pass while broken.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from agent_runtime import snapshot
from agent_runtime.snapshot import SNAPSHOT_CONTRACT_VERSION


#: Trees walked by the structural gate. Production is included deliberately:
#: the producer restating its own constant one line away from the definition is
#: the same defect at a shorter distance.
SCANNED_ROOTS = ("agent_runtime", "hermes_cli", "tests/agent_runtime")

#: Spellings of "this is the snapshot contract version" the detector recognises.
_VERSION_KEYS = {"contract_version", "contractVersion"}

#: The ONE test file allowed to state the literal — this one.
AUTHORITY_FILE = "test_snapshot_contract_version_authority.py"

#: The producer's own DEFINITION of the constant. Obviously exempt — it is the
#: authority every other site derives from, and a gate that flagged it would be
#: demanding the number exist nowhere. Named as a pair (not a bare filename) so
#: the exemption cannot widen to some other literal in the same module, and
#: witnessed by :func:`test_the_definition_site_is_where_it_claims_to_be`.
DEFINITION_SITE = ("snapshot.py", "SNAPSHOT_CONTRACT_VERSION")

#: Integer literals bound to a contract-version name that are NOT restatements,
#: each with the reason it cannot rot. Witnessed below rather than trusted.
FLOOR_ALLOWLIST = {
    ("test_s47_wire_constant_field_removal.py", "S47_CONTRACT_VERSION"): (
        "historical floor, not a pin: asserted only with `>=` against the "
        "emitted value, so it stays true across every future bump and moving "
        "it would be the bug"
    ),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _mentions_version(node: ast.expr) -> bool:
    """True when ``node`` reads the snapshot contract version.

    Covers the three access idioms in this tree: ``frame["parity"]["contract_version"]``
    (subscript), ``envelope.contract_version`` (attribute), and a local
    ``*_CONTRACT_VERSION`` name.
    """

    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in _VERSION_KEYS
    if isinstance(node, ast.Attribute):
        return node.attr in _VERSION_KEYS
    if isinstance(node, ast.Name):
        return node.id.upper().endswith("CONTRACT_VERSION")
    if isinstance(node, ast.Call):
        # ``parity.get("contract_version")``
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            first = node.args[0]
            return isinstance(first, ast.Constant) and first.value in _VERSION_KEYS
    return False


def _is_int_literal(node: ast.expr) -> bool:
    # ``bool`` is an ``int`` subclass; ``contract_version == True`` is nonsense
    # but must not be reported as a version literal.
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)


def restatements(tree: ast.Module, *, filename: str) -> list[tuple[int, str]]:
    """Every site in ``tree`` that binds an integer literal to the contract version.

    Returns ``[(lineno, why), ...]``. Pure and AST-only so the vacuity test can
    drive it on synthetic source.
    """

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        # 1. NAME = <int>   /   NAME: int = <int>
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is not None and _is_int_literal(value):
            for target in targets:
                if isinstance(target, ast.Name) and target.id.upper().endswith("CONTRACT_VERSION"):
                    if (filename, target.id) == DEFINITION_SITE:
                        continue
                    if (filename, target.id) in FLOOR_ALLOWLIST:
                        continue
                    found.append((node.lineno, f"{target.id} = {value.value}"))

        # 2. <version expr> <cmp> <int>   (either side)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for left, right in zip(operands, operands[1:]):
                for a, b in ((left, right), (right, left)):
                    if _mentions_version(a) and _is_int_literal(b):
                        found.append((node.lineno, f"contract version compared to literal {b.value}"))

        # 3. {"contract_version": <int>}
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in _VERSION_KEYS and _is_int_literal(val):
                    found.append((node.lineno, f'"{key.value}": {val.value} in a dict literal'))

    return sorted(set(found))


def _scanned_modules() -> dict[str, ast.Module]:
    root = _repo_root()
    trees: dict[str, ast.Module] = {}
    for scoped in SCANNED_ROOTS:
        directory = root / scoped
        if not directory.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames if d not in {"__pycache__", "node_modules"}]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = Path(dirpath) / name
                try:
                    trees[str(path)] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:
                    continue
    return trees


# --------------------------------------------------------------------------- #
# The pin
# --------------------------------------------------------------------------- #
def test_the_contract_version_literal():
    """THE pin. The only place in the repo this number is written down.

    Moving it is a CROSS-REPO change. Before you edit this line:

    1. bump ``SNAPSHOT_CONTRACT_VERSION`` in ``agent_runtime/snapshot.py`` and
       record the ruling in the ``_parity_envelope`` history comment — what
       left the wire, or what arrived, and why a consumer could not have read
       the frame without the bump;
    2. regenerate BOTH producer-derived fixture families
       (``scripts/generate_agent_runtime_{response,stream}_fixtures.py``) and
       mirror the bytes into the Launcher's ``test/fixtures/harness_stream/``
       and ``test/fixtures/hermes_responses/``;
    3. move the Launcher's ``kSupportedMissionContractVersion`` in the SAME
       landing — ``health()`` requires exact equality, so a one-sided bump makes
       every live frame read ``stale``;
    4. edit this line last.

    Every other assertion in this repo travels on its own.
    """

    assert SNAPSHOT_CONTRACT_VERSION == 54


def test_the_emitted_frame_carries_the_declared_version(isolate_agent_runtime_root):
    """The constant is what the producer actually puts on the wire.

    Near-tautological by construction, and kept for the half that is not: it
    proves the parity envelope is REACHED and still carries the key. The
    non-vacuous half of the pin is the literal above.
    """

    frame = snapshot.build_snapshot()
    assert frame["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION


# --------------------------------------------------------------------------- #
# The structural pin
# --------------------------------------------------------------------------- #
def test_no_other_module_states_the_contract_version():
    """THE GATE. A version bump must not be able to leave a stale literal behind.

    This is the recurrence fix. Six literals with no shared authority meant a
    bump moved whichever ones its author happened to grep; the rest went quietly
    red and taught readers to ignore the signal.
    """

    offenders: list[str] = []
    for path, tree in _scanned_modules().items():
        name = Path(path).name
        if name == AUTHORITY_FILE:
            continue
        for lineno, why in restatements(tree, filename=name):
            offenders.append(f"{path}:{lineno}: {why}")

    assert offenders == [], (
        "these state the snapshot contract version as a literal:\n  "
        + "\n  ".join(offenders)
        + "\n\nImport `SNAPSHOT_CONTRACT_VERSION` from `agent_runtime.snapshot` and "
        "assert relative to it (`== SNAPSHOT_CONTRACT_VERSION`, `- 1` for a stale "
        "frame, `+ 1` for a newer one). The literal belongs in exactly one place: "
        f"{AUTHORITY_FILE}."
    )


def test_the_definition_site_is_where_it_claims_to_be():
    """The one exemption, witnessed rather than trusted.

    If the constant ever moves module, this fails — instead of the exemption
    quietly covering nothing while the real definition goes unguarded somewhere
    else.
    """

    filename, symbol = DEFINITION_SITE
    module = _repo_root() / "agent_runtime" / filename
    assert module.is_file(), f"{filename} no longer exists; the exemption is stale"

    tree = ast.parse(module.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in tree.body  # module scope only — a local would not be the authority
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets)
    ]
    assert len(definitions) == 1, (
        f"{filename} defines {symbol} {len(definitions)} times at module scope; "
        "the authority must be singular"
    )
    assert _is_int_literal(definitions[0].value), f"{symbol} is no longer a plain int literal"
    assert definitions[0].value.value == SNAPSHOT_CONTRACT_VERSION


def test_the_gate_scanned_a_real_tree():
    """Anti-vacuity: a gate whose walker found nothing passes forever."""

    scanned = _scanned_modules()
    assert len(scanned) > 200, f"only {len(scanned)} modules scanned — the walker is misrooted"
    names = {Path(p).name for p in scanned}
    # Files the gate MUST be looking at, because each one held a stale literal.
    for expected in (
        "snapshot.py",
        "test_stage19_visibility.py",
        "test_office_store.py",
        "test_s47_wire_constant_field_removal.py",
        "test_stream_contract_fixture.py",
    ):
        assert expected in names, f"{expected} is not in the scanned set"


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        # --- restatements the detector MUST catch, one per idiom -------------
        ('CURRENT_CONTRACT_VERSION = 54', True),
        ('assert frame["parity"]["contract_version"] == 52', True),
        ('assert snap.envelope.contract_version == 53', True),
        ('assert parity.get("contract_version") == 54', True),
        ('FRAME = {"schema_version": 1, "contract_version": 52}', True),
        # --- derived spellings it must NOT catch ----------------------------
        ('assert frame["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION', False),
        ('assert frame["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION - 1', False),
        ('CURRENT_CONTRACT_VERSION = SNAPSHOT_CONTRACT_VERSION', False),
        # Prose and string assertions are why this is AST-based, not substring.
        ('SRC = \'"contract_version": 46,\'\nassert SRC not in src', False),
    ],
)
def test_the_restatement_detector_is_not_vacuous(source: str, flagged: bool):
    """The detector is exercised against synthetic source, both directions.

    Without this, a detector that resolved nothing would make the gate above
    pass forever — the precise way ``test_response_contract_fixture`` stayed
    green through a contract removal.
    """

    hits = restatements(ast.parse(source), filename="synthetic.py")
    assert bool(hits) is flagged, f"detector returned {hits} for: {source!r}"


def test_the_floor_allowlist_is_witnessed_not_trusted():
    """Every allowlist entry must still exist, and still be a floor.

    An allowlisted name that has become an equality pin is a hole: it would rot
    on the next bump with the gate green, which is the original defect wearing
    an exemption.
    """

    root = _repo_root()
    for (filename, symbol), reason in FLOOR_ALLOWLIST.items():
        matches = list((root / "tests" / "agent_runtime").glob(filename))
        assert matches, f"allowlist names {filename}, which no longer exists"
        tree = ast.parse(matches[0].read_text(encoding="utf-8"))

        assigned = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets)
        ]
        assert assigned, f"{filename} no longer defines {symbol}; drop the allowlist entry"

        # The exemption's whole claim: this name is only ever a LOWER BOUND.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            if not any(isinstance(n, ast.Name) and n.id == symbol for n in operands):
                continue
            assert all(isinstance(op, (ast.GtE, ast.Gt, ast.LtE, ast.Lt)) for op in node.ops), (
                f"{filename}:{node.lineno} compares {symbol} for EQUALITY. The "
                f"allowlist exempts it as a floor ({reason}); an equality pin "
                "rots on the next bump and must not be exempt."
            )
