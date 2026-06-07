# Runbook: Webhook Setup (Jira + Bitbucket)

> **Audience:** Platform `admin` rolü; Atlassian organizasyon admin'i (Jira Cloud/DC) ve Bitbucket workspace admin'i ile birlikte.
> **Scope:** Bir departmanın (`dept_id`) Jira ve Bitbucket webhook abonelikleriyle `automation-service` gateway'ine bağlanması; HMAC secret'inin Vault'a yazılması; 1 saatlik overlap rotation prosedürü.
> **Reversibility:** Tüm adımlar geri alınabilir; yanlış konfigüre edilmiş bir webhook abonelik silinerek baştan kurulabilir.

## 1. Overview

Bu runbook, bir departman için **tek bir Jira webhook** ve **tek bir Bitbucket webhook** aboneliğinin nasıl açılacağını, gövdesinin `automation-service` gateway'i tarafından HMAC-SHA256 ile doğrulanabilmesi için Vault'a hangi secret'ın hangi path'e yazılacağını ve bu secret'ın 1 saatlik overlap penceresiyle nasıl döndürüleceğini anlatır.

Kurulum kuralları:

- Her departman **tek bir** Jira webhook subscription kullanır; URL `{public_url}/webhooks/jira`'dır ve abonelik filtresi o dept'in `jira_project_keys[]` listesindeki tüm projeleri kapsar.
- Her departman **tek bir** Bitbucket webhook subscription kullanır; URL `{public_url}/webhooks/bitbucket`'dir ve abonelik scope'u dept'in `bitbucket_workspace`'i ile uyumludur.
- HMAC secret'ı `vault:webhooks/<provider>/<dept_id>` path'inde tutulur; **global tek bir secret kullanılmaz** - her dept ayrı secret'a sahiptir.
- Secret rotation 1 saatlik overlap penceresiyle yapılır; bu pencerede hem eski hem yeni secret kabul edilir, böylece in-flight event'ler kaybolmaz.

İlgili schema referansı: bu runbook'taki `dept_id`, `jira_project_keys[]` ve `bitbucket_workspace` alanları [`platform/config/departments.schema.json`](../../config/departments.schema.json) tarafından tanımlanır; örnek değerler [`platform/config/departments.json`](../../config/departments.json) içinde.

## 2. Prerequisites

| Öğe | Değer | Kaynak |
|---|---|---|
| Operatör rolü | `admin` (global) - platform tarafında; Atlassian/Bitbucket tarafında organizasyon admin'i | OIDC IdP + Atlassian admin paneli |
| `public_url` | `automation-service` Internet'e açık tabanı (örn. `https://automation.example.com`); reverse proxy / ingress üzerinden TLS sonlandırılır | `.env` `AUTOMATION_PUBLIC_URL` |
| Hedef departman | `dept_id`, `jira_project_keys[]`, `bitbucket_workspace`, `bot.bitbucket.deployment ∈ {cloud, server}` | `platform/config/departments.json` |
| Vault erişimi | KV v2 mount altında `webhooks/jira/<dept_id>` ve `webhooks/bitbucket/<dept_id>` path'lerine yazma yetkisi | Vault root/admin token |
| Atlassian araçları | Jira Cloud için organizasyon admin paneli; Jira DC için `https://<jira-dc>/plugins/servlet/webhooks` admin sayfası | Atlassian admin |
| Bitbucket araçları | Bitbucket Cloud için workspace admin'i; Bitbucket DC için `Repository / Project Settings → Webhooks` (DC plugin'i ile) | Bitbucket admin |
| Test aracı | `curl`, `openssl` (HMAC üretimi için), `vault` CLI veya UI | Yerel makine |

> **`dept_id` çözümlemesi:** `dept_id` değerini `platform/config/departments.json` dosyasındaki `id` alanından oku. Vault path'lerinde **bu değer aynen** kullanılır; lower-case ve `[a-z0-9_-]` ile sınırlıdır.

> **Vault path naming convention**:
>
> - Jira: `vault:webhooks/jira/{dept_id}` - KV v2 secret; alanlar: `secret_current`, `secret_previous` (rotation overlap için), `rotated_at` (ISO-8601).
> - Bitbucket: `vault:webhooks/bitbucket/{dept_id}` - aynı şema.

