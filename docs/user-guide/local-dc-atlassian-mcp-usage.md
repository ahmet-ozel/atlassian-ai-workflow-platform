# Local Data Center Atlassian MCP Kullanım Rehberi

Bu rehber, local ortamda `atlassian-mcp` ayağa kalktıktan sonra Jira,
Confluence ve Bitbucket Data Center servislerine MCP üzerinden nasıl istek
atılacağını anlatır.

Gateway stateless çalışır. Atlassian credential'ları MCP oturumunda tutulmaz.
Her `tools/call` isteği, hedef servis URL'i ve PAT bilgisini HTTP header olarak
taşır.

## Kısa Özet

MCP endpoint:

```text
http://localhost:38090/mcp
```

Data Center auth modeli:

```text
X-Atlassian-<Service>-Url
X-Atlassian-<Service>-Personal-Token
```

JSON-RPC method:

```text
tools/list
tools/call
```

DC için Basic auth header'ları gönderilmez. `Username`, `Api-Token`,
`App-Password` header'ları Cloud içindir.

## Endpointler

Host makineden:

```text
http://localhost:38090/mcp
http://localhost:38090/healthz
```

Compose network içindeki servislerden:

```text
http://atlassian-mcp:8090/mcp
http://atlassian-mcp:8090/healthz
```

Health kontrolü:

```bash
curl http://localhost:38090/healthz
```

## Local DC URL'i Nasıl Seçilir?

`X-Atlassian-*-Url` değeri, MCP container'ının erişebildiği URL olmalıdır.

Atlassian DC host makinede çalışıyorsa:

```text
http://host.docker.internal:8080
```

Aynı Docker network içinde çalışıyorsa:

```text
http://jira:8080
http://confluence:8090
http://bitbucket:7990
```

Internal DNS ile erişiliyorsa:

```text
https://jira.internal
https://confluence.internal
https://bitbucket.internal
```

MCP container içinden `localhost`, host makine değil container'ın kendisidir.
Bu yüzden Atlassian DC host makinedeyse `localhost` yerine genelde
`host.docker.internal` kullanılmalıdır.

## Internal Host Allowlist

Self-hosted DC URL'i private IP'ye çözülüyorsa gateway SSRF koruması isteği
bloklayabilir.

Tipik hata:

```text
Forbidden: Invalid <service> URL - DNS for <host> resolves to non-global IP
```

Bu durumda `platform/infra/.env` içinde domain allowlist ayarlanır:

```env
MCP_ALLOWED_URL_DOMAINS=jira.internal,confluence.internal,bitbucket.internal,host.docker.internal
```

Sadece domain yazılır. Şema, port ve path yazılmaz.

Doğru:

```text
jira.internal
```

Yanlış:

```text
https://jira.internal:8443
```

Değişiklikten sonra `atlassian-mcp` servisi yeniden oluşturulmalıdır.

## Genel HTTP Header'ları

Her MCP isteğinde şu header'lar bulunmalıdır:

```http
Content-Type: application/json
Accept: application/json, text/event-stream
X-Client-Source: local-probe
```

`X-Client-Source` zorunludur. Trafik, audit ve dashboard ayrımı için kullanılır.
Örnek değerler:

```text
local-probe
my-local-service
ide:local
worker:payment
```

Opsiyonel:

```http
X-Trace-Id: <trace-id>
```

## Data Center Credential Header'ları

Jira DC:

```http
X-Atlassian-Jira-Url: https://jira.internal
X-Atlassian-Jira-Personal-Token: <jira-pat>
```

Confluence DC:

```http
X-Atlassian-Confluence-Url: https://confluence.internal
X-Atlassian-Confluence-Personal-Token: <confluence-pat>
```

Bitbucket DC:

```http
X-Atlassian-Bitbucket-Url: https://bitbucket.internal
X-Atlassian-Bitbucket-Personal-Token: <bitbucket-pat>
```

