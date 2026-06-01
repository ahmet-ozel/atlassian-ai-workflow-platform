# Platform

Atlassian-tabanlı, çok-departmanlı AI iş akışı otomasyon platformu. Webhook gateway, Temporal worker'ları, MCP entegrasyonu, admin-dashboard ve Streamlit ön yüzü tek bir Compose stack'inde.

## Quick Start

İlk kez kurulum yapıyorsan **bootstrap-only** modu öneriyoruz: yalnızca `admin-dashboard` ve bağımlılıkları açılır, geri kalan servisler admin-dashboard'daki Setup Wizard üzerinden kullanıcı kontrolü ile aktive edilir.

```bash
cd platform
cp .env.example .env    # default değerler boot bundle için yeterli
make boot               # postgres + vault + admin-dashboard-{api,ui} açılır
```

Sonra tarayıcıda:

```
http://localhost:3000
```

Setup Wizard yedi adımda kalan servisleri sırayla ayağa kaldırır ve son adımda ilk departmanı eklemeni ister.

> **Tüm akışın detaylı anlatımı:** [`docs/runbooks/getting-started.md`](docs/runbooks/getting-started.md).

`make` yüklü değilse (Windows default'u):

```powershell
.\scripts\up.ps1 boot
```

veya POSIX shell'de:

```bash
./scripts/up.sh boot
```

## Yaygın komutlar

| Komut | Davranış |
|---|---|
| `make boot` | Bootstrap-only (default — postgres, vault, admin-dashboard). |
| `make up` | `make boot` alias'ı (geriye dönük uyum). |
| `make up-all` | Tüm servisleri tek seferde başlat (CI / debug). Üretimde kullanılmaz. |
| `make ps` | Çalışan servisleri listele. |
| `make logs` | Aktif servislerin log'larını tail et. |
| `make down` | Tüm container'ları durdur (volume'lar korunur). |
| `make restart` | `make down` + `make boot`. |
| `make profiles` | Manifest'ten türetilen profile listesini yazdır. |
| `make help` | Tüm hedefleri ve açıklamalarını göster. |

> **`make up-all` notu:** Tüm servisleri tek seferde başlatır (CI testleri ve full-stack debug için). İlk açılışta kullanma — credential'lar girilmeden 12 servisin paralel başlatılması "yanlış sırada başlatma" hataları yaratır. Bunun yerine `make boot` + Setup Wizard akışını izle.

## Klasör yapısı

| Yol | İçerik |
|---|---|
| `infra/` | Docker Compose dosyaları (`docker-compose.yml`, `docker-compose.dev.yml`), Postgres migration'ları. |
| `services/` | HTTP servisler — `automation-service`, `assistant-service`, `admin-dashboard-api`, `atlassian-mcp` (gateway). |
| `workers/` | Temporal worker'ları — `automation-worker`, `agent-runner-worker`, `execution-runner-worker`. |
| `ui/` | Frontend — `admin-dashboard` (Next.js), `streamlit-app` (Streamlit). |
| `libs/` | Paylaşılan Python library'leri (vault_client, audit, llm_client, vb.). |
| `config/` | Manifest dosyaları — `services.manifest.json`, `departments.json` ve schema'ları. |
| `prompts/` | LLM prompt'ları — task_creation_assistant, assistant_chat, notification template'leri. |
| `docs/` | Runbook'lar, user-guide, env reference. |
| `tests/` | Cross-service property/integration test'leri (her servis kendi `tests/` klasörüne de sahiptir). |
| `scripts/` | `up.sh`, `up.ps1` wrapper'ları + bakım script'leri. |

## Sonraki adımlar

- **İlk açılış akışı:** [`docs/runbooks/getting-started.md`](docs/runbooks/getting-started.md).
- **Webhook kurulumu:** [`docs/runbooks/webhook-setup.md`](docs/runbooks/webhook-setup.md).
- **Departman çıkarma:** [`docs/runbooks/dept-decommission.md`](docs/runbooks/dept-decommission.md).
- **Çevre değişkenleri:** [`docs/env-reference.md`](docs/env-reference.md).
- **End-user task açma rehberi:** [`docs/user-guide/`](docs/user-guide/).

## Lisans / Katkı

Repo özel kullanım içindir. Geliştirme akışı için `.kiro/specs/` altındaki spec dökümanlarını ve `MIMARI.md` mimari kararlarını oku.
