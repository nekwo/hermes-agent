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
import functools
import os
from pathlib import Path
from unittest import mock

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

#: The authority's own symbol. Derived from :data:`DEFINITION_SITE` so the two
#: cannot drift, and used below to make it STRUCTURALLY impossible for any
#: exemption to cover a second declaration of the snapshot contract version.
AUTHORITY_SYMBOL = DEFINITION_SITE[1]

#: Contract constants belonging to a DIFFERENT contract that happens to spell
#: its name the same way.
#:
#: The detector matches on the name suffix ``CONTRACT_VERSION``, which is the
#: right net for the defect it was built for — six restatements of ONE number —
#: but this repo versions more than one wire. The snapshot frame, the JSON-RPC
#: method manifest and the socket hello handshake each move on their own
#: schedule, on purpose: adding an RPC method must not restamp every live
#: persona instance's ``contract_hash``, and binding the hello proof to the
#: listening port must not make the Launcher read every snapshot frame as stale.
#: Three independent numbers is the design, not the debt.
#:
#: WHY THIS DOES NOT WEAKEN THE GATE. Entries are keyed on the exact
#: ``(filename, symbol)`` PAIR, never on a filename or a name pattern, so an
#: exemption covers precisely one declaration in one module and nothing else. A
#: module that redeclared ``SNAPSHOT_CONTRACT_VERSION = 54`` — the genuine
#: duplicate this gate exists to catch — matches no pair here and is still
#: flagged. Belt and braces, :data:`AUTHORITY_SYMBOL` may never appear as the
#: symbol of an entry: the lookup below refuses it, and
#: :func:`test_no_lane_exemption_can_ever_cover_the_snapshot_version` drives
#: that refusal on a synthetic module rather than trusting the rule.
#:
#: Each entry is witnessed on its own module below: the constant must still be a
#: singular module-scope int literal there, and the module must not import or
#: mention the snapshot authority — a lane that started deriving from
#: ``SNAPSHOT_CONTRACT_VERSION`` would no longer be independent, and the entry
#: would have stopped being true.
LANE_CONTRACT_ALLOWLIST = {
    ("serve_rpc.py", "RPC_CONTRACT_VERSION"): (
        "the JSON-RPC METHOD-SURFACE contract, published at serve_rpc.py:178 as "
        "`{'contract': RPC_CONTRACT_VERSION, 'methods': method_names()}`. It "
        "versions request/result SHAPES on the method manifest, which argv on "
        "the wire cannot version for itself; adding a method deliberately does "
        "not move it. Nothing on the snapshot frame reads it and it never "
        "reaches `parity.contract_version`."
    ),
    ("serve.py", "OPS_CONTRACT_VERSION"): (
        "the serve dispatcher's OP-SURFACE contract (EG-4.1), published under "
        "`ops` on `ready`/`hello_ok`/`version` beside — never inside — the "
        "method manifest. It versions the shape of the ops advertisement "
        "itself ({contract, transport, ops, subscribe_lanes}); adding an op "
        "deliberately does not move it, and nothing on the snapshot frame "
        "reads it. Lives in hermes_cli/harness_parts/serve.py, where the "
        "dispatcher lives."
    ),
    ("serve_socket.py", "HELLO_CONTRACT_VERSION"): (
        "the socket HELLO HANDSHAKE contract, stamped on every `server_hello` "
        "(serve_socket.py:913, :1093) and folded into the HMAC proof preimage "
        "at :527 (`f'v{HELLO_CONTRACT_VERSION}|{port}|{nonce}'`). It gates "
        "whether a client can answer the challenge frame at all — a connection "
        "concern that is settled before any snapshot is ever sent, and one that "
        "must be able to move without restamping contract_hash."
    ),
}

