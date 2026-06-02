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

## 2026-06-01 final reset: sadece Dashboard, MCP ve Streamlit

Bu kosu kullanicinin gercek akisini hedefledi: tek komutla Dashboard boot,
Dashboard browser ekranindan sadece `atlassian-mcp` ve `streamlit-ui`
baslatma, Streamlit browser ekranindan credential girme ve Chat uzerinden
Jira/Confluence/Bitbucket islemlerini test etme.

### Sifirdan baslatma

Temizleme:

```powershell
docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml --profile automation-service --profile assistant-service --profile task-intake-service --profile agent-runner-worker --profile execution-runner-worker --profile automation-worker --profile atlassian-mcp --profile firecrawl --profile opencode-sidecar --profile streamlit-ui --profile temporal --profile temporal-ui --profile minio --profile redis --profile workers down -v --remove-orphans
```

Admin boot:

```powershell
.\scripts\up.ps1 boot
```

Boot sonrasi sadece su servisler ayaktaydi:

- `postgres`
- `vault`
- `admin-dashboard-api`
- `admin-dashboard-ui`

Dashboard `http://127.0.0.1:3000/services` uzerinden browser ile:

- `atlassian-mcp` Start modaliyla baslatildi ve `RUNNING` oldu.
- `streamlit-ui` Start modaliyla baslatildi ve `RUNNING` oldu.
- Diger uygulama servisleri baslatilmadi.

Son Docker durumu:

- `admin-dashboard-api`: healthy
- `admin-dashboard-ui`: healthy
- `atlassian-mcp`: healthy
- `streamlit-ui`: healthy
- `postgres`: healthy
- `vault`: healthy

### Bu kosuda yapilan kod duzeltmeleri

- `admin-dashboard-api` health probe artik Docker healthcheck durumunu tercih
  ediyor. Bu, Docker'da healthy olan `atlassian-mcp` servisinin Dashboard'da
  yanlis `unhealthy` gorunup `streamlit-ui` baslatmasini bloke etmesini
  engelledi.
- Dashboard tarafindan calistirilan `docker compose` komutlari artik
  workspace root `.env` dosyasini `--env-file` ile kullaniyor.
- `admin-dashboard-api` container'ina root `.env` read-only mount edildi:
  `/app/.env`.
- Sonuc: Dashboard'dan baslatilan `streamlit-ui`, `OPENAI_API_KEY` dahil LLM
  konfiglerini aldi. Streamlit Chat ham MCP JSON fallback yerine OpenAI
  Responses API ile Turkce dogal dil cevap verdi.

Secret degerleri dokumana yazilmadi.

### Credential kurallari

Jira & Confluence Cloud:

- `JIRA_URL`: sart, ornek `https://your-company.atlassian.net`
- `JIRA_USERNAME`: sart, Atlassian hesap e-postasi
- `JIRA_API_TOKEN`: sart, id.atlassian.com API token
- `CONFLUENCE_URL`: sart, ornek `https://your-company.atlassian.net/wiki`
- `CONFLUENCE_USERNAME`: sart, Atlassian hesap e-postasi
- `CONFLUENCE_API_TOKEN`: sart, Jira ile ayni Atlassian API token olabilir

Jira & Confluence Server/Data Center:

- `JIRA_URL`: sart
- `JIRA_PERSONAL_TOKEN`: sart
- `CONFLUENCE_URL`: sart
- `CONFLUENCE_PERSONAL_TOKEN`: sart

Bitbucket Cloud:

- `BITBUCKET_URL`: sart, `https://bitbucket.org`
- `BITBUCKET_USERNAME`: sart; API token icin e-posta, app password icin Bitbucket kullanici adi
- `BITBUCKET_API_TOKEN` veya `BITBUCKET_APP_PASSWORD`: biri sart
- `BITBUCKET_WORKSPACE`: opsiyonel; bos birakilirsa chat sorusunda workspace/repo belirtilmeli

Bitbucket Cloud auth hizli kontrolu (tokeni dokumana yazma):

