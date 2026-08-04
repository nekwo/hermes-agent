# Changed-line mutation claims

`python scripts/changed_line_mutation_check.py --base <merge-base>` selects
only explicit claims whose exact production source overlaps the diff, baselines
their focused commands once, applies at most 12 mutations, and requires each
focused test to fail. The original bytes are restored in `finally` after every
candidate.

Claims live in `tests/mutation_claims.json`. Defect/ruling tests should add a
claim in the same change as the production fix. This pilot deliberately uses
exact replacements: a stale claim fails configuration instead of mutating a
similar-looking line silently.

Honest exceptions live in `mutation_exemptions.yaml` (JSON syntax is valid YAML
and keeps the pre-install CI selector dependency-free). Every exception must
name its path, symbol, operator, reason, owner, issue/evidence, and expiry.
Allowed reasons are `equivalent`, `observability-only`, `generated`, and
`contract-out-of-scope`; expired entries fail the check.
