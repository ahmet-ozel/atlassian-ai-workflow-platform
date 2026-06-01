# vps_repo_sync.ps1 - Repo sync: platform/ -> VPS:/opt/yeni_atlassian/platform/
# Requirements: R3.1, R3.2, R3.3, R3.4, R3.5, R22.1, R22.3
#
# Usage: .\vps_repo_sync.ps1
#   Dot-sources vps_common.ps1 for $SSH_KEY, $VPS, Invoke-VpsSsh, Copy-ToVps.
#   Transfers platform/ to VPS using rsync (preferred) or scp -r (fallback).
#   Validates critical files exist on remote, compares SHA256 of docker-compose.yml.
#   Writes evidence to vps-test-evidence/03-sync.txt.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Dot-source shared helpers ---
. "$PSScriptRoot\vps_common.ps1"

# --- Configuration ---
$LOCAL_PLATFORM_DIR = (Resolve-Path "$PSScriptRoot\..").Path
$REMOTE_PLATFORM_DIR = "/opt/yeni_atlassian/platform/"
$WORKSPACE_ROOT = (Resolve-Path "$PSScriptRoot\..\..").Path
$EVIDENCE_DIR = Join-Path $WORKSPACE_ROOT "vps-test-evidence"
$EVIDENCE_FILE = Join-Path $EVIDENCE_DIR "03-sync.txt"

# Files to validate post-sync (R3.2)
$REQUIRED_REMOTE_FILES = @(
    "infra/docker-compose.yml",
    "Makefile",
    "services/atlassian_unified/Dockerfile",
    "config/services.manifest.json"
)

# File for SHA256 comparison (R3.4)
$SHA256_CHECK_FILE = "infra/docker-compose.yml"

# --- Ensure evidence directory exists ---
if (-not (Test-Path $EVIDENCE_DIR)) {
    New-Item -ItemType Directory -Path $EVIDENCE_DIR -Force | Out-Null
}

# --- Evidence accumulator ---
$evidence = @()
$evidence += "=== VPS Repo Sync Evidence (R3) ==="
$evidence += "Timestamp: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
$evidence += "Local source: $LOCAL_PLATFORM_DIR"
$evidence += "Remote destination: ${VPS}:${REMOTE_PLATFORM_DIR}"
$evidence += ""

# --- Ensure remote directory exists ---
Write-Host "[SYNC] Ensuring remote directory exists: $REMOTE_PLATFORM_DIR"
try {
    Invoke-VpsSsh "mkdir -p $REMOTE_PLATFORM_DIR"
} catch {
    Write-Host "[ERROR] Failed to create remote directory: $_" -ForegroundColor Red
    throw
}

# --- Transfer: rsync preferred, scp -r fallback (R3.1, R3.3, R22.3) ---
$syncMethod = "unknown"
$syncExitCode = -1
$transferOutput = ""

# Check if rsync is available locally (Git Bash / WSL / native)
$rsyncAvailable = $false
$rsyncViaWsl = $false
try {
    $rsyncCheck = & rsync --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $rsyncAvailable = $true
    }
} catch {
    $rsyncAvailable = $false
}

# If native rsync not found, check WSL
if (-not $rsyncAvailable) {
    try {
        $wslCheck = & wsl which rsync 2>&1
        if ($LASTEXITCODE -eq 0) {
            $rsyncAvailable = $true
            $rsyncViaWsl = $true
            Write-Host "[SYNC] rsync found via WSL."
        }
    } catch {
        # WSL not available either
    }
}