#: Where each lane-exempt module LIVES, repo-relative parent. The exemption is
#: keyed on the basename (that is what ``restatements`` receives), so the gate
#: must also know the one location that basename is allowed to mean — otherwise
#: a newcomer with the same name anywhere in the scanned roots would inherit an
#: exemption it was never reasoned about. Witnessed by the lookalike test.
LANE_CONTRACT_MODULE_HOMES = {
    "serve_rpc.py": "agent_runtime",
    "serve.py": "hermes_cli/harness_parts",
    "serve_socket.py": "agent_runtime",
}

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
                    # A different contract that spells its name the same way.
                    # The `!= AUTHORITY_SYMBOL` leg is not defensive clutter: it
                    # is what makes "this exemption can never cover a second
                    # declaration of the SNAPSHOT version" a property of the
                    # code rather than a property of what happens to be in the
                    # dict today. Adding such an entry by hand would be inert.
                    if (
                        target.id != AUTHORITY_SYMBOL
                        and (filename, target.id) in LANE_CONTRACT_ALLOWLIST
                    ):
                        continue
                    found.append((node.lineno, f"{target.id} = {value.value}"))

        # 2. <version expr> ==/!= <int>   (either side)
        #
        # EQUALITY ONLY, and the distinction is the whole point rather than a
        # convenience. An equality pin states "the contract is exactly N", which
        # stops being true at the next bump — that is the literal that rots, and
        # the class this gate exists to prevent. An ORDERING comparison
        # (`>= 47`) states "the contract has never gone back below the version
        # this wave moved it to", which is a permanent historical fact that
        # stays true across every future bump. Flagging floors would force the
        # removal-contract tests to give up a guarantee they legitimately own,
        # and would push their authors toward an allowlist entry — an exemption
        # that then has to be policed, for a literal that was never a hazard.
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for op, (left, right) in zip(node.ops, zip(operands, operands[1:])):
                if not isinstance(op, (ast.Eq, ast.NotEq)):
                    continue
                for a, b in ((left, right), (right, left)):
                    if _mentions_version(a) and _is_int_literal(b):
                        found.append((node.lineno, f"contract version pinned to literal {b.value}"))

        # 3. {"contract_version": <int>}
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in _VERSION_KEYS and _is_int_literal(val):
                    found.append((node.lineno, f'"{key.value}": {val.value} in a dict literal'))

    return sorted(set(found))


@functools.lru_cache(maxsize=1)
def _scanned_modules_cached() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(_scanned_modules_uncached().items())


def _scanned_modules() -> dict[str, ast.Module]:
    """Parsing the scanned roots is the expensive step and three cases need it,
    so it is parsed once per session. Callers get a fresh dict; the trees inside
    are shared, and every reader here treats them as read-only."""

    return dict(_scanned_modules_cached())


def _scanned_modules_uncached() -> dict[str, ast.Module]:
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
# The scan is paid HERE, at import
# --------------------------------------------------------------------------- #
# Three tests parse the scanned roots and they share it through the cache above,
# so exactly ONE of them pays — and which one is decided by collection order,
# which none of them chose. Standalone that is ~8s against the 30s per-test cap
# in pyproject.toml, which looks like plenty of headroom and is not: in a
# long-lived single-process run of tests/agent_runtime/ the same walk crossed 30s
# and, because `--timeout-method=thread` KILLS the process, took every test after
# it with it. Measured, on the run that found this.
#
# Same fix as tests/agent_runtime/test_s55_registered_events_have_emitters.py:
# warm at module scope so the cost lands in collection, which pytest-timeout does
# not clock (verified — a module that sleeps 35s at import passes under
# --timeout=30). A per-test budget should not be charged for a per-session walk,
# and no test is excluded to achieve it.
_scanned_modules_cached()

#: Cache occupancy AS OF IMPORT, so the guard below fails deterministically if
#: the warm is deleted rather than depending on which test runs first.
_SCAN_CACHE_SIZE_AT_IMPORT = _scanned_modules_cached.cache_info().currsize


