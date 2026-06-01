# vps_credential_loader.ps1 — Generate platform/.env + services/atlassian_unified/.env from CREDENTIALS.md
# Requirements: R4.1, R4.2, R4.3, R4.4, R4.5, R4.6, R5.1, R5.2, R22.1, R22.4
#
# Usage: . .\vps_credential_loader.ps1
#   Produces platform/.env and services/atlassian_unified/.env locally,
#   scp's both to VPS, emits evidence files, runs property test.
#   Dot-source vps_common.ps1 for SSH/SCP helpers.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Dot-source common helpers ---
. "$PSScriptRoot\vps_common.ps1"

# --- Paths ---
$WorkspaceRoot   = (Resolve-Path "$PSScriptRoot\..\..").Path
$CredentialsFile = Join-Path $WorkspaceRoot "CREDENTIALS.md"
$EnvExampleFile  = Join-Path $WorkspaceRoot "platform\.env.example"
$EnvOutputFile   = Join-Path $WorkspaceRoot "platform\.env"
$EvidenceDir     = Join-Path $WorkspaceRoot "vps-test-evidence"
$EvidenceFile    = Join-Path $EvidenceDir "04-workspace-env-keys.txt"
$VaultDevNote    = Join-Path $EvidenceDir "vault-dev-mode-note.txt"

# --- Ensure evidence directory exists ---
if (-not (Test-Path $EvidenceDir)) {
    New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null
}

# =============================================================================
# STEP 1: Parse CREDENTIALS.md for OpenAI API Key (regex-based)
# =============================================================================
Write-Host "[credential_loader] Parsing CREDENTIALS.md..." -ForegroundColor Cyan

if (-not (Test-Path $CredentialsFile)) {
    throw "CREDENTIALS.md not found at: $CredentialsFile"
}

$credContent = Get-Content -Path $CredentialsFile -Raw

# Extract OpenAI API Key
$openaiKeyMatch = [regex]::Match($credContent, '(?m)^\|\s*API Key\s*\|\s*`(sk-proj-[^`]+)`')
if (-not $openaiKeyMatch.Success) {
    throw "Failed to parse OpenAI API Key from CREDENTIALS.md"
}
$openaiApiKey = $openaiKeyMatch.Groups[1].Value
Write-Host "[credential_loader] OpenAI API Key parsed (length=$($openaiApiKey.Length))" -ForegroundColor Green

# =============================================================================
# STEP 2: Read .env.example template keys
# =============================================================================
Write-Host "[credential_loader] Reading .env.example template..." -ForegroundColor Cyan

if (-not (Test-Path $EnvExampleFile)) {
    throw ".env.example not found at: $EnvExampleFile"
}

$exampleLines = Get-Content -Path $EnvExampleFile
$templateKeys = @{}
foreach ($line in $exampleLines) {
    $m = [regex]::Match($line, '^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$')
    if ($m.Success) {
        $templateKeys[$m.Groups[1].Value] = $m.Groups[2].Value
    }
}
Write-Host "[credential_loader] Template contains $($templateKeys.Count) keys" -ForegroundColor Green

# =============================================================================
# STEP 3: Generate secure random hex values (R4.3, R4.4)
# =============================================================================
Write-Host "[credential_loader] Generating secure random values..." -ForegroundColor Cyan

function New-SecureHex {
    <#
    .SYNOPSIS
        Generate a 32-byte (64 hex char) cryptographically secure random hex string.
    .DESCRIPTION
        Uses [System.Security.Cryptography.RandomNumberGenerator]::Create() per R4.3 spec.
    #>
    param([int]$ByteCount = 16)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] $ByteCount
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ''
}

$postgresPassword = New-SecureHex -ByteCount 16
$vaultToken       = New-SecureHex -ByteCount 16

Write-Host "[credential_loader] POSTGRES_PASSWORD generated (32 hex chars)" -ForegroundColor Green
Write-Host "[credential_loader] VAULT_TOKEN generated (32 hex chars)" -ForegroundColor Green

# =============================================================================
# STEP 4: Build .env content — superset of .env.example keys (R4.1)
# =============================================================================
Write-Host "[credential_loader] Building platform/.env..." -ForegroundColor Cyan

