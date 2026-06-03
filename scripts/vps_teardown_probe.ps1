# vps_teardown_probe.ps1 - Post-deletion SSH probe, Hetzner billing capture, leak check re-run
# Requirements: R19.1, R19.2, R19.3, R19.4, R19.5, R19.6, R23.3, R23.4
#
# Runs on the Windows host AFTER the operator has deleted the VPS from Hetzner Cloud Console.
# Verifies the server is truly gone (SSH probe failure), captures billing info from operator,
# re-runs leak check, and optionally performs credential sweep.
#
# Usage:
#   .\vps_teardown_probe.ps1                     # default: preserve local .env files
#   .\vps_teardown_probe.ps1 -CredentialSweep    # delete local .env files after verification

[CmdletBinding()]
param(
    [switch]$CredentialSweep
)

# --- Dot-source shared helpers ---
. "$PSScriptRoot\vps_common.ps1"

# --- Configuration ---
$WORKSPACE_ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$EVIDENCE_DIR = Join-Path $WORKSPACE_ROOT "vps-test-evidence"
$EVIDENCE_FILE = Join-Path $EVIDENCE_DIR "19-teardown.txt"
$LEAK_CHECK_SCRIPT = Join-Path $PSScriptRoot "vps_leak_check.ps1"
$LOGGER_SCRIPT = Join-Path $PSScriptRoot "vps_open_issue_logger.py"

$VPS_IP = "91.99.149.163"
$SSH_PROBE_TIMEOUT = 5
$POST_DELETE_PAUSE_SECONDS = 60

# Budget thresholds (R23.3, R23.4)
$VPS_HOURS_BUDGET = 8
$OPENAI_COST_THRESHOLD = 5  # USD

# --- Helper Functions ---

function Invoke-OpenIssueLogger {
    [CmdletBinding()]
    param(
        [string]$Requirement,
        [string]$Severity,
        [string]$Category,
        [string]$Summary,
        [string]$EvidencePath,
        [string]$RecommendedAction
    )

    $loggerArgs = @(
        $LOGGER_SCRIPT,
        "--requirement", $Requirement,
        "--severity", $Severity,
        "--category", $Category,
        "--summary", $Summary,
        "--evidence-path", $EvidencePath,
        "--recommended-action", $RecommendedAction
    )

    try {
        Push-Location $WORKSPACE_ROOT
        & python @loggerArgs
        Pop-Location
    }
    catch {
        Pop-Location
        Write-Host "[TEARDOWN] WARNING: Failed to log Open Issue: $_" -ForegroundColor Yellow
    }
}

# --- Main Execution ---

$evidence = @()
$separator = "============================================================"
$evidence += $separator
$evidence += "VPS E2E Teardown Probe Evidence - $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
$evidence += $separator
$evidence += ""

# Ensure evidence directory exists
if (-not (Test-Path $EVIDENCE_DIR)) {
    New-Item -ItemType Directory -Path $EVIDENCE_DIR -Force | Out-Null
}

# =========================================================================
# STEP 1: Runbook click-path documentation (R19.1)
# =========================================================================
$evidence += "--- STEP 1: VPS Deletion Runbook (R19.1) ---"
$evidence += ""
$evidence += "Operator action: Delete VPS from Hetzner Cloud Console"
$evidence += "  Click-path:"
$evidence += "    1. Navigate to: https://console.hetzner.cloud/projects/<id>/servers"
$evidence += "    2. Select server: ai-platform-test (Server ID: 131987507)"
$evidence += "    3. Click the three-dot menu"
$evidence += "    4. Select 'Delete server'"
$evidence += "    5. Type the server name to confirm"
$evidence += "    6. Click 'Delete' to confirm deletion"
$evidence += ""

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " VPS TEARDOWN PROBE" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[TEARDOWN] R19.1: VPS Deletion Runbook" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Please delete the VPS from Hetzner Cloud Console:" -ForegroundColor Yellow
Write-Host "    1. Go to: https://console.hetzner.cloud/projects/<id>/servers" -ForegroundColor White
Write-Host "    2. Select: ai-platform-test (Server ID: 131987507)" -ForegroundColor White
Write-Host "    3. Click: ... (three-dot menu) -> Delete server" -ForegroundColor White
Write-Host "    4. Type the server name to confirm" -ForegroundColor White
Write-Host "    5. Click 'Delete'" -ForegroundColor White
Write-Host ""

