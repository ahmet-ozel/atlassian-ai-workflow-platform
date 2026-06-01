# =============================================================================
# vps_preflight.ps1 — Windows launcher for VPS pre-flight & software-install
#
# Dot-sources vps_common.ps1 for $SSH_KEY, $VPS, Invoke-VpsSsh.
# Pipes vps_preflight.sh to VPS via `ssh ... bash -s`, captures stdout.
# Splits output on section markers into evidence files:
#   vps-test-evidence/01-preflight.txt
#   vps-test-evidence/02-install.txt
#
# On remote exit code != 0: calls vps_open_issue_logger (severity=critical,
# category=infra), exits with code 2 (halt-on-fail per R1.4, R2).
#
# Requirements: R1.4, R1.5, R2.6, R22.2
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Import shared helpers ---------------------------------------------------
. "$PSScriptRoot\vps_common.ps1"

# --- Paths -------------------------------------------------------------------
$ScriptDir = $PSScriptRoot
$WorkspaceRoot = (Resolve-Path "$ScriptDir\..\..").Path
$EvidenceDir = Join-Path $WorkspaceRoot "vps-test-evidence"
$PreflightShPath = Join-Path $ScriptDir "vps_preflight.sh"
$OpenIssueLogger = Join-Path $ScriptDir "vps_open_issue_logger.py"

# Section markers (must match vps_preflight.sh output)
$PreflightMarker = "### SECTION: PREFLIGHT ###"
$InstallMarker = "### SECTION: INSTALL ###"

# --- Ensure evidence directory exists ----------------------------------------
if (-not (Test-Path $EvidenceDir)) {
    New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null
}

# --- Validate vps_preflight.sh exists ----------------------------------------
if (-not (Test-Path $PreflightShPath)) {
    Write-Error "vps_preflight.sh not found at: $PreflightShPath"
    exit 1
}

# --- Execute remote payload via SSH stdin pipe --------------------------------
Write-Host "[vps_preflight] Piping vps_preflight.sh to VPS via SSH..." -ForegroundColor Cyan

# Pipe the shell script content via stdin to ssh
# Temporarily relax error preference so stderr lines (e.g. debconf warnings)
# from the remote host don't terminate the script prematurely.
# Write script to a temp file with LF line endings to avoid bash '\r' errors.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$scriptContent = (Get-Content -Path $PreflightShPath -Raw) -replace "`r`n", "`n" -replace "`r", "`n"
$tempFile = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllText($tempFile, $scriptContent, [System.Text.UTF8Encoding]::new($false))
# Use cmd /c to properly redirect stdin from the temp file
$sshCmd = "ssh -i `"$SSH_KEY`" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -o BatchMode=yes $VPS `"bash -s`" < `"$tempFile`""
$rawOutput = cmd /c $sshCmd 2>&1
$remoteExitCode = $LASTEXITCODE
Remove-Item -Path $tempFile -Force -ErrorAction SilentlyContinue
$ErrorActionPreference = $prevEAP

Write-Host "[vps_preflight] Remote exit code: $remoteExitCode" -ForegroundColor $(if ($remoteExitCode -eq 0) { "Green" } else { "Red" })

# --- Split output into evidence files ----------------------------------------
# Convert output to string array for processing
$outputLines = ($rawOutput | Out-String) -split "`n"

$preflightLines = @()
$installLines = @()
$currentSection = "none"

foreach ($line in $outputLines) {
    $trimmed = $line.TrimEnd()

    if ($trimmed -eq $PreflightMarker) {
        $currentSection = "preflight"
        continue
    }
    elseif ($trimmed -eq $InstallMarker) {
        $currentSection = "install"
        continue
    }

    switch ($currentSection) {
        "preflight" { $preflightLines += $trimmed }
        "install"   { $installLines += $trimmed }
    }
}

# Write evidence files (R1.5, R2.6)
$preflightEvidence = Join-Path $EvidenceDir "01-preflight.txt"
$installEvidence = Join-Path $EvidenceDir "02-install.txt"

$preflightLines | Out-File -FilePath $preflightEvidence -Encoding UTF8 -Force
Write-Host "[vps_preflight] Evidence written: $preflightEvidence" -ForegroundColor Gray

$installLines | Out-File -FilePath $installEvidence -Encoding UTF8 -Force
Write-Host "[vps_preflight] Evidence written: $installEvidence" -ForegroundColor Gray

# --- Handle failure: halt-on-fail (R1.4, R2) ---------------------------------
if ($remoteExitCode -ne 0) {
    # Determine which requirement failed based on exit code
    # Exit 1 = preflight failure (R1), Exit 2 = install failure (R2)
    if ($remoteExitCode -eq 1) {
        $failedRequirement = "R1"
        $failedEvidencePath = "vps-test-evidence/01-preflight.txt"
        $failSummary = "VPS pre-flight check failed (hardware, OS, or port conflict)"
    }
    elseif ($remoteExitCode -eq 2) {
        $failedRequirement = "R2"
        $failedEvidencePath = "vps-test-evidence/02-install.txt"
        $failSummary = "VPS software-install step failed (Docker, Python, ufw, or swap)"
    }
    else {
        $failedRequirement = "R1"
        $failedEvidencePath = "vps-test-evidence/01-preflight.txt"
        $failSummary = "VPS preflight script failed with unexpected exit code $remoteExitCode"
    }

    Write-Host "[vps_preflight] HALT: $failSummary" -ForegroundColor Red

    # Call vps_open_issue_logger (R16.1 — severity=critical, category=infra)
    $loggerArgs = @(
        $OpenIssueLogger,
        "--requirement", $failedRequirement,
        "--severity", "critical",
        "--category", "infra",
        "--summary", $failSummary,
        "--evidence-path", $failedEvidencePath,
        "--recommended-action", "manual_fix"
    )

    try {
        & python $loggerArgs
    }
    catch {
        Write-Warning "Failed to invoke vps_open_issue_logger: $_"
    }

    # Exit 2 = halt-on-fail signal to caller
    exit 2
}

# --- Success ------------------------------------------------------------------
Write-Host "[vps_preflight] Pre-flight and software-install completed successfully." -ForegroundColor Green
Write-Host "[vps_preflight] Evidence files:" -ForegroundColor Gray
Write-Host "  - $preflightEvidence" -ForegroundColor Gray
Write-Host "  - $installEvidence" -ForegroundColor Gray
exit 0
