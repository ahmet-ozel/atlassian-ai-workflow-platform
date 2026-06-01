# MCP Credential Headers — Canonical Contract

> **Spec:** `platform-quick-fixes` G2 — single source of truth for the per-request
> HTTP headers that callers (`automation-service`, `agent-runner-worker`,
> `assistant-service`, `streamlit-app`, IDE proxies, future services) MUST set
> when calling the `atlassian_unified` MCP service.
>
> **Why this exists:** The MCP service is **stateless** (Requirement: design
> §"MCP stateless"). Every outbound call carries credentials in HTTP headers
> rather than in a server-side session. Without a single canonical doc, header
> names drifted across the codebase (`X-Atlassian-Jira-Url`,
> `X-Atlassian-Bitbucket-Url`, `X-Atlassian-Cloud-Id`, etc.) and new clients
> had no clear contract to follow. This document is the contract.
>
> **Parity invariant:** `platform/tests/property/test_mcp_credential_headers_doc.py`
> asserts that every header name listed below maps to a sabit constant in
> `services/atlassian_unified/src/mcp_atlassian/utils/environment.py` (Bitbucket)
> and `services/atlassian_unified/src/mcp_atlassian/servers/dependencies.py`
> (Jira, Confluence). Drift fails the property test.

---

## 1. Generic / cross-service headers

| Header | Required? | Purpose | Example |
|---|---|---|---|
| `X-Client-Source` | **Required** | Identifies the calling component for audit + traffic dashboard filtering. Format: `<component>[:<sub-context>]`. The MCP refuses to route a call without this header (HTTP 400 `missing_client_source`). | `agent-runner-worker`, `streamlit-ui:user@payment`, `automation-service:webhook-jira` |
| `Authorization` | Optional (OAuth only) | Bearer token for Atlassian Cloud OAuth 2.0 (3LO) flow. Mutually exclusive with the per-service token headers. | `Bearer eyJhbGciOi...` |
| `X-Atlassian-Cloud-Id` | Required when OAuth + Cloud | Atlassian Cloud tenant cloud-id. Resolved automatically when MCP is configured globally; must be set per-request when each call targets a different tenant. | `f9d2a8e1-...` |

> **Note on `X-Client-Source` enforcement:** The header is enforced at the MCP
> layer (rejection on miss). At the **client library** layer, `mcp_client.AtlassianClient.__init__`
> takes `client_source: str` as a **required** constructor argument (G6 — see
> `requirements.md`); this is the second line of defence so a mis-wired worker
> cannot reach the network at all without identifying itself.

---

## 2. Jira headers

| Header | Required? | Purpose | Example |
|---|---|---|---|
| `X-Atlassian-Jira-Url` | **Required** when per-request auth is used | Base URL of the target Jira instance. Cloud or Data Center accepted. | `https://example.atlassian.net` (Cloud) or `https://jira.internal/` (DC) |
| `X-Atlassian-Jira-Personal-Token` | **Required** for DC | Personal Access Token (PAT) for Data Center. Treated as a Bearer token. | `MTIzNDU2Nzg5MDEy...` |
| `X-Atlassian-Jira-Username` | Cloud Basic Auth only | Atlassian Cloud account email. Combined with the API token to build `Authorization: Basic <base64>`. | `bot-payment@example.com` |
| `X-Atlassian-Jira-Api-Token` | Cloud Basic Auth only | Atlassian Cloud API token. Combined with username. | `ATATT3xFfGF0...` |

> **Auth resolution order (Jira):**
>
> 1. `Authorization: Bearer ...` header → OAuth 2.0 (Cloud only); requires `X-Atlassian-Cloud-Id`.
> 2. `X-Atlassian-Jira-Personal-Token` → DC PAT (applied against `X-Atlassian-Jira-Url`).
> 3. `X-Atlassian-Jira-Username` + `X-Atlassian-Jira-Api-Token` → Cloud Basic Auth.
> 4. None of the above → fall back to MCP's globally configured credentials (boot-time env).

---

## 3. Confluence headers

| Header | Required? | Purpose | Example |
|---|---|---|---|
| `X-Atlassian-Confluence-Url` | **Required** when per-request auth is used | Base URL of the target Confluence instance. | `https://example.atlassian.net/wiki` (Cloud) or `https://confluence.internal/` (DC) |
| `X-Atlassian-Confluence-Personal-Token` | **Required** for DC | Personal Access Token. | `MTIzNDU2Nzg5MDEy...` |
| `X-Atlassian-Confluence-Username` | Cloud Basic Auth only | Account email. | `bot-payment@example.com` |
| `X-Atlassian-Confluence-Api-Token` | Cloud Basic Auth only | API token. | `ATATT3xFfGF0...` |

