<#
.SYNOPSIS
    Unattended run of hermes' validated suite lane + the mutation-claim
    inventory, writing a dated report. This is a REPORT, not a gate: nothing
    here blocks a push or a landing, and its own exit code is informational
    only (surfaced in Windows Task Scheduler's run history, nothing else
    reads it).

.DESCRIPTION
    Mission Control queue row, "Where does hermes' suite run unattended, now
    that no push gate runs it?" (2026-09-03) — the operator removed both
    repos' pre-push hooks (hermes 504953f6ad) and ruled pushes instant; the
    checks that used to run on every push to `release` now run only when
    someone happens to run them by hand. hermes' own CI is largely inert
    (billing), so there was no lane left that runs unattended at all. This
    script IS that lane; a Windows Scheduled Task (see
    `scripts/hermes-unattended-suite-task.xml` beside this file) is what
    calls it on a schedule the operator sets.

    Two checks, same shape as the retired push gate's Lane B plus the
    mutation-claim inventory the "refactor moves a claimed line" row asked
    for:

      1. `scripts/run_tests.sh` on the VALIDATED four-directory scope —
         `tests/agent_runtime tests/hermes_cli tests/cli tests/state` — never
         the parallel runner's whole-tree default. See AGENTS.md §Testing,
         "Validated scope vs. what the runner discovers by default" for why
         that scope and not the default.
      2. `scripts/changed_line_mutation_check.py --list --base origin/main`
         — the INVENTORY lane (`--list` never mutates the tree; it returns
         before the mutating section runs — see that script's own module
         docstring), so a refactor that silently moved a claimed line off
         its claimed source spelling shows up in the next scheduled report
         even though nothing landing a normal push would ever run it.
      3. `scripts/run_tests.sh tests/test_coverage_claims_resolve.py
         tests/scripts` — the two scopes that sit OUTSIDE the four
         directories above and are therefore run by nobody. Added
         2026-09-04 on the row's own evidence: the coverage-claim gate had
         gone red on `main` by five citations the S2 directory-push wave
         landed, and nothing reported it — the unrun-gate failure this
         whole script exists to stop, happening inside it because the gate
         was not in any of its scopes.

    Both commands' stdout/stderr and exit codes are captured into one dated
    Markdown report under `qa-artifacts/` (git-ignored; see .gitignore).
    Nothing here installs, registers, or enables the Scheduled Task — the
    operator does that by hand from the XML this directory ships, exactly as
    every row in this wave was told: "the operator ENABLES the task; you do
    not register it."

.PARAMETER RepoRoot
    Override the repo root this script operates on. Defaults to this
    script's own grandparent directory (scripts/.. ).

.NOTES
    Environmental failure classes a report from this script can legitimately
    show without a code defect existing — see AGENTS.md §Testing's triage:
    provider-network hangs, WSL-bash PATH shadow (this script explicitly
    resolves Git Bash rather than trusting `bash` on PATH — see
    Resolve-GitBash below — for exactly this reason), and acp/ripgrep
    dependency holes. None of those are inside this script's four-directory
    scope, but they are worth knowing about before treating a report's
    non-zero exit as news.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
Set-Location $RepoRoot

# ── qa-artifacts/ + the dated report path ───────────────────────────────────
$artifactsDir = Join-Path $RepoRoot "qa-artifacts"
if (-not (Test-Path $artifactsDir)) {
    New-Item -ItemType Directory -Path $artifactsDir -Force | Out-Null
}
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd_HHmmss")
$reportPath = Join-Path $artifactsDir "unattended-suite-$stamp.md"