if ($rsyncAvailable) {
    # Use rsync with --delete and --exclude='.git' (R3.1, R3.3)
    $syncMethod = "rsync"
    Write-Host "[SYNC] Using rsync for transfer (R3.1, R3.3)..."

    if ($rsyncViaWsl) {
        # Convert Windows path to WSL path format using wslpath
        $wslSource = (& wsl wslpath -u "$($LOCAL_PLATFORM_DIR -replace '\\','/')") 2>&1 | Out-String
        $wslSource = $wslSource.Trim() + "/"

        # SSH key on Windows NTFS has 777 perms in WSL — copy to WSL native fs with correct perms
        $null = & wsl bash -c "mkdir -p ~/.ssh && cp /mnt/c/Users/ahmet/.ssh/id_ed25519 ~/.ssh/id_ed25519_vps && chmod 600 ~/.ssh/id_ed25519_vps" 2>&1
        $wslSshKey = "~/.ssh/id_ed25519_vps"

        Write-Host "[SYNC] WSL source path: $wslSource"
        Write-Host "[SYNC] WSL SSH key: copied to $wslSshKey (chmod 600)"

        $rsyncCmd = "rsync -avz --delete --exclude=.git --exclude=__pycache__ --exclude=.mypy_cache --exclude=.pytest_cache --exclude=.hypothesis --exclude='*.pyc' --exclude=.env --exclude=node_modules --exclude=.venv -e 'ssh -i $wslSshKey -o StrictHostKeyChecking=accept-new' '$wslSource' '${VPS}:${REMOTE_PLATFORM_DIR}'"

        Write-Host "[SYNC] WSL rsync command: $rsyncCmd"
        $transferOutput = & wsl bash -c $rsyncCmd 2>&1 | Out-String
        $syncExitCode = $LASTEXITCODE

        Write-Host "[SYNC] rsync exit code: $syncExitCode"
    } else {
        # Convert Windows path to rsync-compatible format
        # rsync on Windows (via Git Bash) may need /c/Users/... format
        $rsyncSource = ($LOCAL_PLATFORM_DIR -replace '\\', '/') + "/"
        # If path starts with drive letter like C:, convert to /c/ format for MSYS/Git Bash
        if ($rsyncSource -match '^([A-Za-z]):(.*)$') {
            $rsyncSource = "/" + $Matches[1].ToLower() + $Matches[2]
        }

        $rsyncArgs = @(
            "-avz",
            "--delete",
            "--exclude=.git",
            "--exclude=__pycache__",
            "--exclude=.mypy_cache",
            "--exclude=.pytest_cache",
            "--exclude=.hypothesis",
            "--exclude=*.pyc",
            "--exclude=.env",
            "--exclude=node_modules",
            "--exclude=.venv",
            "-e", "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new",
            $rsyncSource,
            "${VPS}:${REMOTE_PLATFORM_DIR}"
        )

        Write-Host "[SYNC] rsync command: rsync $($rsyncArgs -join ' ')"
        $transferOutput = & rsync @rsyncArgs 2>&1 | Out-String
        $syncExitCode = $LASTEXITCODE

        Write-Host "[SYNC] rsync exit code: $syncExitCode"
    }
} else {
    # Fallback to scp -r (R22.3)
    $syncMethod = "scp-fallback"
    Write-Host "[SYNC] rsync not available, falling back to scp -r (R22.3)..."

    # scp -r transfers the directory contents
    $scpArgs = @(
        "-i", $SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-r",
        "$LOCAL_PLATFORM_DIR\*",
        "${VPS}:${REMOTE_PLATFORM_DIR}"
    )

    # For scp, we need to handle the transfer differently on Windows
    # scp -r copies the directory itself, so we transfer platform/* to remote
    Write-Host "[SYNC] scp command: scp $($scpArgs -join ' ')"
    $transferOutput = & scp -i $SSH_KEY -o "StrictHostKeyChecking=accept-new" -r "${LOCAL_PLATFORM_DIR}" "${VPS}:/opt/yeni_atlassian/" 2>&1 | Out-String
    $syncExitCode = $LASTEXITCODE

    Write-Host "[SYNC] scp exit code: $syncExitCode"
}

$evidence += "--- Transfer Method ---"
$evidence += "Method: $syncMethod"
$evidence += "Exit code: $syncExitCode"
$evidence += ""

# Extract transferred byte count from rsync output if available
$transferredBytes = "N/A"
if ($syncMethod -eq "rsync" -and $transferOutput) {
    # rsync summary line: "sent X bytes  received Y bytes"
    if ($transferOutput -match 'sent\s+([\d,]+)\s+bytes') {
        $transferredBytes = $Matches[1]
    }
    # Also look for total transferred size line
    if ($transferOutput -match 'total size is\s+([\d,]+)') {
        $evidence += "Total size: $($Matches[1]) bytes"
    }
}
$evidence += "Transferred bytes (sent): $transferredBytes"
$evidence += ""

if ($syncExitCode -ne 0) {
    $evidence += "[FAIL] Transfer failed with exit code $syncExitCode"
    $evidence += "Output: $transferOutput"
    $evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding utf8
    Write-Host "[ERROR] Transfer failed (exit $syncExitCode)" -ForegroundColor Red

    # Log Open Issue
    python -m platform.scripts.vps_open_issue_logger `
        --requirement R3 `
        --severity major `
        --category infra `
        --summary "Repo sync transfer failed with exit code $syncExitCode ($syncMethod)" `
        --evidence-path "vps-test-evidence/03-sync.txt" `
        --recommended-action manual_fix

    exit 2
}

