# Runbook: Departman Decommission

> **Audience:** Platform `admin` rolü; gerektiğinde DBA + DevSecOps eşliğinde.
> **Scope:** Bir departmanın (`dept_id`) Compose stack'inden ve veri/kimlik altyapısından **kalıcı olarak** kaldırılması.
> **Reversibility:** Adım 1 ve 2 geri alınabilir; Adım 3 (Vault revoke) ve Adım 4 (DB silme) **yıkıcıdır** ve geri alınamaz.

## Ne zaman kullanılır

Bu runbook aşağıdaki durumlarda izlenir:

- Bir departman organizasyondan ayrıldı veya proje sonlandırıldı; bot hesapları, Atlassian credential'ları ve audit dışı tüm artifact'ler kaldırılacak.
- Bir departman geçici olarak duraklatılacak ama ileride tekrar açılabilir  **yalnızca Adım 1**'i uygula, Adım 2-4'ü atla.
- Bir departman yanlış konfigürasyonla oluşturuldu (yanlış `dept_id`, yanlış `bitbucket_workspace` vb.) ve sıfırdan kurulacak  tüm 4 adım uygulanır.

## Ön gereksinimler

| Öğe | Değer | Kaynak |
|---|---|---|
| Operatör rolü | `admin` (global) | OIDC IdP - `auth-shared.policy` |
| Erişim | `automation-service` HTTP'si, Postgres `automation` DB, Vault root/admin token | `.env` veya 1Password vault |
| Hedef departman | `dept_id` (örn. `marketing`) | `platform/config/departments.json` |
| Iletişim | Departman temsilcisi onayı (Adım 4 öncesi yazılı) | Slack/E-posta arşiv |
| Yedekleme | Postgres logical dump (`pg_dump --schema=public --table=departments --table=audit_events`) | Adım 4 öncesi zorunlu |

> **Audit beklentisi:** Her HTTP çağrısı `audit_events` tablosuna `actor_role=admin`, `dept_id=<hedef>` ile kaydedilir. Manuel SQL aksiyonlarında `audit_events` satırını **manuel** ekle.

## Genel Akış (özet)

```
┌─────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ 1. mode=disabled set│───▶│ 2. Açık workflow     │───▶│ 3. Vault credential  │───▶│ 4. DB kaydı silme    │
│   (geri alınabilir) │    │    drain (gözlem)    │    │    revoke (yıkıcı)   │    │   (yıkıcı)           │
└─────────────────────┘    └──────────────────────┘    └──────────────────────┘    └──────────────────────┘
```

Her adımdan sonra **doğrulama (verify)** alt-adımı **zorunludur**. Doğrulama başarısız olursa sonraki adıma geçme.

---

## Adım 1 - `mode=disabled` set

**Amaç:** Departman için yeni workflow başlatılmasını durdur; mevcut açık workflow'ların doğal olarak tamamlanmasına izin ver. Bu adım **geri alınabilir**: `mode=active` yazılırsa departman tekrar aktif hale gelir.

### 1.1 Action

`automation-service`'in idari endpoint'ini çağır:

```bash
curl -X POST "https://<automation-service>/admin/departments/<dept_id>/disable" \
  -H "Authorization: Bearer <oidc_admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"reason": "decommission per ticket OPS-1234"}'
```

Beklenen yanıt:

```json
HTTP/1.1 200 OK
{ "id": "<dept_id>", "mode": "disabled", "disabled_at": "2025-01-15T10:00:00Z" }
```

> **Alternatif** (admin-dashboard-ui üzerinden): **Departments  `<dept_id>`  Actions  Disable** butonu. Aynı endpoint'i proxy'ler.

Endpoint'in iki yan etkisi vardır:

1. `departments.mode` sütununu `"disabled"` olarak günceller.
2. `audit_events` tablosuna `action="dept_disabled"`, `actor_role="admin"` event'i yazar.

### 1.2 Verify