# ── interpreter: mirror the retired pre-push hook's resolution order ───────
# (HERMES_PYTHON, then `python`, then `python3` on PATH). This is a FALLBACK,
# not the answer: `scripts/run_tests.sh` probes the canonical shared test venv
# itself ($HERMES_TEST_VENV, else ~/.venvs/hermes-test) and prefers it over
# whatever is handed in here. Section 2 has no such probe, so it still needs a
# resolved interpreter of its own.
function Resolve-Python {
    if ($env:HERMES_PYTHON -and (Test-Path $env:HERMES_PYTHON)) {
        return $env:HERMES_PYTHON
    }
    $candidates = @("python", "python3")
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

# ── bash: explicitly Git Bash, never whatever `bash` resolves to on PATH ───
# AGENTS.md §Testing's environmental triage names "WSL-bash PATH shadow" as
# one of the three classes that makes a whole-tree run misread as red: on a
# box with WSL installed, `bash` on PATH can resolve to WSL's bash ahead of
# Git Bash, and scripts/run_tests.sh is written for Git Bash semantics. A
# Scheduled Task runs with its own resolved PATH, not an interactive shell's,
# so this resolves Git Bash by its known install layout instead of trusting
# `Get-Command bash`.
function Resolve-GitBash {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Git\bin\bash.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Git\bin\bash.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\bin\bash.exe")
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $gitCmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($gitCmd) {
        # Git for Windows layout: <root>\cmd\git.exe + <root>\bin\bash.exe
        $gitRoot = Split-Path (Split-Path $gitCmd.Source -Parent) -Parent
        $candidate = Join-Path $gitRoot "bin\bash.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function ConvertTo-UnixPath([string]$WindowsPath) {
    $p = $WindowsPath -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)$') {
        return "/$($Matches[1].ToLower())$($Matches[2])"
    }
    return $p
}

$python = Resolve-Python
$bash = Resolve-GitBash

$sections = New-Object System.Collections.Generic.List[string]
$overallExit = 0

$sections.Add("# hermes unattended suite report`n")
$sections.Add("Generated (UTC): $stamp  ")
$sections.Add("Repo root: ``$RepoRoot```n")

# ── Lane B: the validated 4-directory suite ─────────────────────────────────
$sections.Add("## 1. Validated suite lane (`scripts/run_tests.sh`)`n")
$sections.Add("Scope: ``tests/agent_runtime tests/hermes_cli tests/cli tests/state`` — the four directories R3 was proven on, never the whole-tree default. See AGENTS.md §Testing.`n")

if (-not $bash) {
    $sections.Add("**SKIPPED** — no Git Bash found (checked Program Files\Git\bin\bash.exe and \`git.exe\`'s own layout). \`scripts/run_tests.sh\` needs Git Bash, not WSL's \`bash\` — see this script's header.`n")
    $overallExit = 1
} elseif (-not $python) {
    $sections.Add("**SKIPPED** — no python interpreter resolved (checked \$env:HERMES_PYTHON, then \`python\`/\`python3\` on PATH). Set \`HERMES_PYTHON\` for the account this Scheduled Task runs as.`n")
    $overallExit = 1
} else {
    $repoRootUnix = ConvertTo-UnixPath $RepoRoot
    $pythonUnix = ConvertTo-UnixPath $python
    $laneBCmd = "cd '$repoRootUnix' && HERMES_PYTHON='$pythonUnix' scripts/run_tests.sh tests/agent_runtime tests/hermes_cli tests/cli tests/state"
    $laneBOutFile = Join-Path $artifactsDir "unattended-suite-$stamp.lane-b.log"
    & $bash -lc $laneBCmd 2>&1 | Tee-Object -FilePath $laneBOutFile | Out-Null
    $laneBExit = $LASTEXITCODE
    if ($laneBExit -ne 0) { $overallExit = 1 }
    $sections.Add("Exit code: **$laneBExit**  ")
    $sections.Add("Full output: ``$([System.IO.Path]::GetFileName($laneBOutFile))``  (kept beside this report)`n")
    $tail = Get-Content $laneBOutFile -Tail 40 -ErrorAction SilentlyContinue
    if ($tail) {
        $sections.Add('```')
        $sections.Add(($tail -join "`n"))
        $sections.Add('```')
        $sections.Add("")
    }
}

# ── Mutation-claim inventory (never mutates: `--list` returns before the ──
# mutating section — see the script's own docstring) ────────────────────────
$sections.Add("## 2. Mutation-claim inventory (`scripts/changed_line_mutation_check.py --list`)`n")
$sections.Add("Row this answers: ""A refactor that moves a claimed line reds hermes' mutation gate for everyone, and no lane catches it"" — this is the lane. \`--list\` is inventory-only and never mutates the tree.`n")

