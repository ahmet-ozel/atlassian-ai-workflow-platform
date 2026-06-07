# Bot Ne Yapamaz?

> Sınırlar, yasaklar, bot'un *"yapamam"* dediği durumlar. Hayal kırıklığı yaşamadan önce
> oku.

## Hardcoded Yasaklar (Bot Asla Yapmaz)

Bu işlemler **kod seviyesinde yasaklı** - LLM "yap" dese bile sistem engeller.

### 1. Merge Etmek

Bot **PR merge etmez**. Daima draft PR açar; sen incelersin, sen merge edersin.

> Bu kural override edilemez. Kullandığın CI sistemi otomatik merge yapsa bile bot
> bunu kendi tetiklemez.

### 2. Confluence Sayfası Silmek

Bot Confluence sayfası **silmez**. Sadece oluşturur veya günceller. Sayfayı silmek
istiyorsan kendin yap.

### 3. PR Approve / Decline

Bot başka bir PR'ı approve veya decline edemez. Kendi açtığı PR'ı bile.

### 4. Banned Tool Listesi

Aşağıdaki tool'lar Atlassian MCP gateway tarafından LLM'e **hiç gösterilmez**:

- `bitbucket_merge_pr`, `bitbucket_approve_pr`, `bitbucket_decline_pr`
- `confluence_delete_page`
- (admin tarafından eklenmiş diğer banned tool'lar)

## Out-of-Scope Kararı (Bot "Yapamam" Der)

Bot bu durumlarda kendisi *"out_of_scope"* der ve task'ı geri çevirir:

| Senaryo | Örnek |
|---|---|
| Production data değişikliği | "DB'deki order'ları temizle" |
| Müşteri / dış iletişim | "Bu kullanıcıya email gönder" |
| Ticari / yasal karar | "Bu sözleşmeyi imzala", "Fiyatı %10 düşür" |
| Yetkisi olmayan sistem | "Slack'te yeni kanal aç" (departmanın Slack capability'si yoksa) |
| Etik endişe | "Kullanıcıyı haberi olmadan izle" |
| Capability uyumsuzluğu | "Bitbucket'a commit at" diyor ama dept'in BB capability'si yok |

### Bot ne der?

>  Bu task'ı yapamam.
>
> **Sebep:** Production database operations are outside bot capabilities. This requires
> DBA approval and manual execution.
>
> **Alternatif:**
> 1. Veritabanı erişim yetkim yok. DBA ekibine ticket açabilirim, ister misiniz?
> 2. Sadece local test environment'ında script çalıştırabilirim - bu kapsamda mı?

Task'ı **To Do** durumuna çeker, assignee bot olarak kalır - başka biri devralabilir.
Description'ı düzeltip yeniden bot'a atayabilirsin.

## Capability Sınırları

Departmanına atanmış olan **capability**'ler bot'un ne yapabileceğini belirler.

| Capability | Olmazsa bot ne yapamaz? |
|---|---|
| Bitbucket | Kod yazma, PR açma, repo'da değişiklik |
| Confluence | Doküman oluşturma / güncelleme, araştırma yayınlama |
| Jira (zorunlu) | (Bot'un temeli - tüm departmanlarda var) |
| Web search (Firecrawl) | İnternette araştırma yapma - gerektiğinde |
| Code execution (SSH) | Test çalıştırma, script execution |
| Vision (LLM) | Image / PDF attachment'ları okuma |

Hangi capability'lerin var olduğunu görmek için: **Streamlit Departments sayfası**  kendi
departmanını seç.

## Multi-Repo Task'lar

Bot **tek repo** ile çalışır. Description'da iki veya daha fazla repo'ya değişiklik
isterse:

>  Bot tek repo ile çalışır. Birden fazla repo değişmesi gerekiyorsa **iki seçeneğin
> var**:
>
> 1. **Ayrı task'lar aç** - her repo için bir task, sırayla bot'a ata.
> 2. **Epic + subtask kullan** - bir Epic aç, repo başına bir subtask ekle, Epic'i bot'a
>    ata. Bot subtask'ları sırayla işler.

## Çok Büyük Task'lar (`needs_breakdown`)

Bot bir task'ı tek seferde yapamayacak kadar büyük görürse alt-task önerisi yapar:

>  Bu task'ı tek seferde yapamam. Aşağıdaki alt-task'lara bölersem yapabilirim:
>
> **Sebep:** Task hem backend API hem frontend UI değişikliği içeriyor - tek workflow'da
> yapamam.
>
> **Önerilen alt-task'lar:**
> - Backend: /api/v2/payment endpoint'i (~20 dk)
> - Frontend: ödeme formu yeni alanlar (~15 dk)
> - Integration: e2e test (~10 dk)
>
> Onaylıyorsanız `evet` yazın - Epic + 3 subtask oluşturayım.

## Süre / Maliyet Limitleri

Bot bu limit'leri aşamaz:

| Limit | Default | Override |
|---|---|---|
| Workflow süresi (start_to_close_timeout) | 30 dk | LLM tahminine göre dinamik (max 240 dk) |
| Task başına LLM maliyeti | $5 | Departman config'inde |
| Departman başına eş zamanlı task | 5 | Departman config'inde |
| Aynı task için iterasyon | 5 | Departman config'inde |
| Prompt context tokenları | LLM model'in penceresi | Otomatik özetleme (§16.6.21) |

Limit aşıldığında bot kullanıcıya açıkça bildirir.

## Branch Adı Override

Bot her zaman `ai/{issue_key}` branch'i açar - bu adı override edemezsin. Mevcut bir
feature branch'e commit yapılmasını istiyorsan task'ı bot'a atayarak değil, manuel commit
atarak yap; sonra bot'u PR reviewer olarak ekle.

## "Bot Production'a Deploy Etsin"

 Hayır. Bot CI/CD pipeline'ını tetiklemez. PR açar, sen merge edersin, deploy senin
kontrolündedir.

İstisna: `pr_draft_strategy=open_after_test_pass` config'i açıksa bot CI testi geçtikten
sonra PR'ı **draft'tan açığa** çekebilir - bu da deploy değil, sadece "review için
hazır" sinyali.

## "Bot Bunu Hızlandırsın"

Bot LLM hızıyla sınırlıdır. Hızlandırmak için:

- Description'ı net yaz - gereksiz `needs_info` döngüsü olmasın
- Issue type'ı doğru seç (Story  AC, Bug  reproduce; Task  serbest)
- Attachment ekle - bot okumakla zaman kaybetmesin

## "Bot Bunu Bedava Yapsın"

Bot vLLM (kendi GPU'nuz) kullanıyorsa marginal cost düşüktür ama yine de:

- Sürekli `[fix]` zinciri = LLM cost patlaması
- Aşırı uzun description = context maliyeti
- Vision attachment = ek cost

Bot her iterasyon başında tahmini maliyet paylaşır. Limit aşılmadan onayını ister.

---

**İlgili:**

- Bot ne yapabilir  [`task-creation.md`](task-creation.md)
- "Yapamam" dedi, ne yapayım?  Description'ı düzelt, yeniden ata.
