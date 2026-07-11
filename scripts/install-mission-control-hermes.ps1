param(
    [string]$TargetRoot = "",
    [string]$RepoUrl = "https://github.com/nekwo/hermes-agent.git",
    [string]$Branch = "main",
    [switch]$SkipClone,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Frame {
    param(
        [string]$Stage,
        [string]$State,
        [string]$Message,
        [hashtable]$Details = @{}
    )
    $frame = [ordered]@{
        schema_version = 1
        kind = "installer_event"
        stage = $Stage
        state = $State
        message = $Message
        safe_details = $Details
    }
    $frame | ConvertTo-Json -Compress -Depth 8
}

function Write-InstallError {
    param(
        [string]$Code,
        [string]$Message,
        [hashtable]$Details = @{}
    )
    $frame = [ordered]@{
        schema_version = 1
        kind = "error"
        error = [ordered]@{
            code = $Code
            message = $Message
            hint = "Fix the dependency or failed install stage, then rerun this idempotent installer."
            retryable = $true
            error_id = "err_$([guid]::NewGuid().ToString('N').Substring(0, 8))"
            correlation_id = "mission_control_installer"
            safe_details = $Details
        }
    }
    $frame | ConvertTo-Json -Compress -Depth 8
}

function Invoke-Step {
    param(
        [string]$Stage,
        [string]$Code,
        [scriptblock]$Body
    )
    try {
        Write-Frame -Stage $Stage -State "running" -Message "$Stage started"
        & $Body
        Write-Frame -Stage $Stage -State "completed" -Message "$Stage completed"
    } catch {
        Write-InstallError -Code $Code -Message "$Stage failed" -Details @{ error_class = $_.Exception.GetType().Name; stage = $Stage }
        exit 7
    }
}

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-InstallError -Code "install_dependency_missing" -Message "Missing required command: $Name" -Details @{ command = $Name }
        exit 7
    }
}

function Invoke-Native {
    param(
        [string]$File,
        [string[]]$Arguments,
        [string]$Cwd = ""
    )
    $previous = Get-Location
    try {
        if ($Cwd) { Set-Location -LiteralPath $Cwd }
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$File exited with code $LASTEXITCODE"
        }
    } finally {
        Set-Location $previous
    }
}

Require-Command "git"
Require-Command "python"

if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = Join-Path (Get-Location) "HermesMissionControl"
}

$root = [System.IO.Path]::GetFullPath($TargetRoot)
$clone = Join-Path $root "hermes-agent"
$hermesHome = Join-Path $root ".hermes"
$venv = Join-Path $clone ".venv"
$pythonExe = Join-Path $venv "Scripts\python.exe"
$hermesExe = Join-Path $venv "Scripts\hermes.exe"

New-Item -ItemType Directory -Force -Path $root | Out-Null
New-Item -ItemType Directory -Force -Path $hermesHome | Out-Null

Invoke-Step -Stage "clone" -Code "install_clone_failed" -Body {
    if ((Test-Path (Join-Path $clone ".git")) -or $SkipClone) {
        Write-Frame -Stage "clone" -State "skipped" -Message "Repository already exists" -Details @{ repo = "hermes-agent" }
    } else {
        # core.longpaths is required on Windows: the repo carries paths beyond
        # MAX_PATH (260) under website/i18n and bundled skill schemas, which
        # otherwise fail checkout with "Filename too long".
        Invoke-Native -File "git" -Arguments @("clone", "-c", "core.longpaths=true", "--branch", $Branch, $RepoUrl, $clone)
        Invoke-Native -File "git" -Arguments @("config", "core.longpaths", "true") -Cwd $clone
    }
}

Invoke-Step -Stage "venv" -Code "install_venv_failed" -Body {
    if (-not (Test-Path $pythonExe)) {
        Invoke-Native -File "python" -Arguments @("-m", "venv", $venv)
    }
    Invoke-Native -File $pythonExe -Arguments @("-m", "pip", "install", "--upgrade", "pip") -Cwd $clone
    Invoke-Native -File $pythonExe -Arguments @("-m", "pip", "install", "-e", $clone) -Cwd $clone
}

$env:HERMES_HOME = $hermesHome
$env:ETERNIA_HERMES_HOME = $hermesHome

Invoke-Step -Stage "postinstall" -Code "install_postinstall_failed" -Body {
    # --json emits a machine-readable shell-provisioning + PATH summary on the
    # final stdout line (Git Bash resolution, HERMES_GIT_BASH_PATH, hermes shim).
    Invoke-Native -File $hermesExe -Arguments @("postinstall", "--yes", "--json") -Cwd $clone
}

Invoke-Step -Stage "harness_init" -Code "install_postinstall_failed" -Body {
    Invoke-Native -File $hermesExe -Arguments @("harness", "init") -Cwd $clone
}

Invoke-Step -Stage "install_harness_skills" -Code "install_postinstall_failed" -Body {
    Invoke-Native -File $hermesExe -Arguments @("harness", "install-harness-skills") -Cwd $clone
}

Invoke-Step -Stage "harness_status" -Code "install_postinstall_failed" -Body {
    $previous = Get-Location
    try {
        Set-Location -LiteralPath $clone
        $statusText = (& $hermesExe harness status --json) -join "`n"
        if ($LASTEXITCODE -ne 0) {
            throw "harness status exited with code $LASTEXITCODE"
        }
        $status = $statusText | ConvertFrom-Json
        $mismatches = @()
        foreach ($agent in @($status.agents)) {
            foreach ($skill in @($agent.skill_hash_mismatches)) {
                if ($skill) {
                    $mismatches += "$($agent.persona_id):$skill"
                }
            }
        }
        if ($mismatches.Count -gt 0) {
            throw "skill hash mismatches remain: $($mismatches -join ', ')"
        }
        Write-Frame -Stage "harness_status" -State "verified" -Message "Harness status verified" -Details @{
            runtimeHealthOk = [bool]$status.runtime_health.ok
            hermesInstance = "hermes-agent/.venv/Scripts/hermes.exe"
            hermesRootPath = ".hermes"
            skillHashMismatches = @()
        }
    } finally {
        Set-Location $previous
    }
}

Write-Frame -Stage "complete" -State "completed" -Message "Hermes Mission Control install completed" -Details @{
    hermesInstance = "hermes-agent/.venv/Scripts/hermes.exe"
    hermesRootPath = ".hermes"
    providerLoginConfigured = $false
}
exit 0
