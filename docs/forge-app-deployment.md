# Runbook: Forge Add-On Deployment (`AI Bot Task` issue type)

> **Audience:** Platform `admin` rolü; Atlassian organizasyon admin'i (Jira Cloud) ve Forge developer hesabı sahibi.
> **Scope:** [`platform/forge-app/`](../forge-app/) skeleton'ının Atlassian Forge platformuna deploy edilmesi ve `FEATURE_FLAG_FORGE_ADDON_ENABLED` opt-in flag'i ile devreye alınması.
> **Reversibility:** Tüm adımlar geri alınabilir; `forge install` `forge uninstall` ile geri alınır, flag `false`'a çekilir, runtime davranışı plain Markdown template'ine düşer.

## 1. Overview

Bu runbook, [`platform/forge-app/`](../forge-app/) altındaki Forge skeleton'ını bir Atlassian Cloud site'ına deploy etmek ve platformun runtime'da bu add-on'u kullanmasını sağlayacak `FEATURE_FLAG_FORGE_ADDON_ENABLED` flag'ini açmak için izlenecek **uçtan uca** prosedürdür.

Forge add-on, **AI Bot Task** adlı özel bir Jira issue type'ı ve beş zorunlu custom field (`AI Görev Tipi`, `Hedef Repo`, `Branch`, `Output Hedefi`, `Cleanup Policy`) sağlar. Bu sayede end-user'lar AI bot task'ı açarken eksik metadata bırakamaz; aksi halde varsayılan davranış [`platform/prompts/task_creation_assistant.md`](../prompts/task_creation_assistant.md) içindeki Markdown template'idir.

Kurulum kuralları:

- Forge add-on **opt-in**'dir; flag default `false`.
- Flag kapalıyken platform Forge'u **runtime'da kullanmaz**; chat assistant düz Markdown template'e fallback eder.
- Manifest yalnızca `read:jira-work` ve `write:jira-work` scope'larını ister; daha geniş scope eklemek manifest review gerektirir.
- `app.id` skeleton'da placeholder; gerçek değer `forge register` çıktısından `manifest.yml`'ye yazılır.

İlgili dosyalar:

- [`platform/forge-app/manifest.yml`](../forge-app/manifest.yml) - modül tanımları (`jira:issueType`, `jira:customField`, `function`).
- [`platform/forge-app/src/index.js`](../forge-app/src/index.js) - Forge function entrypoint (placeholder + `populateHedefRepo` resolver).
- [`platform/forge-app/package.json`](../forge-app/package.json) - `@forge/api` ve `@forge/cli` pin'leri.
- [`platform/forge-app/README.md`](../forge-app/README.md) - quick-start özeti (bu runbook'a link verir).

## 2. Prerequisites

| Öğe | Değer / Beklenti | Notlar |
|---|---|---|
| Operatör rolü | Platform `admin` + Atlassian site admin (Jira) + Forge developer hesabı | Forge developer hesabı: <https://developer.atlassian.com/console/myapps/> |
| Node.js | `>= 20.x` (LTS) | `forge-app/package.json` `engines.node` ile uyumlu |
| Forge CLI | Kurulu değil (Adım 3'te kurulur) | Versiyon `forge --version` ile doğrulanır |
| Atlassian site | `<your-instance>.atlassian.net` (Cloud) | Forge yalnızca Atlassian Cloud üzerinde çalışır; Jira Data Center desteklenmez |
| Jira ürünü | Hedef site'da Jira Software / Service Management aktif | Issue type yalnızca Jira ürünü olan site'lara install edilebilir |
| Postgres erişimi | `automation.feature_flags` tablosunu güncelleme yetkisi | Adım 5'te flag `true`'ya çekilir |
| Repo erişimi | `platform/forge-app/` dizinine `cd` edebilen bir geliştirme makinesi | İlk deploy için `forge register` `manifest.yml` dosyasını lokal olarak günceller; commit edilmelidir |

> **Cloud-only kısıtı:** Forge yalnızca Atlassian Cloud üzerinde çalışır. Jira Data Center kullanan kurulumlarda bu add-on devre dışıdır; flag `false` bırakılır ve [`task-creation-assistant-prompt.md`](task-creation-assistant-prompt.md) Markdown template'i kullanılmaya devam eder.

## 3. Forge CLI kurulumu ve authentication

Bu adımlar geliştirme makinesinde **bir kez** yapılır; sonraki deploy'larda atlanır.

### 3.1 CLI'yi kur

```bash
npm install -g @forge/cli
forge --version
```

Beklenen çıktı `@forge/cli/<x.y.z>` formatında bir versiyon satırı. `package.json` `devDependencies` bölümündeki `@forge/cli` pin'i (bkz. [`platform/forge-app/package.json`](../forge-app/package.json)) lokal CLI versiyonu ile **uyumlu** olmalıdır; major versiyon farkı varsa pin'i güncelleyip lockfile'ı tazele.

### 3.2 Atlassian developer hesabınla login ol

```bash
forge login
```

CLI seni interaktif olarak Atlassian email + API token sorar. API token <https://id.atlassian.com/manage-profile/security/api-tokens> üzerinden üretilir. Token üretildikten sonra:

- Token'ı parola yöneticisine kaydet; CLI bir kez aldıktan sonra `~/.forge/credentials` altında saklar.
- CI ortamında `FORGE_EMAIL` ve `FORGE_API_TOKEN` environment variable'ları üzerinden non-interaktif login mümkündür.

### 3.3 (İlk deploy için) `app.id` üret

Skeleton'daki `manifest.yml` `app.id` alanı placeholder olduğu için ilk deploy'dan önce gerçek bir Forge app id'si almak zorunludur:

```bash
cd platform/forge-app
forge register
```

CLI uygulama adını sorar (önerilen: `ai-bot-task-addon`). Komut başarıyla bittiğinde `manifest.yml` `app.id` alanı gerçek `ari:cloud:ecosystem::app/<uuid>` ile **lokal olarak** güncellenir.

> **Önemli:** `forge register` `manifest.yml`'yi diskte değiştirir. Değişikliği commit et:
>
> ```bash
> git add platform/forge-app/manifest.yml
> git commit -m "forge-app: register app id"
> ```
>
> Eğer `forge register` zaten çalıştırıldıysa (manifest'te placeholder UUID yerine gerçek bir UUID varsa) bu adım atlanır.

## 4. Deploy ve install

### 4.1 Deploy (development environment)

`forge-app/` dizininden:

```bash
cd platform/forge-app
forge deploy
```

`forge deploy` default olarak `development` ortamına bundle yükler. Beklenen çıktı:

```
✔ Deployed your app
Deployed to environment: development
```

Hata durumunda CLI manifest validation çıktısını gösterir; ilk olarak `permissions.scopes` ve `modules.function.handler` referanslarını gözden geçir.

> **Staging / production environment'lar:** Manifest'in `environment.definitions` listesi `development`, `staging`, `production` üçünü tanımlar (bkz. [`manifest.yml`](../forge-app/manifest.yml)). Staging veya production'a deploy etmek için:
>
> ```bash
> forge deploy --environment staging
> forge deploy --environment production
> ```
>
> Geçiş kuralı: `development` → `staging` → `production`, her aşamada smoke test (Adım 6) tamamlandıktan sonra ilerle.

### 4.2 Hedef site'a install et

`forge install` add-on'u belirli bir Atlassian Cloud site'ına bağlar:

```bash
forge install --site <your-instance>.atlassian.net --product jira
```

CLI interaktif olarak deploy edildiği environment'ı sorar (default `development`). İlk install'da Atlassian, `permissions.scopes` listesindeki scope'ları onayını ister; CLI çıktısında izin URL'sini açıp grant ettikten sonra install tamamlanır.

> **Site identifier:** `--site` parametresi tam domain'dir (`<your-instance>.atlassian.net`); `https://` prefix'i olmadan. Yanlış site verildiğinde CLI `Cannot find site` hatası döner.

İnstall sonrası site'da:

- Jira'da yeni issue oluşturma akışında `AI Bot Task` issue type'ı görünür.
- Bu type seçildiğinde 5 zorunlu custom field formu açılır.
- `Hedef Repo` dropdown'u şimdilik **boş** gelir (`populateHedefRepo` resolver'ı stub); dept config entegrasyonu follow-up task tarafından yapılacak.

### 4.3 Uninstall (rollback)

Bir site'dan add-on'u kaldırmak için:

```bash
forge uninstall --site <your-instance>.atlassian.net --product jira
```

Uninstall sonrası `AI Bot Task` issue type ve custom field'lar Jira UI'sından kaybolur. Rollback için manifest veya kod değişikliği gerekmez; sadece flag (Adım 5) `false`'a çekilir.

## 5. Opt-in flag - `FEATURE_FLAG_FORGE_ADDON_ENABLED`

Forge add-on'un site'a kurulu olması platformun onu **kullandığı** anlamına gelmez. Runtime davranışını `automation.feature_flags` tablosundaki `FEATURE_FLAG_FORGE_ADDON_ENABLED` satırı kontrol eder; default `false`.

| Flag durumu | Platform davranışı |
|---|---|
| `false` (default) | Chat assistant `task-creation-assistant-prompt.md` Markdown template'ini render eder; Forge add-on yok sayılır. |
| `true` | Chat assistant kullanıcıyı doğrudan Jira `AI Bot Task` issue type'ına yönlendirir; template render edilmez. |

### 5.1 Flag'i aç

```sql
-- automation Postgres veritabanında çalıştır
UPDATE automation.feature_flags
   SET enabled = true,
       updated_at = NOW(),
       updated_by = '<admin_username>'
 WHERE name = 'FEATURE_FLAG_FORGE_ADDON_ENABLED';
```

Beklenen: `UPDATE 1`. Flag tablosundaki satır yoksa (fresh kurulumda admin önce satırı insert eder), önce:

```sql
INSERT INTO automation.feature_flags (name, enabled, description, updated_by)
VALUES (
  'FEATURE_FLAG_FORGE_ADDON_ENABLED',
  false,
  'Forge add-on AI Bot Task issue type opt-in.',
  '<admin_username>'
)
ON CONFLICT (name) DO NOTHING;
```

Sonra UPDATE ile `true`'ya çek.

### 5.2 Flag UI üzerinden değiştirme (önerilen)

Üretim ortamında flag manipülasyonu admin-dashboard üzerinden yapılır (her değişim audit'lenir):

1. Admin dashboard → **Feature Flags** sayfasına git.
2. `FEATURE_FLAG_FORGE_ADDON_ENABLED` satırındaki toggle'ı `On` durumuna çevir.
3. Onay diyaloğunda değişikliğin runtime etkisini doğrula → `Save`.
4. Audit log'da `feature_flag_toggled` action'ı yazıldığını doğrula:
   ```bash
   psql -h <pg_host> -U postgres -d automation -c \
     "SELECT action, actor, details_json, created_at
        FROM audit_events
       WHERE action = 'feature_flag_toggled'
         AND details_json->>'flag_name' = 'FEATURE_FLAG_FORGE_ADDON_ENABLED'
       ORDER BY created_at DESC LIMIT 5;"
   ```

### 5.3 Flag'i kapat (rollback)

```sql
UPDATE automation.feature_flags
   SET enabled = false,
       updated_at = NOW(),
       updated_by = '<admin_username>'
 WHERE name = 'FEATURE_FLAG_FORGE_ADDON_ENABLED';
```

Veya UI üzerinden toggle'ı `Off` durumuna çevir. Flag `false`'a çekildikten sonra:

- Chat assistant bir sonraki kullanıcı talebinde Markdown template'ine fallback eder.
- Site'daki `AI Bot Task` issue type **olduğu yerde kalır** (Forge install'ı temizlemek için ayrıca Adım 4.3 gereklidir); ancak platform onu yok sayar.

> **Tam temizlik:** Flag kapatma + `forge uninstall` birlikte yapıldığında add-on tamamen devre dışı kalır. Flag kapatıp install'ı bırakmak güvenlidir; install kalsa bile platform runtime'da kullanmaz.

## 6. Doğrulama

Aşağıdaki smoke test'ler deploy + install + flag toggle dizisinin doğru çalıştığını uçtan uca doğrular.

### 6.1 Forge tarafı

1. Atlassian developer console (<https://developer.atlassian.com/console/myapps/>) → uygulamayı aç → **Distribution → Installations** sekmesi → hedef site `<your-instance>.atlassian.net` listede görünüyor.
2. Aynı sitedeki Jira'da **Create issue** ekranını aç → issue type listesinde `AI Bot Task` görünür.
3. `AI Bot Task`'ı seç → 5 zorunlu field formu render edilir (`AI Görev Tipi` dropdown'u 5 değer; `Branch` text default `develop`; `Cleanup Policy` default `delete_on_success`).

### 6.2 Platform tarafı (flag kapalı)

`FEATURE_FLAG_FORGE_ADDON_ENABLED=false` iken Streamlit chat'te yeni bir task talep et:

> "Yeni bir code change task'ı oluştur."

Beklenen: Asistan `task-creation-assistant-prompt.md` template'ini render eder; çıktı düz Markdown başlıkları içerir (`## Görev`, `## Hedef Repo`, vb.).

### 6.3 Platform tarafı (flag açık)

Adım 5.1 ile flag'i `true`'ya çektikten sonra aynı talebi tekrar gönder. Beklenen: Asistan kullanıcıyı Jira `AI Bot Task` issue type'ına yönlendiren bir mesaj döner (template render edilmez):

> "Bu departmanda 'AI Bot Task' issue type'ı kurulu. Jira'da yeni issue açarken type olarak onu seçin - zorunlu alanlar form olarak gelir."

(Tam mesaj için bkz. [`task-creation-assistant-prompt.md` § Forge Add-On (Opsiyonel)](task-creation-assistant-prompt.md).)

## 7. Troubleshooting

| Belirti | Olası neden | Aksiyon |
|---|---|---|
| `forge deploy` `Validation failed: app.id is invalid` | `manifest.yml` `app.id` placeholder UUID'sinde | Adım 3.3 ile `forge register` çalıştır; çıktıyı manifest'e yaz; commit et. |
| `forge install` `Cannot find site` | `--site` parametresi yanlış domain veya site Forge'a erişim vermemiş | Domain'i `https://` olmadan, `<sub>.atlassian.net` formatında ver; Atlassian admin'in **Connected apps** ekranında izin verdiğini doğrula. |
| `AI Bot Task` issue type Jira'da görünmüyor | Install development environment'da yapıldı, ama site staging/production environment kullanıyor | `forge install` `--environment <env>` parametresi ile uyumlu environment'a install et; veya `forge deploy --environment <env>` ile uygun bundle'ı yükle. |
| Flag `true` ama assistant hâlâ template render ediyor | Postgres satırı güncellendi, ama `automation-service` cache'i yenilenmedi (TTL 60 sn) | 60 sn bekle veya `automation-service` `/admin/feature-flags/refresh` endpoint'ini çağır. Audit'te `feature_flag_toggled` event'inin yazıldığını doğrula. |
| `Hedef Repo` dropdown'u boş | `populateHedefRepo` resolver henüz dept config'e bağlanmadı | Bu beklenen davranış. Follow-up work `automation-service`'in dept repo listesini Forge resolver'ına expose etmesini gerektirir. Geçici çözüm: kullanıcı `Hedef Repo`'yu boş bırakıp formu submit edemez; flag'i `false`'a çekerek Markdown template'e geç. |
| Forge function timeout (`exceeded 25 seconds`) | Resolver senkron HTTP çağrısı yapıyor (örn. dept config fetch) ve external service yavaş | Forge function'larının 25 sn'lik timeout'u vardır; uzun süren operasyonlar `automation-service`'e taşınmalı. Resolver yalnızca cache'lenmiş veriyi return eder. |
| `permissions.scopes` değişikliği install sırasında yeniden onay istemiyor | Site'daki install eski scope set'ini cache'liyor | `forge uninstall` + `forge install` döngüsü ile force re-grant yap. |

## 8. Operasyonel notlar

### 8.1 Manifest review hijyeni

`manifest.yml` `permissions.scopes` listesini tutuk tut. Yeni bir scope eklemek aşağıdaki kontrolleri tetikler:

- Atlassian platform tarafından scope review (özellikle `read:jira-user`, `read:account` gibi PII'ya dokunan scope'lar için).
- Kurulu site'larda yeniden consent prompt'u; kullanıcılar yeni izinleri grant etmezse add-on çalışmaz.
- Audit kaydı: scope değişimi her zaman PR description'ında gerekçeli açıklanmalı; review'cı scope-of-least-privilege kuralını doğrulamalı.

### 8.2 Custom field isimleri

5 custom field'ın isimleri (`AI Görev Tipi`, `Hedef Repo`, `Branch`, `Output Hedefi`, `Cleanup Policy`) platformun geri kalanı (özellikle [`task-creation-assistant-prompt.md`](task-creation-assistant-prompt.md) ve `automation-service` consumer'ları) ile **lockstep** olmalıdır. Manifest'te isim değişimi **her zaman** aynı PR'da diğer dosyalarla birlikte yapılır; aksi halde:

- Markdown template parser ile Forge field'ları arasındaki mapping kırılır.
- Workflow input'u eksik field hatası verir.

### 8.3 Versiyonlama

Forge add-on versiyonu `package.json` `version` alanı ile yönetilir. Major versiyon (`1.0.0` → `2.0.0`) breaking change (örn. custom field key rename) gerektirir; bu durumda `forge install` migration sihirbazı tetiklenir. Minor / patch deploy'ları (`0.1.0` → `0.1.1`) kullanıcı için saydam.

### 8.4 Flag-gated başlatma

`automation-service` ve `automation-service`'in tükettiği consumer'lar `FEATURE_FLAG_FORGE_ADDON_ENABLED` flag'ine `services.manifest.json` `feature_flag_dependency` üzerinden bağlanmaz; flag yalnızca **runtime** davranışını değiştirir, servis lifecycle'ı etkilemez.

---

## İlgili Referanslar

- [`platform/forge-app/manifest.yml`](../forge-app/manifest.yml) - modül tanımları.
- [`platform/forge-app/README.md`](../forge-app/README.md) - quick-start özeti (bu runbook'a link verir).
- [`platform/prompts/task_creation_assistant.md`](../prompts/task_creation_assistant.md) - flag kapalı default davranış (Markdown template).
