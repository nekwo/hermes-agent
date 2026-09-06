# w18/hf field notes — 2026-09-06

Lane hf of wave 18: four reds, three Linux-only from CI run 34051553815, one
Windows-only. One commit per row. Notes are written as each row lands.

## Row 4 — `test_two_profiles_get_different_homes` (Windows-only red)

The row asked whether "the two profiles collapse to one home through a
case-folded or short-path comparison". They do not. The product is correct and
the red is in the assertion:

```
assert home_a.endswith("alpha/home")
E   AssertionError: assert False
E    +  ...'...\\.hermes\\profiles\\alpha\\home'.endswith('alpha/home')
```

`get_subprocess_home()` returned two distinct, correct paths — the
`home_a != home_b` line above it passed. What failed is a hardcoded POSIX
separator inside a suffix match. On Linux `os.sep` is `/` and the suffix holds
by accident of the platform; on Windows it can never hold.

The test's real rule is per-profile **identity**, not a string suffix: each
profile's subprocess HOME is that profile's own `{HERMES_HOME}/home`. Rewritten
to assert the exact path (`home_a == str(base / "alpha" / "home")`), which is
separator-correct on both hosts and strictly stronger than the suffix it
replaces. Every other assertion in this file already used that spelling
(`== str(profile_home)`); this case was the outlier.

Killing mutation: made the container branch of `get_subprocess_home()` return
`<profiles>/home` instead of `<profiles>/<name>/home` — i.e. actually collapse
the two profiles onto one home, the failure the row hypothesised — and the
rewritten test reds (exit 1). Product restored.