Gateway bu PAT'i hedef Atlassian DC servisine şu şekilde iletir:

```http
Authorization: Bearer <pat>
```

DC çağrılarında bunları göndermeyin:

```http
X-Atlassian-Jira-Username
X-Atlassian-Jira-Api-Token
X-Atlassian-Confluence-Username
X-Atlassian-Confluence-Api-Token
X-Atlassian-Bitbucket-Username
X-Atlassian-Bitbucket-App-Password
X-Atlassian-Bitbucket-Api-Token
```

## PAT'i Önce Doğrudan Test Edin

MCP'ye geçmeden önce PAT'in hedef DC servisinde çalıştığını doğrulayın.

Jira:

```bash
curl -H "Authorization: Bearer <jira-pat>" \
  -H "Accept: application/json" \
  "https://jira.internal/rest/api/2/myself"
```

Confluence:

```bash
curl -H "Authorization: Bearer <confluence-pat>" \
  -H "Accept: application/json" \
  "https://confluence.internal/rest/api/user/current"
```

Bitbucket:

```bash
curl -H "Authorization: Bearer <bitbucket-pat>" \
  -H "Accept: application/json" \
  "https://bitbucket.internal/rest/api/1.0/projects?limit=1"
```

Bu istekler 200 dönmüyorsa MCP üzerinden yapılan istek de başarılı olmaz.

## JSON-RPC Gövdesi

Tool listeleme:

```json
{
  "jsonrpc": "2.0",
  "id": "tools",
  "method": "tools/list",
  "params": {}
}
```

Tool çağırma:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "tools/call",
  "params": {
    "name": "jira_get_issue",
    "arguments": {
      "issue_key": "ABC-123"
    }
  }
}
```

Alanlar:

| Alan | Açıklama |
|---|---|
| `jsonrpc` | Her zaman `2.0` |
| `id` | İstek id'si. Log takibi için anlamlı verilebilir. |
| `method` | `tools/list` veya `tools/call` |
| `params.name` | Çağrılacak MCP tool adı |
| `params.arguments` | Tool parametreleri |

Tool adı ve parametre şeması için önce `tools/list` çağırın.

## Response Formatı

Gateway cevapları direkt JSON veya SSE dönebilir. Direkt JSON'da response body
JSON-RPC envelope'dur. SSE'de aynı envelope `data:` satırının içinde gelir.

```text
data: {"jsonrpc":"2.0","id":"req-1","result":{"content":[...]}}
```

Client iki formatı da desteklemelidir. Şunların ikisi de hata kabul edilir:

```json
{"error":{"message":"Unknown tool"}}
```

```json
{"result":{"isError":true,"content":[...]}}
```

## Tool Listeleme İsteği

```bash
curl -X POST http://localhost:38090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Client-Source: local-probe" \
  -d '{"jsonrpc":"2.0","id":"tools","method":"tools/list","params":{}}'
```

`tools/list` boş dönüyorsa `ATLASSIAN_OAUTH_ENABLE=true` olduğundan emin olun.
Bu flag sadece tool keşfini etkiler. Gerçek auth, her `tools/call` isteğinde
gönderilen `X-Atlassian-*` header'larından gelir.

## Jira DC Örnekleri

Tam curl örneği:

```bash
curl -X POST http://localhost:38090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Client-Source: local-probe" \
  -H "X-Atlassian-Jira-Url: https://jira.internal" \
  -H "X-Atlassian-Jira-Personal-Token: <jira-pat>" \
  -d '{"jsonrpc":"2.0","id":"jira-get","method":"tools/call","params":{"name":"jira_get_issue","arguments":{"issue_key":"ABC-123"}}}'