# Start with all template keys, then override specific ones
$envValues = @{}
foreach ($key in $templateKeys.Keys) {
    $envValues[$key] = $templateKeys[$key]
}

# --- R4.2: LLM configuration ---
$envValues["LLM_PROVIDER"]   = "openai"
$envValues["LLM_MODEL_NAME"] = "gpt-4o-mini"
$envValues["OPENAI_API_KEY"] = $openaiApiKey

# --- R4.3: Postgres credentials ---
$envValues["POSTGRES_USER"]     = "ai"
$envValues["POSTGRES_DB"]       = "ai"
$envValues["POSTGRES_PASSWORD"] = $postgresPassword
$envValues["POSTGRES_DSN"]      = "postgresql://ai:${postgresPassword}@postgres:5432/ai"

# --- R4.4: Vault dev-mode token ---
$envValues["VAULT_TOKEN"] = $vaultToken
$envValues["VAULT_ADDR"]  = "http://vault:8200"

# --- R4.5: Feature flags forced to false ---
$envValues["FEATURE_FLAG_TASK_INTAKE_ENABLED"]    = "false"
$envValues["FEATURE_FLAG_FIRECRAWL_ENABLED"]      = "false"
$envValues["FEATURE_FLAG_PR_AUTO_MERGE_ENABLED"]  = "false"
$envValues["FEATURE_FLAG_AUDIT_PRUNE_ENABLED"]    = "false"

# --- R9.4: Observability / Log settings ---
$envValues["LOG_REDACTION_ENABLED"] = "true"
$envValues["LOG_LEVEL"]             = "INFO"
$envValues["LOG_FORMAT"]            = "json"

# =============================================================================
# STEP 5: Write platform/.env file
# =============================================================================

$envContent = @"
# =============================================================================
# platform/.env — Generated by vps_credential_loader.ps1
# Generated: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
# WARNING: This file contains secrets. NEVER commit to git.
# =============================================================================

# --- Postgres (R4.3) ---
POSTGRES_USER=$($envValues["POSTGRES_USER"])
POSTGRES_PASSWORD=$($envValues["POSTGRES_PASSWORD"])
POSTGRES_DB=$($envValues["POSTGRES_DB"])
POSTGRES_DSN=$($envValues["POSTGRES_DSN"])

# --- Vault (R4.4 — dev-mode token) ---
VAULT_ADDR=$($envValues["VAULT_ADDR"])
VAULT_TOKEN=$($envValues["VAULT_TOKEN"])

# --- Temporal ---
TEMPORAL_HOST=$($envValues["TEMPORAL_HOST"])
TEMPORAL_TASK_QUEUE=$($envValues["TEMPORAL_TASK_QUEUE"])

# --- MCP / Firecrawl ---
MCP_BASE_URL=$($envValues["MCP_BASE_URL"])
FIRECRAWL_BASE_URL=$($envValues["FIRECRAWL_BASE_URL"])

# --- LLM (R4.2) ---
LLM_PROVIDER=$($envValues["LLM_PROVIDER"])
LLM_MODEL_NAME=$($envValues["LLM_MODEL_NAME"])
VLLM_BASE_URL=$($envValues["VLLM_BASE_URL"])
OPENAI_API_KEY=$($envValues["OPENAI_API_KEY"])
ANTHROPIC_API_KEY=$($envValues["ANTHROPIC_API_KEY"])

# --- MinIO ---
MINIO_ENDPOINT=$($envValues["MINIO_ENDPOINT"])
MINIO_ROOT_USER=$($envValues["MINIO_ROOT_USER"])
MINIO_ROOT_PASSWORD=$($envValues["MINIO_ROOT_PASSWORD"])

# --- SSH runners ---
SSH_HOST=$($envValues["SSH_HOST"])
RUNNER_BASE_PATH=$($envValues["RUNNER_BASE_PATH"])
RUNNER_DISK_WARN_PCT=$($envValues["RUNNER_DISK_WARN_PCT"])
RUNNER_DISK_EVICT_PCT=$($envValues["RUNNER_DISK_EVICT_PCT"])