```powershell
$env:BITBUCKET_USERNAME = "your.email@company.com"
$env:BITBUCKET_API_TOKEN = "<bitbucket-api-token-or-app-password>"
curl.exe -u "$($env:BITBUCKET_USERNAME):$($env:BITBUCKET_API_TOKEN)" "https://api.bitbucket.org/2.0/user"
```

Beklenen sonuc: JSON icinde `username`, `display_name` ve
`account_status: active` gibi alanlar doner. Bu sadece Bitbucket Cloud auth
kontroludur; repo, PR, pipeline veya webhook islemleri icin ilgili
workspace/repo yetkileri ve token scope'lari ayrica gerekir.

Streamlit credential dogrulamasi da Bitbucket Cloud'da workspace bosken ayni
auth kontrolunu yapar: `https://api.bitbucket.org/2.0/user`. Bu ekran 401
donerken yukaridaki curl ayni username/token ile basariliysa, kod veya MCP
endpointi degil, Streamlit'e girilen deger ya da `.env` degeri farklidir.

Bitbucket Server/Data Center:

- `BITBUCKET_URL`: sart
- `BITBUCKET_PERSONAL_TOKEN`: sart
- `BITBUCKET_PROJECT_KEY`: opsiyonel
- `BITBUCKET_SSL_VERIFY`: opsiyonel; self-signed sertifika varsa bilincli olarak `false` yapilabilir

### Girilen bilgiler

Streamlit `http://127.0.0.1:18501/credentials` browser ekraninda uc sekme
dolduruldu:

- Jira:
  - Deployment: `Cloud`
  - URL kaynagi: `CREDENTIALS.md` Jira URL
  - E-posta ve API token kaynagi: `CREDENTIALS.md`
- Confluence:
  - Deployment: `Cloud`
  - URL kaynagi: `CREDENTIALS.md` Confluence URL
  - E-posta ve API token kaynagi: `CREDENTIALS.md`
- Bitbucket:
  - Deployment: `Cloud`
  - URL: `https://bitbucket.org`
  - Workspace: opsiyonel; testte varsayilan workspace icin `CREDENTIALS.md` degeri girildi
  - E-posta ve API token kaynagi: `CREDENTIALS.md`

Her uc formda `Bagla ve dogrula` browser uzerinden calistirildi ve basari
mesaji alindi.

### Browser Chat testleri

Streamlit sidebar acilarak Chat sayfasina gecildi; credential warning
gorulmedi. Asagidaki sorular Chat input alanindan browser ile gonderildi:

- Jira acik issue listesi:
  - Soru: `Jira project KAN acik issue listesini getir...`
  - Cevap dogal dil geldi; ilk kayitlar `KAN-140`, `KAN-139`, `KAN-138`.
- Jira task olusturma:
  - Soru: `Jira task olustur project KAN ...`
  - Sonuc: `KAN-141` olusturuldu.
  - Baslik: `Streamlit final browser LLM E2E 2026-06-01T11-26-44-206Z`
  - Durum: `Yapilacaklar`
- Confluence sayfa listesi:
  - Ilk 3 sayfa dondu.
  - Space key: `E2ETEST`
  - Ornek sayfa: `Strict Live E2E strict-live-7e0d4e8b51`
- Bitbucket Cloud repo listesi:
  - Workspace: `johni_test`
  - Repo: `smoke-test`
  - Private: `Evet`
- Bitbucket commit listesi:
  - Repo: `johni_test/smoke-test`
  - Son commit hash ornegi: `5a8d70f5dc78a4ab1d9c4f6c8a1a7c42422f91ab`
- Bitbucket acik PR listesi:
  - PR `28`, `27`, `26` dondu.
  - Her ucunun durumu `ACIK`.

### Son degerlendirme

Bu final kosuda istenen ana akista bloklayici hata kalmadi:

- Tek komutla admin boot calisti.
- Dashboard'dan sadece MCP ve Streamlit baslatildi.
- Streamlit'te Jira, Confluence, Bitbucket credential'lari browserdan girildi.
- Chat cevaplari LLM ile dogal dil formatinda geldi.
- Jira create, Jira list/detail, Confluence list, Bitbucket repo/commit/PR
  akislari browserdan basarili dogrulandi.


## 2026-06-02 sifirdan reset + browser E2E (Confluence ve audit precheck fix)

Bu kosu kullanicinin istegi uzerine tamamen sifirdan yapildi: tum DB/Vault
state silindi, tek komutla admin boot edildi, Dashboard browser ekranindan
sadece `atlassian-mcp` ve `streamlit-ui` baslatildi, Streamlit browser
ekranindan credential girildi ve Chat uzerinden Jira/Confluence/Bitbucket
islemleri test edildi. Tum kabul testleri browserdan yapildi.

### Sifirdan reset ve admin boot

Komut satiri yalnizca reset, image build ve admin boot icin kullanildi.

Temizleme (tum profiller + volume):

```powershell
docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.dev.yml --profile automation-service --profile assistant-service --profile task-intake-service --profile agent-runner-worker --profile execution-runner-worker --profile automation-worker --profile atlassian-mcp --profile firecrawl --profile opencode-sidecar --profile streamlit-ui --profile temporal --profile temporal-ui --profile minio --profile redis --profile workers down -v --remove-orphans
```

- `infra_pg_data` named volume silindi -> Postgres temiz DB ile yeniden olustu
  (migration'lar admin-dashboard-api lifespan'inda tekrar uygulandi).
- Vault dev-mode container yeniden yaratildigi icin bos state ile basladi
  (dev mode in-memory, kalici secret yok).
- Reset sonrasi `docker ps` ve `docker volume ls` ile `infra-*` container ve
  `infra_*` volume kalmadigi dogrulandi.

Tek komutla admin boot:

```powershell
.\scripts\up.ps1 boot
```

Boot sonrasi yalnizca su dort servis healthy oldu:

- `postgres`
- `vault`
- `admin-dashboard-api`
- `admin-dashboard-ui`

Profile-gated servislerin (atlassian-mcp, streamlit-ui, automation-service,
worker'lar, vb.) hicbiri acilmadi. Dashboard `http://127.0.0.1:3000/services`
ekraninda "Calisan: 2" (admin api + ui) gorundu.

### Browser ile MCP ve Streamlit baslatma

Dashboard `Servisler` ekranindaki "Hizli baslangic" panelinden, browser
uzerinden:

- `atlassian-mcp`: Start modalinde env override yok; `Start` onaylandi,
  container healthy oldu.
- `streamlit-ui`: Start modalinde `OPENAI_API_KEY` sensitive alanina
  `CREDENTIALS.md` icindeki OpenAI key girildi; diger alanlar compose/default
  degerleriyle birakildi (`LLM_PROVIDER=openai`, `LLM_MODEL_NAME=gpt-4o-mini`,
  `MCP_BASE_URL=http://atlassian-mcp:8090`, vb.). `Start` onaylandi, container
  healthy oldu.

Diger uygulama servisleri (automation-service, assistant-service, worker'lar,
firecrawl, opencode-sidecar) bilincli olarak baslatilmadi.

Adresler:

- Admin Dashboard: `http://127.0.0.1:3000/services`
- Streamlit: `http://127.0.0.1:18501`

### Bu kosuda bulunan ve giderilen iki bug

**1. Streamlit chat — Confluence listeleme/arama bos donuyordu.**

Belirti: "Confluence son guncellenen 5 sayfayi listele ... space bilgisini ver"
veya "strict-live ara ... space key bilgisini ver" sorularinda chat "sayfa
bulunamadi" donuyordu. Oysa MCP `confluence_search` tool'u dogrudan cagrildiginda
sayfalari donuyordu ve Confluence API'de (E2ETEST space) sayfalar mevcuttu.

Kok neden: `chat_planner._extract_confluence_space_key` regex'i `re.IGNORECASE`
ile calistigi icin "space bilgisini" -> `VE`, "space key bilgisini" -> `KEY`
gibi prose kelimeleri sahte space key olarak yakaliyordu. Bu sahte key
`space = "VE" and type=page ...` gibi bir CQL uretip sifir sonuc donduruyordu.

Fix (`platform/ui/streamlit-app/chat_planner.py`):
- Regex artik key token'ini buyuk harf olarak (gercek Confluence space key'leri
  buyuk harf: E2ETEST, KAN, JOH) ariyor; `space` anahtar kelimesi case-insensitive
  kaldi.