```bash
# DB kaydı disabled mı?
psql -h <pg_host> -U postgres -d automation \
  -c "SELECT id, mode FROM departments WHERE id = '<dept_id>';"
# beklenen: mode = 'disabled'

# Audit event yazıldı mı?
psql -h <pg_host> -U postgres -d automation \
  -c "SELECT action, actor_role, created_at FROM audit_events
      WHERE dept_id = '<dept_id>' AND action = 'dept_disabled'
      ORDER BY created_at DESC LIMIT 1;"
# beklenen: en az bir satır

# Yeni workflow start denemesi reddediliyor mu? (smoke)
# Test webhook'unu Jira'da tetikle (veya curl ile sahte event):
curl -X POST "https://<automation-service>/webhooks/jira/issue_created" \
  -H "X-Hub-Signature-256: <hmac>" -H "Content-Type: application/json" \
  -d '<test_event_for_dept>'
# beklenen: HTTP 202 + decision="denied" (gövdede dept_disabled gerekçesi)
```

### 1.3 Rollback (yalnız Adım 1)

```bash
# Yanlışlıkla disable edildiyse:
curl -X POST "https://<automation-service>/admin/departments/<dept_id>/enable" \
  -H "Authorization: Bearer <oidc_admin_token>"
# (veya admin-dashboard-ui üzerinden Mode = active set)
```

> Adım 2'ye geçtikten sonra rollback **mümkündür** ama açık workflow'lar `cancel` edildiyse onları yeniden başlatmak gerekir.

---

## Adım 2 - Açık workflow drain

**Amaç:** Departmana ait Temporal workflow'larının (a) doğal olarak tamamlanmasını beklemek veya (b) zaman aşımına uğradıysa zorla iptal etmek. Hiçbir workflow `running` durumda bırakılmaz.

> **Önemli:** Adım 1 yeni start'ları engeller; Adım 2 mevcut açık execution'ları kapatır. İki adım birlikte "kuyruğun boşaltılması" anlamına gelir.

### 2.1 Açık workflow'ları listele

Temporal CLI ile:

```bash
# tüm açık (Running) workflow'lar
tctl --address <temporal_host>:7233 \
  workflow list \
  --query "ExecutionStatus='Running' AND DeptId='<dept_id>'"
```

Veya Temporal Web UI: `https://<temporal-web>/namespaces/<ns>/workflows?status=Running&query=DeptId%3D'<dept_id>'`.

> **Not:** `DeptId` search attribute'ünün indekslenmiş olduğunu varsayar. İndekslenmediyse `WorkflowId STARTS_WITH '<dept_id>-'` sorgusunu kullan; workflow_id konvansiyonu için bkz. `temporal_shared.identifiers`.

### 2.2 Drain stratejisi seç

| Strateji | Ne zaman? | Aksiyon |
|---|---|---|
| **Doğal drain** | Açık workflow ≤ 5 ve hepsi 30 dk içinde bitecek | Bekle; 30 dk sonra Adım 2.3'e geç |
| **Hızlandırılmış drain** | Açık workflow > 5 veya uzun süreli (örn. `bot_branch_retention` cron) | `tctl workflow signal` ile `drain` sinyali yolla; workflow'lar checkpoint'te biter |
| **Zorla iptal** | Operasyon penceresi sınırlı veya workflow takılı | `tctl workflow cancel`; risk: yarım iş; idempotency telafi eder |

### 2.3 Doğal drain

```bash
# her ~5 dk bir yeniden say:
watch -n 300 "tctl --address <temporal_host>:7233 workflow list \
  --query \"ExecutionStatus='Running' AND DeptId='<dept_id>'\" | wc -l"
```

Sayı `0` olduğunda Adım 2.5'e geç.

### 2.4 Hızlandırılmış drain (opsiyonel)

```bash
# her açık workflow'a drain sinyali yolla (workflow side'da implemente edilmiş olmalı):
tctl --address <temporal_host>:7233 workflow list \
  --query "ExecutionStatus='Running' AND DeptId='<dept_id>'" \
  --output json | jq -r '.executions[].execution.workflowId' | while read wid; do
    tctl --address <temporal_host>:7233 workflow signal \
      --workflow_id "$wid" --name "drain" --input '{}'
  done
```

Veya zorla iptal (son çare):

```bash
tctl --address <temporal_host>:7233 workflow list \
  --query "ExecutionStatus='Running' AND DeptId='<dept_id>'" \
  --output json | jq -r '.executions[].execution.workflowId' | while read wid; do
    tctl --address <temporal_host>:7233 workflow cancel --workflow_id "$wid"
  done
```