# --- Feature flags (R4.5 — all disabled for E2E test) ---
FEATURE_FLAG_AI_ENABLED=$($envValues["FEATURE_FLAG_AI_ENABLED"])
FEATURE_FLAG_EXECUTION_ENABLED=$($envValues["FEATURE_FLAG_EXECUTION_ENABLED"])
FEATURE_FLAG_TASK_INTAKE_ENABLED=$($envValues["FEATURE_FLAG_TASK_INTAKE_ENABLED"])
FEATURE_FLAG_FIRECRAWL_ENABLED=$($envValues["FEATURE_FLAG_FIRECRAWL_ENABLED"])
FEATURE_FLAG_PR_AUTO_MERGE_ENABLED=$($envValues["FEATURE_FLAG_PR_AUTO_MERGE_ENABLED"])
FEATURE_FLAG_AUDIT_PRUNE_ENABLED=$($envValues["FEATURE_FLAG_AUDIT_PRUNE_ENABLED"])
SSH_RUNNER_DEPT_PINNING_ENABLED=$($envValues["SSH_RUNNER_DEPT_PINNING_ENABLED"])
SSH_DEPT_QUOTA_ENABLED=$($envValues["SSH_DEPT_QUOTA_ENABLED"])

# --- Observability / Log (R9.4) ---
LOG_REDACTION_ENABLED=$($envValues["LOG_REDACTION_ENABLED"])
LOG_LEVEL=$($envValues["LOG_LEVEL"])
LOG_FORMAT=$($envValues["LOG_FORMAT"])
"@

Set-Content -Path $EnvOutputFile -Value $envContent -Encoding UTF8 -NoNewline
Write-Host "[credential_loader] Written: $EnvOutputFile" -ForegroundColor Green

# =============================================================================
# STEP 6: Save Vault dev-mode note (R4.4)
# =============================================================================
$vaultNote = @"
VAULT DEV-MODE NOTE
===================
Date: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")

The VAULT_TOKEN in platform/.env is a dev-mode token generated for this
VPS E2E test run. It is NOT suitable for production deployment.

Production deployment uses Vault AppRole authentication and is out of scope
for the vps-e2e-deployment-test spec.

This note should be referenced in TEST_REPORT.md under Requirement R4.4.
"@

Set-Content -Path $VaultDevNote -Value $vaultNote -Encoding UTF8
Write-Host "[credential_loader] Vault dev-mode note saved: $VaultDevNote" -ForegroundColor Green

# =============================================================================
# STEP 7: SCP .env to VPS + chmod 600 (R22.4)
# =============================================================================
Write-Host "[credential_loader] Transferring .env to VPS..." -ForegroundColor Cyan

# Ensure remote directory exists
Invoke-VpsSsh "mkdir -p /opt/yeni_atlassian/platform"

# Copy .env file
Copy-ToVps -LocalPath $EnvOutputFile -RemotePath "/opt/yeni_atlassian/platform/.env"

# Set restrictive permissions
Invoke-VpsSsh "chmod 600 /opt/yeni_atlassian/platform/.env"

Write-Host "[credential_loader] .env deployed to VPS with chmod 600" -ForegroundColor Green

# =============================================================================
# STEP 8: Generate evidence file — KEY NAMES ONLY, no values (R22)
# =============================================================================
Write-Host "[credential_loader] Generating evidence file..." -ForegroundColor Cyan

# Extract all KEY=... lines from the generated .env, output only key names
$envLines = Get-Content -Path $EnvOutputFile
$keyNames = @()
foreach ($line in $envLines) {
    $m = [regex]::Match($line, '^\s*([A-Z_][A-Z0-9_]*)\s*=')
    if ($m.Success) {
        $keyNames += $m.Groups[1].Value
    }
}

$evidenceContent = @"
# Evidence: platform/.env key inventory (values redacted for security)
# Generated: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
# Requirement: R4.1 (superset of .env.example), R22 (no secrets in evidence)
# Total keys: $($keyNames.Count)
# ---
$($keyNames | Sort-Object | ForEach-Object { $_ } | Out-String)
"@

