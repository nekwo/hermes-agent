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

The placement verb's D2 (`docs/agent-runtime-harness/06-office-and-board.md`,
"The placement verb" — the plan file that used to be cited here shipped and was
deleted 2026-08-27) makes hermes the **authority**: `runtime.agent.create` / `harness agent create` with
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
  "constants": { "origin_x": 0.0, ... },    // every constant, pinned by name
  "cases": [
    {
      "name": "empty_floor",
      "why": "...",                          // the property the case defends
      "kind": "agent" | "desk",              // folder + lane must RESOLVE from it
      "folder": "Agents",                    // the scan's folder scope
      "lane_offset": [0.0, 0.0],             // the lattice nudge
      "items": [                             // the floor, already flattened
        { "item_id": "a0", "kind": "agent", "folder": "Agents",
          "position": [0.0, 0.0], "hidden": false }
      ],
      "expected": [0.0, 1.6]                 // the slot BOTH sides must return
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

The launcher's `Vector2` stores **32-bit** floats, so `1.4` round-trips as
`1.399999976…` and a 25-column scan accumulates about `1e-6`. `1e-4` is four
orders of magnitude above that and four below the smallest gap this lattice
produces (`0.7`). Never assert exact equality across the two sides.

The lattice origin itself is now `(0.0, 0.0)` — operator ruling 2026-08-27 — so
the origin slot is exact in both widths and only the SPACINGS accumulate error.

Exactly ONE case is placed at the occupancy boundary, and it is placed by hand
for the reason below; every other case sits far enough from it that float width
cannot flip its verdict.

## The one case ON the boundary, and the one thing it cannot pin

`boundary_item_at_exact_radius` is the only case whose verdict turns on the
occupancy comparison. `slot_at(0,0) + (occupancy_radius, 0)` spelled the obvious
way makes the two repos **disagree** — and it did so at the old lattice position
AND still does at the world origin, for two different reasons. Both were
measured, the second of them by trying the obvious spelling and watching the
launcher red.

Against the old origin `(-5.0, 6.4)` the subtraction itself rounded:

| Width | `dist((-5.0, 6.4), (-4.3, 6.4))` | Verdict under `<` |
| --- | --- | --- |
| float64 (hermes) | `0.7000000000000002` | free — the item does NOT block |
| float32 (launcher `Vector2`) | `0.6999998092651367` | taken — the item DOES block |

At the world origin (ruling 2026-08-27) that subtraction is exact, so it is
tempting to conclude the obvious spelling now works. **It does not**, and the
reason is worth stating because it is easy to get wrong: only `Vector2`
*storage* is 32-bit. `occupancyRadius` is a plain Dart `double` const, so the
launcher stores `0.7` into a float32 component as `0.69999998807907104` and
compares that against a float64 `0.7` — the item blocks there and stays free on
hermes. Same disagreement, new mechanism.

So the case sits on `slot_at(0,0).x + 1468007 * 2**-21` =
`0.700000286102294921875`, the nearest float32-representable point at or outside
the radius. Both widths measure `0.7000002861022949` there, both leave the origin
slot free, and the case pins `occupancy_radius` to within one float32 ulp
(`4.8e-7`) instead of to within the whole `0.7`.

**What no case here can pin is the STRICTNESS of that comparison.** `<` and `<=`
differ only where a measured distance is EXACTLY `occupancy_radius`, and by the
paragraph above no case in this shared file can produce one on both sides at
once. `<` → `<=` in `MissionOfficePlacementPolicy._isBlocked` is therefore an
**equivalent mutant** at the lattice origin, where every case in this file sits —
re-measured on 2026-08-27 after the origin moved, not carried over on faith.
hermes pins its own strictness one level down, at the predicate:
`test_an_item_exactly_at_the_occupancy_radius_does_not_block` feeds `_is_blocked`
a distance of exactly the radius, which no case in this file can produce. The
launcher has no equivalent, because `_isBlocked` is private to the library and
every public door goes through the lattice.

hermes also keeps an off-axis unit case
(`test_an_off_axis_lattice_probe_can_measure_the_radius_exactly`) for a separate
float64 subtlety: `dx*dx + dy*dy` is ROUNDED rather than exact, so an item at
`(0.6999999999999998, 1e-08)` probed from `slot_at(0, 0)` sums to
`0.4899999999999999` — below both `0.48999999999999994` (`0.7*0.7`) and `0.49`
— while its root is EXACTLY `float64(0.7)`. That point is not
float32-representable, so it cannot live in this shared fixture. It was
re-derived when the lattice moved; the witness changed, the squared value did
not, which is the point — it is a property of the radius, not of where the
lattice sits.

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