- `_SPACE_KEY_STOPWORDS` deny-list eklendi (VE, KEY, ID, SPACE, PAGE, SAYFA,
  BILGISINI, ... ) — yanlislikla yakalanan prose kelimeleri eler.
- Streamlit image yeniden build edildi ve Dashboard'dan Stop+Start ile yeni
  image'a gecirildi.

Dogrulama (browser chat):
- "son guncellenen 5 sayfa" -> 5 sayfa dondu, hepsi space E2ETEST.
- "strict-live ara" -> ilk sayfa `Strict Live E2E strict-live-b51ab38778`,
  space key `E2ETEST`, id `1966091`.

**2. Admin Dashboard — uzun idle sonrasi Stop/Start 502 "audit DB precheck
failed: TimeoutError" donuyordu.**

Belirti: Servis ilk acilista (taze pool) Start calisiyor, ~1 saat idle sonrasi
Stop/Start `502 {"detail":"audit DB precheck failed: TimeoutError"}` donuyordu.
Postgres'e dogrudan `SELECT 1` ve container icinden taze asyncpg pool aninda
calisiyordu — yani DB saglikliydi, sorun lifespan'da bir kez olusturulan audit
writer pool'unun bayatlamasiydi (Docker/NAT uzun idle TCP socket'i sessizce
dusurur, sonraki `acquire()` + sorgu yarim-acik baglantida asili kalir).

Fix (`platform/services/admin-dashboard-api/src/lifecycle/audit_writer.py`):
- Pool factory artik `max_inactive_connection_lifetime=180s` ve
  `command_timeout=10s` ile pool aciyor (idle baglantilar socket dusmeden once
  geri donusturuluyor).
- `precheck` icindeki `SELECT 1` `asyncio.wait_for` ile 5s'e baglandi; takilirsa
  TimeoutError uretip connection-level hata olarak siniflandiriliyor.
- `precheck` bir kez self-heal yapiyor: connection-level hatada pool'u kapatip
  factory'den yeniden olusturup tekrar deniyor; ikinci deneme de basarisizsa
  `AuditUnreachableError` (-> 502) doniyor.
- Yeni unit testi `test_precheck_self_heals_stale_pool_via_reset` eklendi;
  mevcut connection-error testleri `persist_failure` ile kalici hata simule
  edecek sekilde guncellendi.
- admin-dashboard-api dev-modda `src/` bind-mount + `uvicorn --reload`
  oldugundan fix hot-reload ile devreye girdi; ayrica taze tuned pool icin
  container restart edildi.

Dogrulama (browser): Dashboard'dan `streamlit-ui` Stop basarili oldu (container
`Exited`), ardindan Start ile yeni image'a gecirildi.

### Kod dogrulamalari

- `python -m pytest ui/streamlit-app/tests -q`: tum testler passed (35).
- `python -m pytest services/admin-dashboard-api/tests/unit -k "audit or services_lifecycle"`:
  `116 passed`.
- `services/admin-dashboard-api` genelinde gorulen diger basarisizliklar
  test ortami kaynakli (Windows host'ta `/app/config/...` container path'leri
  yok); audit/lifecycle degisikligiyle ilgisi yok.
- Gizli degerler bu dokumana ve repoya yazilmadi; tokenlar `CREDENTIALS.md`
  icinden okunup browser/Start-modal alanlarina girildi.

### Browser credential girisi

