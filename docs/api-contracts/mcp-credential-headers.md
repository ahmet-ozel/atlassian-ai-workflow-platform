# MCP Credential Headers - Canonical Contract

> HTTP headers that callers (`automation-service`, `agent-runner-worker`,
> `assistant-service`, `streamlit-app`, IDE proxies, future services) MUST set
> when calling the `atlassian_mcp_bitbucket` MCP service.
>
> **Why this exists:** The MCP service is **stateless**. Every outbound call carries credentials in HTTP headers
> rather than in a server-side session. Without a single canonical doc, header
> names drifted across the codebase (`X-Atlassian-Jira-Url`,
> `X-Atlassian-Bitbucket-Url`, `X-Atlassian-Cloud-Id`, etc.) and new clients
> had no clear contract to follow. This document is the contract.
>
> **Parity invariant:** `platform/tests/property/test_mcp_credential_headers_doc.py`
> asserts that every header name listed below maps to a sabit constant in
> `services/atlassian_mcp_bitbucket/src/mcp_atlassian/utils/environment.py` (Bitbucket)
> and `services/atlassian_mcp_bitbucket/src/mcp_atlassian/servers/dependencies.py`
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
> takes `client_source: str` as a **required** constructor argument; this is the
> second line of defence so a mis-wired worker
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
> 1. `Authorization: Bearer ...` header  OAuth 2.0 (Cloud only); requires `X-Atlassian-Cloud-Id`.
> 2. `X-Atlassian-Jira-Personal-Token`  DC PAT (applied against `X-Atlassian-Jira-Url`).
> 3. `X-Atlassian-Jira-Username` + `X-Atlassian-Jira-Api-Token`  Cloud Basic Auth.
> 4. None of the above  fall back to MCP's globally configured credentials (boot-time env).

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

### Cloud auth quick probe

For Bitbucket Cloud API token or app-password auth, first prove the secret
against Bitbucket's user endpoint. Do not paste the real token into docs,
tickets, logs, or screenshots:

```powershell
$env:BITBUCKET_USERNAME = "your.email@company.com"
$env:BITBUCKET_API_TOKEN = "<bitbucket-api-token-or-app-password>"
curl.exe -u "$($env:BITBUCKET_USERNAME):$($env:BITBUCKET_API_TOKEN)" "https://api.bitbucket.org/2.0/user"
```

Expected result: a user JSON body containing fields such as `username`,
`display_name`, and `account_status`. This verifies Cloud auth only; repository,
pull-request, pipeline, and webhook calls still require the matching workspace,
repository permission, and token scopes.

---

## 4b. Connecting an IDE (VS Code / Cursor / JetBrains) to the MCP

IDE MCP clients connect over HTTP to the gateway and send the same per-request
headers as any other caller. A minimal working config:

```jsonc
{
  "servers": {
    "atlassian": {
      "type": "http",
      "url": "http://<mcp-host>:38090/mcp",
      "headers": {
        "X-Client-Source": "ide:<your-name>",

        // Server / Data Center  Personal Access Token (sent as Bearer)
        "X-Atlassian-Jira-Url": "https://jira.internal",
        "X-Atlassian-Jira-Personal-Token": "<jira-PAT>",

        "X-Atlassian-Confluence-Url": "https://wiki.internal",
        "X-Atlassian-Confluence-Personal-Token": "<confluence-PAT>",

        "X-Atlassian-Bitbucket-Url": "https://bitbucket.internal",
        "X-Atlassian-Bitbucket-Personal-Token": "<bitbucket-PAT>"
      }
    }
  }
}
```

For **Atlassian Cloud**, swap each `*-Personal-Token` for the Cloud Basic Auth
pair (`*-Username` = account email + `*-Api-Token` = API token), or use
`Authorization: Bearer` + `X-Atlassian-Cloud-Id` for OAuth. See §2-§4 for the
full per-service header sets and the auth resolution order.

**Rules that avoid the common failures:**