Auth resolution order matches Jira (see above).

---

## 4. Bitbucket headers

Bitbucket has **two deployment variants** (Cloud and Data Center) with
**different authentication methods**. The MCP detects which variant from the
URL header and applies the matching auth. See `bitbucket-cloud-dc-parity`
spec §"Authentication truth table" for the full matrix.

| Header | Required? | Variant | Purpose | Example |
|---|---|---|---|---|
| `X-Atlassian-Bitbucket-Url` | **Required** for per-request auth | Both | Base URL. URL host classification decides which auth headers are honoured. | `https://api.bitbucket.org` (Cloud) or `https://bitbucket.internal/` (DC) |
| `X-Atlassian-Bitbucket-Personal-Token` | DC | DC | Personal Access Token, applied as Bearer. **Discarded** if the URL resolves to a Cloud host. | `Mzc4NjU0...` |
| `X-Atlassian-Bitbucket-Cloud-Access-Token` | Cloud | Cloud | OAuth 2.0 access token (Cloud Workspace App / 3LO). Mutually exclusive with App Password. | `ATCTT3x...` |
| `X-Atlassian-Bitbucket-Username` | Cloud + App Password only | Cloud | Account username (NOT email). Combined with App Password for Basic Auth. | `bot-payment` |
| `X-Atlassian-Bitbucket-App-Password` | Cloud + App Password only | Cloud | App Password (legacy auth). Combined with username for Basic Auth. | `ATBBxxxx...` |

> **Truth table (rows referenced from `bitbucket-cloud-dc-parity` design):**
>
> | Row | URL | Headers set | Result |
> |---|---|---|---|
> | A | DC URL | `Bitbucket-Personal-Token` | DC PAT auth |
> | B | Cloud URL | `Bitbucket-Cloud-Access-Token` | OAuth Bearer |
> | C | Cloud URL | `Bitbucket-Username` + `Bitbucket-App-Password` | Cloud Basic |
> | D | Cloud URL | (none Cloud-shaped) | 401 `unauthorized` |
> | K | (no URL) | `Bitbucket-Personal-Token` | DC PAT against globally configured URL |

---

## 5. Caller-side helper

Use `mcp_client.AtlassianClient` (in `libs/mcp_client/`) — **never** assemble
these headers by hand. The client library enforces the contract and the
`X-Client-Source` requirement:

```python
from mcp_client import AtlassianClient

client = AtlassianClient(
    mcp_base_url="http://atlassian-mcp:8090",
    client_source="agent-runner-worker",   # required (G6)
)

# When the call needs per-request credentials, pass them through the
# `creds` argument; the helper translates them to the canonical headers.
result = await client.jira_get_issue(
    "PAY-4211",
    creds={
        "url": "https://example.atlassian.net",
        "username": "bot-payment@example.com",
        "api_token": "ATATT3xFfGF0...",
    },
)
```

---

## 6. Constants reference

The header strings above are mirrored as Python constants in:

- `services/atlassian_unified/src/mcp_atlassian/utils/environment.py`
  → `BITBUCKET_URL_HEADER`, `BITBUCKET_DC_PAT_HEADER`,
    `BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER`,
    `BITBUCKET_CLOUD_APP_PASSWORD_HEADER`,
    `BITBUCKET_CLOUD_USERNAME_HEADER`.
- `services/atlassian_unified/src/mcp_atlassian/servers/dependencies.py`
  → Jira `url_header="X-Atlassian-Jira-Url"`,
    `token_header="X-Atlassian-Jira-Personal-Token"`,
    Confluence equivalents.

When adding a new header, **edit this document first**, then mirror the
constant in the matching `*.py` file. The property test
(`test_mcp_credential_headers_doc.py`) cross-checks the two surfaces and
fails the build on drift.

---

## 7. Security & redaction

- **Never log** raw header values for `*-Token`, `*-Api-Token`,
  `*-App-Password`, `Authorization`, `*-Cloud-Access-Token`. The
  redaction filter at `services/atlassian_unified/src/mcp_atlassian/utils/logging.py::format_request_headers`
  already masks these; new headers MUST be added to its
  `sensitive_headers` set.
- **Never persist** credential headers to disk, audit log, or trace
  exporter. Audit rows reference credentials by `vault:<path>`, not by
  value.
- **Vault path convention:** `vault:atlassian/<dept_id>/<service>` —
  resolved by `automation-service.credentials.CredentialResolver` and
  passed in-memory to the MCP via the headers above.