Write-Host "[SYNC] Transfer completed successfully." -ForegroundColor Green

# --- Post-sync file existence validation (R3.2) ---
Write-Host "[SYNC] Validating required files on remote (R3.2)..."
$evidence += "--- Post-Sync File Validation (R3.2) ---"
$allFilesExist = $true

foreach ($file in $REQUIRED_REMOTE_FILES) {
    $remotePath = "${REMOTE_PLATFORM_DIR}${file}"
    try {
        Invoke-VpsSsh "test -f '$remotePath'"
        $evidence += "[OK] $remotePath exists"
        Write-Host "  [OK] $file" -ForegroundColor Green
    } catch {
        $evidence += "[FAIL] $remotePath NOT FOUND"
        Write-Host "  [FAIL] $file" -ForegroundColor Red
        $allFilesExist = $false
    }
}
$evidence += ""

if (-not $allFilesExist) {
    $evidence += "[FAIL] One or more required files missing on remote."
    $evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding utf8
    Write-Host "[ERROR] Post-sync validation failed: required files missing." -ForegroundColor Red

    python -m platform.scripts.vps_open_issue_logger `
        --requirement R3 `
        --severity major `
        --category infra `
        --summary "Post-sync validation failed: required remote files missing" `
        --evidence-path "vps-test-evidence/03-sync.txt" `
        --recommended-action manual_fix

    exit 2
}

Write-Host "[SYNC] All required files verified on remote." -ForegroundColor Green

# --- SHA256 hash comparison (R3.4) ---
Write-Host "[SYNC] Comparing SHA256 hashes for $SHA256_CHECK_FILE (R3.4)..."
$evidence += "--- SHA256 Comparison (R3.4) ---"
$evidence += "File: $SHA256_CHECK_FILE"

# Local SHA256 via PowerShell Get-FileHash
$localFilePath = Join-Path $LOCAL_PLATFORM_DIR $SHA256_CHECK_FILE
$localHash = (Get-FileHash -Path $localFilePath -Algorithm SHA256).Hash.ToLower()
$evidence += "Local SHA256:  $localHash"
Write-Host "  Local SHA256:  $localHash"

# Remote SHA256 via sha256sum
$remoteFilePath = "${REMOTE_PLATFORM_DIR}${SHA256_CHECK_FILE}"
$remoteSha256Output = Invoke-VpsSsh "sha256sum '$remoteFilePath'"
# sha256sum output format: "<hash>  <filepath>"
$remoteHash = ($remoteSha256Output -split '\s+')[0].ToLower()
$evidence += "Remote SHA256: $remoteHash"
Write-Host "  Remote SHA256: $remoteHash"

$evidence += ""

if ($localHash -eq $remoteHash) {
    $evidence += "[PASS] SHA256 hashes match."
    Write-Host "[SYNC] SHA256 hashes match." -ForegroundColor Green
} else {
    $evidence += "[FAIL] SHA256 MISMATCH - local and remote docker-compose.yml differ!"
    $evidence += "  Local:  $localHash"
    $evidence += "  Remote: $remoteHash"
    $evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding utf8
    Write-Host "[ERROR] SHA256 mismatch! Halting." -ForegroundColor Red

    # Halt + Open Issue on mismatch
    python -m platform.scripts.vps_open_issue_logger `
        --requirement R3 `
        --severity major `
        --category infra `
        --summary "SHA256 mismatch for $SHA256_CHECK_FILE between local and VPS" `
        --evidence-path "vps-test-evidence/03-sync.txt" `
        --recommended-action manual_fix

    exit 2
}

# --- Write final evidence (R3.5) ---
$evidence += ""
$evidence += "--- Summary ---"
$evidence += "Sync method: $syncMethod"
$evidence += "Sync exit code: $syncExitCode"
$evidence += "Transferred bytes: $transferredBytes"
$evidence += "SHA256 match: YES"
$evidence += "All required files present: YES"
$evidence += "Verdict: PASS"

$evidence | Out-File -FilePath $EVIDENCE_FILE -Encoding utf8

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Repo Sync Complete - R3 PASS" -ForegroundColor Cyan
Write-Host " Evidence: $EVIDENCE_FILE" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
