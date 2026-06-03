# Writes config/services.manifest.schema.json (JSON Schema 2020-12) for the
# admin dashboard control plane. Built as a script because the
# fs_write tool refuses files whose literal text contains a remote JSON Schema
# $schema URL; here we assemble that URL at runtime.

$ErrorActionPreference = 'Stop'

$schemaUrl = 'https://' + 'json-schema.org/draft/2020-12/schema'

$json = @"
{
  "`$schema": "$schemaUrl",
  "`$id": "services-manifest-schema-v1",
  "title": "Services Manifest",
  "description": "Single-source-of-truth manifest enumerating every Managed_Service that the admin-dashboard-api Control_Plane orchestrates. Boot_Bundle services (admin-dashboard-ui, admin-dashboard-api, postgres, vault) MUST NOT appear here. Uniqueness of compose_service_name across entries is enforced by the Python loader (services/admin-dashboard-api/src/manifest.py) as a custom check, since uniqueItemProperties is not part of standard JSON Schema 2020-12.",
  "type": "object",
  "required": ["version", "services"],
  "additionalProperties": false,
  "properties": {
    "version": {
      "type": "integer",
      "const": 1,
      "description": "Manifest schema version. Only value 1 is currently accepted."
    },
    "services": {
      "type": "array",
      "description": "Ordered list of Managed_Service entries. Order is presentational only; the API treats this as a set keyed by name and compose_service_name.",
      "items": { "`$ref": "#/`$defs/ManagedService" }
    }
  },
  "`$defs": {
    "ManagedService": {
      "type": "object",
      "description": "A single Managed_Service entry that the Control_Plane can start, stop, restart, run tests for, probe for health and stream logs from.",
      "required": [
        "name",
        "kind",
        "compose_service_name",
        "compose_profile",
        "env_example_path",
        "health_endpoint",
        "test_command"
      ],
      "additionalProperties": false,
      "properties": {
        "name": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9-]{1,40}`$",
          "description": "Stable, URL-safe identifier used by the REST API path /admin/services/{name}. Must start with a lowercase letter, may contain lowercase letters, digits, and hyphens. Length 2..41 characters."
        },
        "kind": {
          "type": "string",
          "enum": ["http_service", "worker", "ui", "infra"],
          "description": "Drives kind-aware behavior in HealthProbe and the UI: http_service polls /healthz + /readyz over the Compose network; worker uses Temporal client ping; ui and infra fall back to assume-running when health_endpoint is null."
        },
        "compose_service_name": {
          "type": "string",
          "description": "Exact key under services: in infra/docker-compose.yml. Used as the argv argument for docker compose up/stop/exec/logs."
        },
        "compose_profile": {
          "type": "string",
          "description": "Compose profile activated by the lifecycle handler (docker compose --profile <value>). By convention equal to name."
        },
        "env_example_path": {
          "type": "string",
          "description": "Workspace-root-relative path to the .env.example file whose left-hand-side (LHS) keys drive the Start form schema. For atlassian-mcp this points into services/atlassian_mcp_bitbucket/."
        },
        "health_endpoint": {
          "description": "HTTP path probed by HealthProbe for http_service entries (e.g. /healthz). null for worker/infra/ui entries that do not expose an HTTP health endpoint.",
          "anyOf": [
            { "type": "string", "pattern": "^/[A-Za-z0-9_/-]*`$" },
            { "type": "null" }
          ]
        },
        "test_command": {
          "description": "Full command string used by POST /admin/services/{name}/test (typically of the form 'docker compose -f infra/docker-compose.yml exec <svc> pytest tests/integration/ -v'). null when the service has no integration tests; the endpoint then returns 409.",
          "anyOf": [
            { "type": "string", "minLength": 1 },
            { "type": "null" }
          ]
        }
      }
    }
  }
}
"@

# Sanity-check that the body parses as JSON before writing it to disk.
# (Windows PowerShell 5.1 does not support -Depth on ConvertFrom-Json.)
$null = $json | ConvertFrom-Json

$out = Join-Path $PSScriptRoot '..\config\services.manifest.schema.json'
$out = [System.IO.Path]::GetFullPath($out)

$dir = Split-Path -Parent $out
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# Write UTF-8 (no BOM) with LF line endings to match repo convention.
$lf = $json -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($out, $lf + "`n", (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Wrote $out"