Set-Content -Path $EvidenceFile -Value $evidenceContent.Trim() -Encoding UTF8
Write-Host "[credential_loader] Evidence written: $EvidenceFile ($($keyNames.Count) keys)" -ForegroundColor Green

# =============================================================================
# STEP 9: Validate superset coverage (R4.1)
# =============================================================================
Write-Host "[credential_loader] Validating .env.example superset coverage..." -ForegroundColor Cyan

$missingKeys = @()
foreach ($key in $templateKeys.Keys) {
    if ($key -notin $keyNames) {
        $missingKeys += $key
    }
}

if ($missingKeys.Count -gt 0) {
    Write-Host "[credential_loader] WARNING: Missing keys from .env.example:" -ForegroundColor Yellow
    $missingKeys | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    Write-Host "[credential_loader] .env may not satisfy R4.1 superset requirement" -ForegroundColor Yellow
} else {
    Write-Host "[credential_loader] All .env.example keys present in generated .env (R4.1 satisfied)" -ForegroundColor Green
}

# =============================================================================
# STEP 10: Generate services/atlassian_unified/.env (R5.1, R5.2)
# =============================================================================
Write-Host "[credential_loader] Generating MCP env (services/atlassian_unified/.env)..." -ForegroundColor Cyan

$McpEnvOutputFile = Join-Path $WorkspaceRoot "platform\services\atlassian_unified\.env"
$McpEvidenceFile  = Join-Path $EvidenceDir "05-mcp-env-keys.txt"

# --- Parse Jira API Token from CREDENTIALS.md ---
$jiraTokenMatch = [regex]::Match($credContent, '(?m)^\|\s*API Token\s*\|\s*`(ATATT3x[^`]+)`')
if (-not $jiraTokenMatch.Success) {
    throw "Failed to parse Jira API Token from CREDENTIALS.md"
}
$jiraApiToken = $jiraTokenMatch.Groups[1].Value
Write-Host "[credential_loader] Jira API Token parsed (length=$($jiraApiToken.Length))" -ForegroundColor Green

# Confluence uses the same token as Jira (Atlassian Cloud unified auth)
$confluenceApiToken = $jiraApiToken

# --- Parse Bitbucket Token B (Kişisel API Token, Basic Auth) from CREDENTIALS.md ---
# Token B is under "Token 2: Kişisel API Token (Basic Auth)" section
$bitbucketTokenBMatch = [regex]::Match($credContent, '(?ms)Token 2.*?\|\s*Token\s*\|\s*`(ATATT3x[^`]+)`')
if (-not $bitbucketTokenBMatch.Success) {
    throw "Failed to parse Bitbucket Token B from CREDENTIALS.md"
}
$bitbucketTokenB = $bitbucketTokenBMatch.Groups[1].Value
Write-Host "[credential_loader] Bitbucket Token B parsed (length=$($bitbucketTokenB.Length))" -ForegroundColor Green

# --- Build MCP .env content (R5.1, R5.2) ---
$mcpEnvContent = @"
# =============================================================================
# services/atlassian_unified/.env — Generated by vps_credential_loader.ps1
# Generated: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
# WARNING: This file contains secrets. NEVER commit to git.
# =============================================================================

# --- Jira (R5.1) ---
JIRA_URL=https://example.atlassian.net
JIRA_USERNAME=user@example.com
JIRA_API_TOKEN=$jiraApiToken

# --- Confluence (R5.1) ---
CONFLUENCE_URL=https://example.atlassian.net/wiki
CONFLUENCE_USERNAME=user@example.com
CONFLUENCE_API_TOKEN=$confluenceApiToken

# --- Bitbucket — Token B (Basic Auth, primary) (R5.2) ---
BITBUCKET_USERNAME=user@example.com
BITBUCKET_PASSWORD=$bitbucketTokenB

# --- MCP Server Settings (R5.1) ---
TRANSPORT=streamable-http
PORT=8090
READ_ONLY=false
MCP_VERY_VERBOSE=true
"@

Set-Content -Path $McpEnvOutputFile -Value $mcpEnvContent -Encoding UTF8 -NoNewline
Write-Host "[credential_loader] Written: $McpEnvOutputFile" -ForegroundColor Green