if (-not $python) {
    $sections.Add("**SKIPPED** — no python interpreter resolved (see above).`n")
    $overallExit = 1
} else {
    $mutationOutFile = Join-Path $artifactsDir "unattended-suite-$stamp.mutation-list.log"
    & $python "scripts/changed_line_mutation_check.py" --list --base origin/main 2>&1 |
        Tee-Object -FilePath $mutationOutFile | Out-Null
    $mutationExit = $LASTEXITCODE
    if ($mutationExit -ne 0) { $overallExit = 1 }
    $sections.Add("Exit code: **$mutationExit**  ")
    $sections.Add("Full output: ``$([System.IO.Path]::GetFileName($mutationOutFile))``  (kept beside this report)`n")
    $content = Get-Content $mutationOutFile -ErrorAction SilentlyContinue
    if ($content) {
        $sections.Add('```')
        $sections.Add(($content -join "`n"))
        $sections.Add('```')
        $sections.Add("")
    }
}

# ── The scopes nobody runs ─────────────────────────────────────────────────
# `tests/test_coverage_claims_resolve.py` and `tests/scripts/` are both
# OUTSIDE the four directories section 1 runs, so before this section they
# were run only when someone typed them. That is not a hypothetical: on
# 2026-09-04 the coverage-claim gate was red on `main` by five citations the
# S2 directory-push wave had landed, and no lane had reported it. Its own
# section rather than four-plus-two roots in section 1, because section 1's
# scope is a RULED one (R3's parity/integrity proof was run on exactly those
# four directories) and quietly widening it would make every future report's
# "the validated suite" mean something the ruling does not cover.
$sections.Add("## 3. The scopes outside the validated four (`tests/test_coverage_claims_resolve.py`, `tests/scripts/`)`n")
$sections.Add("Neither is inside section 1's ruled four-directory scope, so before 2026-09-04 nothing ran them unattended. The coverage-claim gate went red on ``main`` by five S2-wave citations and no lane reported it; this section is why the next one would.`n")

if (-not $bash) {
    $sections.Add("**SKIPPED** — no Git Bash found (see section 1).`n")
    $overallExit = 1
} elseif (-not $python) {
    $sections.Add("**SKIPPED** — no python interpreter resolved (see section 1).`n")
    $overallExit = 1
} else {
    $repoRootUnix = ConvertTo-UnixPath $RepoRoot
    $pythonUnix = ConvertTo-UnixPath $python
    $outsideCmd = "cd '$repoRootUnix' && HERMES_PYTHON='$pythonUnix' scripts/run_tests.sh tests/test_coverage_claims_resolve.py tests/scripts"
    $outsideOutFile = Join-Path $artifactsDir "unattended-suite-$stamp.outside-scope.log"
    & $bash -lc $outsideCmd 2>&1 | Tee-Object -FilePath $outsideOutFile | Out-Null
    $outsideExit = $LASTEXITCODE
    if ($outsideExit -ne 0) { $overallExit = 1 }
    $sections.Add("Exit code: **$outsideExit**  ")
    $sections.Add("Full output: ``$([System.IO.Path]::GetFileName($outsideOutFile))``  (kept beside this report)`n")
    $tail = Get-Content $outsideOutFile -Tail 40 -ErrorAction SilentlyContinue
    if ($tail) {
        $sections.Add('```')
        $sections.Add(($tail -join "`n"))
        $sections.Add('```')
        $sections.Add("")
    }
}

$sections.Add("## Summary`n")
$sections.Add($(if ($overallExit -eq 0) { "**Everything above exited clean.**" } else { "**Something above did not exit clean — read the section it is in before treating this as a code defect.** See AGENTS.md §Testing's environmental-failure triage first: provider-network hangs, WSL-bash PATH shadow, acp/ripgrep dependency holes are all known-environmental, not code, classes." }))

$sections -join "`n" | Set-Content -Path $reportPath -Encoding utf8

Write-Host "Report written: $reportPath"
exit $overallExit
