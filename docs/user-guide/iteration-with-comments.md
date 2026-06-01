# Comment ile Düzeltme (İterasyon)

> Bot ilk denemede istediğini tam yapmadıysa, comment ile yönlendirip yeniden çalıştırırsın.

## Temel Akış

Bot bitirdi → sen Jira comment'i ekle → bot yeni iterasyonu başlatır.

```
Bot: ✅ Tamamlandı, PR: <link>
   ↓
Sen: "[fix] retry sayısını 5'e çıkar, exception type'ı RequestException olarak değiştir"
   ↓
Bot: 🤖 Anlaşıldı, iter-2'yi başlatıyorum...   ← yeni iterasyon
   ↓
Bot: ✅ Iter-2 tamamlandı, PR güncellendi: <link>
```

## `[fix]` Etiketi

Comment'inde `[fix]` ile başlarsan bot **kesin olarak** kod düzeltme yapacağını anlar
ve mevcut PR/branch'e yeni commit atar.

✅ Doğru:
> `[fix]` exception class yanlış. RequestException yerine ConnectionError yakala.

✅ Doğru (etiketsiz, niyet net):
> Retry sayısını 5'e çıkar, lütfen.

❌ Belirsiz:
> Yanlış olmuş.   ← bot ne yanlış olduğunu sorar (`needs_info`)

## Kaç Kez `[fix]` Yapabilirim?

Default **iterasyon limiti 5** (departman config'inde değiştirilebilir,
[detay: §16.6.20](../../MIMARI.md)). Limite ulaşırsan bot şunu der:

> 🤖 Bu task için 5 iterasyon tamamlandı. Daha fazla düzeltme istiyorsan **yeni bir
> task aç** — orada baştan başlayabilirim.

Bu kural sonsuz döngüyü ve LLM cost patlamasını engeller.

## Branch Yönetimi

Bot her iterasyonda **aynı branch**'e yeni commit atar (`ai/PAY-4211`). PR
güncellenmiş olarak kalır. Eğer:

- **PR merge edildi** ve sen yeni comment yazıyorsan → bot yeni branch açar
  (`ai/PAY-4211-iter-2`), yeni PR açar ([detay: §16.6.3](../../MIMARI.md)).
- **PR kapatıldı (decline)** → bot yeni iterasyona başlamaz, *"PR kapatılmış, ne
  yapacağımı söyle"* der.

## Comment ile Bot'u Yönlendirme

### Sadece "yanlış" demeden ne istediğini söyle

❌ "Hayır, böyle değil"
✅ "Hayır, send_webhook fonksiyonunu sarma — yeni fonksiyon yaz: send_webhook_with_retry"

### Atomik istekler ver

❌ "Test'i değiştir, retry'ı azalt, hata mesajı ekle, log seviyesini info yap"
✅ Üç ayrı comment veya tek tek `[fix]`'lerle

Bot bir comment'i tek bir görev gibi işler. Çok madde tek seferde verirsen biri eksik
kalabilir.

### Kod parçaları yapıştır

Yanlış olan kısmı doğrudan göster:

```
[fix] Şu kısım yanlış:

\`\`\`python
def send_webhook(url):
    requests.post(url)
\`\`\`

Bunun yerine:

\`\`\`python
@retry(max_attempts=3, backoff_factor=2)
def send_webhook(url):
    requests.post(url, timeout=10)
\`\`\`
```

## Bot Soru Sorarsa

Bot bazen `[fix]`'i anlamayıp soru sorabilir. Bu durumda:

> 🤖 `[fix]` aldım ama hangi exception class'ı kullanmamı istediğin net değil.
> RequestException, ConnectionError, Timeout? Hangisi?

Cevap ver. Bot 7 güne kadar bekler. Yanlış cevap verirsen `needs_info loop cap`
devreye girer ([detay: §16.6.50](../../MIMARI.md)).

## Onay Gerektiren Durumlar

Bot bazı işleri yapmadan önce onayını ister:

### Maliyet onayı

Tahmini maliyet limit'in %70'ine ulaşırsa:

> 🤖 Tahmini maliyet $0.85 — limitin %70'i üstünde ($1.00). Devam edeyim mi?
> (`onaylıyorum` veya `iptal`)

### Kritik dosya onayı (S8)

Bot infrastructure/Dockerfile/migrations gibi kritik dosyalara dokunacağında:

> 🤖 Şu kritik dosyalara dokunacağım:
> - `infrastructure/main.tf`
> - `Dockerfile`
> Devam edeyim mi?

### Confluence küçülme onayı

Var olan Confluence sayfasını %30+ küçültecekse onay ister
([detay: §16.6.61](../../MIMARI.md)).

## İptal

Herhangi bir noktada `[cancel]` yazarsan bot temiz şekilde durur:

- Açtığı branch silinir
- Yarım kalan commit'ler atılmaz
- Workflow durumu *"user cancelled"*

## "Bot Yanlış Anladı, Baştan Başlasın"

Bot kapsamı yanlış anladıysa:

1. Mevcut PR'ı kapat (decline)
2. Description'ı düzelt — daha açık yaz
3. Yeni task aç — eskisini "Done" yap

Bot eski branch'i değil yeni branch açar (eski merge edilmediği için temiz başlangıç).

## "Bot Aynı Hatayı Tekrarlıyor"

3 iterasyondan sonra hâlâ aynı hatayı yapıyorsa:

- Description ve önceki comment'ler **çelişebilir** — bot ikisini birlikte okuyor.
  Description'ı güncelleyip task'ı yeniden bot'a ata.
- Prompt sorunu olabilir — AI Admin'e bildir.

## Auto `[fix]` (CI Fail Sonrası)

Eğer departmanında `auto_fix_on_ci_fail` açıksa, PR CI'sı fail olduğunda bot
**otomatik** `[fix]` çalıştırır — sen comment yazmadan
([detay: §16.6.47](../../MIMARI.md)).

---

**İlgili:**

- Bot ne yapamaz: [`what-bot-cannot-do.md`](what-bot-cannot-do.md)
- SSS: [`faq.md`](faq.md)
