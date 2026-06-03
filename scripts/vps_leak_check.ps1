# vps_leak_check.ps1 - Credential leak and .gitignore validation (B3 Leak_Checker)
# Requirements: R6.1, R6.2, R6.3, R6.4, R6.5
#
# Validates that no .env files or credentials file appear in git working tree
# (staged or untracked) on both local Windows host and remote VPS.
# Also asserts that workspace-root .gitignore contains required patterns.
#
# Usage: .\vps_leak_check.ps1
#        .\vps_leak_check.ps1 -SkipRemote  (skip VPS-side check)

[CmdletBinding()]
param(
    [switch]$SkipRemote
)

# --- Dot-source shared helpers ---
. "$PSScriptRoot\vps_common.ps1"

# --- Configuration ---
$WORKSPACE_ROOT = (Resolve-Path "$PSScriptRoot\..").Path
$EVIDENCE_DIR = Join-Path $WORKSPACE_ROOT "vps-test-evidence"
$EVIDENCE_FILE = Join-Path $EVIDENCE_DIR "06-leakcheck.txt"
$LOGGER_SCRIPT = Join-Path $PSScriptRoot "vps_open_issue_logger.py"

# Sensitive path patterns for git status output (R6.1)
# Matches: .env, anything/.env, services/*/.env, credentials.md
$LEAK_PATTERNS = @(
    '\.env$',
    '/\.env$',
    '\\\.env$',
    'services/.+/\.env$',
    'services\\.+\\\.env$',
    'credentials\.md$'
)

# Required .gitignore literal lines (R6.2)
$REQUIRED_GITIGNORE_LINES = @(
    '.env',
    '**/.env',
    'credentials.md'
)

# --- Helper Functions ---

function Test-LeakInGitStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]]$StatusLines
    )

    $matched = @()
    foreach ($line in $StatusLines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        # git status --porcelain format: XY <path>
        $filePath = $line
        if ($line.Length -gt 3) {
            $filePath = $line.Substring(3).Trim()
        }
        # Handle rename format: "orig -> dest"
        if ($filePath -match ' -> (.+)$') {
            $filePath = $Matches[1]
        }
        foreach ($pattern in $LEAK_PATTERNS) {
            if ($filePath -match $pattern) {
                $matched += $line
                break
            }
        }
    }
    return , $matched
}

function Get-RedactedGitStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]]$StatusLines
    )

    $redacted = @()
    $count = 0
    foreach ($line in $StatusLines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $count++
        if ($count -le 50) {
            $redacted += $line
        }
    }
    if ($count -gt 50) {
        $redacted += "... ($count total lines, truncated at 50)"
    }
    return , $redacted
}

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
        Write-Host "[LEAK CHECK] WARNING: Failed to log Open Issue: $_" -ForegroundColor Yellow
    }
}

# --- Main Execution ---

$evidence = @()
$separator = "============================================================"
$evidence += $separator
$evidence += "VPS E2E Leak Check Evidence - $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
$evidence += $separator
$evidence += ""

$allPassed = $true

# =========================================================================
# CHECK 1: Local git status (R6.1)
# =========================================================================
$evidence += "--- CHECK 1: Local git status --porcelain (R6.1) ---"
Write-Host "[LEAK CHECK] R6.1: Checking local git status for sensitive files..." -ForegroundColor Cyan

# First determine if workspace root is a git repository
$isLocalGitRepo = $false
try {
    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $null = & git -C $WORKSPACE_ROOT rev-parse --git-dir 2>$null
    if ($LASTEXITCODE -eq 0) {
        $isLocalGitRepo = $true
    }
    $ErrorActionPreference = $oldEAP
}
catch {
    $isLocalGitRepo = $false
    $ErrorActionPreference = $oldEAP
}

