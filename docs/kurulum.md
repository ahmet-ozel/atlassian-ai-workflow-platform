# Kurulum ve Browser Smoke Testi

Tarih: 2026-05-31
Saat dilimi: Europe/Istanbul

## Reset ve servis acilisi

Bu kosu en bastan, temiz kurulum senaryosu gibi yapildi.

Komut satiri yalnizca reset ve admin boot icin kullanildi:

- Eski `infra` compose state'i `down -v --remove-orphans` ile indirildi.
- Profile bagli kalan `atlassian-mcp` ve `streamlit-ui` container'lari da indirildi.
- `infra_pg_data` named volume silindi; Postgres temiz DB ile yeniden olustu.
- Vault dev mode container yeniden yaratildigi icin bos state ile basladi.
- Sadece admin boot icin gerekli servisler komut satirindan baslatildi:
  - postgres
  - vault
  - admin-dashboard-api
  - admin-dashboard-ui

Admin boot sonrasi diger her islem browser uzerinden yapildi.

Admin Dashboard `Servisler` ekranindan, browser uzerinden sadece su iki servis baslatildi:

- atlassian-mcp
- streamlit-ui

Start modalinde girilen bilgiler:

- `atlassian-mcp`: env override yok; modalde `Start` onaylandi.
- `streamlit-ui`:
  - `OPENAI_API_KEY`: `CREDENTIALS.md` icinden okundu, panoya alindi ve browser start modalindeki sensitive alana yapistirildi.
  - `VLLM_API_KEY`: provider OpenAI oldugu icin dummy `not-needed` girildi.
  - Diger alanlar compose/default degerleriyle birakildi:
    - `PORT=8501`
    - `LOG_LEVEL=INFO`
    - `ASSISTANT_BASE_URL=http://assistant-service:8081`
    - `MCP_BASE_URL=http://atlassian-mcp:8090`
    - `LLM_PROVIDER=openai`
    - `LLM_MODEL_NAME=gpt-4o-mini`
    - `OPENAI_BASE_URL=https://api.openai.com/v1`
    - `CLIENT_SOURCE=streamlit-app`

Browser Dashboard son durum kontrolu:

- admin-dashboard-api: running
- admin-dashboard-ui: running
- atlassian-mcp: running
- streamlit-ui: running

Kapali birakilan servisler:

- automation-service
- assistant-service
- task-intake-service
- automation-worker
- agent-runner-worker
- execution-runner-worker
- firecrawl
- opencode-sidecar

Kullanilan adresler:

- Admin Dashboard: `http://127.0.0.1:3000/services`
- Streamlit: `http://127.0.0.1:18501`

## Kod ve runtime duzeltmeleri

Bu kosudan once su eksikler giderildi:

- MCP tool cagrisina transient hata retry'i eklendi.
  - Jira `SSLEOFError`, timeout, 502/503/504 gibi gecici hatalarda ayni tool tekrar deneniyor.
  - Unknown tool fallback hatalari gereksiz retry edilmiyor.
- Bitbucket repo listeleme planner'i duzeltildi.
  - Uzun Turkce repo listeleme promptlari artik Bitbucket `query` alanina serbest metin basmiyor.
  - Sadece acik arama/filtre niyeti ve guvenli tek repo terimi varsa `name ~ "..."` query uretiliyor.
- Streamlit Dockerfile yeni chat runtime modullerini image icine alacak sekilde guncellendi.
- Streamlit start modalinde OpenAI alaninin gorunmesi icin `.env.example` LLM alanlari eklendi.

Test:

- `python -m py_compile` basarili.
- `python -m pytest tests -q` sonucu: `21 passed`.

Gizli degerler repo dosyalarina yazilmadi.

## Streamlit credential girisi

Tum credential girisleri Streamlit `Credentials` ekraninda browser uzerinden yapildi.

Gizli degerler bu dosyaya yazilmadi. Tokenlar `CREDENTIALS.md` icinden okundu, browser alanlarina yapistirildi, sonra pano temizlendi.

Jira:

- Deployment: Cloud
- URL: `https://example.atlassian.net`
- Username: `user@example.com`
- API token: `CREDENTIALS.md` icinden okundu
- Sonuc: `Jira credential dogrulandi`

Confluence:

- Deployment: Cloud
- URL: `https://example.atlassian.net/wiki`
- Username: `user@example.com`
- API token: `CREDENTIALS.md` icinden okundu
- Sonuc: `Confluence credential dogrulandi`

Bitbucket:

- Deployment: Cloud
- URL: `https://bitbucket.org`
- Username: `user@example.com`
- Workspace: `example_workspace`
- API token: `CREDENTIALS.md` icinden okundu
- Sonuc: `Bitbucket credential dogrulandi`

## Browser chat testleri

Tum testler Streamlit `Chat` ekranindan browser uzerinden yapildi.

LLM sonucu:

- Streamlit `OPENAI_API_KEY` ile basladi.
- Chat cevaplari dogal dil Turkce ozet olarak geldi.
- Ham MCP JSON fallback gorulmedi.

Basarili Jira testleri:

- Soru: `Jira KAN projesindeki acik issue'lari listele. Ilk 3 issue key, baslik ve durum bilgisini kisa cevapla.`
  - Donen kayitlar: `KAN-136`, `KAN-135`, `KAN-134`
  - Durumlar: `Yapilacaklar`

- Soru: `Jira task olustur. project KAN, baslik: Streamlit Browser E2E reset 2026-05-31 23-35, aciklama: ...`
  - Olusan task: `KAN-137`
  - Baslik: `Streamlit Browser E2E reset 2026-05-31 23-35`
  - Durum: `Yapilacaklar`
  - Oncelik: `Orta`
  - Olusturma zamani: `2026-05-31 23:35`

- Soru: `Jira KAN-137 detayini getir; baslik, durum, issue type ve assignee alanlarini soyle.`
  - Baslik: `Streamlit Browser E2E reset 2026-05-31 23-35`
  - Durum: `Yapilacaklar`
  - Issue Type: `Gorev`
  - Assignee: `Unassigned`

Basarili Confluence testleri:

- Soru: `Confluence son guncellenen 5 sayfayi listele; baslik ve space bilgisini kisa ver.`
  - `Strict Live E2E strict-live-7e0d4e8b51`
  - `Strict Live E2E strict-live-0cbb5a9b17`
  - `Strict Live E2E strict-live-cd9e56feef`
  - `Strict Live E2E strict-live-976d5ea30e`
  - `Strict Live E2E strict-live-8058055997`
  - Space: `E2ETEST`

- Soru: `Confluence strict-live-7e0d4e8b51 aramasi yap; bulunan sayfanin basligini, space bilgisini ve id bilgisini soyle.`
  - Baslik: `Strict Live E2E strict-live-7e0d4e8b51`
  - Space: `E2ETEST`
  - ID: `2719747`

Basarili Bitbucket Cloud testleri:

- Soru: `Bitbucket workspace repolarini listele; repo adlarini, private/public bilgisini ve project key bilgisini kisa ve anlasilir sekilde dondur.`
  - Repo: `smoke-test`
  - Visibility: `Private`
  - Project key: `JOH`
  - Onceki 400 Bad Request problemi tekrar etmedi.

- Soru: `Bitbucket example_workspace/smoke-test son 5 commit bilgisini getir; hash, tarih ve mesaj alanlarini kisa ozetle.`
  - `5a8d70f5dc78a4ab1d9c4f6c8a1a7c42422f91ab`
  - `2026-05-24 11:42:21`
  - `Strict live E2E commit strict-live-7e0d4e8b51`

- Soru: `Bitbucket example_workspace/smoke-test acik pull request listesini getir; PR numarasi, baslik, state, source ve destination branch bilgilerini ver.`
  - PR `28`: `Strict Live E2E PR strict-live-7e0d4e8b51`, `OPEN`, source `ai-strict-live/strict-live-7e0d4e8b51`, destination `main`
  - PR `27`: `Strict Live E2E PR strict-live-0cbb5a9b17`, `OPEN`, source `ai-strict-live/strict-live-0cbb5a9b17`, destination `main`
  - PR `26`: `OPEN`, destination `main`; source branch verisi MCP sonucunda eksik gorundu.

- Soru: `Bitbucket example_workspace/smoke-test repo icin PR 28 bilgisini tekrar dogrula; baslik, OPEN state, source branch ve destination branch alanlarini net soyle.`
  - Baslik: `Strict Live E2E PR strict-live-7e0d4e8b51`
  - State: `OPEN`
  - Source branch: `ai-strict-live/strict-live-7e0d4e8b51`
  - Destination branch: `main`
  - Eksik bilgi yok.

## Genel degerlendirme

Bu kosuda istenen akisin tamami browser uzerinden dogrulandi:

- Reset ve admin boot disinda servis aksiyonu komut satirindan yapilmadi.
- `atlassian-mcp` ve `streamlit-ui` Admin Dashboard browser ekranindan baslatildi.
- Streamlit credential girisi browser uzerinden yapildi.
- Jira, Confluence ve Bitbucket Cloud chat testleri browser uzerinden yapildi.
- Jira task olusturma ve olusan task detayi basarili.
- Bitbucket uzun repo listeleme promptu artik 400 uretmedi.
- OpenAI key runtime'a baglandigi icin chat ham JSON yerine dogal dil cevap verdi.

Kalan uygulama kaynakli bloklayici eksik gorulmedi. Bitbucket PR listesinde PR `26` icin source branch bilgisinin eksik donmesi veri/MCP sonucundaki alan eksikligi olarak not edildi; PR `28` detay dogrulamasi tam ve tutarli geldi.

## 2026-06-01 tam servis browser audit