## 3. Jira webhook setup

Bu bölüm Jira Cloud (Atlassian organizasyon admin'i tarafından) ve Jira Data Center (server admin'i tarafından) varyantlarını ayrı alt-bölümlerde anlatır. Her iki varyantta da **dept başına tek bir webhook** kurulur ve filtre o dept'in tüm `jira_project_keys[]` projelerini kapsar.

### 3.1 Jira Cloud (Atlassian admin)

#### 3.1.1 HMAC secret üret ve Vault'a yaz

Önce 32 byte rastgele secret üret ve `vault:webhooks/jira/{dept_id}` path'ine yaz:

```bash
DEPT_ID="payment"
SECRET=$(openssl rand -hex 32)

vault kv put "secret/webhooks/jira/${DEPT_ID}" \
  secret_current="${SECRET}" \
  secret_previous="" \
  rotated_at="$(date -u +%FT%TZ)"
```

> **Not:** `secret_previous` boş bırakılır; ilk rotation'da doldurulur. `automation-service` her iki alanı okur ve verify sırasında her ikisiyle de imza karşılaştırması yapar (1h overlap).

#### 3.1.2 Webhook subscription'ı aç

Atlassian admin panelinden:

1. Atlassian admin (`https://admin.atlassian.com`) → **Products → Jira → System → Webhooks**.
2. **Create a webhook** butonuna bas.
3. Aşağıdaki alanları doldur:

| Alan | Değer |
|---|---|
| **Name** | `automation-service / {dept_id}` (örn. `automation-service / payment`) |
| **URL** | `{public_url}/webhooks/jira` (örn. `https://automation.example.com/webhooks/jira`) |
| **Secret** | Adım 3.1.1'de üretilen `${SECRET}` değeri |
| **Events** | `jira:issue_created`, `jira:issue_assigned`, `jira:issue_updated`, `jira:issue_commented` (Comment created event'i `issue_commented` olarak gelir) |
| **JQL filter** | `project IN ({jira_project_keys})` - dept'in `jira_project_keys[]` listesindeki anahtarları virgülle ayrılmış olarak yaz (örn. `project IN (PAY)`) |
| **Exclude body** | **uncheck** (gateway, payload'ı dept çözümleme + mention filter için ister) |

4. **Save** ile aboneliği oluştur.

#### 3.1.3 İmza header'ı

Atlassian, gövde üzerinden HMAC-SHA256 imzasını **`X-Atlassian-Webhook-Signature`** header'ında `sha256=...` formatında gönderir. `automation-service` gateway'i bu header'ı okur ve `vault:webhooks/jira/{dept_id}` Vault path'inden çekilen `secret_current` (ve overlap penceresinde `secret_previous`) ile karşılaştırır. Eşleşmezse HTTP 401 + `webhook_hmac_invalid` audit kaydı yazılır.

### 3.2 Jira Data Center (DC)

Jira DC'de webhook tanımı sistem admin sayfasından yapılır.

1. `https://<jira-dc>/plugins/servlet/webhooks` admin sayfasına gir.
2. **Create a webhook** → aşağıdaki alanları doldur:

| Alan | Değer |
|---|---|
| **Name** | `automation-service / {dept_id}` |
| **URL** | `{public_url}/webhooks/jira` |
| **Secret** | `${SECRET}` (Adım 3.1.1) |
| **Events** | `Issue: created`, `Issue: assigned`, `Issue: updated`, `Comment: created` (DC bunları sırasıyla `jira:issue_created`, `jira:issue_assigned`, `jira:issue_updated`, `jira:issue_commented` olarak yayınlar) |
| **Issue related events JQL** | `project IN ({jira_project_keys})` |

3. Kaydet.

> **Header parite:** Jira DC de `X-Atlassian-Webhook-Signature` header'ını gönderir; gateway tarafında ek branş yok.

### 3.3 Doğrulama

Webhook UI'sında **Send test event** butonu varsa kullan. Beklenen davranış: `automation-service` HTTP 200 döner; HTTP 401 dönerse Vault'a yazılan secret ile UI'daki secret eşleşmiyor demektir, Adım 3.1.1'i tekrar et.

## 4. Bitbucket webhook setup

Bitbucket varyantı Jira ile parite gösterir; URL ve event listesi farklıdır. Bitbucket Cloud ile DC arasındaki UI farkları için iki alt-bölüm verilmiştir.

### 4.1 Bitbucket Cloud (workspace admin)

#### 4.1.1 HMAC secret üret ve Vault'a yaz

```bash
DEPT_ID="payment"
SECRET=$(openssl rand -hex 32)

vault kv put "secret/webhooks/bitbucket/${DEPT_ID}" \
  secret_current="${SECRET}" \
  secret_previous="" \
  rotated_at="$(date -u +%FT%TZ)"
```

#### 4.1.2 Webhook subscription'ı aç

Bitbucket Cloud `bot.bitbucket.workspace` seviyesinde webhook destekler (workspace düzeyi tüm repo'ları kapsar; bu tek-abonelik kuralına uyumludur).

1. `https://bitbucket.org/<workspace>/workspace/settings/webhooks` sayfasına gir.
2. **Add webhook** → aşağıdaki alanları doldur:

| Alan | Değer |
|---|---|
| **Title** | `automation-service / {dept_id}` |
| **URL** | `{public_url}/webhooks/bitbucket` (örn. `https://automation.example.com/webhooks/bitbucket`) |
| **Secret** | `${SECRET}` (Adım 4.1.1) |
| **Status** | `Active` |
| **Triggers** | **Choose from a full list of triggers** seçeneğini aç ve şunları işaretle: `Pull Request → Created` (`pullrequest:created`), `Pull Request → Comment created` (`pullrequest:commented`), `Pull Request → Updated` (`pullrequest:updated`). `Pull Request → Merged` (`pullrequest:fulfilled`) **işaretlenir** (gateway loop guard ile drop eder; ama dahil olması ileride config değişimi gerektirmez). |

3. Save.

#### 4.1.3 İmza header'ı

Bitbucket, gövde üzerinden HMAC-SHA256 imzasını **`X-Hub-Signature`** header'ında `sha256=...` formatında gönderir. `automation-service` gateway'i bu header'ı okur ve `vault:webhooks/bitbucket/{dept_id}` Vault path'inden çekilen `secret_current` (+ overlap penceresinde `secret_previous`) ile karşılaştırır.

### 4.2 Bitbucket Data Center (DC)

Bitbucket DC'de webhook tanımı **proje** veya **repository** düzeyinde yapılır. Tek-abonelik kuralı için workspace karşılığı **Project Settings → Webhooks** seviyesidir; bu da o projedeki tüm repo'ları kapsar.

1. `https://<bitbucket-dc>/projects/<project>/settings/webhooks` sayfasına gir.
2. **Create webhook** → aşağıdaki alanları doldur:

| Alan | Değer |
|---|---|
| **Name** | `automation-service / {dept_id}` |
| **URL** | `{public_url}/webhooks/bitbucket` |
| **Secret** | `${SECRET}` (Adım 4.1.1 - DC için aynı path: `vault:webhooks/bitbucket/{dept_id}`) |
| **Events** | `Pull request → Opened` (`pr:opened`, gateway tarafında `pullrequest:created`'a normalize edilir), `Pull request → Comment added` (`pr:comment:added` → `pullrequest:commented`), `Pull request → Modified` / `Source branch updated` (`pr:modified`, `pr:from_ref_updated` → `pullrequest:updated`) |

3. Kaydet.

> **Header parite:** Bitbucket DC `X-Hub-Signature` header'ını gönderir; Cloud ile aynı verify branş'ı kullanılır.
> **Event normalization:** Gateway, DC dialect'i (`pr:*`) ile Cloud dialect'i (`pullrequest:*`) arasındaki farkı `WebhookEvent.event_type` normalize'ında soyutlar; tüketici workflow'lar her iki varyantta da aynı kararı verir.

### 4.3 Doğrulama

Bitbucket Cloud UI'sında **View requests** sekmesinden son N olayı incele; `automation-service` 200/202 dönüyorsa kurulum başarılı, 401 dönüyorsa secret uyuşmazlığı.

## 5. Secret rotation (1h overlap)

Webhook HMAC secret'ı **1 saatlik overlap penceresi** ile döndürülür. Bu süre boyunca gateway hem `secret_current` hem `secret_previous` ile imza doğrular; pencere kapandıktan sonra `secret_previous` `null`'a alınır.

> **Önemli:** Rotation sırasında Atlassian/Bitbucket UI'sındaki secret bir tek değer alır. Bu nedenle UI'daki secret **her zaman `secret_current`** ile senkronize edilir; provider'ın secret'ı değiştirildiği andan itibaren gelen istekler `secret_current` ile imzalı, henüz UI güncellemesinden önce yola çıkmış istekler ise `secret_previous` ile imzalıdır. Overlap penceresi bu in-flight isteklerin düşmemesini sağlar.

### 5.1 UI üzerinden rotation (önerilen yöntem)

Admin Dashboard artık webhook secret rotation'ını doğrudan UI üzerinden desteklemektedir. Bu yöntem audit kaydı bırakır, overlap penceresini otomatik yönetir ve manuel `vault` / `openssl` komutlarına gerek kalmaz.

#### Adımlar

1. **Admin Dashboard'a giriş yap** → sol menüden **Security** sayfasına git.
2. **"Webhook Secrets"** kartında ilgili `dept × provider` satırını bul.
3. **"Döndür"** butonuna tıkla → onay diyaloğu açılır.
4. Onayladıktan sonra sistem:
   - 32 byte yeni secret üretir.
   - Vault'taki mevcut `secret_current`'ı `secret_previous`'a indirir.
   - Yeni secret'ı `secret_current`'a yazar.
   - `rotated_at` timestamp'ini günceller.
   - `webhook_secret_rotated` audit kaydı yazar.
5. Ekranda yeni secret **bir kere** gösterilir (kopyalanabilir code block).
6. **Atlassian / Bitbucket webhook UI'sına** git ve secret alanını yeni değerle güncelle.
7. Admin Dashboard'a geri dön:
   - Overlap süresi (varsayılan 1 saat, `WEBHOOK_ROTATION_OVERLAP_S` env ile yapılandırılabilir) boyunca hem eski hem yeni secret kabul edilir.
   - Kalan süre canlı geri sayım olarak gösterilir.
   - Süre dolduğunda `secret_previous` otomatik olarak temizlenir (background Temporal workflow).
   - İsterseniz **"Sonlandır"** butonuna basarak overlap penceresini manuel olarak erken kapatabilirsiniz (provider tarafında secret'ı güncellediğinizden emin olduktan sonra).

> **Audit izlenebilirlik:** Her rotation ve finalize işlemi `webhook_secret_rotated` / `webhook_secret_rotation_finalized` audit action'ları ile kayıt altına alınır; `admin-dashboard → Audit` sayfasından izlenebilir.

#### API endpoint'leri (programatik erişim)

| Endpoint | Açıklama |
|---|---|
| `GET /admin/security/webhooks` | Tüm dept × provider matrisini döner (status, last_rotated_at, overlap_window_remaining_s) |
| `POST /admin/security/webhooks/{dept_id}/{provider}/rotate` | Rotation başlatır; yanıtta yeni secret bir kere döner |
| `POST /admin/security/webhooks/{dept_id}/{provider}/finalize` | Overlap penceresini manuel sonlandırır |

### 5.2 Alternatif: Manuel CLI ile rotation (gelişmiş kullanıcı)

> **Not:** Aşağıdaki CLI tabanlı prosedür, admin-dashboard UI'sının kullanılamadığı durumlarda (örn. headless ortam, CI pipeline, acil müdahale) veya gelişmiş kullanıcıların tercih ettiği senaryolarda kullanılabilir. Standart kullanım için yukarıdaki **5.1 UI üzerinden rotation** yöntemi önerilir.

```bash
DEPT_ID="payment"
PROVIDER="jira"   # veya "bitbucket"

# 1) Yeni secret üret
NEW_SECRET=$(openssl rand -hex 32)

# 2) Vault'taki mevcut current'ı previous'a indir, yeni current'ı yaz
OLD_CURRENT=$(vault kv get -field=secret_current "secret/webhooks/${PROVIDER}/${DEPT_ID}")
vault kv put "secret/webhooks/${PROVIDER}/${DEPT_ID}" \
  secret_current="${NEW_SECRET}" \
  secret_previous="${OLD_CURRENT}" \
  rotated_at="$(date -u +%FT%TZ)"

# 3) Atlassian/Bitbucket UI'sında webhook'un Secret alanını ${NEW_SECRET} ile güncelle
#    (manuel adım - provider UI'sından)

# 4) 1 saat bekle (overlap penceresi)

# 5) Pencereyi kapat - previous'ı temizle
vault kv put "secret/webhooks/${PROVIDER}/${DEPT_ID}" \
  secret_current="${NEW_SECRET}" \
  secret_previous="" \
  rotated_at="$(date -u +%FT%TZ)"
```

> **Bekleme süresi:** Adım 4'teki **1 saat** zorunludur; daha kısa bir pencere in-flight event'lerin (örn. async retry'lar) HTTP 401 ile düşmesine neden olabilir. Daha uzun pencere de güvenlik kaybı yaratmaz, sadece eski secret'ın yaşam süresini uzatır.

> **CLI kullanıcıları için ipucu:** Manuel rotation sonrasında admin-dashboard UI'sındaki overlap geri sayımı otomatik olarak güncellenmez; Vault'taki `rotated_at` timestamp'ine göre background Temporal workflow yine de 1 saat sonra `secret_previous`'ı temizler.

### 5.3 Otomasyon

Üretim ortamında bu adımlar `vault_client` (foundation) tarafından sağlanan rotation API'si üzerinden çalıştırılır:

```yaml
# Örnek cron (illustrative)
schedule: "0 3 1 * *"  # her ayın 1'i 03:00 UTC
job: vault_client.rotate_webhook_secret
args:
  dept_id: "payment"
  provider: "jira"
  overlap_window: 1h
```

Atlassian/Bitbucket UI tarafındaki secret güncellemesi şu an manuel; ileride Atlassian REST API üzerinden otomasyon planlanmıştır.

## 6. Verification

Bu bölüm bir webhook'un canlı olarak doğru çalıştığını uçtan uca doğrulamak içindir.

### 6.1 İmzalı test event gönder

Aşağıdaki snippet, Vault'tan secret'ı çeker, örnek bir Jira `issue_created` payload'ını HMAC-SHA256 ile imzalar ve gateway'e POST eder:

```bash
DEPT_ID="payment"
SECRET=$(vault kv get -field=secret_current "secret/webhooks/jira/${DEPT_ID}")
PAYLOAD='{"webhookEvent":"jira:issue_created","issue":{"key":"PAY-9999","fields":{"project":{"key":"PAY"}}}}'
SIG="sha256=$(printf '%s' "${PAYLOAD}" | openssl dgst -sha256 -hmac "${SECRET}" -hex | awk '{print $2}')"

curl -i -X POST "${AUTOMATION_PUBLIC_URL}/webhooks/jira" \
  -H "Content-Type: application/json" \
  -H "X-Atlassian-Webhook-Signature: ${SIG}" \
  --data "${PAYLOAD}"
```

Beklenen:

```
HTTP/1.1 202 Accepted
{"workflow_id":"automation-jira-PAY-9999","decision":"dispatched"}
```

Bitbucket varyantı için `X-Hub-Signature` header'ı ve `pullrequest:created` payload'ı kullanılır:

```bash
DEPT_ID="payment"
SECRET=$(vault kv get -field=secret_current "secret/webhooks/bitbucket/${DEPT_ID}")
PAYLOAD='{"pullrequest":{"id":42,"title":"test"},"repository":{"workspace":{"slug":"example-co"},"slug":"payment-callbacks"}}'
SIG="sha256=$(printf '%s' "${PAYLOAD}" | openssl dgst -sha256 -hmac "${SECRET}" -hex | awk '{print $2}')"

curl -i -X POST "${AUTOMATION_PUBLIC_URL}/webhooks/bitbucket" \
  -H "Content-Type: application/json" \
  -H "X-Event-Key: pullrequest:created" \
  -H "X-Hub-Signature: ${SIG}" \
  --data "${PAYLOAD}"
```

### 6.2 Audit log doğrulaması

Test event'in audit'e ulaştığını doğrula:

```bash
psql -h <pg_host> -U postgres -d automation -c \
  "SELECT action, dept_id, created_at FROM audit_events
   WHERE action IN ('webhook_event_dispatched','webhook_hmac_invalid','webhook_dept_unresolved','loop_guard_dropped')
   ORDER BY created_at DESC LIMIT 5;"
```

Beklenen: en üst satır `webhook_event_dispatched` olmalı; HMAC test'i başarısız olduysa `webhook_hmac_invalid` görünür.

### 6.3 Metrik doğrulaması

`automation-service` metric endpoint'inden (`/metrics`) son `webhook_events_total{provider="jira",result="dispatched"}` sayacının arttığını gözle. `result="hmac_invalid"` sayacının test sırasında **artmaması** gerekir; arttıysa Adım 3.1.1 / 4.1.1'i tekrarla.

## 7. Troubleshooting

| Belirti | Olası neden | Aksiyon |
|---|---|---|
| HTTP 401 + audit `webhook_hmac_invalid` | UI'daki secret ile Vault'taki `secret_current` eşleşmiyor; veya rotation overlap penceresi 1 saatten kısa tutuldu ve eski secret silindi | Vault'taki `secret_current`'ı UI'daki secret ile yeniden senkronla; gerekirse Adım 3.1.1 / 4.1.1'i tekrarla. Rotation sırasında 1h pencereyi tam doldur. |
| HTTP 400 + audit `webhook_dept_unresolved` | Webhook payload'ındaki `project.key` veya `repository.workspace.slug` hiçbir dept'in `jira_project_keys[]` / `bitbucket_workspace`'i ile eşleşmiyor | `platform/config/departments.json` içinde ilgili dept'in alanlarını doğrula; mapping admin endpoint'i ile sync et (`POST /admin/departments/{id}/repo-mappings/sync`) |
| HTTP 200 + audit `loop_guard_dropped` | Event'in `actor.account_id`'si bir dept'in `bot.<service>.account_id`'sine eşit; bot kendi event'ini tetiklemiş | Aksiyon gerekmez; bu beklenen davranış. Tetikleyenin gerçekten bot olmadığını doğrulamak için `audit_events` tablosunda actor_account_id alanını incele. |
| HTTP 200 + audit `duplicate_event_dropped` | Aynı `delivery_id` (X-Atlassian-Webhook-Identifier veya X-Hook-UUID) `processed_events` tablosunda mevcut; provider retry yapmış | Aksiyon gerekmez; idempotency koruması aynı event ile tekrar workflow başlatılmasını engeller. |
| HTTP 200 + audit `comment_ignored_unauthorized_actor` | `jira:issue_commented` event'inin yazarı bot mention setinde değil ve iter > 1 | Aksiyon gerekmez; mention filter devrededir. Yorumcu bot'u mention etmeli (`@bot-username`) veya issue reporter olmalı. |
| Webhook UI'sında "delivery failed: timeout" | `automation-service` 500 ms'i aştı veya gateway down | Gateway loglarını incele; uzun süren işlem webhook handler içinde değil, workflow içinde yürütülmelidir. Activity'ye taşı. |
| Replay rejection (HTTP 401, audit `replay_window_exceeded`) | Bazı imza şemalarında timestamp pencereli replay koruması var; sistem saati senkron değil veya event çok eski | NTP doğrula; gerekirse provider ile senkronla. |
| Bitbucket DC `pr:opened` geliyor ama workflow başlamıyor | Event normalize tablosunda DC dialect kaydedilmemiş | Gateway loglarında `event_type` alanını incele; gerekirse `webhook_event_ignored` audit'lendi mi kontrol et. |

> **Genel kural:** Her HTTP 4xx yanıtı `audit_events` tablosunda neden ile birlikte kayıt bırakır. İlk debug adımı `audit_events`'in son 5 satırını sorgulamaktır.

## 8. Loop guard caveats

Webhook gateway'i, bot'un kendi event'leriyle sonsuz döngüye girmesini engellemek için iki kademeli bir loop guard uygular. Bu bölüm operatörlerin webhook'u kurarken dikkat etmesi gereken iki konuyu özetler.

### 8.1 Bot account ID'leri `departments.json`'da kayıtlı olmalı

Birinci kademe loop guard, gelen event'in `actor.account_id` (Jira) veya `actor.uuid` / `actor.account_id` (Bitbucket) alanını sistemdeki tüm departmanların `bot.<service>.account_id` setine karşı kontrol eder. Eşleşme varsa event `loop_guard_dropped` audit'i ile drop edilir.

Bu mekanizmanın çalışabilmesi için her dept'in `platform/config/departments.json` dosyasındaki `bot.jira.account_id` ve `bot.bitbucket.account_id` alanları **dolu** olmalıdır. Bu alanlar boş bırakılırsa bot kendi yorumlarını / kendi PR'larını kendi tetikler ve workflow sonsuz iter loop'una girer (MAX_ITER caps olsa bile, gereksiz LLM cost'u oluşur).

`departments.json` örneği:

```json
{
  "id": "payment",
  "bot": {
    "jira": {
      "account_id": "5fc9e78d2730890076b8e1f0",
      "username": "ai-bot-payment"
    },
    "bitbucket": {
      "account_id": "{abc12345-...}",
      "deployment": "cloud"
    }
  }
}
```

> **Doğrulama:** Yeni bir dept eklediğinde credential probe testi (`test_credential_probe.py`) bot account_id'nin Atlassian/Bitbucket'ta gerçekten resolve edildiğini ve `whoami` çağrısı ile dönen ID ile `departments.json`'daki değerin eşleştiğini doğrular. Probe başarısız olursa loop guard güvenilir çalışmaz.

### 8.2 Regex fallback for legacy installs without `account_id`

İkinci kademe loop guard, `actor.account_id` payload'da gelmediğinde (örn. legacy Jira Server kurulumlarında veya custom webhook proxy'lerinin alanı kırptığı durumlarda) devreye girer. Gateway, yorum gövdesini şu regex ile tarar:

```
^\s*\[bot:
```

Yani yorum baştan boşluklarla başlayıp ardından `[bot:` ön ekiyle devam ediyorsa event drop edilir (`loop_guard_regex_dropped` audit). Bu pattern, bot'un her yorumunun başına `[bot:summary]`, `[bot:needs_info]`, `[bot:explain]` gibi etiketler koyma kuralı ile birlikte çalışır.

> **Operatör için anlamı:**
>
> - Manuel olarak yazılan yorumlar **asla** `[bot:` ile başlamamalıdır; aksi halde yorum drop edilir ve workflow tetiklenmez.
> - Streamlit inline reply `[bot:hear]` etiketini kullanır, bu mention filter bypass içindir; loop guard regex'i `[bot:hear]` ile başlayan yorumları da drop eder - bunlar Streamlit tarafında **bot olmayan** kullanıcılar adına yazıldığı için account_id ile loop guard'a takılmazlar; yine de Streamlit'in bot account ID'sini kullanmaması ve etiketin `[bot:hear]` yerine alternatif bir prefix'e taşınması ileride değerlendirilebilir.
> - Modern Jira Cloud / Bitbucket Cloud kurulumları her zaman `account_id` gönderir; bu fallback yalnızca legacy DC kurulumları için geçerlidir.

### 8.3 Loop guard'ın atlanmaması gereken event tipleri

`pullrequest:fulfilled` (PR merge) event'i bot'un kendi PR'ını merge ettiğinde de tetiklenir. Bu event her zaman loop guard ile drop edilmelidir; hiçbir workflow type'ı `pullrequest:fulfilled`'a tepki olarak başlatılmaz. Webhook UI'sında bu trigger'ı işaretli bırakmak güvenlidir; gateway zaten bu event'i drop eder.

---

## İlgili Referanslar

- [`platform/config/departments.json`](../../config/departments.json) - `dept_id`, `jira_project_keys[]`, `bitbucket_workspace` alanları.
- [`platform/config/departments.schema.json`](../../config/departments.schema.json) - alanların tip ve constraint tanımları.