if (-not $isLocalGitRepo) {
    $evidence += "Workspace root is not a git repository (no .git directory found)."
    $evidence += "Without git tracking, there is no risk of accidentally committing sensitive files via git."
    $evidence += "RESULT: PASS - no git repo means no git-tracked leak risk"
    Write-Host "[LEAK CHECK] R6.1: PASS - workspace is not a git repo (no leak risk via git)" -ForegroundColor Green
}
else {
    $gitStatusRaw = & git -C $WORKSPACE_ROOT status --porcelain 2>&1
    $gitExitCode = $LASTEXITCODE

    if ($gitExitCode -ne 0) {
        $evidence += "ERROR: git status failed with exit code $gitExitCode"
        $evidence += "Output: $gitStatusRaw"
        $evidence += "RESULT: SKIPPED (git command failure)"
        Write-Host "[LEAK CHECK] WARNING: git status failed (exit $gitExitCode)" -ForegroundColor Yellow
    }
    else {
        $statusLines = @()
        if ($gitStatusRaw) {
            $statusLines = @($gitStatusRaw | ForEach-Object { $_.ToString() })
        }

        $leakMatches = @(Test-LeakInGitStatus -StatusLines $statusLines)
        $redactedStatus = @(Get-RedactedGitStatus -StatusLines $statusLines)

        $evidence += "git status line count: $($statusLines.Count)"
        $evidence += "Redacted output (first 50 lines):"
        if ($redactedStatus.Count -eq 0) {
            $evidence += "  (clean working tree)"
        }
        else {
            foreach ($line in $redactedStatus) {
                $evidence += "  $line"
            }
        }
        $evidence += ""

        if ($leakMatches.Count -gt 0) {
            $allPassed = $false
            $evidence += "RESULT: FAIL - sensitive files detected in git status:"
            foreach ($m in $leakMatches) {
                $evidence += "  LEAK: $m"
            }
            Write-Host "[LEAK CHECK] FAIL: Sensitive files found in local git status:" -ForegroundColor Red
            foreach ($m in $leakMatches) {
                Write-Host "  $m" -ForegroundColor Red
            }
        }
        else {
            $evidence += "RESULT: PASS - no sensitive file patterns matched"
            Write-Host "[LEAK CHECK] PASS: No sensitive files in local git status" -ForegroundColor Green
        }
    }
}

$evidence += ""

# =========================================================================
# CHECK 2: .gitignore contains required patterns (R6.2)
# =========================================================================
$evidence += "--- CHECK 2: .gitignore required patterns (R6.2) ---"
Write-Host "[LEAK CHECK] R6.2: Checking .gitignore for required patterns..." -ForegroundColor Cyan

$gitignorePath = Join-Path $WORKSPACE_ROOT ".gitignore"

if (-not (Test-Path $gitignorePath)) {
    $allPassed = $false
    $evidence += "RESULT: FAIL - .gitignore file not found at $gitignorePath"
    Write-Host "[LEAK CHECK] FAIL: .gitignore not found" -ForegroundColor Red
}
else {
    $gitignoreContent = Get-Content $gitignorePath -Encoding UTF8
    $gitignoreLines = @($gitignoreContent | ForEach-Object { $_.Trim() })

    $missingPatterns = @()
    $foundPatterns = @()

    foreach ($required in $REQUIRED_GITIGNORE_LINES) {
        if ($gitignoreLines -contains $required) {
            $foundPatterns += $required
        }
        else {
            $missingPatterns += $required
        }
    }

    $evidence += "Required patterns checked:"
    foreach ($p in $REQUIRED_GITIGNORE_LINES) {
        if ($gitignoreLines -contains $p) {
            $evidence += "  [FOUND] $p"
        }
        else {
            $evidence += "  [MISSING] $p"
        }
    }
    $evidence += ""

    if ($missingPatterns.Count -gt 0) {
        $allPassed = $false
        $evidence += "RESULT: FAIL - missing required .gitignore patterns:"
        foreach ($m in $missingPatterns) {
            $evidence += "  MISSING: $m"
        }
        Write-Host "[LEAK CHECK] FAIL: Missing .gitignore patterns: $($missingPatterns -join ', ')" -ForegroundColor Red
    }
    else {
        $evidence += "RESULT: PASS - all required patterns present"
        Write-Host "[LEAK CHECK] PASS: All required .gitignore patterns found" -ForegroundColor Green
    }
}

$evidence += ""

# =========================================================================
# CHECK 3: Remote VPS git status (R6.3)
# =========================================================================
$evidence += "--- CHECK 3: Remote VPS git status --porcelain (R6.3) ---"

