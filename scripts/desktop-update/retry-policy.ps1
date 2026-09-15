function Test-HermesUpdateShouldRetry {
    param(
        [int]$ExitCode,
        [string]$InstallRoot,
        [string]$Output = ""
    )

    if ($ExitCode -eq 0) { return $false }
    # History review is deterministic. A fresh process cannot resolve a fold.
    if ($Output -match "(?m)^HERMES_UPDATE_HISTORY_REVIEW_REQUIRED\r?$") { return $false }
    if ($ExitCode -ne 2) { return $true }

    # Exit 2 is shared by non-retryable safety refusals and the self-lock
    # deferral. Only the latter writes this marker. The handoff treats it as a
    # retry signal for one fresh-process attempt, whose early-recovery pass
    # completes core dependencies before native modules load.
    $deferredInstallMarker = Join-Path $InstallRoot ".update-incomplete"
    return Test-Path -LiteralPath $deferredInstallMarker
}
