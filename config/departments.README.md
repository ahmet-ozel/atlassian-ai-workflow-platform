# `config/departments.json`

This directory holds the bootstrap department roster consumed by
`automation-service` and the admin dashboard.

## Files

- `departments.json` - three example departments (`payment`, `hr`, `legal`)
  used as the initial roster. Every `bot.{jira,bitbucket,confluence}.account_id`
  is intentionally `""` (empty string).
- `departments.schema.json` - JSON Schema 2020-12 validator. Strict
  (`additionalProperties: false` at every level).

## Why are all `account_id` values empty?

The Atlassian `accountId` for each bot service is resolved on first contact:

1. Operator commits the department definition with `account_id: ""`.
2. On startup (or first MCP probe), `automation-service` calls Atlassian's
   `/myself` (Jira/Confluence) or `/user` (Bitbucket) endpoints using the
   credentials behind `credential_ref`.
3. The discovered `accountId` is cached in Postgres
   (`shared.department_bot_identity`) keyed by `(department_id, service)`.
4. Subsequent runs read from cache; the JSON file is never rewritten.

## Schema notes

- `id` is kebab-case only: `^[a-z][a-z0-9-]{1,30}$` (no underscores).
- Each `bot` object must contain at least one of `{jira, bitbucket, confluence}`.
- `BotEntry` accepts either a single `credential_ref` (Vault path) **or** the
  `email` + `api_token_ref` pair.
- The schema does **not** allow a top-level `_comment` field inside
  `departments.json` itself (`additionalProperties: false` at the root).
  This README is the durable home for that comment so the JSON file
  remains schema-valid and idempotent for tooling.

## `mode` enum - `active` / `shadow` / `disabled`

`Department.mode` üç değerden birini alır:

| Mode | Anlam | Capability gate davranışı | Açık workflow'lar |
|---|---|---|---|
| `active` (default) | Normal operasyon. Webhook'lar işlenir, workflow'lar başlatılır, `output_actions` execute edilir. | İzinli (capability'ler yeterliyse). | Çalışmaya devam eder. |
| `shadow` | Bot plan üretir ancak `output_actions` execute edilmez (dry-run). | İzinli; workflow start edilir fakat side-effect'ler atlanır. | Plan adımları çalışır, execution adımları atlanır. |
| `disabled` | Departman geçici olarak askıya alınmış. | **Reddeder.** Departmana gelen tüm workflow start çağrıları HTTP 202 + `decision: denied` ile döner ve audit'e `dept_disabled` kaydı yazılır. | Çalışan workflow'lar `automation-service` tarafından drain edilir (yeni signal/start gönderilmez); zaten başlamış aktiviteler tamamlanır. |

### `disabled` modu için geçiş kuralları

1. **Aktivasyon yolu:** `POST /admin/departments/{id}/disable` (yalnızca `admin`
   rolü) endpoint'i `mode=disabled` set eder ve audit'e `actor_role=admin`,
   `action=dept_disable` kaydı düşer.
2. **Drain davranışı:** `mode=disabled` set edildikten sonra `automation-service`
   yeni workflow start çağrılarını reddeder; halihazırda Temporal'da çalışan
   workflow'lar tamamlanana kadar ilerler. Workflow'lara yeni signal gönderilmez.
3. **Webhook akışı:** Departmana ait Jira/Bitbucket/Confluence webhook'ları
   `dept_id` çözüldükten sonra `mode == "disabled"` kontrolüyle drop edilir;
   audit'e `webhook_dept_disabled` kaydı yazılır.
4. **Geri dönüş:** `disabled`  `active` geçişi de yine `admin` rolüyle yapılır;
   credential rotate gerekmez ancak operatörün capability gate'i **yeniden**
   doğrulaması (probe çalıştırması) önerilir.
5. **Setup wizard ile ilişki:** `POST /admin/departments/wizard` akışı bir adımdaki
   probe başarısız olursa kaydı `mode=disabled` olarak commit eder veya hiç
   commit etmez. `disabled` durumundan `active`'e geçiş için tüm
   probe'ların yeşil olması gerekir.
6. **Decommission:** Tam silme (departmanı kaldırma) için
   `docs/runbooks/dept-decommission.md` runbook'u izlenir; ilk adım her zaman
   `mode=disabled` set etmektir.

### Vault path konvansiyonu - `credential_ref`

`BotEntry.credential_ref` (ve `api_token_ref`, `slack_webhook_url`,
`api_key_ref`, `fallback_api_key_ref`) alanları için schema regex pattern'i
`^vault:[a-zA-Z0-9/_-]+$`'dir. Bu **mevcut karakter sınıfını** kabul etmeye
devam eder; yeni-yazılan path'lerin lowercase + kebab-case kullanması bir
**konvansiyon** (style guide) gereğidir, schema seviyesinde zorlanmaz.

## Validation

```bash
python -c "import json,sys; from jsonschema import Draft202012Validator; \
  s=json.load(open('config/departments.schema.json',encoding='utf-8')); \
  d=json.load(open('config/departments.json',encoding='utf-8')); \
  Draft202012Validator(s).validate(d); print('OK')"
```

## Cost & Notification Fields

`Department` nesnesi ops katmanı için cost tracking, notification ve feature
flag override alanları içerir.

### `budget_caps` (zorunlu)

`Department.budget_caps` her departman için **zorunludur** ve dört alt alan
içerir; tümü USD cinsinden, hepsi `>= 0`:

| Alan | Tip | Açıklama |
|---|---|---|
| `weekly_usd_dept` | `number` | Departmanın toplam haftalık USD bütçesi. |
| `weekly_usd_user` | `number` | Departman içindeki tek bir kullanıcının haftalık USD bütçesi. |
| `monthly_usd_dept` | `number` | Departmanın toplam aylık USD bütçesi. |
| `monthly_usd_user` | `number` | Departman içindeki tek bir kullanıcının aylık USD bütçesi. |

`automation-service.BudgetCapPolicy.enforce(dept_id, user_id)` bu dört alanı
sırayla `dept_weekly  user_weekly  dept_monthly  user_monthly` olarak
kontrol eder; ilk aşımda yeni workflow start çağrısı **HTTP 429** ile
reddedilir ve `audit budget_exceeded` (zorunlu) yazılır. `cost_tag IN
('sandbox','probe')` kayıtları usage hesabından dışlanır.

Mevcut üç fixture (`payment`, `hr`, `legal`) ortak bir varsayılan ile gelir
(`weekly_usd_dept=500`, `weekly_usd_user=50`, `monthly_usd_dept=1800`,
`monthly_usd_user=150`); operatörler bu değerleri prod ortamında departman
büyüklüğüne göre revize eder.

### `notify_on_success` (opsiyonel, default `false`)

Bir workflow başarıyla tamamlandığında bildirim gönderilip gönderilmeyeceğini
kontrol eder. Default `false` çünkü tasarım kararı "success-gated, failure
mandatory"dir: success bildirimleri gürültü yaratır ve opt-in'dir.

**Önemli:** Failure bildirimleri bu alandan **bağımsızdır** ve dept
config'den bağımsız olarak Slack'e **her zaman** gönderilir.

### `notify_channels` (opsiyonel, default `[]`)

Bu departman için etkin bildirim kanallarının listesi. Enum:
`["slack", "email", "teams"]`. `uniqueItems: true`.

- `slack`: `slack_webhook_ref` üzerinden gönderim (success-gated).
- `email`: `notify_email` adresine SMTP üzerinden gönderim.
- `teams`: Microsoft Teams entegrasyonu (henüz iskelet; gelecekteki backlog).

`notify_on_success=true` iken bu listede yer alan tüm kanallara mesaj gider.
Failure durumunda Slack zaten zorunlu olduğu için liste yalnızca opsiyonel
ek kanalları kontrol eder (örn. failure'da email de istiyorsa
`notify_channels=["slack","email"]`).

### `slack_webhook_ref` (opsiyonel, regex `^vault:[a-zA-Z0-9/_-]+$`)

Departmanın Slack incoming webhook URL'sini içeren Vault path'i. **Plain-text
webhook URL JSON'a yazılmaz**.
Örnek: `vault:notifications/payments/slack`.

`null` veya alan yoksa Slack notification akışı sessizce atlanır
(notification kaybedilmez; `notification_log` kaydı `status="skipped_no_webhook"`
ile yazılır).

### `notify_email` (opsiyonel, RFC 5322 format)

JSON Schema `format: email` ile doğrulanan RFC 5322 e-posta adresi. SMTP
bağlantı kimliği ayrı ve global bir Vault path'inden çözülür
(`vault:notifications/smtp/credential`); bu alan yalnızca alıcı adresidir.
`null` veya alan yoksa email kanalı atlanır.

### `feature_flag_overrides` (opsiyonel, default `{}`)

Free-form bool map. Anahtar feature flag adı, değer `true` veya `false`.
Global `feature_flags` tablosundaki default değeri **bu departman için**
override eder. Örnek:

```json
"feature_flag_overrides": {
  "explain_cache_enabled": true,
  "research_enabled": false
}
```

Override öncelik sırası (yüksekten düşüğe):
1. `feature_flag_overrides[name]` (departman bazlı)
2. `feature_flags.enabled` (global tablo)
3. `feature_flags.default_value` (boot-time fallback)

Toggle aksiyonları admin-dashboard `/feature-flags` panelinde yapıldığında
audit'e `feature_flag_toggled` event'i ile yazılır.

## Migration Notes

`budget_caps` alanı **zorunlu** olduğundan mevcut `departments.json` fixture'ı
(`payment`, `hr`, `legal`) varsayılan değerlerle migrate edilmiştir. Üretimde
çalışan bir kurulumdan upgrade için:

1. Operatör `departments.json` dosyasına her dept için `budget_caps` bloğunu
   üstte tanımlanan 4 alanla doldurur (≥0, USD).
2. Boot anında schema validator yeni alanı arar; eksiklik servis başlangıcını
   `additionalProperties: false` paritesiyle başarısız sayar.
3. Diğer dört alan (`notify_on_success`, `notify_channels`,
   `slack_webhook_ref`, `notify_email`, `feature_flag_overrides`) opsiyoneldir;
   eksiklerse default davranışlar (`false`, `[]`, `null`, `{}`) uygulanır.

## DEPRECATED - `ssh_workspace_quota_mb`

> **Status:** Deprecated since the single-runner model was adopted. The field is preserved in the schema for backwards
> compatibility with existing `departments.json` files but the runtime
> **ignores** it. It will be removed in a future release.

### Why

The platform runs **exactly one** SSH runner host shared by all
departments under `RUNNER_BASE_PATH`. Per-department disk quotas
no longer make sense in this topology - the disk is a single shared
resource, not partitioned per dept. Disk pressure is now managed
globally by `WorkspaceCleanupSchedulerWorkflow` using two
host-wide thresholds:

| Env | Default | Behaviour |
|---|---|---|
| `RUNNER_DISK_WARN_PCT` | `80` | Admin-dashboard'a sarı banner gönderilir; eviction tetiklenmez. |
| `RUNNER_DISK_EVICT_PCT` | `90` | En eski `iter-N` klasörleri (mtime'a göre) kullanım eşiğin altına düşene kadar tek tek silinir. Her silme `workspace_auto_pruned` audit'i yazar. |

### What still works

- The schema still accepts `ssh_workspace_quota_mb` so existing JSON
  files validate without edits.
- The `check_disk_quota` activity remains in the codebase and continues
  to honour the field when explicitly invoked by legacy call sites
  (none in current production paths).
- `SSH_DEPT_QUOTA_ENABLED` env flag remains in `.env.example` for
  env-coverage parity but is a runtime no-op.

### Migration

You can leave the field as-is in your `departments.json` - it will
simply be ignored. To clean up your config:

1. Set `RUNNER_DISK_WARN_PCT` and `RUNNER_DISK_EVICT_PCT` in your
   `execution-runner-worker/.env` (defaults are sane for most
   deployments).
2. Remove `ssh_workspace_quota_mb` from each department entry in
   `departments.json` (optional - schema keeps accepting it).
3. Confirm `WorkspaceCleanupSchedulerWorkflow` is registered in
   Temporal (the `automation-worker` boot script registers it
   automatically on startup).

## DEPRECATED - `ssh.host` and any per-dept SSH host overrides

The same single-runner contract removes any concept of a per-department
SSH host. There is no schema field for per-dept SSH hosts (and there
never has been one in the canonical schema - only ad-hoc references in
older `capabilities.py` paths). All departments share `SSH_HOST`
(canonical env var, with `SSH_HOST_1` accepted as a deprecated alias
for backwards compatibility).