# Wait for operator confirmation
$deleteConfirm = Read-Host "Have you deleted the VPS from Hetzner Cloud Console? (yes/no)"
if ($deleteConfirm -ne "yes") {
    Write-Host "[TEARDOWN] Operator did not confirm VPS deletion. Aborting." -ForegroundColor Red
    $evidence += "RESULT: ABORTED - operator did not confirm VPS deletion"
    $evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding UTF8 -Force
    exit 1
}

$evidence += "Operator confirmed VPS deletion at $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
$evidence += ""

# =========================================================================
# STEP 2: Post-deletion SSH probe (R19.2)
# =========================================================================
$evidence += "--- STEP 2: Post-deletion SSH probe (R19.2) ---"
$evidence += ""

Write-Host "[TEARDOWN] R19.2: Waiting $POST_DELETE_PAUSE_SECONDS seconds for VPS decommission..." -ForegroundColor Cyan
$evidence += "Pausing $POST_DELETE_PAUSE_SECONDS seconds before SSH probe..."

Start-Sleep -Seconds $POST_DELETE_PAUSE_SECONDS

$evidence += "Pause complete. Probing SSH connectivity..."
$evidence += "Command: ssh -o ConnectTimeout=$SSH_PROBE_TIMEOUT root@$VPS_IP echo ok"
$evidence += ""

Write-Host "[TEARDOWN] R19.2: Probing SSH to $VPS_IP (expecting failure)..." -ForegroundColor Cyan

$sshProbeOutput = $null
$sshProbeExitCode = $null

try {
    $sshArgs = @(
        "-i", $SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=$SSH_PROBE_TIMEOUT",
        "-o", "BatchMode=yes",
        "root@$VPS_IP",
        "echo ok"
    )

    $sshProbeOutput = & ssh @sshArgs 2>&1
    $sshProbeExitCode = $LASTEXITCODE
}
catch {
    $sshProbeOutput = $_.Exception.Message
    $sshProbeExitCode = 255
}

$evidence += "SSH probe exit code: $sshProbeExitCode"
$evidence += "SSH probe output:"
if ($sshProbeOutput) {
    foreach ($line in $sshProbeOutput) {
        $evidence += "  $line"
    }
}
else {
    $evidence += "  (no output)"
}
$evidence += ""

if ($sshProbeExitCode -ne 0) {
    $evidence += "RESULT: PASS - SSH connection failed as expected (server decommissioned)"
    Write-Host "[TEARDOWN] R19.2: PASS - SSH probe failed (server is gone)" -ForegroundColor Green
}
else {
    $evidence += "RESULT: FAIL - SSH connection succeeded (server still reachable)"
    Write-Host "[TEARDOWN] R19.2: FAIL - SSH probe succeeded, server is still alive!" -ForegroundColor Red
    Write-Host "  The VPS may not have been fully deleted yet." -ForegroundColor Red
    Write-Host "  Please verify deletion in Hetzner Console and re-run this script." -ForegroundColor Yellow

    Invoke-OpenIssueLogger `
        -Requirement "R19" `
        -Severity "critical" `
        -Category "infra" `
        -Summary "Post-deletion SSH probe succeeded - VPS still reachable at $VPS_IP" `
        -EvidencePath "vps-test-evidence/19-teardown.txt" `
        -RecommendedAction "manual_fix"

    $evidence += ""
    $evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding UTF8 -Force
    exit 1
}

$evidence += ""

# =========================================================================
# STEP 3: Hetzner billing capture (R19.3)
# =========================================================================
$evidence += "--- STEP 3: Hetzner Billing Capture (R19.3) ---"
$evidence += ""

Write-Host ""
Write-Host "[TEARDOWN] R19.3: Hetzner Billing Information" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Please open the Hetzner billing page:" -ForegroundColor Yellow
Write-Host "    https://console.hetzner.cloud/projects/<id>/billing" -ForegroundColor White
Write-Host ""
Write-Host "  Find the final invoice/usage for server 'ai-platform-test'." -ForegroundColor Yellow
Write-Host ""

$billedEur = Read-Host "Enter the billed amount in EUR (e.g., 0.42)"
$billedHours = Read-Host "Enter the total billable hours (e.g., 5.2)"