Streamlit `http://127.0.0.1:18501/credentials` ekraninda uc sekme dolduruldu,
hepsinde `Bagla ve dogrula` basarili:

- Jira: Cloud, URL + e-posta + API token kaynak `CREDENTIALS.md` -> "Jira
  credential dogrulandi".
- Confluence: Cloud, URL `.../wiki` + ayni token -> "Confluence credential
  dogrulandi".
- Bitbucket: Cloud, URL `https://bitbucket.org`, workspace `johni_test`,
  e-posta + kapsamli cloud API token (`CREDENTIALS.md`) -> "Bitbucket
  credential dogrulandi".

### Browser Chat testleri (hepsi basarili)

Jira:
- Acik issue listesi -> `KAN-148`, `KAN-147`, `KAN-146` (durum Yapilacaklar).
- Task olusturma -> `KAN-149` ("Sifirdan kurulum E2E browser testi 2026-06-01").
- `KAN-149` detay -> baslik/durum/issue type/assignee tutarli.
- Task olusturma -> `KAN-150` ("Confluence space-key fix dogrulama 2026-06-02").
- `KAN-150` detay -> baslik + aciklama read-after-write dogrulandi.

Confluence (fix sonrasi):
- Son 5 sayfa -> baslik + space E2ETEST dondu.
- "strict-live" arama -> `Strict Live E2E strict-live-b51ab38778`, space
  `E2ETEST`, id `1966091`.

Bitbucket Cloud:
- Repo listesi -> `smoke-test`, Private, project key `JOH`.
- Son 3 commit -> hash + tarih + mesaj dondu.
- Acik PR listesi -> PR 28/27/26, hepsi OPEN, source + destination branch tam
  (onceki kosuda PR 26 source branch eksikti; bu kosuda eksiksiz geldi).
- PR 28 detay -> baslik/state/source/destination tam ve tutarli.

LLM:
- Chat cevaplari OpenAI Responses API ile dogal dil Turkce ozet olarak geldi;
  ham MCP JSON fallback gorulmedi.

### Son durum

`docker ps` ile sadece istenen 6 servis healthy:
`postgres`, `vault`, `admin-dashboard-api`, `admin-dashboard-ui`,
`atlassian-mcp`, `streamlit-ui`. Baska servis acik degil.

### Genel degerlendirme

Istenen ana akis sifirdan eksiksiz dogrulandi: tek komutla admin boot,
Dashboard browser ekranindan sadece MCP + Streamlit baslatma, Streamlit
browserdan credential girisi, Chat uzerinden Jira/Confluence/Bitbucket
islemleri. Bu kosuda iki gercek bug bulundu ve giderildi (Confluence space-key
yanlis cikarimi, audit precheck bayat-pool timeout'u); ikisi de browserdan
yeniden test edilip dogrulandi. Bloklayici eksik kalmadi.


## 2026-06-02 Admin Dashboard UI tutarlilik ve sadelestirme

Kullanici "dashboard kullanici dostu, sade, profesyonel mi; AI yapimi belli
olmamali" diye sordu. Dashboard incelendi: yapi islevsel ve mantikli gruplanmis
(Genel / Yonetim / Gozlemlenebilirlik / Yapilandirma / Hata Ayiklama), 17 sayfa.
Tespit edilen ana sorun: navigasyon, sayfa basliklari ve ana akistaki butonlar
**ASCII'ye indirgenmis Turkce** kullaniyordu ("Yonetim", "Guvenlik", "Is
akislari", "Gozlemlenebilirlik", "MCP trafigi", "Hizli baslangic", "Baslat",
"Chat ac") ama sayfa govdeleri duzgun Turkce ("Platforma hos geldin"). Bu
tutarsizlik tam da "ozensiz / AI yapimi" izlenimi veriyordu.

Yapilan duzeltmeler (sadece kullaniciya gorunen metinler; mimari/islev
degismedi):

