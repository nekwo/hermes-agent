# Exact-case contributor emails

Git records author emails exactly. Two historical contributors used
`agent@Agents-Mac-mini.local` (@skip-agent, original PR 88052) and
`agent@agents-Mac-mini.local` (@momomojo, existing mapping). Their files cannot
coexist in one directory on Windows. Keep the former under `emails/case-variants/`
and the latter in its existing location. The release loader preserves each exact
filename as the lookup key; local and CI audits recognize both. Do not lowercase
the keys or overwrite either contributor. No Git commit identity is rewritten.

Use this subdirectory only for verified distinct identities whose filenames would
otherwise collide. Ordinary mappings continue through `scripts/add_contributor.py`.
