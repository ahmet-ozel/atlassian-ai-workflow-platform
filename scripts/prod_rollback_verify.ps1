param(
    [string]$Service = "admin-dashboard-api",
    [string]$HealthUrl = "http://localhost:8082/healthz",
    [int]$TimeoutSec = 90,
    [string[]]$ComposeFiles = @("infra/docker-compose.yml")
)

$ErrorActionPreference = "Stop"

function Invoke-Compose {
    param([string[]]$ArgsList)

    $args = @()
    foreach ($file in $ComposeFiles) {
        $args += @("-f", $file)
    }
    $args += $ArgsList
    & docker compose @args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($ArgsList -join ' ')"
    }
}

function Wait-Health {
    param([string]$Url, [int]$Seconds)

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    } while ((Get-Date) -lt $deadline)

    throw "health check failed after ${Seconds}s: $Url"
}

$containerId = (& docker compose -f $ComposeFiles[0] ps -q $Service).Trim()
if (-not $containerId) {
    throw "service is not running: $Service"
}

$imageId = (& docker inspect --format '{{.Image}}' $containerId).Trim()
if (-not $imageId) {
    throw "could not resolve image id for $Service"
}

$safeService = $Service -replace '[^a-zA-Z0-9_.-]', '-'
$rollbackImage = "platform-rollback-gate/${safeService}:previous"
& docker tag $imageId $rollbackImage
if ($LASTEXITCODE -ne 0) {
    throw "failed to tag rollback image"
}

$overridePath = Join-Path $env:TEMP "rollback-$safeService-$([guid]::NewGuid()).yml"
@"
services:
  ${Service}:
    image: $rollbackImage
"@ | Set-Content -LiteralPath $overridePath -Encoding UTF8

try {
    $ComposeFiles = $ComposeFiles + $overridePath
    Invoke-Compose @("up", "-d", "--no-build", "--no-deps", "--force-recreate", $Service)
    Wait-Health -Url $HealthUrl -Seconds $TimeoutSec
    Write-Output "ROLLBACK_SMOKE_OK service=$Service image=$rollbackImage"
} finally {
    $ComposeFiles = $ComposeFiles | Where-Object { $_ -ne $overridePath }
    if (Test-Path -LiteralPath $overridePath) {
        Remove-Item -LiteralPath $overridePath -Force
    }
    Invoke-Compose @("up", "-d", "--no-build", "--no-deps", "--force-recreate", $Service)
    Wait-Health -Url $HealthUrl -Seconds $TimeoutSec
}