- `components/AppShell.tsx`: sol menu grup ve link etiketleri + sayfa
  basliklari duzgun Turkce'ye cevrildi (Yonetim->Yonetim, Is akislari->Is
  akislari diakritikli, Guvenlik, Gozlemlenebilirlik, MCP trafigi, Denetim
  kaydi, Ozellik bayraklari, Firecrawl izin listesi, Canli test, Hata Ayiklama,
  Kurulum sihirbazi).
- `app/services/_components/ServiceQuickStart.tsx`: "Hizli baslangic" ->
  "Hizli baslangic" (diakritikli), "Baslat" -> "Baslat", "Restart" -> "Yeniden
  baslat", "Chat ac" -> "Chat'i ac", "Credentials" -> "Kimlik bilgileri",
  "Yonet", durum rozetleri (tanimli degil, okunamadi, vb.).
- `app/services/page.tsx`: servis tablosu aksiyon butonlari "Start/Stop/
  Restart" -> "Baslat/Durdur/Yeniden baslat"; disabled tooltip mesajlari
  Turkce'ye cevrildi; "Health durumu" -> "Saglik durumu". `ActionButton`'a
  `danger` prop eklendi (onceden `label === "Stop"` ile stillemeye baglıydi;
  artik etiket Turkce olunca da danger stili dogru calisir).
- `app/services/_components/McpSetupTab.tsx` ve `McpDeploymentSelector.tsx`:
  MCP kurulum rehberi metinleri (env var aciklamalari, kurulum adimlari, scope
  tablosu, baslik/altbaslik) duzgun Turkce'ye cevrildi.
- `app/services/_components/ExternalProvidersSection.tsx`: AI model uyari
  metni duzgun Turkce.

Dogrulama:
- `npm test` (admin-dashboard): `34 passed`.
- getDiagnostics: degisen dosyalarda hata yok.
- Browser: nav "YONETIM/IS AKISLARI/GUVENLIK/HATA AYIKLAMA", quick start
  "Hizli baslangic / Yeniden baslat / Chat'i ac / Kimlik bilgileri", servis
  tablosu butonlari "Baslat/Durdur/Yeniden baslat" diakritikli dogru render
  edildi.