def test_the_shared_scan_is_paid_at_import_and_not_by_whichever_test_runs_first():
    """REGRESSION GUARD for the timeout, pinning the cause rather than a clock.

    A wall-clock assertion would be a flake generator on a box that measured this
    same walk between 5s and 30s depending on load — and it would be asserting
    the symptom. What must be true is that the walk ran once, at import.
    """

    assert _SCAN_CACHE_SIZE_AT_IMPORT == 1, (
        "the scanned-module tree was NOT warmed at module import (cache size at "
        f"import: {_SCAN_CACHE_SIZE_AT_IMPORT}). Whichever test parses first now "
        "pays for the whole walk inside its own item, against the 30s per-test "
        "cap — and --timeout-method=thread kills the process, so nothing after "
        "this file would run. Restore the module-scope warm."
    )
    assert _scanned_modules_cached.cache_info().misses == 1, (
        "the scanned-module walk ran more than once; a caller is going around "
        f"the cache ({_scanned_modules_cached.cache_info()})"
    )


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
        # A FLOOR is a permanent historical fact ("this wave's move can never
        # regress"), not a pin — it stays true across every future bump, so it
        # is not the rotting class and must not be flagged.
        ('assert frame["parity"]["contract_version"] >= 47', False),
        ('assert snap["parity"]["contract_version"] > 40', False),
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


def test_no_lane_exemption_can_ever_cover_the_snapshot_version():
    """THE PROOF that the exemption above did not widen the gate.

    The whole value of this file is catching a SECOND declaration of the
    snapshot contract version. An allowlist is the classic way that value gets
    given away — one entry at a time, each individually reasonable.

    So the refusal is driven, not asserted about the dict's current contents: a
    lane entry for ``SNAPSHOT_CONTRACT_VERSION`` is INSTALLED here, in the worst
    spelling (the authority's own module, the exact literal in flight), and the
    detector must still flag it. A gate that merely happened to lack the entry
    would pass this by accident; only one that refuses the symbol passes it on
    purpose.
    """

    poisoned = ast.parse(f"{AUTHORITY_SYMBOL} = {SNAPSHOT_CONTRACT_VERSION}")

    # 1. Baseline: in some OTHER module it is already a restatement today.
    assert restatements(poisoned, filename="serve_rpc.py"), (
        "a redeclaration of the snapshot version is not being flagged even "
        "without an exemption — the detector itself has regressed"
    )

    # 2. And it stays flagged with a lane exemption naming it exactly.
    with mock.patch.dict(
        LANE_CONTRACT_ALLOWLIST,
        {("serve_rpc.py", AUTHORITY_SYMBOL): "a lie, installed on purpose"},
    ):
        assert restatements(poisoned, filename="serve_rpc.py"), (
            f"a LANE_CONTRACT_ALLOWLIST entry was able to exempt {AUTHORITY_SYMBOL}. "
            "That is the one thing this gate exists to catch, and the exemption "
            "must refuse the authority symbol structurally, not by omission."
        )

    # 3. The definition site's exemption is a PAIR for the same reason: it must
    #    not travel to another module.
    assert restatements(poisoned, filename="serve_socket.py")
    assert not restatements(poisoned, filename=DEFINITION_SITE[0])


def test_the_lane_allowlist_exempts_a_pair_and_not_a_pattern():
    """Neither half of the key may be a wildcard.

    A filename-scoped exemption would let ``serve_rpc.py`` state any contract
    version it liked; a symbol-scoped one would let any module claim to own the
    RPC contract. Both are the shape this gate was built to refuse, so both are
    driven.
    """

    (lane_file, lane_symbol), _ = next(iter(LANE_CONTRACT_ALLOWLIST.items()))

    # The exempt pair itself is clean.
    assert not restatements(ast.parse(f"{lane_symbol} = 1"), filename=lane_file)
    # Same symbol, different module — still a restatement.
    assert restatements(ast.parse(f"{lane_symbol} = 1"), filename="snapshot_wire.py")
    # Same module, different contract-version symbol — still a restatement.
    assert restatements(ast.parse("SOME_OTHER_CONTRACT_VERSION = 1"), filename=lane_file)


