# vps_common.ps1 — Shared PowerShell helpers for VPS E2E Deployment Harness
# Requirements: R22.1, R22.2
#
# Usage: . "$PSScriptRoot\vps_common.ps1"  (dot-source from other ps1 scripts)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Canonical SSH parameters (R22.2) ---
$SSH_KEY = "$env:USERPROFILE\.ssh\id_ed25519"
$VPS     = "root@91.99.149.163"

# --- Helper Functions ---

function Invoke-VpsSsh {
    <#
    .SYNOPSIS
        Execute a remote command on VPS_Host via SSH.
    .DESCRIPTION
        Wraps ssh invocation using the canonical form defined in R22.2.
        Each call opens a new SSH session (no ControlMaster sockets).
    .PARAMETER Command
        The remote command string to execute on VPS_Host.
    .PARAMETER TimeoutSeconds
        Optional SSH ConnectTimeout (default 30).
    .OUTPUTS
        Returns the stdout output of the remote command.
        Throws on non-zero exit code.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Command,

        [Parameter()]
        [int]$TimeoutSeconds = 30
    )

    $sshArgs = @(
        "-i", $SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=$TimeoutSeconds",
        "-o", "BatchMode=yes",
        $VPS,
        $Command
    )

    $output = & ssh @sshArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $errorMsg = "SSH command failed (exit $LASTEXITCODE): $Command`nOutput: $output"
        throw $errorMsg
    }
    return $output
}

function Copy-ToVps {
    <#
    .SYNOPSIS
        Copy a local file or directory to VPS_Host via scp.
    .DESCRIPTION
        Uses scp with the canonical SSH key (R22.2). Falls back to scp -r
        for directories. No ControlMaster, no WSL dependency (R22.3).
    .PARAMETER LocalPath
        Path to the local file or directory to transfer.
    .PARAMETER RemotePath
        Destination path on VPS_Host (e.g., /opt/yeni_atlassian/platform/.env).
    .PARAMETER Recursive
        If set, uses -r flag for directory transfer.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$LocalPath,

        [Parameter(Mandatory = $true, Position = 1)]
        [string]$RemotePath,

        [Parameter()]
        [switch]$Recursive
    )

    $scpArgs = @("-i", $SSH_KEY, "-o", "StrictHostKeyChecking=accept-new")

    if ($Recursive) {
        $scpArgs += "-r"
    }

    $scpArgs += $LocalPath
    $scpArgs += "${VPS}:${RemotePath}"

    & scp @scpArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "SCP transfer failed (exit $LASTEXITCODE): $LocalPath -> ${VPS}:${RemotePath}"
    }
}