Bu ek kosu, ilk Streamlit + MCP dogrulamasi tamamlandiktan sonra tum servisleri
tek tek Dashboard uzerinden test etmek icin yapildi.

Onemli kural:

- Kabul/smoke testleri Dashboard ve Streamlit browser ekranindan yapildi.
- Komut satiri yalnizca kod duzeltme, image build ve admin-dashboard-api yeniden
  ayaga kaldirma icin kullanildi.
- Servis credential degerleri dokumana yazilmadi; sadece hangi alanlarin
  `CREDENTIALS.md`/env kaynagindan alindigi not edildi.

### Uygulanan duzeltmeler

- `admin-dashboard-api` compose tanimina LLM env aktarimi eklendi:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `LLM_PROVIDER`
  - `LLM_MODEL_NAME`
  - `VLLM_BASE_URL`
- `vllm` ve `anthropic` external provider kayitlari opsiyonel yapildi.
  - Bu provider'lar artik sadece `VLLM_ENABLED` / `ANTHROPIC_ENABLED` acilirsa
    veya ilgili base URL env'i operator tarafindan verilirse Dashboard'da
    gorunur.
  - Aktif provider OpenAI oldugu icin Dashboard artik kullanilmayan provider'lari
    kirmizi hata gibi gostermiyor.
- `admin-dashboard-api` image'i yeniden build edildi ve OpenAI key runtime'a
  secret yazmadan env uzerinden verildi.

Kod dogrulamalari:

- `docker compose -f platform/infra/docker-compose.yml config --quiet`: basarili.
- `python -m json.tool platform/config/services.manifest.json`: basarili.
- `python -m pytest tests/unit/test_external_providers_router.py -q`: `4 passed`.

### Browser Dashboard son durum

Dashboard `http://127.0.0.1:3000/services` uzerinden son gorunen durum:

- Toplam servis: `12`
- Calisan: `12`
- Sagliksiz: `0`
- Hatali: `0`
- External Providers: sadece `openai`
- OpenAI status: `OK`
- OpenAI latency: yaklasik `1028 ms`

Dashboard `Test` butonlariyla son browser test sonucu:

- `automation-service`: `PASSED`, `{"status":"ready"}`
- `assistant-service`: `PASSED`, `{"status":"ready"}`
- `admin-dashboard-api`: `PASSED`, `{"status":"ready"}`
- `task-intake-service`: `PASSED`, `{"status":"ready"}`
- `agent-runner-worker`: `PASSED`, `ok`
- `execution-runner-worker`: `PASSED`, `ok`
- `automation-worker`: `PASSED`, `ok`
- `atlassian-mcp`: `PASSED`, `{"status":"ok"}`
- `firecrawl`: `PASSED`, `{"status":"ok"}`
- `opencode-sidecar`: `PASSED`
- `streamlit-ui`: `PASSED`, `ok`
- `admin-dashboard-ui`: `PASSED`

### Browser log incelemesi

Dashboard `Loglar` modaliyla bakilan kritik servislerde son durum:

- `admin-dashboard-api`: `/healthz`, `/readyz`, `/api/v1/services/external`
  cevaplari `200 OK`; OpenAI external probe basarili.
- `agent-runner-worker`: son satirlar Temporal baglantisi ve
  `agent-runner-worker ready (queue=agent-runner-tq)`. Daha eski DNS retry
  satirlari admin rebuild sirasindan kalma gecmis log.
- `execution-runner-worker`: servis calisiyor, fakat `SSH_HOST` yok ve
  `infrastructure.ssh_runners` bos oldugu icin gercek SSH execution runner
  kapasitesi dis konfig bekliyor.
- `automation-worker`: servis calisiyor; `SLACK_ADMIN_WEBHOOK` yoksa
  `audit_prune_failed` alarmi stub'a dusuyor. Prod icin webhook girilmeli.
- `opencode-sidecar`: servis calisiyor; `OPENCODE_SERVER_PASSWORD` verilmedigi
  icin server unsecured uyarisi var. Prod icin parola girilmeli.
- `streamlit-ui`: servis calisiyor; `streamlit-cookies-controller` kaynakli
  `st.components.v1.html` deprecation uyarisi gorundu. Testi bloklamadi, ama
  Streamlit guncelleme sonrasi takip edilmeli.

### Genel sonuc

Browser/Dashboard kabul testlerinde tum servisler ayakta ve testleri basarili.
Jira, Confluence ve Bitbucket Cloud chat E2E akisi onceki bolumde browserdan
dogrulandi; OpenAI key hem Streamlit chat tarafinda dogal dil cevap icin hem de
Dashboard external provider probe tarafinda calisir durumda.

Prod'a cikmadan once dis konfig olarak tamamlanmasi gerekenler:

- SSH runner kaydi veya `SSH_HOST`/key bilgileri.
- Slack admin webhook.
- OpenCode server password.
- Streamlit cookie bridge deprecation icin paket/API guncellemesi.