- **Server/DC uses Bearer, not Basic.** Send only `X-Atlassian-<Service>-Url`
  and `X-Atlassian-<Service>-Personal-Token`. The gateway applies the PAT as
  `Authorization: Bearer <token>`. **Do not** also send `*-Username` /
  `*-Api-Token` for a DC host - the gateway has no `*-Api-Token` PAT branch, so
  with no recognised credential it falls through to the Cloud OAuth path and
  fails with `Cloud OAuth authentication requires a valid cloud_id`.
- **Why Bearer matters for DC:** Basic auth can hit Jira/Confluence Seraph
  `CAPTCHA_CHALLENGE` after failed logins (HTTP 403,
  `X-Authentication-Denied-Reason: CAPTCHA_CHALLENGE`). Bearer/PAT is not
  subject to the CAPTCHA gate, so a PAT keeps working without unlocking the
  account.
- **Use the real host name, not `localhost`.** Point `url` at the host curl can
  reach (e.g. `http://<mcp-host>:38090/mcp`). Behind a corporate proxy,
  `localhost` may be routed to the proxy and return `Unknown Host`.
- **Reconnect after editing the config** so the client drops cached headers.

**Verify the token independently of the IDE** (DC PAT, bypasses CAPTCHA/Basic):

```bash
curl -H "Authorization: Bearer <jira-PAT>" -H "Accept: application/json" \
  "https://jira.internal/rest/api/2/myself"
```

200 + user JSON  the token is valid and the gateway will accept the same PAT.

**Quick gateway probe** (returns the real error behind a client-side
`-32001`/`32603` "tool failed" code):

```bash
curl -X POST http://<mcp-host>:38090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Client-Source: probe" \
  -H "X-Atlassian-Jira-Url: https://jira.internal" \
  -H "X-Atlassian-Jira-Personal-Token: <jira-PAT>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"jira_get_user_profile","arguments":{"user_identifier":"<user>"}}}'
```

**Self-hosted hosts and the SSRF guard:** if the gateway returns
`Forbidden: Invalid <service> URL - DNS for <host> resolves to non-global IP`,
the host resolves to a private IP and is blocked by the upstream SSRF guard. Add
its domain to `MCP_ALLOWED_URL_DOMAINS` in `infra/.env` and recreate the
`atlassian-mcp` service (see `docs/env-reference.md` §4).

---

## 4c. IDE tool discovery (`ATLASSIAN_OAUTH_ENABLE`)

If the IDE connects but shows **"Discovered 0 tools"**, the gateway is not
advertising its toolsets. In stateless mode `tools/list` only returns the Jira/
Confluence toolsets when `ATLASSIAN_OAUTH_ENABLE=true` is set on the
`atlassian-mcp` service (it is the default in Compose). This flag only enables
tool discovery; real auth still comes from the per-request `X-Atlassian-*`
headers above. Bitbucket tools are not listed by `tools/list` in this mode but
`tools/call` against them still works.

---

## 5. Caller-side helper

Use `mcp_client.AtlassianClient` (in `libs/mcp_client/`) - **never** assemble
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

- `services/atlassian_mcp_bitbucket/src/mcp_atlassian/utils/environment.py`
   `BITBUCKET_URL_HEADER`, `BITBUCKET_DC_PAT_HEADER`,
    `BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER`,
    `BITBUCKET_CLOUD_APP_PASSWORD_HEADER`,
    `BITBUCKET_CLOUD_USERNAME_HEADER`.
- `services/atlassian_mcp_bitbucket/src/mcp_atlassian/servers/dependencies.py`
   Jira `url_header="X-Atlassian-Jira-Url"`,
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
  redaction filter at `services/atlassian_mcp_bitbucket/src/mcp_atlassian/utils/logging.py::format_request_headers`
  already masks these; new headers MUST be added to its
  `sensitive_headers` set.
- **Never persist** credential headers to disk, audit log, or trace
  exporter. Audit rows reference credentials by `vault:<path>`, not by
  value.
- **Vault path convention:** `vault:atlassian/<dept_id>/<service>` -
  resolved by `automation-service.credentials.CredentialResolver` and
  passed in-memory to the MCP via the headers above.