$evidence += "Hetzner Invoice Excerpt (operator-provided):"
$evidence += "  Billed amount: EUR $billedEur"
$evidence += "  Billable hours: $billedHours h"
$evidence += "  Captured at: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
$evidence += ""

Write-Host "[TEARDOWN] Billing recorded: EUR $billedEur, $billedHours hours" -ForegroundColor Green

# R23.3: Budget warning if VPS hours > 8
$billedHoursNum = 0
try {
    $billedHoursNum = [double]$billedHours
}
catch {
    Write-Host "[TEARDOWN] WARNING: Could not parse billable hours as number" -ForegroundColor Yellow
}

if ($billedHoursNum -gt $VPS_HOURS_BUDGET) {
    $budgetWarning = "[BUDGET] VPS runtime exceeded 8h target (actual: ${billedHours}h)"
    $evidence += $budgetWarning
    $evidence += ""
    Write-Host "[TEARDOWN] $budgetWarning" -ForegroundColor Yellow

    Invoke-OpenIssueLogger `
        -Requirement "R23" `
        -Severity "minor" `
        -Category "infra" `
        -Summary ("VPS billable hours (" + $billedHours + " h) exceeded 8h budget target") `
        -EvidencePath "vps-test-evidence/19-teardown.txt" `
        -RecommendedAction "doc_update"
}

# R23.4: OpenAI cost kill-switch reminder
Write-Host ""
Write-Host "[TEARDOWN] R23.4: OpenAI Cost Kill-Switch Reminder" -ForegroundColor Cyan
Write-Host '  If OpenAI dashboard shows usage > $5 for this test session,' -ForegroundColor Yellow
Write-Host "  the teardown should have been triggered earlier." -ForegroundColor Yellow
Write-Host ""

$openaiCostInput = Read-Host "Enter approximate OpenAI cost for this session in USD (or 'skip' to skip)"

if ($openaiCostInput -ne "skip" -and $openaiCostInput -ne "") {
    $evidence += "OpenAI Cost (operator-provided): USD $openaiCostInput"

    $openaiCostNum = 0
    try {
        $openaiCostNum = [double]$openaiCostInput
    }
    catch {
        Write-Host "[TEARDOWN] WARNING: Could not parse OpenAI cost as number" -ForegroundColor Yellow
    }

    if ($openaiCostNum -gt $OPENAI_COST_THRESHOLD) {
        $costWarning = "[BUDGET] OpenAI cost (USD $openaiCostInput) exceeded threshold"
        $evidence += $costWarning
        Write-Host "[TEARDOWN] $costWarning" -ForegroundColor Yellow

        Invoke-OpenIssueLogger `
            -Requirement "R23" `
            -Severity "minor" `
            -Category "infra" `
            -Summary ("OpenAI cost (USD " + $openaiCostInput + ") exceeded kill-switch threshold of USD " + $OPENAI_COST_THRESHOLD) `
            -EvidencePath "vps-test-evidence/19-teardown.txt" `
            -RecommendedAction "doc_update"
    }
}
else {
    $evidence += "OpenAI Cost: skipped by operator"
}

$evidence += ""

# =========================================================================
# STEP 4: Leak check re-run (R19.4)
# =========================================================================
$evidence += "--- STEP 4: Leak Check Re-run (R19.4) ---"
$evidence += ""

Write-Host ""
Write-Host "[TEARDOWN] R19.4: Re-running leak check (local only, VPS is gone)..." -ForegroundColor Cyan

$leakCheckResult = $null
$leakCheckExitCode = $null

try {
    # Run leak check with -SkipRemote since VPS is deleted
    $leakCheckOutput = & $LEAK_CHECK_SCRIPT -SkipRemote 2>&1
    $leakCheckExitCode = $LASTEXITCODE
    $leakCheckResult = $leakCheckOutput | Out-String
}
catch {
    $leakCheckResult = "Exception during leak check: $_"
    $leakCheckExitCode = 1
}

$evidence += "Leak check re-run (local only, -SkipRemote):"
$evidence += "  Exit code: $leakCheckExitCode"
if ($leakCheckResult) {
    $leakLines = $leakCheckResult -split "`n" | Select-Object -First 30
    foreach ($line in $leakLines) {
        $evidence += "  $line"
    }
    if (($leakCheckResult -split "`n").Count -gt 30) {
        $evidence += "  ... (truncated)"
    }
}
$evidence += ""

