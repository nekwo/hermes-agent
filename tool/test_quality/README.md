# Changed-line mutation claims

`python scripts/changed_line_mutation_check.py --base <merge-base>` selects
only explicit claims whose exact production source overlaps the diff, baselines
their focused commands once, applies at most 16 mutations, and requires each
focused test to fail. 16, not the script's own `--max-candidates` default of
12: CI's `mutation-claims` job passes `--max-candidates 16` explicitly and the
comment beside that call site carries the reason for the raise. The number a
run enforces is the one on the command line, so read it there — this paragraph
said 12 while the gate ran 16. The original bytes are restored in `finally` after every
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
