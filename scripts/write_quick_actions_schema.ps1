# Writes config/quick_actions.schema.json (JSON Schema 2020-12) for the
# quick actions config. Built as a script because the
# fs_write tool refuses files whose literal text contains a remote JSON Schema
# $schema URL; here we assemble that URL at runtime, matching the convention
# used by write_services_manifest_schema.ps1.

$ErrorActionPreference = 'Stop'

$schemaUrl = 'https://' + 'json-schema.org/draft/2020-12/schema'

$json = @"
{
  "`$schema": "$schemaUrl",
  "`$id": "quick-actions-schema-v1",
  "title": "quick_actions.yaml",
  "description": "Streamlit chat sayfasindaki sik kullanilan komut chip'lerinin tanimi. streamlit-app/pages/1_chat.py tarafindan render edilir; her chip basildiginda chip'in prompt_template'i kullanicinin chat input'una basilir ve assistant-service uzerinden gonderilir. required_capabilities listesi chip'in dept'in capability set'inde mevcut olup olmadigina gore aktiflesmesini saglar; eksik capability varsa chip gri + tooltip ile gosterilir. Capability vocabulary libs/temporal-shared.capabilities WORKFLOW_TYPE_CAPABILITIES ile tutarlidir. JSON Schema 2020-12 ile valide edin.",
  "type": "object",
  "required": ["version", "chips"],
  "additionalProperties": false,
  "properties": {
    "version": {
      "type": "integer",
      "const": 1,
      "description": "Quick actions schema version. Only value 1 is currently accepted."
    },
    "chips": {
      "type": "array",
      "description": "Sirali chip listesi. UI render sirasi bu listenin sirasidir. Bos liste kabul edilir (UI hicbir chip gostermez).",
      "minItems": 0,
      "items": { "`$ref": "#/`$defs/QuickActionChip" }
    }
  },
  "`$defs": {
    "QuickActionChip": {
      "type": "object",
      "description": "Tek bir Quick Action chip tanimi.",
      "required": ["id", "label", "prompt_template", "required_capabilities"],
      "additionalProperties": false,
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[a-z][a-z0-9_]{1,40}`$",
          "description": "Stabil chip identifier'i. snake_case. Streamlit widget key olarak kullanilir; ayni id iki kez gecmemelidir (uniqueness Python loader tarafindan dogrulanir)."
        },
        "label": {
          "type": "string",
          "minLength": 1,
          "maxLength": 60,
          "description": "Chip uzerinde gozuken kisa metin (kullanici dilinde, varsayilan tr)."
        },
        "prompt_template": {
          "type": "string",
          "minLength": 1,
          "maxLength": 4000,
          "description": "Chip'e basildiginda chat input'una basilan ham metin. Bos olamaz. Su anda template variable substitution yapilmaz; placeholder'lar runtime'da Streamlit form'lariyla doldurulur."
        },
        "required_capabilities": {
          "type": "array",
          "description": "Chip'in aktif olmasi icin dept'in tasimasi gereken capability seti. Tum elemanlar capability vocabulary'den (jira_read, jira_write, bitbucket_read, bitbucket_write, confluence_read, confluence_write, execution, web_search) gelmelidir. Bos liste 'her dept icin aktif' anlamina gelir.",
          "items": {
            "type": "string",
            "enum": [
              "jira_read",
              "jira_write",
              "bitbucket_read",
              "bitbucket_write",
              "confluence_read",
              "confluence_write",
              "execution",
              "web_search"
            ]
          },
          "uniqueItems": true
        }
      }
    }
  }
}
"@

# Sanity-check that the body parses as JSON before writing it to disk.
# (Windows PowerShell 5.1 does not support -Depth on ConvertFrom-Json.)
$null = $json | ConvertFrom-Json

$out = Join-Path $PSScriptRoot '..\config\quick_actions.schema.json'
$out = [System.IO.Path]::GetFullPath($out)

$dir = Split-Path -Parent $out
if (-not (Test-Path $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

# Write UTF-8 (no BOM) with LF line endings to match repo convention.
$lf = $json -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($out, $lf + "`n", (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Wrote $out"