if ($leakCheckExitCode -eq 0) {
    $evidence += "RESULT: PASS - leak check re-run passed (R6 invariant holds)"
    Write-Host "[TEARDOWN] R19.4: PASS - Leak check re-run passed" -ForegroundColor Green
}
else {
    $evidence += "RESULT: FAIL - leak check re-run failed"
    Write-Host "[TEARDOWN] R19.4: FAIL - Leak check re-run detected issues" -ForegroundColor Red

    Invoke-OpenIssueLogger `
        -Requirement "R19" `
        -Severity "critical" `
        -Category "config" `
        -Summary "Post-teardown leak check failed - sensitive files may be exposed in git" `
        -EvidencePath "vps-test-evidence/19-teardown.txt" `
        -RecommendedAction "config_change"
}

$evidence += ""

# =========================================================================
# STEP 5: Credential sweep (R19.5) - only if -CredentialSweep flag is set
# =========================================================================
$evidence += "--- STEP 5: Credential Sweep (R19.5) ---"
$evidence += ""

$localEnvFiles = @(
    (Join-Path $WORKSPACE_ROOT "platform\.env"),
    (Join-Path $WORKSPACE_ROOT "platform\services\atlassian_mcp_bitbucket\.env")
)

if ($CredentialSweep) {
    Write-Host ""
    Write-Host "[TEARDOWN] R19.5: Credential sweep requested - removing local .env files..." -ForegroundColor Cyan

    $removedFiles = @()
    foreach ($envFile in $localEnvFiles) {
        if (Test-Path $envFile) {
            Remove-Item -Path $envFile -Force
            $removedFiles += $envFile
            Write-Host "  Removed: $envFile" -ForegroundColor Yellow
        }
        else {
            Write-Host "  Not found (already absent): $envFile" -ForegroundColor Gray
        }
    }

    $sweepMsg = "[CREDENTIAL SWEEP] removed local .env files"
    $evidence += $sweepMsg
    $evidence += "  Files removed:"
    foreach ($f in $removedFiles) {
        $evidence += "    $f"
    }
    if ($removedFiles.Count -eq 0) {
        $evidence += "    (none found - already absent)"
    }
    $evidence += ""

    Write-Host "[TEARDOWN] $sweepMsg" -ForegroundColor Green
}
else {
    $evidence += "Credential sweep: NOT performed (default - local .env files preserved)"
    $evidence += "  To perform sweep, re-run with: .\vps_teardown_probe.ps1 -CredentialSweep"
    $evidence += ""
    $evidence += "  Local .env file status:"
    foreach ($envFile in $localEnvFiles) {
        if (Test-Path $envFile) {
            $evidence += "    [EXISTS] $envFile"
        }
        else {
            $evidence += "    [ABSENT] $envFile"
        }
    }
    $evidence += ""

    Write-Host ""
    Write-Host "[TEARDOWN] R19.5: Credential sweep NOT performed (default behavior)" -ForegroundColor Yellow
    Write-Host "  Local .env files are preserved. Run with -CredentialSweep to remove them." -ForegroundColor Gray
}

$evidence += ""

# =========================================================================
# FINAL SUMMARY
# =========================================================================
$evidence += $separator
$evidence += "TEARDOWN SUMMARY"
$evidence += $separator
$evidence += ""
$evidence += "  VPS deletion confirmed: yes"
$evidence += "  SSH probe (post-delete): FAIL (expected - server gone)"
$evidence += "  Hetzner billed: EUR $billedEur ($billedHours h)"
if ($openaiCostInput -and $openaiCostInput -ne "skip") {
    $evidence += "  OpenAI cost: USD $openaiCostInput"
}
$evidence += "  Leak check re-run: $(if ($leakCheckExitCode -eq 0) { 'PASS' } else { 'FAIL' })"
$evidence += "  Credential sweep: $(if ($CredentialSweep) { 'PERFORMED' } else { 'NOT performed' })"
if ($billedHoursNum -gt $VPS_HOURS_BUDGET) {
    $evidence += "  [BUDGET] VPS runtime exceeded 8h target"
}
$evidence += ""
$evidence += $separator

# =========================================================================
# Write evidence file (R19.6)
# =========================================================================
$evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding UTF8 -Force

Write-Host ""
Write-Host "[TEARDOWN] Evidence written to: $EVIDENCE_FILE" -ForegroundColor Cyan
Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host " TEARDOWN COMPLETE" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""

exit 0
