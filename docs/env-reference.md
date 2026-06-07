# Environment Variable Reference (Master Table)

> **Status:** Single source of truth for all Compose-stack environment variables.
> **Parity invariants:** `tests/property/test_env_coverage.py`, `tests/property/test_sensitive_key_parity.py`.

## Overview

Bu döküman, `platform/` Compose stack'indeki tüm servis, worker, sidecar ve UI bileşenlerinin tükettiği çevre değişkenlerinin **tek doğruluk kaynağı**dır ve aşağıdaki sütun şemasını kullanır:

| Sütun | Anlam |
|---|---|
| `name` | Çevre değişkeninin adı (UPPER_SNAKE_CASE). |
| `service(s)` | Değişkeni tüketen servis/worker/UI listesi. `*` tüm bileşenler anlamına gelir. |
| `required` | `true` ise servis o değişken olmadan başlatılamaz; `false` ise default değer ile devam eder. |
| `default` | Değişken set edilmediğinde uygulanacak değer. `-` set zorunluluğunu işaret eder; `_dev_only` placeholder dev ortamı içindir. |
| `description` | Değişkenin tek-cümlelik amacı. |
| `vault_path` | Secret tipindeki değişkenler için production'da değerin çözüldüğü Vault KV path'i (`vault:<...>`). Plain-text değerler ile dolu olamaz. |
| `feature_flag` | `true` ise değişken bir özellik anahtarıdır (default-off; opt-in açıldığında davranışı değiştirir). |

### Kategoriler (12)