if ($SkipRemote) {
    $evidence += "RESULT: SKIPPED - -SkipRemote flag set"
    Write-Host "[LEAK CHECK] R6.3: Skipped (SkipRemote flag)" -ForegroundColor Yellow
}
else {
    Write-Host "[LEAK CHECK] R6.3: Checking remote VPS git status..." -ForegroundColor Cyan

    try {
        # Check if /opt/atlassian-ai-workflow-platform is a git repo on VPS
        # rsync with --exclude='.git' means it typically won't be
        $remoteGitCheck = $null
        try {
            $remoteGitCheck = Invoke-VpsSsh -Command "if [ -d /opt/atlassian-ai-workflow-platform/.git ]; then echo IS_GIT_REPO; else echo NOT_GIT_REPO; fi"
        }
        catch {
            $remoteGitCheck = "SSH_FAILED"
        }

        $remoteGitCheckStr = ""
        if ($remoteGitCheck) {
            $remoteGitCheckStr = ($remoteGitCheck | Out-String).Trim()
        }
        else {
            $remoteGitCheckStr = "SSH_FAILED"
        }

        if ($remoteGitCheckStr -eq "NOT_GIT_REPO") {
            # Expected case: rsync --exclude='.git' means no .git directory on VPS
            $evidence += "Remote /opt/atlassian-ai-workflow-platform is NOT a git repository."
            $evidence += "This is expected when rsync --exclude='.git' was used for transfer (R3.3)."
            $evidence += "Without .git directory, git status cannot track files - leak via git is not possible."
            $evidence += "RESULT: SKIP - not a git repo (rsync --exclude='.git' transfer)"
            Write-Host "[LEAK CHECK] R6.3: SKIP - /opt/atlassian-ai-workflow-platform is not a git repo (expected with rsync)" -ForegroundColor Yellow
        }
        elseif ($remoteGitCheckStr -eq "IS_GIT_REPO") {
            # Repo exists on VPS - run the same leak check
            try {
                $remoteStatusRaw = Invoke-VpsSsh -Command "cd /opt/atlassian-ai-workflow-platform && git status --porcelain"
                $remoteLines = @()
                if ($remoteStatusRaw) {
                    $remoteLines = @($remoteStatusRaw | ForEach-Object { $_.ToString() })
                }

                $remoteLeaks = @(Test-LeakInGitStatus -StatusLines $remoteLines)
                $remoteRedacted = @(Get-RedactedGitStatus -StatusLines $remoteLines)

                $evidence += "Remote git status line count: $($remoteLines.Count)"
                $evidence += "Redacted output:"
                if ($remoteRedacted.Count -eq 0) {
                    $evidence += "  (clean working tree)"
                }
                else {
                    foreach ($line in $remoteRedacted) {
                        $evidence += "  $line"
                    }
                }
                $evidence += ""

                if ($remoteLeaks.Count -gt 0) {
                    $allPassed = $false
                    $evidence += "RESULT: FAIL - sensitive files detected in remote git status:"
                    foreach ($m in $remoteLeaks) {
                        $evidence += "  LEAK: $m"
                    }
                    Write-Host "[LEAK CHECK] FAIL: Sensitive files found in remote git status:" -ForegroundColor Red
                    foreach ($m in $remoteLeaks) {
                        Write-Host "  $m" -ForegroundColor Red
                    }
                }
                else {
                    $evidence += "RESULT: PASS - no sensitive file patterns matched on VPS"
                    Write-Host "[LEAK CHECK] PASS: No sensitive files in remote git status" -ForegroundColor Green
                }
            }
            catch {
                $evidence += "ERROR: Remote git status command failed: $_"
                $evidence += "RESULT: SKIP - could not execute remote git status"
                Write-Host "[LEAK CHECK] WARNING: Remote git status failed: $_" -ForegroundColor Yellow
            }
        }
        else {
            # SSH connection failed or unexpected response
            $evidence += "WARNING: Could not determine if remote is a git repo."
            $evidence += "Response: $remoteGitCheckStr"
            $evidence += "RESULT: SKIP - SSH connectivity issue"
            Write-Host "[LEAK CHECK] R6.3: SKIP - could not reach VPS" -ForegroundColor Yellow
        }
    }
    catch {
        $evidence += "ERROR: Exception during remote check: $_"
        $evidence += "RESULT: SKIP - exception occurred"
        Write-Host "[LEAK CHECK] WARNING: Remote check exception: $_" -ForegroundColor Yellow
    }
}

$evidence += ""

# =========================================================================
# FINAL VERDICT
# =========================================================================
$evidence += $separator
if ($allPassed) {
    $evidence += "FINAL VERDICT: PASS - all leak checks passed"
    $evidence += $separator
    Write-Host ""
    Write-Host "[LEAK CHECK] === ALL CHECKS PASSED ===" -ForegroundColor Green
}
else {
    $evidence += "FINAL VERDICT: FAIL - one or more leak checks failed"
    $evidence += $separator
    Write-Host ""
    Write-Host "[LEAK CHECK] === CHECKS FAILED ===" -ForegroundColor Red
}

# =========================================================================
# Write evidence file (R6.5)
# =========================================================================
if (-not (Test-Path $EVIDENCE_DIR)) {
    New-Item -ItemType Directory -Path $EVIDENCE_DIR -Force | Out-Null
}

$evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding UTF8 -Force
Write-Host "[LEAK CHECK] Evidence written to: $EVIDENCE_FILE" -ForegroundColor Cyan

# =========================================================================
# On failure: log critical Open Issue and halt (R6.4)
# =========================================================================
if (-not $allPassed) {
    Write-Host "[LEAK CHECK] Logging critical Open Issue (R6.4)..." -ForegroundColor Red

    Invoke-OpenIssueLogger `
        -Requirement "R6" `
        -Severity "critical" `
        -Category "config" `
        -Summary "Credential leak detected: .env or credentials file visible in git working tree" `
        -EvidencePath "vps-test-evidence/06-leakcheck.txt" `
        -RecommendedAction "config_change"

    Write-Host "[LEAK CHECK] HALTING - credential leak must be remediated before proceeding (R6.4)" -ForegroundColor Red
    exit 1
}

Write-Host "[LEAK CHECK] Leak check complete - safe to proceed." -ForegroundColor Green
exit 0