def test_each_lane_contract_is_witnessed_as_an_INDEPENDENT_contract():
    """Every lane entry's premise, asserted on the module it names.

    The claim is not "this constant is fine", it is "this is a DIFFERENT
    contract that versions on its own schedule". Two things have to be true for
    that, and neither is checked by the entry existing:

    1. the symbol is still a singular module-scope int literal there — if it
       became derived, or was defined twice, the exemption is covering something
       other than what it was written for;
    2. the module does not reach for ``SNAPSHOT_CONTRACT_VERSION`` at all. A lane
       that started importing, comparing against, or deriving from the snapshot
       authority would no longer be independent, and the honest response would
       be to delete the second number rather than keep exempting it.
    """

    root = _repo_root()
    for (filename, symbol), reason in LANE_CONTRACT_ALLOWLIST.items():
        # An entry's module may live in any scanned root (OPS_CONTRACT_VERSION
        # lives with the dispatcher in hermes_cli/harness_parts, not in
        # agent_runtime), and `restatements` keys on the BARE name — so resolve
        # through the same scan the gate reads, and demand the name is unique
        # across it: a second module with the same basename would let this
        # exemption cover a file it was never written for.
        candidates = [
            Path(path) for path in _scanned_modules() if Path(path).name == filename
        ]
        assert candidates, f"lane allowlist names {filename}, which no longer exists"
        assert len(candidates) == 1, (
            f"lane allowlist key {filename!r} is ambiguous across the scanned "
            f"roots: {sorted(str(c) for c in candidates)} — bare-name keying "
            "requires uniqueness"
        )
        module = candidates[0]
        assert module.is_file(), f"lane allowlist names {filename}, which no longer exists"

        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)

        definitions = [
            node
            for node in tree.body  # module scope only
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == symbol for t in node.targets)
        ]
        assert len(definitions) == 1, (
            f"{filename} defines {symbol} {len(definitions)} times at module "
            f"scope; the exemption ({reason}) covers a singular constant"
        )
        assert _is_int_literal(definitions[0].value), (
            f"{filename}:{definitions[0].lineno}: {symbol} is no longer a plain "
            "int literal. If it now derives from another version, the two "
            "contracts are entangled and this exemption is wrong."
        )

        entangled = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == AUTHORITY_SYMBOL
        ]
        assert entangled == [], (
            f"{filename} now references {AUTHORITY_SYMBOL} at line(s) {entangled}. "
            f"The exemption claims an independent contract ({reason}); a lane "
            "that derives from the snapshot authority is not independent, and "
            "the second number should be deleted rather than exempted."
        )


def test_a_lane_exemption_cannot_be_claimed_by_a_lookalike_basename():
    """The detector keys on BASENAME, so a second file of the same name anywhere
    in the scanned roots would inherit the exemption for free.

    ``tests/agent_runtime/serve_rpc.py`` does not exist today. If it ever does,
    it would be exempt from a gate it was never reasoned about — so the
    uniqueness the exemption silently depends on is stated here instead of
    assumed.
    """

    counts: dict[str, list[str]] = {}
    for path in _scanned_modules():
        counts.setdefault(Path(path).name, []).append(path)

    for filename, _symbol in LANE_CONTRACT_ALLOWLIST:
        paths = counts.get(filename, [])
        assert len(paths) == 1, (
            f"{filename} exists {len(paths)} times in the scanned roots ({paths}). "
            "The lane exemption is keyed on the basename, so every one of them "
            "is exempt. Make the key a path, or rename the newcomer."
        )
        home = LANE_CONTRACT_MODULE_HOMES.get(filename)
        assert home is not None, (
            f"{filename} has a lane exemption but no declared home in "
            "LANE_CONTRACT_MODULE_HOMES — declare where the exempt module lives"
        )
        parent = Path(paths[0]).parent.as_posix()
        assert parent.endswith(home), (
            f"the only {filename} is at {paths[0]}, not under {home}/ — either "
            "the module moved (update its declared home with the reasoning) or "
            "a lookalike replaced it"
        )


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