Onemli operasyonel not (steering'e de eklendi): admin-dashboard-ui Next.js
dev watcher'i Windows bind-mount uzerinden host degisikligini GORMUYOR; UI
kaynagi degisince `docker restart infra-admin-dashboard-ui-1` gerekiyor
(tarayicida hard-reload tek basina yetmiyor).

Sonuc: Ana kullanim akisi (boot -> dashboard -> MCP+Streamlit ac -> chat)
artik bastan sona tutarli, profesyonel, diakritikli Turkce. Islev/mimari
degismedi; sadece metin tutarliligi saglandi.


## 2026-06-02 Diğer servisler + departman/credential E2E testi

Kullanıcı isteği: Streamlit/MCP/Dashboard çekirdek akışı tamamlandıktan sonra
diğer servisleri (automation-service, assistant-service, task-intake, workers,
firecrawl, opencode-sidecar, temporal, minio, redis) ve departman/credential
akışını uçtan uca test et, bugları düzelt. (Gerçek webhook tetikleme hariç.)

### Bulunan ve giderilen 3 gerçek bug

**BUG-1: departments.json seed şema ihlali → tüm dept create'leri 422.**
- `config/departments.json` içindeki `test` departmanının
  `repo_mappings[0].jira_project_key` boştu (""). Şema `^[A-Z][A-Z0-9_]{1,9}$`
  ister. Create handler TÜM dökümanı valide ettiği için, bu tek bozuk kayıt
  her yeni departman oluşturmayı `HTTP 422 schema_validation_failed` ile
  blokluyordu.
- Fix: `jira_project_key` → `"JOH"` (smoke-test reposunun gerçek project key'i).

**BUG-2: Departman credential probe yanlış MCP tool adı → HTTP 502 RuntimeError.**
- `services/automation-service/src/automation_service/atlassian.py` →
  `jira_myself` `jira_get_current_user_profile` tool'unu çağırıyor. Çalışan
  MCP image'i (pinned) bu tool'u tanımıyor ("Unknown tool") — kaynak kodda var
  ama image stale. MCP yalnızca `jira_get_user_profile` (user_identifier arg'lı)
  expose ediyor.
- Fix: `jira_myself` önce `jira_get_current_user_profile`'ı dener; "unknown
  tool" hatasında `jira_get_user_profile` + `{user_identifier: <email>}`
  fallback'ine düşer. `_call_tool` hata mesajına tool çıktısını ekledi.

**BUG-3: Credential save 504 upstream timeout.**
- AdminProxy timeout 30s'ti; canlı Atlassian read+write probe (myself → issue
  bul → comment ekle → comment sil) cloud round-trip'lerde 30s'i aşıyordu.
- Fix: `services/admin-dashboard-api/src/main.py` AdminProxy
  `request_timeout_seconds=90.0`.

### Önemli operasyonel keşif
Dashboard'dan Start edilen servisler `docker-compose.dev.yml` override'ı
ALMAZ (compose_runner sadece tek `-f` kullanır) → image-baked source çalışır,
bind-mount/--reload yok. automation-service fix'ini uygulamak için servis
`docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile
automation-service up -d automation-service` ile dev-override'lı yeniden
yaratıldı (canlı source).

### Test sonuçları (hepsi browser/Dashboard)
- 12 servis ayağa kaldırıldı, Dashboard "Servisler" → Çalışan 12, Sağlıksız 0,
  Hatalı 0.
- Dashboard "Test" butonu TÜM servislerde PASSED:
  automation-service, assistant-service, admin-dashboard-api,
  task-intake-service, agent-runner-worker, execution-runner-worker,
  automation-worker, atlassian-mcp, firecrawl, opencode-sidecar, streamlit-ui,
  admin-dashboard-ui — hepsi `{"status":"ready"}` / `ok`.
- External provider OpenAI: OK.
- Departman oluşturma (`payment` / Payment Team) browser'dan başarılı.
- Jira bot credential girişi + canlı probe → Vault'a yazıldı
  (`secret/data/atlassian/payment/jira`, version 2; url+username+token
  doğrulandı). Dept detayda "✓ Aktif".
- Workflows sayfası: Temporal bağlı, `SSHHealthcheckCronWorkflow` Running —
  worker'lar Temporal'a register olmuş.

### UI tutarlılık (devam)
- Departman detay sayfası ve listedeki İngilizce metinler Türkçeleştirildi:
  "← Departments"→"← Departmanlar", "Department ·"→"Departman ·",
  "Manage Credential"→"Kimlik bilgilerini yönet", "Active"→"Aktif",
  "Assigned SSH Runners"→"Atanmış SSH Runner'ları",
  "Decommission"→"Kaldır". MCP setup tab + deployment selector tamamen
  Türkçeleştirildi.

### Doğrulamalar
- `npm test` (admin-dashboard): 34 passed.
- automation-service atlassian-client testleri: passed (2). Genel suite'te
  2 pre-existing/ortam kaynaklı başarısızlık var (test_jira_comment.py header
  adı `X-Atlassian-Jira-Personal-Token` beklerken kod `api-token` gönderiyor —
  benim değişikliğimle ilgisiz; budget/jira_comment.py modülü).

### Henüz test edilmeyen
- Gerçek webhook tetikleme (Jira'da task atanınca otomatik workflow). Kod +
  altyapı (webhooks/jira.py, _resolve_dept_id, CredentialResolver, Temporal
  workflow, inject_git_credentials) hazır ve worker'lar register oldu; ancak
  canlı webhook secret + callback URL (ngrok) kurulumu gerektirdiği için
  uçtan uca tetiklenmedi.
- Streamlit Task Creator: dept-scoped (kullanıcının dept_ids auth claim'i
  gerekir); dev auth modunda user'a dept_ids enjekte edilmeden açılmıyor —
  tasarım gereği, bug değil.