# =============================================================================
# STEP 11: SCP MCP .env to VPS + chmod 600 (R5.1)
# =============================================================================
Write-Host "[credential_loader] Transferring MCP .env to VPS..." -ForegroundColor Cyan

# Ensure remote directory exists
Invoke-VpsSsh "mkdir -p /opt/yeni_atlassian/platform/services/atlassian_unified"

# Copy MCP .env file
Copy-ToVps -LocalPath $McpEnvOutputFile -RemotePath "/opt/yeni_atlassian/platform/services/atlassian_unified/.env"

# Set restrictive permissions
Invoke-VpsSsh "chmod 600 /opt/yeni_atlassian/platform/services/atlassian_unified/.env"

Write-Host "[credential_loader] MCP .env deployed to VPS with chmod 600" -ForegroundColor Green

# =============================================================================
# STEP 12: Generate MCP evidence file — KEY NAMES ONLY (R22)
# =============================================================================
Write-Host "[credential_loader] Generating MCP evidence file..." -ForegroundColor Cyan

$mcpEnvLines = Get-Content -Path $McpEnvOutputFile
$mcpKeyNames = @()
foreach ($line in $mcpEnvLines) {
    $m = [regex]::Match($line, '^\s*([A-Z_][A-Z0-9_]*)\s*=')
    if ($m.Success) {
        $mcpKeyNames += $m.Groups[1].Value
    }
}

$mcpEvidenceContent = @"
# Evidence: services/atlassian_unified/.env key inventory (values redacted for security)
# Generated: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
# Requirement: R5.1 (MCP credential file), R5.2 (Bitbucket Token B primary), R22 (no secrets in evidence)
# Total keys: $($mcpKeyNames.Count)
# ---
$($mcpKeyNames | Sort-Object | ForEach-Object { $_ } | Out-String)
"@

Set-Content -Path $McpEvidenceFile -Value $mcpEvidenceContent.Trim() -Encoding UTF8
Write-Host "[credential_loader] MCP evidence written: $McpEvidenceFile ($($mcpKeyNames.Count) keys)" -ForegroundColor Green

# =============================================================================
# STEP 13: Run property test — test_env_coverage.py (R4.6)
# =============================================================================
Write-Host "[credential_loader] Running property test: test_env_coverage.py..." -ForegroundColor Cyan

try {
    $pytestOutput = Invoke-VpsSsh "cd /opt/yeni_atlassian/platform && python3 -m pytest tests/property/test_env_coverage.py -v 2>&1"
    $pytestExitCode = 0
    Write-Host "[credential_loader] Property test PASSED (exit 0)" -ForegroundColor Green
} catch {
    $pytestOutput = $_.Exception.Message
    $pytestExitCode = 1
    Write-Host "[credential_loader] Property test FAILED" -ForegroundColor Red
    Write-Host $pytestOutput -ForegroundColor Yellow
    throw "Property test test_env_coverage.py failed (R4.6 violation). Halting."
}

# =============================================================================
# SUMMARY
# =============================================================================
Write-Host ""
$separator = "=" * 70
Write-Host $separator -ForegroundColor Cyan
Write-Host "[credential_loader] COMPLETE - platform/.env + MCP .env generation" -ForegroundColor Green
Write-Host "  Local:    $EnvOutputFile" -ForegroundColor White
Write-Host "  Remote:   /opt/yeni_atlassian/platform/.env (chmod 600)" -ForegroundColor White
Write-Host "  MCP Local:  $McpEnvOutputFile" -ForegroundColor White
Write-Host "  MCP Remote: /opt/yeni_atlassian/platform/services/atlassian_unified/.env (chmod 600)" -ForegroundColor White
Write-Host "  Evidence: $EvidenceFile" -ForegroundColor White
Write-Host "  MCP Evidence: $McpEvidenceFile" -ForegroundColor White
Write-Host "  Keys:     $($keyNames.Count) (platform) + $($mcpKeyNames.Count) (MCP)" -ForegroundColor White
Write-Host "  Vault:    dev-mode (see $VaultDevNote)" -ForegroundColor White
Write-Host "  Property: test_env_coverage.py PASSED" -ForegroundColor White
Write-Host $separator -ForegroundColor Cyan
