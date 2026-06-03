# Runbook: Getting Started — İlk Açılış Akışı

> **Audience:** Platform `admin` rolü (yeni kurulumda first operator); CI / debug senaryolarında DevOps.
> **Scope:** `platform/` deposunu klonlayan bir operatörün, makineyi sıfırdan ayağa kaldırıp ilk task'ı açabilir hale gelmesi. Compose stack'in **bootstrap-only** (admin-dashboard + bağımlılıkları) modda başlatılması, admin-dashboard üzerinden Setup Wizard akışı, ardından kalan servislerin sırayla aktive edilmesi.
> **Reversibility:** Tüm adımlar geri alınabilir; `make down` ile tüm container'lar durur (volume'lar korunur).

## 1. Overview

Platform iki açılış moduna sahiptir:

- **Bootstrap-only (default — production-go şartı):** `make boot` yalnızca dört bootstrap servisini açar — `postgres`, `vault`, `admin-dashboard-api`, `admin-dashboard-ui`. Geri kalan tüm servisler (`atlassian-mcp`, `automation-service`, `assistant-service`, `agent-runner-worker`, `execution-runner-worker`, `firecrawl`, `streamlit-ui`, vb.) `profiles:` anahtarı ile gated'dir ve admin-dashboard'daki **Setup Wizard** üzerinden kullanıcı kontrolü ile açılır. Bu, ilk açılışta operatörün credential'ları girmeden 12 servisi paralel başlatma yükünü ve "yanlış sırada başlatma" risklerini ortadan kaldırır.
- **Tüm servisler (CI / debug):** `make up-all` `config/services.manifest.json` dosyasından `--profile` listesini türetip her şeyi tek seferde açar. Yalnızca CI testlerinde, smoke test'lerde veya tüm stack'i hızlıca debug ederken kullanılır.

Bu runbook **bootstrap-only** akışını kanonik kabul eder; `make up-all` yalnızca §6'da bir alternatif olarak tanımlanır.

İlgili referanslar:

- [`platform/Makefile`](../../Makefile) — `boot`, `up`, `up-all` hedeflerinin tanımı.
- [`platform/scripts/up.sh`](../../scripts/up.sh) ve [`platform/scripts/up.ps1`](../../scripts/up.ps1) — make olmayan host'lar için eşdeğer wrapper'lar.
- [`platform/infra/docker-compose.yml`](../../infra/docker-compose.yml) — boot bundle servisleri (`profiles:` anahtarı taşımayanlar) ve profile-gated servisler.
- [`platform/config/services.manifest.json`](../../config/services.manifest.json) — profile listesinin single source of truth'u.

## 2. Prerequisites

