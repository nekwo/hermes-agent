# Office-layout policy cases (cross-repo)

`cases.json` is the pin that keeps hermes and the EterniaLauncher placing an
**unaimed** agent on the same slot. Both repos commit **byte-identical copies**:

| Repo | Path | Policy under test | Gate |
| --- | --- | --- | --- |
| hermes | `tests/fixtures/office_layout/cases.json` | `agent_runtime/office_layout_policy.py` | `tests/agent_runtime/test_office_layout_policy.py` |
| launcher | `test/fixtures/harness_office_layout/cases.json` | `MissionOfficePlacementPolicy` (`lib/features/mission_control/office/mission_office_placement_policy.dart`) | `test/features/mission_control/mission_office_placement_policy_test.dart` |

`MANIFEST.sha256` sits beside each copy and pins the bytes; the two manifests
carry the same digest, so a file edited on one side only reds that side's
manifest test instead of leaving both repos green while they disagree.

## Why two policies at all

Plan D2 (`docs/agent-runtime-harness/planned/agent-placement-verb.md`) makes
hermes the **authority**: `runtime.agent.create` / `harness agent create` with
no position resolve one here, server-side, so every door shares one answer. The
launcher keeps its copy as a **prediction** — a pending chip and a staged scene
node need a world position before the ack comes back — and adopts the server's
actor when it lands. A prediction that could disagree with the authority is the
defect the plan exists for, which is what this fixture is for.

## Shape

```jsonc
{
  "schema_version": 1,
  "tolerance": 0.0001,
  "constants": { "origin_x": -5.0, ... },   // every constant, pinned by name
  "cases": [
    {
      "name": "empty_floor",
      "why": "...",                          // the property the case defends
      "kind": "agent" | "desk",              // folder + lane must RESOLVE from it
      "folder": "Agents",                    // the scan's folder scope
      "lane_offset": [0.0, 0.0],             // the lattice nudge
      "items": [                             // the floor, already flattened
        { "item_id": "a0", "kind": "agent", "folder": "Agents",
          "position": [-5.0, 6.4], "hidden": false }
      ],
      "expected": [-3.6, 6.4]                // the slot BOTH sides must return
    }
  ]
}
```

Both tests assert, per case: `folder`/`lane_offset` are what the side's own
kind→lane mapping produces for `kind`, and the scan over `items` returns
`expected` within `tolerance`.

## Two things the cases exist to catch, stated because they are asymmetric

* **`hidden` is launcher-only.** hermes has no `hidden` field — personal view
  state never enters `OfficeSurface` — so `hidden_item_blocks` passes on one
  side by honouring the flag and on the other by never having had it. Both must
  count the item as occupying its slot.
* **A blank `folder` is hermes-only.** `office_store._normalize_item` persists
  `folder: ""` for an item written without one; the launcher's decoder
  substitutes the kind's default at read time. `blank_folder_falls_back_to_kind`
  pins that hermes applies the same fallback when it SCANS, or a legacy item
  would block the lattice on one repo and be invisible on the other.

## Tolerance is not slack

The launcher's `Vector2` stores **32-bit** floats, so `6.4` round-trips as
`6.400000095…` and a 25-row scan accumulates about `1e-6`. `1e-4` is four
orders of magnitude above that and four below the smallest gap this lattice
produces (`0.7`). No case is placed near the occupancy boundary, so float width
can never flip a verdict. Never assert exact equality across the two sides.

## Update rule

These cases are **hand-maintained** — there is no generator, on purpose: a
generator that ran the policy would make the fixture a mirror of the code
rather than a pin on it.

Change them only in a **cross-stack change that lands hermes and the launcher
together**: edit `cases.json`, copy the bytes to the other repo verbatim,
re-hash BOTH `MANIFEST.sha256` files, and run both gates. A manifest updated on
one side only leaves both suites green while the two repos disagree, which is
the exact drift this pair exists to prevent.

**Adding a constant to the policy means adding a case that would move if that
constant moved.** The fixture proves equality on the cases in the file, not on
every floor.