> **Audit:** Her cancel `automation-service` üzerinden değil doğrudan Temporal CLI ile yapıldıysa, manuel olarak `audit_events`'e `action="workflow_cancelled_admin"`, `payload={workflow_id, reason}` yaz.

### 2.5 Verify

```bash
# 0 açık workflow olmalı
tctl --address <temporal_host>:7233 workflow list \
  --query "ExecutionStatus='Running' AND DeptId='<dept_id>'"
# beklenen: empty

# son 24 saatlik tamamlanma sayısı (sanity check)
tctl --address <temporal_host>:7233 workflow list \
  --query "ExecutionStatus IN ('Completed','Failed','Canceled','Terminated') \
           AND DeptId='<dept_id>' AND CloseTime > '2025-01-14T10:00:00Z'"
```

> **Stop & ask:** Hala çalışan workflow varsa veya drain sinyali yanıt vermiyorsa, workflow sahiplerine başvur; Adım 3'e **geçme**.

---

## Adım 3 - Vault credential revoke

**Amaç:** Departman bot'larına ait tüm Atlassian credential'larını ve webhook secret'larını Vault'tan **kalıcı olarak** silmek. Bu adımdan sonra credential `read(...)  not_found` döner; rotation overlap penceresi de geçersiz olur.

>  **Yıkıcı.** Aşağıdaki path'lere yazılı tüm secret değerleri silinir; geri yüklemek için yedek + Vault audit log gerekir. Adım 1 ve 2 doğrulanmadan bu adıma geçme.

### 3.1 Silinecek path'leri belirle

Departman Vault path konvansiyonu:

| Path | İçerik |
|---|---|
| `vault:atlassian/<dept_id>/jira` | Jira bot credential'ı |
| `vault:atlassian/<dept_id>/bitbucket` | Bitbucket bot credential'ı |
| `vault:atlassian/<dept_id>/confluence` | Confluence bot credential'ı |
| `vault:webhooks/jira/<dept_id>` | Jira webhook HMAC secret (mevcut + previous) |
| `vault:webhooks/bitbucket/<dept_id>` | Bitbucket webhook HMAC secret (mevcut + previous) |
| `vault:webhooks/confluence/<dept_id>` | Confluence webhook HMAC secret (varsa) |

> Per-user oturum credential'ları (`vault:atlassian/_user_session/<session_id>/...`) departman bazında değildir; bu runbook'un kapsamı dışındadır ve TTL ile otomatik süresi dolar.

### 3.2 Yedekle (zorunlu)

```bash
# Tüm dept path'lerini bir tek dosyaya export et (production yedeği için):
mkdir -p /tmp/vault-backup-<dept_id>
for path in \
  "atlassian/<dept_id>/jira" \
  "atlassian/<dept_id>/bitbucket" \
  "atlassian/<dept_id>/confluence" \
  "webhooks/jira/<dept_id>" \
  "webhooks/bitbucket/<dept_id>" \
  "webhooks/confluence/<dept_id>"; do
    vault kv get -format=json -mount=secret "$path" > "/tmp/vault-backup-<dept_id>/$(echo $path | tr '/' '_').json" 2>/dev/null || true
done

# Bu yedek dosyasını şifreli dış kasaya (1Password / AWS Secrets Manager) yükle.
# Onay alındıktan sonra /tmp/vault-backup-<dept_id> klasörünü güvenli sil.
```

### 3.3 Atlassian credential'larını revoke

> **Önce upstream'de revoke et:** Atlassian token'ı Vault'tan silmek **kullanım yetkisini iptal etmez**; Atlassian tarafında token revoke gereklidir. Aksi halde token süresi dolana kadar başka yerden kullanılabilir.

1. Atlassian Admin Console  Bots / Service accounts  `<dept_id>` botu  **Revoke API token**.
2. Bot Atlassian hesabını gerekirse devre dışı bırak (account suspension).

### 3.4 Vault path'lerini sil

```bash
# automation-service'in admin endpoint'i tercih edilen yoldur (audit otomatik yazılır):
curl -X POST "https://<automation-service>/admin/departments/<dept_id>/credentials/revoke" \
  -H "Authorization: Bearer <oidc_admin_token>"
# beklenen: HTTP 200, gövdede silinen path listesi
```

Endpoint mevcut değilse veya CLI tercih ediliyorsa, doğrudan Vault CLI:

```bash
for path in \
  "atlassian/<dept_id>/jira" \
  "atlassian/<dept_id>/bitbucket" \
  "atlassian/<dept_id>/confluence" \
  "webhooks/jira/<dept_id>" \
  "webhooks/bitbucket/<dept_id>" \
  "webhooks/confluence/<dept_id>"; do
    # KV v2: hem mevcut hem geçmiş versiyonları kalıcı sil
    vault kv metadata delete -mount=secret "$path"
done
```

> **KV v2 davranışı:** `vault kv delete` yalnızca mevcut versiyonu işaretler; **geçmiş silmek için `metadata delete` zorunludur**. Aksi halde rotation overlap için tutulan eski versiyon hala okunabilir kalır.

### 3.5 Verify

```bash
# Path'lerin tamamen silindiğini doğrula
for path in \
  "atlassian/<dept_id>/jira" \
  "atlassian/<dept_id>/bitbucket" \
  "atlassian/<dept_id>/confluence" \
  "webhooks/jira/<dept_id>" \
  "webhooks/bitbucket/<dept_id>" \
  "webhooks/confluence/<dept_id>"; do
    echo "=== $path ==="
    vault kv get -mount=secret "$path" 2>&1 | grep -E "(No value|404|not found)" \
      || echo " STILL PRESENT - DO NOT PROCEED"
done
# beklenen: tüm path'ler için "No value found" / 404
```

```bash
# automation-service log'u: kalan referans var mı?
kubectl logs deploy/automation-service --since=10m | grep -i "<dept_id>" | grep -i "vault" \
  | grep -vi "redacted"
# beklenen: yalnızca redaction filter işaretlemesi veya hiç satır yok
```

> **Stop & ask:** Hala bir path okunabilirse, rotation overlap penceresinde olabilir; 1 saat bekle ve `metadata delete`'i tekrar çalıştır. Hala çözülmüyorsa Vault admin'e başvur.

---

## Adım 4 - DB kaydı silme

**Amaç:** `departments` tablosundan kaydı kaldırmak. Audit izi (`audit_events`) **silinmez**; uyumluluk için saklanır.

>  **Yıkıcı ve geri alınamaz.** Adım 1, 2, 3 hepsi doğrulanmadan bu adıma geçme. Yedek (Adım 4.2) zorunludur.

### 4.1 Bağımlı kayıtları kontrol et

```sql
-- Açık workflow referansı kalmış mı? (Adım 2 doğrulaması)
SELECT count(*) FROM audit_events
WHERE dept_id = '<dept_id>' AND created_at > now() - interval '1 hour'
  AND action IN ('webhook_received','workflow_started','capability_denied');
-- beklenen: 0 (son 1 saatte aktivite yok)

-- probe_artifacts orphan kalmış mı? (cleanup gerekir)
SELECT id, artifact_type, state FROM probe_artifacts
WHERE dept_id = '<dept_id>' AND state = 'partial_orphan';
-- varsa: önce admin-dashboard'da "Cleanup" yaparak temizle
```

### 4.2 Yedek

```bash
# Tabloya özel logical dump:
pg_dump -h <pg_host> -U postgres -d automation \
  --table=public.departments \
  --data-only --inserts \
  --where="id = '<dept_id>'" \
  --file=/tmp/dept-backup-<dept_id>-departments.sql

# audit kayıtlarının da yedeği (silmiyoruz ama referans için):
pg_dump -h <pg_host> -U postgres -d automation \
  --table=public.audit_events \
  --data-only \
  --where="dept_id = '<dept_id>'" \
  --file=/tmp/dept-backup-<dept_id>-audit.sql

# Yedek dosyalarını şifreli arşive yükle, dosyaları güvenli sil.
```

### 4.3 Sil

