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
claim in the same change as the production fix. A stale claim fails
configuration instead of mutating a similar-looking line silently.

## The anchor: `symbol` first, then the block (H-H14, 2026-08-30)

`symbol` is **load-bearing**, not a label. It is a dotted definition path —
`upsert_actor`, `OfficeStore.archive_actors_for_instance`, a module constant —
optionally followed by `/<prose>` naming which line inside it the claim is about
(`build_parser/work_list`); only the half before the `/` is resolved. `module`
(or `module scope`) means the whole file and is the right spelling for a claim
on an import or a module-level binding.

Two things follow, and both replace a silence:

- **`find` must occur exactly once INSIDE that symbol**, not once in the file.
  A sibling function spelling the same line is no longer a configuration error,
  and the mutation is spliced at the resolved offset rather than handed to
  `str.replace`, which would rewrite whichever copy came first.
- **A `symbol` that no longer resolves is fatal**, and the error names the
  symbol that does hold the block ("it is in `normalize_agent_create` — re-anchor
  the claim's symbol"). Before this, `symbol` was never read at all:
  `r1-create-stops-fencing-the-supplied-placement` named a `_parse_request` that
  `agent_create.py` had not had for months, and the gate said nothing.

A block that MOVED or was **re-indented** inside its symbol still anchors: the
registered `find` is matched again with every line shifted by one constant, up
to `MAX_REINDENT_COLUMNS`, and the replacement is re-indented onto the block's
current column. Relative nesting is preserved by construction, so an extraction
out of a nested `try` (two levels of dedent) re-anchors and a block whose inner
structure changed does not. A re-anchored run says so — `RE-ANCHORED: <id> …
re-indented -8 columns` — after the `mutation candidates:` line, for selected
and unselected claims alike.

Honest exceptions live in `mutation_exemptions.yaml` (JSON syntax is valid YAML
and keeps the pre-install CI selector dependency-free). Every exception must
name its path, symbol, operator, reason, owner, issue/evidence, and expiry.
Allowed reasons are `equivalent`, `observability-only`, `generated`, and
`contract-out-of-scope`; expired entries fail the check.