1. [Postgres](#1-postgres)
2. [Vault](#2-vault)
3. [Temporal](#3-temporal)
4. [MCP / Firecrawl](#4-mcp--firecrawl)
5. [LLM (vLLM / OpenAI / Anthropic)](#5-llm-vllm--openai--anthropic)
6. [MinIO](#6-minio)
7. [OIDC](#7-oidc)
8. [Auth](#8-auth)
9. [Webhook secrets](#9-webhook-secrets)
10. [SSH runners](#10-ssh-runners)
11. [Feature flags](#11-feature-flags)
12. [Observability / Log](#12-observability--log)

> Toplam tanımlı değişken sayısı: **≥ 60**.

---

## 1. Postgres

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `POSTGRES_USER` | `postgres` (init), `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`, `task-intake-service` | true | `ai` | Postgres init kullanıcı adı; Compose `postgres` servisi başlangıç sırasında bu kullanıcıyı oluşturur. | - | false |
| `POSTGRES_PASSWORD` | `postgres` (init), `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`, `task-intake-service` | true | `ai_dev_only` | Postgres kullanıcısının parolası; production'da Vault'tan resolve edilir, plain-text disk'e düşmez. | `vault:infrastructure/postgres/password` | false |
| `POSTGRES_DB` | `postgres` (init), `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`, `task-intake-service` | true | `ai` | Compose'un init aşamasında oluşturduğu varsayılan veritabanı adı. | - | false |
| `POSTGRES_DSN` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`, `task-intake-service` | true | `postgresql://ai:ai_dev_only@postgres:5432/ai` | Tam Postgres bağlantı dizesi; uygulama katmanı `db-shared` üzerinden bu DSN ile RLS oturumlarını açar. | `vault:infrastructure/postgres/dsn` | false |
| `POSTGRES_HOST` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `postgres` | DSN'nin parçalanmış kullanımında host adı; Compose iç ağında servis adı ile çözülür. | - | false |
| `POSTGRES_PORT` | `postgres`, `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `5432` | Postgres TCP portu; host'a yayın yapılmazsa Compose iç ağ ile sınırlıdır. | - | false |
| `POSTGRES_RLS_ROLE_DEFAULT` | `automation-service`, `admin-dashboard-api` | false | `system` | RLS izolasyon helper'ının (`db-shared.with_dept_session`) `app.current_role` set'lemediği durumda kullandığı rol. | - | false |
| `POSTGRES_STATEMENT_TIMEOUT_MS` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `30000` | Tek bir SQL deyiminin maksimum çalışma süresi (milisaniye). | - | false |

## 2. Vault

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `VAULT_ADDR` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | true | `http://vault:8200` | Hashicorp Vault HTTP API kök URL'i; `VaultClient.read/write` çağrılarında temel adres olarak kullanılır. | - | false |
| `VAULT_TOKEN` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | true | `dev-token-not-for-prod` | Vault'a kimlik doğrulama tokeni; production'da kısa ömürlü AppRole token'ına döndürülür ve plain-text dosyaya yazılmaz. | `vault:infrastructure/vault/root_token` | false |
| `VAULT_BACKEND` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `hashicorp` | `VaultClient` factory'sinin seçtiği backend; `hashicorp` (prod) veya `local-dev` (sodium-libsecret şifreli dosya). | - | false |
| `VAULT_KV_MOUNT` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `secret` | Vault KV v2 mount path'i; `vault:<path>` referansları bu mount altında çözülür. | - | false |
| `VAULT_NAMESPACE` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | (boş) | Vault Enterprise namespace; OSS Vault için boş bırakılır. | - | false |
| `VAULT_REQUEST_TIMEOUT_S` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | `5` | Vault HTTP isteklerinin saniye cinsinden zaman aşımı. | - | false |

## 3. Temporal

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `TEMPORAL_HOST` | `automation-service`, `agent-runner-worker`, `execution-runner-worker`, `admin-dashboard-api`, `task-intake-service` | true | `temporal:7233` | Temporal frontend gRPC adresi; client ve worker'lar workflow start/poll için bu adresi kullanır. | - | false |
| `TEMPORAL_NAMESPACE` | `automation-service`, `agent-runner-worker`, `execution-runner-worker`, `admin-dashboard-api`, `task-intake-service` | false | `default` | Temporal namespace; multi-tenant ayrım gerekirse override edilir. | - | false |
| `TEMPORAL_TASK_QUEUE` | `agent-runner-worker`, `execution-runner-worker` | true | (worker bazlı: `agent-runner` / `execution-runner`) | Worker'ın dinleyeceği task queue adı; activity dispatch bu kuyruk üzerinden yapılır. | - | false |
| `TEMPORAL_WORKFLOW_RUN_TIMEOUT_S` | `automation-service`, `agent-runner-worker`, `execution-runner-worker` | false | `3600` | Bir workflow execution'ının saniye cinsinden çalıştırma zaman aşımı. | - | false |
| `TEMPORAL_TLS_ENABLED` | `automation-service`, `agent-runner-worker`, `execution-runner-worker`, `admin-dashboard-api` | false | `false` | Temporal frontend ile TLS bağlantısı kurulup kurulmayacağını belirler. | - | false |
| `TEMPORAL_TLS_CA_FILE` | `automation-service`, `agent-runner-worker`, `execution-runner-worker` | false | (boş) | TLS açıkken kullanılan CA sertifika dosya yolu. | - | false |

## 4. MCP / Firecrawl

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `MCP_BASE_URL` | `automation-service`, `assistant-service`, `agent-runner-worker`, `streamlit-ui`, `task-intake-service` | true | `http://atlassian-mcp:8090` | `atlassian_mcp_bitbucket` MCP HTTP endpoint'i; tüm Jira/Bitbucket/Confluence çağrıları yalnızca bu URL üzerinden gider. | - | false |
| `MCP_REQUEST_TIMEOUT_S` | `automation-service`, `assistant-service`, `agent-runner-worker` | false | `30` | MCP HTTP isteklerinin saniye cinsinden zaman aşımı. | - | false |
| `MCP_ALLOWED_URL_DOMAINS` | `atlassian-mcp` | false | (boş) | MCP gateway'in SSRF guard allowlist'i (virgülle ayrılmış domain listesi, suffix-match). Upstream guard, Atlassian URL'i private (non-global) bir IP'ye çözülen her isteği bloklar (`DNS for <host> resolves to non-global IP`). Self-hosted Server/DC (10.x / 192.168.x / 172.16-31.x) bu yüzden varsayılan olarak bloke olur; ilgili domain (`example.org`  jira./wiki./bitbucket.example.org'yi kapsar) buraya eklenince eşleşen host'lar IP kontrolünü atlar. Boş = guard tam aktif + DNS kontrolü (public host geçer, private IP bloke) - Atlassian Cloud için doğru varsayılan. Değer site-özel olduğu için `infra/.env`'e yazılır, compose YAML'a gömülmez. `http://`/port yazma, sadece domain. | - | false |
| `ATLASSIAN_OAUTH_ENABLE` | `atlassian-mcp` | false | `true` | Stateless/multi-tenant modu açar. Boot'ta credential olmadığında bile `tools/list`'in Jira+Confluence toolset'lerini döndürmesini sağlar; kapalıyken IDE/client "Discovered 0 tools" görür. Gerçek auth yine per-request `X-Atlassian-*` header'larından gelir; bu flag yalnızca tool keşfini açar. | - | false |
| `MCP_BANNED_TOOLS` | `automation-service`, `assistant-service`, `agent-runner-worker` | false | `bitbucket_merge_pr,confluence_delete_page` | Tool katalog filtresinin LLM'e sunmadığı sabit araç listesi; override edilse bile yerleşik liste kalır. | - | false |
| `FIRECRAWL_BASE_URL` | `agent-runner-worker`, `task-intake-service` | true | `http://firecrawl:3002` | Self-hosted Firecrawl HTTP endpoint'i; web search/scrape çağrıları bu URL'den geçer. | - | false |
| `FIRECRAWL_ENABLED` | `automation-service`, `agent-runner-worker` | false | `false` | Global firecrawl etkinleştirme bayrağı; `web_search` capability türetimi bu değere bağlıdır. | - | false |
| `FIRECRAWL_EGRESS_ALLOWLIST` | `firecrawl` | false | (boş) | Firecrawl'ın dış host'lara izin verdiği virgülle ayrılmış liste; listede olmayan host'lar HTTP 403 ile reddedilir. | - | false |
| `FIRECRAWL_API_KEY` | `firecrawl`, `agent-runner-worker`, `task-intake-service` | false | (boş) | Firecrawl auth header'ı için paylaşılan secret; production'da Vault'tan resolve edilir. | `vault:infrastructure/firecrawl/api_key` | false |
| `OPENCODE_ENDPOINT` | `agent-runner-worker`, `assistant-service` | false | `http://opencode-sidecar:4096` | Opencode sidecar'ın Compose-internal HTTP endpoint'i; host ağına yayınlanmaz. | - | false |

## 5. LLM (vLLM / OpenAI / Anthropic)

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `LLM_PROVIDER` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | true | `openai` | Aktif LLM sağlayıcısı: `vllm` / `openai` / `anthropic`. | - | false |
| `LLM_MODEL_NAME` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | `gpt-5.5` | Çağrılarda kullanılan model adı; provider tarafında karşılığı olmalıdır. | - | false |
| `LLM_REASONING_EFFORT` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | (boş) | Reasoning destekli modeller (o-serisi / gpt-5 ailesi / Claude 4) için `minimal`/`low`/`medium`/`high`; desteklemeyen modelde gönderilmez. | - | false |
| `LLM_VERBOSITY` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | (boş) | gpt-5 ailesi için çıktı ayrıntı düzeyi `low`/`medium`/`high`; desteklemeyen modelde gönderilmez. | - | false |
| `VLLM_BASE_URL` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | `http://host.docker.internal:8000/v1` | Compose dışı vLLM endpoint'i; Compose stack vLLM paketlemez. | - | false |
| `OPENAI_API_KEY` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | (boş) | OpenAI fallback API anahtarı; production'da plain-text yerine Vault path'inden çözülür. | `vault:infrastructure/openai/api_key` | false |
| `ANTHROPIC_API_KEY` | `assistant-service`, `agent-runner-worker`, `automation-service`, `admin-dashboard-api` | false | (boş) | Anthropic fallback API anahtarı; production'da plain-text yerine Vault path'inden çözülür. | `vault:infrastructure/anthropic/api_key` | false |
| `LLM_REQUEST_TIMEOUT_S` | `assistant-service`, `agent-runner-worker` | false | `60` | LLM HTTP çağrılarının saniye cinsinden zaman aşımı. | - | false |
| `LLM_MAX_TOKENS_OUTPUT` | `assistant-service`, `agent-runner-worker` | false | `2048` | LLM yanıtının maksimum token sayısı. | - | false |
| `LLM_TEMPERATURE` | `assistant-service`, `agent-runner-worker` | false | `0.2` | LLM örnekleme sıcaklığı. | - | false |

## 6. MinIO

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `MINIO_ENDPOINT` | `agent-runner-worker`, `execution-runner-worker`, `automation-service`, `admin-dashboard-api` | true | `minio:9000` | MinIO S3-uyumlu API endpoint'i; artifact upload/download için kullanılır. | - | false |
| `MINIO_ROOT_USER` | `minio` (init), `agent-runner-worker`, `execution-runner-worker` | true | `minio` | MinIO root erişim anahtarı kullanıcı adı. | - | false |
| `MINIO_ROOT_PASSWORD` | `minio` (init), `agent-runner-worker`, `execution-runner-worker` | true | `miniosecret_dev_only` | MinIO root parolası; production'da Vault'tan resolve edilir, plain-text disk'e düşmez. | `vault:minio/access_keys` | false |
| `MINIO_DEFAULT_BUCKET` | `agent-runner-worker`, `execution-runner-worker`, `automation-service` | false | `platform-artifacts` | Servislerin artifact yazarken kullandığı varsayılan bucket adı. | - | false |
| `MINIO_USE_TLS` | `agent-runner-worker`, `execution-runner-worker`, `automation-service` | false | `false` | MinIO bağlantısı HTTPS olarak kurulup kurulmayacağını belirler. | - | false |

## 7. OIDC

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `OIDC_ISSUER_URL` | `admin-dashboard-api`, `assistant-service` | false (prod'da true) | (boş) | OIDC provider issuer kök URL'i; `.well-known/openid-configuration` üzerinden discovery yapılır. | - | false |
| `OIDC_ISSUER` | `admin-dashboard-api` | false | (boş) | Token doğrulamada beklenen `iss` claim değeri (production AUTH_MODE'da zorunlu). | - | false |
| `OIDC_CLIENT_ID` | `admin-dashboard-api`, `assistant-service` | false (prod'da true) | (boş) | OIDC client identifier; provider konsolundan alınır. | - | false |
| `OIDC_CLIENT_SECRET` | `admin-dashboard-api`, `assistant-service` | false | (boş) | OIDC client secret; production'da Vault'tan resolve edilir. | `vault:infrastructure/oidc/client_secret` | false |
| `OIDC_AUDIENCE` | `admin-dashboard-api`, `assistant-service` | false | (boş) | Token doğrulamada beklenen `aud` claim değeri. | - | false |
| `OIDC_JWKS_URL` | `admin-dashboard-api`, `assistant-service` | false | (boş) | RS256 imza doğrulaması için JWKS endpoint'i (en az 5 dakika cache'lenir). | - | false |
| `OIDC_SCOPES` | `admin-dashboard-api`, `assistant-service` | false | `openid profile email` | Authorization Code akışında istenen scope listesi. | - | false |
| `OIDC_REDIRECT_URI` | `admin-dashboard-api`, `assistant-service` | false | (boş) | OIDC callback URL'i; provider'da kayıtlı redirect URI ile birebir eşleşmelidir. | - | false |

## 8. Auth

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `AUTH_PROVIDER` | `admin-dashboard-api`, `assistant-service` | false | `oidc` | Aktif kimlik doğrulama mekanizması: `oidc` (prod) veya `local` (dev kullanıcı adı/şifre). | - | false |
| `AUTH_MODE` | `admin-dashboard-api` | false | `dev` | `dev` modunda imzasız token kabul edilir; `production` modunda JWKS imza ve claim doğrulaması zorlanır. | - | false |
| `AUTH_LOCAL_USERNAME` | `admin-dashboard-api`, `assistant-service` | false | `admin` | `AUTH_PROVIDER=local` modunda kabul edilen kullanıcı adı (yalnızca dev). | - | false |
| `AUTH_LOCAL_PASSWORD` | `admin-dashboard-api`, `assistant-service` | false | `admin_dev_only` | Local auth modunun parolası (yalnızca dev); plain-text production'da kullanılamaz. | `vault:infrastructure/auth/local_password` | false |
| `AUTH_SESSION_TTL_S` | `admin-dashboard-api`, `assistant-service` | false | `28800` | Auth oturumunun saniye cinsinden yaşam süresi (default 8 saat). | - | false |
| `AUTH_COOKIE_SECURE` | `admin-dashboard-api`, `assistant-service` | false | `true` | Auth cookie'lerinin yalnızca HTTPS üzerinden gönderilmesini zorlar. | - | false |
| `AUTH_DEFAULT_ROLE` | `admin-dashboard-api` | false | `viewer` | OIDC claim'inde rol bulunamazsa atanan varsayılan rol; `viewer`/`lead`/`admin`/`dept_admin` enum'undadır. | - | false |

## 9. Webhook secrets

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `WEBHOOK_HMAC_HEADER_JIRA` | `automation-service` | false | `X-Hub-Signature-256` | Jira webhook isteklerinde HMAC imzasının okunduğu header adı. | - | false |
| `WEBHOOK_HMAC_HEADER_BITBUCKET` | `automation-service` | false | `X-Hub-Signature` | Bitbucket webhook isteklerinde HMAC imzasının okunduğu header adı. | - | false |
| `WEBHOOK_HMAC_HEADER_CONFLUENCE` | `automation-service` | false | `X-Atlassian-Webhook-Identifier` | Confluence webhook isteklerinde imza/identifier'ın okunduğu header adı. | - | false |
| `WEBHOOK_SECRET_VAULT_PREFIX` | `automation-service` | false | `vault:webhooks` | Departman bazlı webhook secret'ının çözüldüğü Vault path öneki; tam path `vault:webhooks/<provider>/<dept_id>`. | - | false |
| `WEBHOOK_ROTATION_OVERLAP_S` | `automation-service` | false | `3600` | Webhook secret rotation sırasında eski secret'ın kabul edilmeye devam ettiği saniye penceresi (default 1 saat). | - | false |
| `WEBHOOK_REPLAY_WINDOW_S` | `automation-service` | false | `300` | Webhook timestamp'inin replay-attack koruması için kabul edilen sapma penceresi. | - | false |
| `WEBHOOK_BODY_MAX_BYTES` | `automation-service` | false | `1048576` | Webhook isteklerinde kabul edilen maksimum payload boyutu (byte). | - | false |

## 10. SSH runners

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `SSH_HOST` | `execution-runner-worker` | false | (boş) | Tek SSH runner host adresi. `execution` capability'si bu env veya deprecated `SSH_HOST_1` alias'ı set olduğunda türetilir. Tüm departmanlar bu tek host'u `RUNNER_BASE_PATH` altında paylaşır. | - | false |
| `SSH_HOST_1` | `execution-runner-worker` | false | (boş) | DEPRECATED - `SSH_HOST` kanonik değişkenin geriye uyumluluk alias'ı. Yeni deployment'larda kullanılmaz; mevcut deployment'larda `SSH_HOST` boşsa fallback olarak okunur. | - | false |
| `SSH_HOST_2` | `execution-runner-worker` | false | (boş) | DEPRECATED - multi-runner legacy slot. Single-runner kanonik kontrat altında runtime tarafından **yok sayılır**; gelecekteki bir release'de kaldırılacaktır. | - | false |
| `SSH_HOST_3` | `execution-runner-worker` | false | (boş) | DEPRECATED - multi-runner legacy slot. Single-runner kanonik kontrat altında runtime tarafından **yok sayılır**; gelecekteki bir release'de kaldırılacaktır. | - | false |
| `SSH_USER_DEFAULT` | `execution-runner-worker` | false | `runner` | SSH bağlantılarında kullanılan varsayılan kullanıcı adı. | - | false |
| `SSH_PORT_DEFAULT` | `execution-runner-worker` | false | `22` | SSH bağlantılarında kullanılan varsayılan TCP portu. | - | false |
| `SSH_KEY_VAULT_PREFIX` | `execution-runner-worker` | false | `vault:ssh/runners` | SSH key dual-slot rotation için kullanılan Vault path öneki; aktif slot `<prefix>/<runner_id>/active`, önceki slot `<prefix>/<runner_id>/previous`. | - | false |
| `SSH_CONNECT_TIMEOUT_S` | `execution-runner-worker` | false | `15` | SSH bağlantısının saniye cinsinden kurulum zaman aşımı. | - | false |
| `SSH_COMMAND_TIMEOUT_S` | `execution-runner-worker` | false | `1800` | Tek bir SSH komutunun saniye cinsinden çalışma zaman aşımı (default 30 dakika). | - | false |
| `SSH_KNOWN_HOSTS_PATH` | `execution-runner-worker` | false | `/etc/ssh/ssh_known_hosts` | Doğrulanmış host anahtarlarının okunduğu dosya yolu; eşleşmeyen host fingerprint'i bağlantıyı reddeder. | - | false |
| `RUNNER_BASE_PATH` | `execution-runner-worker` | false | `/var/ai-runner` | Workspace kök klasörü; task workspace'leri `{RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}/` formatında oluşturulur (`runners/workspace_path.build_workspace_path`). Bu path tek SSH host'ta tüm departmanlarca paylaşılır. | - | false |
| `SSH_BASE_PATH` | `execution-runner-worker` | false | `/var/ai-runner` | (deprecated alias for `RUNNER_BASE_PATH`) - eski deployment'lar için `pydantic-settings` `AliasChoices("RUNNER_BASE_PATH", "SSH_BASE_PATH")` üzerinden fallback olarak okunur; yeni kurulumlarda yalnızca `RUNNER_BASE_PATH` set edilmelidir. | - | false |
| `RUNNER_DISK_WARN_PCT` | `execution-runner-worker`, `automation-worker` | false | `80` | Workspace disk kullanım uyarı eşiği (yüzde). Bu yüzdeye ulaşıldığında `WorkspaceCleanupSchedulerWorkflow` admin-dashboard'a sarı banner gönderir; eviction tetiklenmez. | - | false |
| `RUNNER_DISK_EVICT_PCT` | `execution-runner-worker`, `automation-worker` | false | `90` | Workspace disk auto-prune eviction eşiği (yüzde). Bu yüzdeye ulaşıldığında `WorkspaceCleanupSchedulerWorkflow` (Temporal cron, hourly) en eski `iter-N` klasörlerini (mtime'a göre) kullanım eşiğin altına düşene kadar tek tek siler ve `workspace_auto_pruned` audit yazar. | - | false |

## 11. Feature flags

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `FEATURE_FLAG_AI_ENABLED` | `automation-service`, `assistant-service`, `agent-runner-worker` | false | `true` | Açıldığında AI/LLM içeren workflow'ların başlatılmasına izin verilir; `false` olduğunda LLM çağrıları kısa devre yapar. | - | true |
| `FEATURE_FLAG_EXECUTION_ENABLED` | `automation-service`, `execution-runner-worker` | false | `true` | Açıldığında SSH/Docker tabanlı execution workflow'ları çalıştırılır; `false` olduğunda `execution` capability'si türetilse bile workflow başlatılmaz. | - | true |
| `FEATURE_FLAG_TASK_INTAKE_ENABLED` | `task-intake-service`, `automation-service` | false | `false` | Açıldığında `task-intake-service` Compose profile'ı etkin olur ve görev alımı boru hattı çalışır; varsayılan kapalıdır. | - | true |
| `SSH_RUNNER_DEPT_PINNING_ENABLED` | `automation-service`, `execution-runner-worker` | false | `false` | DEPRECATED - single-runner kanonik kontrat altında no-op. Per-dept SSH host pinning kaldırıldı; tüm departmanlar tek host'u paylaşır. Default `false` korunur ve env-coverage parity için okunmaya devam eder; runtime tarafından **yok sayılır**. Gelecekteki bir release'de kaldırılacak. | - | true |
| `SSH_DEPT_QUOTA_ENABLED` | `automation-service`, `execution-runner-worker` | false | `false` | DEPRECATED - single-runner kanonik kontrat altında no-op. Per-dept disk quota kaldırıldı; disk yönetimi global `RUNNER_DISK_WARN_PCT` / `RUNNER_DISK_EVICT_PCT` eşikleri üzerinden `WorkspaceCleanupSchedulerWorkflow` tarafından yapılır. Default `false` korunur ve env-coverage parity için okunmaya devam eder; runtime tarafından **yok sayılır**. Gelecekteki bir release'de kaldırılacak. | - | true |
| `FEATURE_FLAG_FIRECRAWL_ENABLED` | `automation-service`, `agent-runner-worker` | false | `false` | Açıldığında `web_search` capability türetimi ve Firecrawl çağrıları aktiftir; varsayılan kapalıdır. | - | true |
| `FEATURE_FLAG_PR_AUTO_MERGE_ENABLED` | `automation-service`, `agent-runner-worker` | false | `false` | Açıldığında PR auto-merge denemeleri etkinleşir; production'da `false` kalmalıdır. | - | true |
| `FEATURE_FLAG_AUDIT_PRUNE_ENABLED` | `automation-service`, `admin-dashboard-api` | false | `false` | Açıldığında AuditPruneWorkflow düzenli çalışır ve eski audit kayıtlarını arşivler; varsayılan kapalıdır. | - | true |

## 12. Observability / Log

| name | service(s) | required | default | description | vault_path | feature_flag |
|---|---|---|---|---|---|---|
| `LOG_LEVEL` | `*` (tüm servis/worker/UI) | true | `INFO` | Yapılandırılmış log seviyesi: `DEBUG` / `INFO` / `WARNING` / `ERROR`. | - | false |
| `LOG_FORMAT` | `*` (tüm servis/worker/UI) | false | `json` | Log çıktı formatı: `json` (prod) veya `console` (dev). | - | false |
| `LOG_REDACTION_ENABLED` | `*` (tüm servis/worker/UI) | false | `true` | Açıldığında `Authorization`, `Bearer`, `api_token=`, `password=`, `secret=` desenleri `***REDACTED***` ile değiştirilir. | - | false |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | (boş) | OpenTelemetry collector OTLP endpoint'i; boş bırakılırsa trace/metric export edilmez. | - | false |
| `OTEL_SERVICE_NAME` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | (compose service adı) | OpenTelemetry tracing'de servisin tanımlayıcı adı. | - | false |
| `OTEL_RESOURCE_ATTRIBUTES` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | (boş) | OpenTelemetry resource attribute listesi (`key=value,key=value`). | - | false |
| `METRICS_PROMETHEUS_PORT` | `automation-service`, `assistant-service`, `admin-dashboard-api` | false | `9090` | Prometheus scrape endpoint'inin servis içinde dinlediği port. | - | false |
| `SENTRY_DSN` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker` | false | (boş) | Sentry hata raporlama DSN'i; boş bırakıldığında raporlama devre dışı kalır. | `vault:infrastructure/sentry/dsn` | false |
| `CLIENT_SOURCE` | `automation-service`, `assistant-service`, `admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`, `streamlit-ui`, `task-intake-service` | false | (compose service adı) | `X-Client-Source` HTTP header'ında gönderilen istemci kimliği; audit korelasyon için kullanılır. | - | false |
| `HEALTH_POLL_INTERVAL_SECONDS` | `admin-dashboard-api` | false | `10` | Background `/healthz` polling cadence'i; izin verilen aralık `[5, 30]`. | - | false |
| `HEALTH_READY_TIMEOUT_SECONDS` | `admin-dashboard-api` | false | `60` | `compose up` sonrası servisin `/healthz=200` döndürmesi için beklenen maksimum süre; izin verilen aralık `[5, 180]`. | - | false |
| `HEALTH_FAIL_STREAK_THRESHOLD` | `admin-dashboard-api` | false | `3` | Tek bir `health_streak_alert` audit kaydını tetikleyen ardışık `unhealthy` poll sayısı; izin verilen aralık `[1, 10]`. | - | false |

---

## Parity & Validation Rules

1. **`.env.example` superset** - `platform/.env.example` `name` listesi, bu döküman'daki `required=true` satırlarının **superset**'idir; eksik bir zorunlu değişken `tests/property/test_env_coverage.py` testinde başarısızlığa yol açar.
2. **Per-service parity** - `services/<name>/.env.example` ve `workers/<name>/.env.example` dosyalarında listelenen her değişken bu döküman'da da tanımlıdır. Tanımsız bir değişken `tests/property/test_env_coverage.py`'da testi başarısız sayar.
3. **Vault path parity** - `vault_path` sütunu dolu olan değişkenlerin `.env.example` dosyalarındaki değeri `boş` veya `_dev_only` ekli placeholder olmalıdır; production değer commit edilirse `tests/property/test_sensitive_key_parity.py` testi başarısız sayar.
4. **Feature flag default-off invariant** - `feature_flag=true` işaretli her değişken `default=false` ile başlar ve opt-in açılması gerekir. `SSH_RUNNER_DEPT_PINNING_ENABLED` ve `SSH_DEPT_QUOTA_ENABLED` bu kuralın tarihi referans örnekleridir; her ikisi de single-runner kanonik kontrat altında **deprecated no-op** durumdadır ve gelecekteki bir release'de kaldırılacaktır.
5. **Log redaction invariant** - Tüm servisler `RedactionFilter` middleware'ı ile `Authorization: Basic <...>`, `Bearer <...>`, `api_token=<...>`, `password=<...>`, `secret=<...>` desenlerini `***REDACTED***` ile değiştirir.

## Change Process

- Yeni bir env değişkeni eklenirken **önce** bu döküman güncellenir, **sonra** `services/<name>/.env.example` ve gerekirse `platform/.env.example` dosyalarına yansıtılır.
- Bir değişken artık kullanılmıyorsa, satır silinmeden önce bağlı `.env.example` dosyalarından temizlenmelidir; aksi halde `test_env_coverage.py` "tanımsız değişken" olarak raporlar.
- Secret tipindeki yeni değişkenler **mutlaka** `vault_path` sütunu doldurulmuş olmalı ve `.env.example` dosyalarında plain-text production değer ile bırakılmamalıdır.
