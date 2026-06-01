# =============================================================================
# platform/scripts/up.ps1 — Windows/PowerShell equivalent of `make boot` / `make up-all`
# =============================================================================
# Implements platform-mimari-foundation task 10.3 / Requirement 2.8 for hosts
# that do not have GNU make installed (the typical Windows developer setup)
# AND platform-real-usage-gaps R2 (R2.1, R2.2, R2.4).
#
# Default semantics ("boot bundle"):
#
#   docker compose `
#     -f infra/docker-compose.yml `
#     -f infra/docker-compose.dev.yml `
#     up -d
#
# (NO --profile flags — only services without a `profiles:` key start:
# postgres, vault, admin-dashboard-api, admin-dashboard-ui. Operators drive
# the rest from the admin-dashboard Setup Wizard.)
#
# Opt-in full-stack semantics (`-All` switch or `up-all` command):
#
#   docker compose `
#     -f infra/docker-compose.yml `
#     -f infra/docker-compose.dev.yml `
#     --profile <p1> --profile <p2> ... `
#     up -d
#
# The profile list is DERIVED at runtime from config/services.manifest.json —
# every entry whose `kind` is one of {infra, http_service, worker, sidecar, ui}
# contributes its `compose_profile` field. This keeps the manifest as the
# single source of truth (requirements §1.1, §1.10, §2.1).
#
# Usage (from anywhere; the script resolves repo paths itself):
#   .\scripts\up.ps1                     # default: boot bundle (no profiles)
#   .\scripts\up.ps1 up                  # same as default
#   .\scripts\up.ps1 up -All             # full stack (every manifest profile)
#   .\scripts\up.ps1 up-all              # same as `up -All`
#   .\scripts\up.ps1 boot                # explicit boot bundle
#   .\scripts\up.ps1 down                # docker compose down (with profiles)
#   .\scripts\up.ps1 logs                # docker compose logs -f --tail=200
#   .\scripts\up.ps1 ps                  # docker compose ps
#   .\scripts\up.ps1 restart             # down + boot
#   .\scripts\up.ps1 profiles            # print the derived profile list
#
# Environment overrides:
#   $env:PY        Python interpreter used to parse the manifest
#                  (default: "python", which works with the py-launcher
#                  shipped on Windows).
#   $env:COMPOSE   Compose CLI string (default: "docker compose"). Set to
#                  "docker-compose" on legacy hosts that still ship the v1
#                  binary.
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'up-all', 'boot', 'down', 'logs', 'ps', 'restart', 'profiles', 'help')]
    [string]$Command = 'up',

    [Parameter()]
    [switch]$All
)

$ErrorActionPreference = 'Stop'

# Resolve repo paths relative to this script regardless of caller's CWD.
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$platformDir = (Resolve-Path (Join-Path $scriptDir '..')).Path

$py      = if ($env:PY)      { $env:PY }      else { 'python' }
$compose = if ($env:COMPOSE) { $env:COMPOSE } else { 'docker compose' }

$composeBase = Join-Path $platformDir 'infra\docker-compose.yml'
$composeDev  = Join-Path $platformDir 'infra\docker-compose.dev.yml'
$manifest    = Join-Path $platformDir 'config\services.manifest.json'

if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Error "up.ps1: manifest not found: $manifest"
    exit 1
}

# --- Derive profile list from manifest ---------------------------------------
# Inline Python script kept short and side-effect-free: read JSON, filter on
# the foundation `kind` enum, emit one profile per stdout line. PowerShell
# splits the output on newlines back into a string array.
$pyExpr = @"
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    manifest = json.load(fh)
kinds = {'infra', 'http_service', 'worker', 'sidecar', 'ui'}
for entry in manifest['services']:
    if entry['kind'] in kinds:
        print(entry['compose_profile'])
"@

# Split $py so a value like "py -3" expands into separate tokens.
$pyArgv  = $py -split '\s+'
$pyExe   = $pyArgv[0]
$pyHead  = if ($pyArgv.Length -gt 1) { $pyArgv[1..($pyArgv.Length - 1)] } else { @() }
$pyArgs  = @() + $pyHead + @('-c', $pyExpr, $manifest)

$profilesRaw = & $pyExe @pyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "up.ps1: failed to derive profiles from $manifest"
    exit $LASTEXITCODE
}

$profiles = @($profilesRaw |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ })

if ($profiles.Count -eq 0) {
    Write-Error 'up.ps1: no profiles derived from manifest — nothing to do'
    exit 1
}

# Build "--profile p1 --profile p2 ..." token list.
$profileFlags = @()
foreach ($p in $profiles) {
    $profileFlags += '--profile'
    $profileFlags += $p
}

# Standard `docker compose ...` argv prefix. Splitting $compose on whitespace
# keeps both the modern "docker compose" and the legacy "docker-compose"
# invocations working without any further conditionals.
$composeArgv = $compose -split '\s+'
$composeExe  = $composeArgv[0]
$composeHead = if ($composeArgv.Length -gt 1) { $composeArgv[1..($composeArgv.Length - 1)] } else { @() }

# Boot bundle prefix: base + dev override, NO --profile flags.
$bootPrefixArgs = @() + $composeHead + @('-f', $composeBase, '-f', $composeDev)

# Full-stack prefix: same plus every manifest profile.
$fullPrefixArgs = $bootPrefixArgs + $profileFlags

# --- Subcommand dispatch ------------------------------------------------------
function Invoke-Compose {
    param(
        [string[]]$Prefix,
        [string[]]$Extra
    )
    & $composeExe @Prefix @Extra
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Command) {
    'up' {
        if ($All) {
            Invoke-Compose -Prefix $fullPrefixArgs -Extra @('up', '-d')
        }
        else {
            Invoke-Compose -Prefix $bootPrefixArgs -Extra @('up', '-d')
        }
    }
    'up-all' {
        Invoke-Compose -Prefix $fullPrefixArgs -Extra @('up', '-d')
    }
    'boot' {
        Invoke-Compose -Prefix $bootPrefixArgs -Extra @('up', '-d')
    }
    'down' {
        Invoke-Compose -Prefix $fullPrefixArgs -Extra @('down')
    }
    'logs' {
        Invoke-Compose -Prefix $fullPrefixArgs -Extra @('logs', '-f', '--tail=200')
    }
    'ps' {
        Invoke-Compose -Prefix $fullPrefixArgs -Extra @('ps')
    }
    'restart' {
        Invoke-Compose -Prefix $fullPrefixArgs -Extra @('down')
        Invoke-Compose -Prefix $bootPrefixArgs -Extra @('up', '-d')
    }
    'profiles' {
        $profiles | ForEach-Object { Write-Output $_ }
    }
    'help' {
        @'
platform/scripts/up.ps1 — Compose lifecycle wrapper (requirement 2.8, R2)

Commands:
  up             Default. Bootstrap-only: postgres, vault, admin-dashboard
                 (no --profile flags). Drive the rest from the Setup Wizard.
  up -All        Bring up base + dev override with EVERY manifest profile.
                 Use for CI or debugging when you need the full stack.
  up-all         Alias for `up -All`.
  boot           Explicit alias for the default `up` (bootstrap-only).
  down           Stop and remove containers (volumes preserved).
  logs           Tail logs from every running service (Ctrl-C to exit).
  ps             Show the state of running Compose services.
  restart        Equivalent to `down` followed by `boot`.
  profiles       Print the profile list derived from config/services.manifest.json.
'@ | Write-Output
    }
}