```

Yaygın Jira tool payloadları:

| Tool | `arguments` |
|---|---|
| `jira_get_issue` | `{"issue_key":"ABC-123"}` |
| `jira_search` | `{"jql":"project = ABC ORDER BY updated DESC","limit":10}` |
| `jira_create_issue` | `{"project_key":"ABC","summary":"Local MCP test issue","description":"...","issue_type":"Task"}` |
| `jira_add_comment` | `{"issue_key":"ABC-123","body":"Local MCP comment test."}` |
| `jira_transition_issue` | `{"issue_key":"ABC-123","transition_id":"31"}` |

`transition_id` workflow'a göre değişir. Önce ilgili transition tool'unu
`tools/list` ile bulun.

## Confluence DC Örnekleri

Tam curl örneği:

```bash
curl -X POST http://localhost:38090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Client-Source: local-probe" \
  -H "X-Atlassian-Confluence-Url: https://confluence.internal" \
  -H "X-Atlassian-Confluence-Personal-Token: <confluence-pat>" \
  -d '{"jsonrpc":"2.0","id":"conf-search","method":"tools/call","params":{"name":"confluence_search","arguments":{"query":"release notes","limit":10}}}'
```

Yaygın Confluence tool payloadları:

| Tool | `arguments` |
|---|---|
| `confluence_search` | `{"query":"release notes","limit":10}` |
| `confluence_cql_search` | `{"cql":"space = \"ABC\" and type = page order by lastmodified desc","limit":10}` |
| `confluence_create_page` | `{"space_key":"ABC","title":"Local MCP Test Page","content":"..."}` |

## Bitbucket DC Örnekleri

Tam curl örneği:

```bash
curl -X POST http://localhost:38090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Client-Source: local-probe" \
  -H "X-Atlassian-Bitbucket-Url: https://bitbucket.internal" \
  -H "X-Atlassian-Bitbucket-Personal-Token: <bitbucket-pat>" \
  -d '{"jsonrpc":"2.0","id":"bb-repo","method":"tools/call","params":{"name":"bitbucket_get_repository","arguments":{"project_key":"ABC","repo_slug":"my-repo"}}}'
```

Yaygın Bitbucket tool payloadları:

| Tool | `arguments` |
|---|---|
| `bitbucket_get_repository` | `{"project_key":"ABC","repo_slug":"my-repo"}` |
| `bitbucket_list_branches` | `{"project_key":"ABC","repo_slug":"my-repo","limit":25}` |
| `bitbucket_create_branch` | `{"project_key":"ABC","repo_slug":"my-repo","name":"feature/x","target":"main"}` |
| `bitbucket_create_pull_request` | `{"project_key":"ABC","repo_slug":"my-repo","title":"Local MCP PR test","source_branch":"feature/x","destination_branch":"main"}` |

Bazı gateway/tool versiyonları `project_key`, bazıları `workspace` parametresi
bekleyebilir. DC için genelde `project_key` kullanılır. Kesin bilgi için
`tools/list` çıktısındaki `inputSchema` alanına bakın.

## Tek İstekte Birden Fazla Servis Credential'ı

Bir tool yalnızca kendi servis credential'ını gerektirir. Örneğin
`jira_get_issue` için Jira header'ları yeterlidir.

IDE gibi genel client'larda üç servis credential'ı birlikte verilebilir:

```http
X-Client-Source: ide:local
X-Atlassian-Jira-Url: https://jira.internal
X-Atlassian-Jira-Personal-Token: <jira-pat>
X-Atlassian-Confluence-Url: https://confluence.internal
X-Atlassian-Confluence-Personal-Token: <confluence-pat>
X-Atlassian-Bitbucket-Url: https://bitbucket.internal
X-Atlassian-Bitbucket-Personal-Token: <bitbucket-pat>
```

Gateway, çağrılan tool adına göre ilgili servisin client'ını kullanır.

## Servis İçinden Çağırma

Platform içindeki Python servisleri doğrudan header üretmek yerine ortak helper
kullanmalıdır. Helper `X-Client-Source` ve trace bilgisini korur, credential'ı
sadece ilgili request süresince ekler.

```python
from http_shared import make_mcp_client, with_atlassian_creds