```sql
-- TEK transaction içinde sil; final audit event'ini ekle.
BEGIN;

-- RLS aktif olduğundan superuser veya BYPASSRLS rolü ile bağlan
SET LOCAL app.current_role = 'admin';
SET LOCAL app.current_dept_id = '<dept_id>';

-- (a) son audit event yaz (silmeden önce, FK kısıtı yok ama referans için)
INSERT INTO audit_events (actor_id, actor_role, dept_id, action, resource, result, payload)
VALUES (
  '<operator_user_id>',
  'admin',
  '<dept_id>',
  'dept_decommissioned',
  'departments/<dept_id>',
  'success',
  '{"runbook":"dept-decommission.md","ticket":"OPS-1234"}'::jsonb
);

-- (b) kayıt silme
DELETE FROM departments WHERE id = '<dept_id>';
-- beklenen: DELETE 1

-- (c) probe_artifacts varsa cascade silinmez; manuel
DELETE FROM probe_artifacts WHERE dept_id = '<dept_id>';

COMMIT;
```

> **Audit kayıtlarını silme.** `audit_events` tablosuna ait satırlar hukuki ve forensic gereklilikle saklanır. Bu adımda yalnızca `departments` ve `probe_artifacts` etkilenir.

### 4.4 `departments.json` config dosyasından çıkar

```bash
# repo'da:
# 1. platform/config/departments.json dosyasından <dept_id> entry'sini kaldır.
# 2. PR aç: "decommission: <dept_id>"
# 3. CI doğrulama (schema validation, property tests) yeşil olduğunda merge et.
# 4. Merge sonrası automation-service redeploy edilir; servis schema validation'ı yine geçer.
```

> **Boot invariant:** `departments.json` schema validation'ı boot zamanında çalışır. Config dosyasından bir entry silmek tek başına yeterlidir; DB ve Vault hali zaten temiz olduğundan stale referans riski yoktur.

### 4.5 Verify

```sql
-- Kayıt gitti mi?
SELECT id FROM departments WHERE id = '<dept_id>';
-- beklenen: 0 satır

-- Final audit event yazıldı mı?
SELECT action, result, created_at FROM audit_events
WHERE dept_id = '<dept_id>' AND action = 'dept_decommissioned'
ORDER BY created_at DESC LIMIT 1;
-- beklenen: 1 satır, result = 'success'

-- probe_artifacts temiz mi?
SELECT count(*) FROM probe_artifacts WHERE dept_id = '<dept_id>';
-- beklenen: 0
```

```bash
# Servis kendi durumunu yeniden doğrulasın (departments.json içermiyor):
curl -s "https://<automation-service>/healthz"
# beklenen: 200 OK; departman hala referans veriliyorsa boot fail

# admin-dashboard-ui departments listesi:
# beklenen: <dept_id> görünmemeli
```

---

## Sonrası: temizlik kontrol listesi

- [ ] `services.manifest.json` - eğer dept-bağlı bir entry varsa kaldırıldı.
- [ ] `.env`, `.env.example` - `<DEPT>_*` placeholder'ları temizlendi (varsa).
- [ ] Atlassian Admin: bot kullanıcısı suspended/deleted; webhook endpoint'leri Atlassian tarafında iptal edildi.
- [ ] Slack/PagerDuty: departmana özel alert kanalları (varsa) arşivlendi.
- [ ] OPS ticket (OPS-1234) - runbook'taki tüm Verify çıktıları PR/ticket comment'ine eklendi.
- [ ] Yedek dosyaları (`/tmp/dept-backup-*`, `/tmp/vault-backup-*`) güvenli arşive taşındı; lokal kopyalar shred ile silindi.

## Sorun giderme

| Belirti | Olası neden | Çözüm |
|---|---|---|
| Adım 1: HTTP 403 | Operatör `admin` değil | `dept_admin` self-disable yapamaz; global `admin` kullan |
| Adım 1: HTTP 404 | `dept_id` yanlış | `SELECT id FROM departments` ile doğrula |
| Adım 2: Workflow takılı (running > 1h) | Activity heartbeat fail veya retry loop | Workflow tasarımına bak; gerekirse `cancel` ile zorla bitir |
| Adım 3: Vault path silinemedi | KV v2 metadata kaldı | `vault kv metadata delete` (sadece `delete` değil) |
| Adım 3: Atlassian token hala çalışıyor | Atlassian-side revoke yapılmadı | Atlassian Admin Console üzerinden token revoke et |
| Adım 4: `DELETE 0 rows` | Daha önce silinmiş veya RLS engelliyor | `app.current_role=admin` set edildi mi? Superuser ile dene |
| Adım 4: Boot fail (`automation-service` start) | `departments.json`'da kalan referans | Config dosyasını da güncellediğinden emin ol (4.4) |