| Öğe | Değer | Doğrulama |
|---|---|---|
| Docker Engine + Compose v2 | `docker --version`, `docker compose version` | Compose v2 plugin gerekli (`docker-compose` v1 binary yetersiz) |
| Make (opsiyonel) | GNU make | `make --version`. Yoksa `scripts/up.sh` (Linux/macOS) veya `scripts/up.ps1` (Windows) kullanılır. |
| Python 3.12+ | `python --version` veya `python3 --version` | Makefile profile listesini manifest'ten türetmek için kullanır (`PY=python` Windows default; `PY=python3` Linux/macOS override) |
| Disk alanı | ≥ 5 GB | `postgres`, `vault`, build cache için |
| `.env` dosyası | `platform/.env` (yoksa `.env.example`'dan kopyala) | `cp .env.example .env` (POSIX) veya `Copy-Item .env.example .env` (PS) |
| Açık portlar | 3000 (admin-dashboard-ui), 8082 (admin-dashboard-api), 5432 (postgres), 8200 (vault) | `netstat -an \| grep <port>` ile çakışma yok |

> **`.env` notu:** Boot bundle'ın çalışması için `.env`'de `POSTGRES_PASSWORD`, `VAULT_TOKEN`, `NEXT_PUBLIC_ADMIN_API_BASE_URL` gibi anahtarların `.env.example`'daki default'larla bırakılması yeterlidir; ileri konfigürasyon Setup Wizard içinde yapılır.

## 3. İlk açılış akışı (boot bundle)

Aşağıdaki sıra **kanonik first-time setup**'tır. Adımlar sırayla izlenir; bir adımın `verify` alt-bölümü başarısız olursa sonraki adıma geçme.

### 3.1 Boot bundle'ı başlat

`platform/` dizinine git ve sadece bootstrap servisleri başlat:

```bash
# POSIX (Linux/macOS)
cd platform
make boot

# Windows (make yoksa)
cd platform
.\scripts\up.ps1 boot
```

Beklenen davranış: **dört container** çalışır durumda olur — başka servis açılmaz.

#### 3.1.1 Verify

```bash
docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml ps
```

Beklenen çıktı (kısaltılmış):

```
NAME                    SERVICE                 STATUS    PORTS
platform-postgres-1     postgres                Up        0.0.0.0:5432->5432/tcp
platform-vault-1        vault                   Up        0.0.0.0:8200->8200/tcp
platform-admin-...-api  admin-dashboard-api     Up        0.0.0.0:8082->8082/tcp
platform-admin-...-ui   admin-dashboard-ui      Up        0.0.0.0:3000->3000/tcp
```

`atlassian-mcp`, `automation-service`, `agent-runner-worker`, `execution-runner-worker`, `assistant-service`, `firecrawl`, `opencode-sidecar`, `streamlit-ui` listede **görünmemelidir** — bu servisler profile-gated'tir ve henüz açılmamıştır.

> **Eğer profile-gated bir servis listede görünüyorsa:** `make boot` yerine yanlışlıkla `make up-all` çağırmış olabilirsin. Önce `make down` ile kapat, sonra `make boot` ile tekrar dene.

### 3.2 Admin-dashboard'ı tarayıcıdan aç

```
http://localhost:3000
```

Beklenen davranış: Setup Wizard ekranı açılır; sol kenarda yedi adım (`vault`, `postgresql`, `temporal`, `mcp_server`, `workers`, `services`, `add_first_department`) listelenir; ilk adım (`vault`) `pending` durumdadır.

> **Eğer "Cannot connect" hatası geliyorsa:**
>
> - `docker compose ... ps` çıktısında `admin-dashboard-ui` STATUS sütunu `Up` ve `healthy` mi? Healthcheck şu URL'i probe eder: `http://localhost:3000/api/health`.
> - Henüz `Up` ama `health: starting` ise birkaç saniye bekle (Next.js ilk build'inden sonra hazır olur).
> - Başka bir uygulama 3000 portunu tutuyor olabilir; `lsof -iTCP:3000` veya `netstat -ano | findstr 3000` ile çakışmayı kontrol et.

### 3.3 Setup Wizard akışı

Setup Wizard, Compose stack'in geri kalanını **kullanıcı onayıyla** ve doğru sırayla aktive eder. Her adım Compose'a bir veya birden çok `--profile` flag'i ekleyerek ilgili servisi ayağa kaldırır; ardından servisin healthcheck'i çalışır ve `automation.setup_wizard_state` tablosuna kaydedilir.

| Adım | Aktive olan servis(ler) / Kaynak | Adım nedir |
|---|---|---|
| 1. `vault` | `vault` (zaten boot bundle'da `Up`) | Initial token konfigürasyonu, KV v2 mount kontrolü, secret path izinleri. |
| 2. `postgresql` | `postgres` (zaten `Up`) | `automation` schema migration'larının uygulanması; `automation.departments`, `automation.audit_events` ve diğer tabloların yaratılması. |
| 3. `temporal` | `temporal` profile'ı (`temporal-server`, `temporal-ui`) | Temporal server (7233) ve UI (8233) ayağa kalkar; namespace `default` register edilir. |
| 4. `mcp_server` | `atlassian-mcp` profile'ı | MCP gateway (8090) ayağa kalkar; canlılık probe'u yeşil olur. |
| 5. `workers` | `automation-worker`, `agent-runner-worker`, `execution-runner-worker` profile'ları | Worker'lar Temporal'a register olur; task queue'lar listelenebilir. |
| 6. `services` | `automation-service`, `assistant-service`, `streamlit-ui`, `firecrawl` (opsiyonel) profile'ları | HTTP servisler ayağa kalkar; healthcheck'ler yeşil olur. |
| 7. `add_first_department` | Yeni dept oluşturma akışı | Operatör bir dept ekler, bot credential'ları girer, connectivity probe çalışır; en az bir aktif dept commit edildiğinde wizard tamamlanır. |

Her adımda UI:

1. Adım başlığı + kısa açıklama gösterir.
2. **"Bu Adımı Çalıştır"** butonu basıldığında ilgili profile(s) için `compose up -d --profile <p>` çağrılır.
3. Servis healthcheck'i yeşile dönene kadar progress bar ilerler; başarısız olursa hata detayı + retry butonu sunulur.
4. Başarılı olduğunda otomatik olarak sonraki adıma geçer.

> **Adım 7 (`add_first_department`) detayları:** Operatör "Yeni Departman Ekle" modal'ında dept ID, Atlassian/Bitbucket bot credential'larını girer; UI inline probe çalıştırır (Atlassian `/myself` ve Bitbucket `/user`). Probe yeşilse dept commit edilir. Akış sonunda Setup Wizard "Tamamlandı" ekranına döner ve iki çağrı butonu sunar: **"Streamlit'i Aç"** (ilk task'ı oraya açmak için) ve **"Servisleri Yönet"** (admin services panel'ine git).

### 3.4 Verify (full setup)

Setup Wizard "Tamamlandı" ekranına vardığında:

```bash
# Tüm servisler Up ve healthy mi?
docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml ps

# Setup state Postgres'e yazıldı mı?
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U postgres -d automation \
  -c "SELECT step_name, status FROM automation.setup_wizard_state ORDER BY step_name;"
```

Beklenen: yedi adımın hepsi `status='completed'`.

İlk task akışını test etmek için **Streamlit**'i (`http://localhost:8501`) aç ve Task Creator sayfasından bir task gönder.

## 4. Yaygın komutlar

| Komut | Davranış |
|---|---|
| `make boot` | Boot bundle'ı başlat (default — postgres, vault, admin-dashboard). |
| `make up` | `make boot` alias'ı (geriye dönük uyum). |
| `make ps` | Çalışan servisleri listele (boot bundle'ı ve aktif profile'ları içerir). |
| `make logs` | Aktif servislerin log'larını tail et (`Ctrl-C` ile çık). |
| `make down` | Tüm container'ları durdur (volume'lar korunur). |
| `make restart` | `make down` + `make boot` (tek komutla yeniden başlatma). |
| `make profiles` | Manifest'ten türetilen profile listesini yazdır. |
| `make help` | Tüm hedeflerin açıklamasını göster. |

`make` olmayan host'larda (Windows default'u) eşdeğer komutlar:

```powershell
.\scripts\up.ps1 boot
.\scripts\up.ps1 ps
.\scripts\up.ps1 down
.\scripts\up.ps1 logs
.\scripts\up.ps1 restart
.\scripts\up.ps1 profiles
```

veya POSIX shell'de:

```bash
./scripts/up.sh boot
./scripts/up.sh ps
./scripts/up.sh down
./scripts/up.sh logs
./scripts/up.sh restart
./scripts/up.sh profiles
```

## 5. Troubleshooting

| Belirti | Olası neden | Aksiyon |
|---|---|---|
| `make boot` sonrası 5'ten fazla servis listede | Yanlışlıkla `make up-all` çağrıldı veya eski stack hâlâ ayakta | `make down` çalıştır, sonra `make boot` ile sadece bootstrap servisleri aç. |
| `admin-dashboard-ui` `health: starting` durumundan çıkmıyor | Next.js ilk build cache'i olmadığı için 30-60 sn sürebilir | Bekle; 2 dakikadan uzun sürerse `docker compose logs admin-dashboard-ui` ile build hatasını incele. |
| Setup Wizard adım 4 (`mcp_server`) `failed` | `atlassian-mcp` profile'ı açılırken healthcheck timeout (`MCP gateway 8090 not responding`) | `docker compose logs atlassian-mcp` — Atlassian credential'ları `.env`'de eksik olabilir; `MCP_BASE_URL` ve ilgili token'ları doldur, ardından retry. |
| Setup Wizard adım 7 (`add_first_department`) `Probe failed` | Bot Atlassian credential'ı geçersiz veya `account_id` resolve edilemedi | Modal'da gösterilen probe hata mesajını oku; credential'ı düzelt veya `account_id`'yi manuel olarak Atlassian admin panelinden al. Auto-probe sonrası tekrar dene. |
| `make up-all` ile başlatınca da bir servis `unhealthy` | Profile-gated servisin bağımlılıkları (örn. `postgres`, `temporal`) henüz hazır değil | Compose `depends_on` zinciri healthcheck-aware'dır; 1-2 dakika bekle. Hâlâ unhealthy ise `docker compose logs <service>` ile root cause'a bak. |
| Port 3000 / 8082 / 8200 / 5432 çakışıyor | Host'ta başka uygulama portu tutuyor | Çakışan uygulamayı kapat veya `infra/docker-compose.dev.yml` içinde port mapping'i değiştir. |

## 6. CI / Debug akışı (`make up-all`)

`make up-all` her servisi tek seferde başlatır; yalnızca aşağıdaki senaryolarda kullanılır:

- **CI smoke test'leri:** End-to-end pipeline'lar tüm stack'i ayağa kaldırıp `pytest tests/integration/` çalıştırır.
- **Debug:** Birden fazla servisin etkileşimini gözlemlemek gerektiğinde (örn. webhook gateway → automation-worker → execution-runner zinciri).
- **Geri dönük uyum:** Eski geliştirici muscle-memory'sinin kırılmaması için (eski `make up` davranışı bu hedefe taşındı).

```bash
# Tüm servisleri başlat
make up-all

# Manifest'ten türetilen profile listesini gör
make profiles

# Durdur
make down
```

> **Production-go uyarısı:** `make up-all` üretim ortamlarında **kullanılmaz**. Üretim deploy'ları orchestration platformunun (Kubernetes, ECS, vb.) kendi tooling'ini kullanır; bu Makefile yalnızca yerel geliştirme ve CI içindir.

## 7. Sonraki adımlar

İlk açılış tamamlandıktan sonra:

- **Webhook kurulumu** için → [`webhook-setup.md`](webhook-setup.md) runbook'unu izle (her dept için Jira + Bitbucket webhook abonelikleri).
- **Departman ekleme/çıkarma** için → admin-dashboard `/departments` sayfası veya [`dept-decommission.md`](dept-decommission.md) runbook'u.
- **Task açma** için → Streamlit'te Task Creator sayfası (`http://localhost:8501`) veya Jira'ya doğrudan task description yapıştırma (`prompts/task_creation_assistant.md` rehberi ile).

---

## İlgili Referanslar

- [`platform/Makefile`](../../Makefile) — `boot`, `up`, `up-all` hedef tanımları.
- [`platform/scripts/up.sh`](../../scripts/up.sh) ve [`platform/scripts/up.ps1`](../../scripts/up.ps1) — make olmayan host'lar için wrapper.
- [`platform/infra/docker-compose.yml`](../../infra/docker-compose.yml) — boot bundle (profilesiz) ve profile-gated servis tanımları.
- [`platform/config/services.manifest.json`](../../config/services.manifest.json) — profile listesi single source of truth.