client = make_mcp_client(
    client_source="my-service",
    base_url="http://atlassian-mcp:8090",
    headers={"Accept": "application/json, text/event-stream"},
)

body = {
    "jsonrpc": "2.0",
    "id": "jira-get",
    "method": "tools/call",
    "params": {
        "name": "jira_get_issue",
        "arguments": {"issue_key": "ABC-123"},
    },
}

async with client:
    async with with_atlassian_creds(
        client,
        dept_id="payment",
        service="jira",
        credential_resolver=credential_resolver,
    ) as authed:
        response = await authed.post("/mcp", json=body)
```

## IDE Config Örneği

```json
{
  "servers": {
    "atlassian-local-dc": {
      "type": "http",
      "url": "http://localhost:38090/mcp",
      "headers": {
        "X-Client-Source": "ide:local",
        "X-Atlassian-Jira-Url": "https://jira.internal",
        "X-Atlassian-Jira-Personal-Token": "<jira-pat>",
        "X-Atlassian-Confluence-Url": "https://confluence.internal",
        "X-Atlassian-Confluence-Personal-Token": "<confluence-pat>",
        "X-Atlassian-Bitbucket-Url": "https://bitbucket.internal",
        "X-Atlassian-Bitbucket-Personal-Token": "<bitbucket-pat>"
      }
    }
  }
}
```

Config değişince IDE/client bağlantısını yeniden başlatın. Bazı client'lar eski
header'ları cache'leyebilir.

## Sık Hatalar

| Hata | Sebep | Çözüm |
|---|---|---|
| `400 missing_client_source` | `X-Client-Source` yok | Her request'e ekleyin |
| `401` / `403` | PAT geçersiz veya yetkisiz | PAT'i doğrudan REST endpoint'inde test edin |
| `Cloud OAuth authentication requires a valid cloud_id` | DC için Cloud header'ı gönderildi | `*-Username` / `*-Api-Token` yerine `*-Personal-Token` gönderin |
| `Unknown tool` | Tool adı farklı | `tools/list` ile gerçek adı bulun |
| `DNS ... non-global IP` | Internal host SSRF guard'a takıldı | `MCP_ALLOWED_URL_DOMAINS` ayarlayın |
| `Connection refused` | MCP çalışmıyor veya port farklı | `/healthz` kontrol edin |
| `Unknown Host` | URL container içinden çözülemiyor | Servis adı, internal DNS veya `host.docker.internal` kullanın |
| `certificate verify failed` | Self-signed TLS | Sertifikayı container'a güvenilir yapın veya local lab'de HTTP kullanın |
| JSON parse hatası | Yanıt SSE döndü | `data:` satırındaki JSON'u parse edin |

## Güvenlik Kuralları

- Gerçek PAT değerlerini commit, log, screenshot veya ticket içine koymayın.
- `Authorization`, `*-Token`, `*-Api-Token`, `*-App-Password` değerlerini
  loglamayın.
- `X-Client-Source` değerini anlamlı verin.
- DC için Basic auth yerine PAT kullanın.
- Write tool'larını önce test proje, space veya repo üzerinde deneyin.

## Kontrol Listesi

1. `curl http://localhost:38090/healthz` 200 dönüyor.
2. Atlassian DC URL'i MCP container'ından erişilebilir.
3. Internal domain gerekiyorsa `MCP_ALLOWED_URL_DOMAINS` ayarlı.
4. PAT doğrudan Atlassian DC REST endpoint'inde 200 dönüyor.
5. Her MCP isteğinde `Content-Type`, `Accept`, `X-Client-Source` var.
6. DC auth için sadece `X-Atlassian-*-Url` ve `X-Atlassian-*-Personal-Token`
   gönderiliyor.
7. Önce `tools/list`, sonra gerçek tool adıyla `tools/call` yapılıyor.
8. Client hem JSON hem SSE response formatını parse ediyor.
