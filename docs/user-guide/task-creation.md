# Task Açma

> Bot'a iş atamadan önce hazırlamanız gereken bilgiler ve ipuçları.

## En Hızlı Yol

1. Streamlit'te **Task Creator** sayfasına git (sol menüden).
2. Form'u doldur:
   - **Başlık** - "Ödeme retry mekanizması ekle" gibi tek satırlık özet
   - **Description** - bot'un ne yapacağını anlatır (aşağıda detay)
   - **Repo** - değişiklik hangi Bitbucket repo'sunda yapılsın? (departmanına ait
     repo'lar listelenir; kendi repo'n yoksa AI Admin'e başvur)
   - **Issue type** - Story, Task, Bug, Epic - bot bu tipe göre yaklaşımını ayarlar
3. **Task Oluştur** butonu → bot **5 saniye içinde** Jira'ya `🤖 Task alındı, analiz
   ediyorum...` comment'i yazar.
4. Birkaç dakika sonra bot planını + tahmini süre/maliyet'i ikinci comment olarak ekler.

## Description Nasıl Yazılır?

Bot description'dan **ne yapacağını** çıkarır. Belirsizlik olursa size soru sorar
(`needs_info`) - bu süre kaybeder. Üç soruya cevap verecek şekilde yazın:

### 1. Ne istiyorsun? (Hedef)

❌ Yetersiz: *"payment-callbacks repo'sunu güncelle"*
✅ Net: *"payment-callbacks repo'sunda webhook retry mekanizması ekle. 3 deneme,
exponential backoff, hata Sentry'ye loglansın."*

### 2. Nereye dokunulsun? (Kapsam)

❌ Yetersiz: *"retry ekle"*
✅ Net: *"src/handlers/webhook.py içine retry decorator. Mevcut send_webhook fonksiyonunu
sarmasın, yeni send_webhook_with_retry yaz."*

### 3. Nasıl test edilsin? (Doğrulama)

❌ Yetersiz: *"test et"*
✅ Net: *"tests/handlers/test_webhook.py içinde 3 unit test ekle: (a) ilk deneme başarılı,
(b) ikinci denemede başarılı, (c) üç denemede de fail. pytest -k webhook ile geçmeli."*

## Issue Type Seçimi

| Tip | Bot davranışı |
|---|---|
| **Story** | Acceptance Criteria (AC) okur, her AC'yi doğrulayan kod yazar. AC eksikse soru sorar. |
| **Task** | Serbest format - description'da yazanı yapar, AC beklemez. |
| **Bug** | Önce reproduce adımlarını çıkarır → failing test yazar → fix uygular → test geçtiğini doğrular. PR'da Root Cause + Fix Summary bölümleri olur. |
| **Epic** | Subtask'ları sırayla işler ([detay: SSS](faq.md#bot-epic-aldı-tüm-subtask’ları-yapacak-mı)). |

## Sık Sorulan Field'lar

### Repo (zorunlu - code_change task'larında)

Bot hangi repo'ya commit atacak? Form'da dropdown - sadece departmanına atanmış repo'ları
gösterir. Yeni repo varsa AI Admin'e başvur (`automation.repo_mappings` tablosuna ekleme
gerekir).

### Branch (opsiyonel)

Bot her zaman `ai/{issue_key}` branch'i açar (örn. `ai/PAY-4211`). Mevcut bir feature
branch'e commit yapılmasını istiyorsan **task'ı bot'a atayarak yapamazsın** - bu güvenlik
kuralı. Manuel commit at, sonra PR'a bot'u reviewer olarak ekle.

### Attachment (opsiyonel)

Hata screenshot'u, UI mockup'ı, log dosyası ekleyebilirsin. Bot Vision LLM ile
görüntüleri okur.

## Tek/Çift Repo'da Değişiklik?

⚠️ **Bot tek repo ile çalışır.** Backend ve frontend'i birlikte değiştirmek istersen iki
seçeneğin var:

1. **Ayrı task'lar** - backend için bir task, frontend için ayrı task. Sırayla bot'a ata.
2. **Epic + subtask** - Epic aç, repo başına subtask ekle, Epic'i bot'a ata. Bot
   subtask'ları sırayla yapar.

Tek task'ta iki repo isteyince bot kabul etmez (`out_of_scope` döner).

## Dil Seçimi

Bot, Jira/Confluence dilinizi takip eder. Departmanın **default dili** Türkçe ise bot
Türkçe konuşur; İngilizce ise İngilizce.

## Bot'a Atama

Form submit'i bot'u otomatik atar - manuel atama gerekmez. Eğer Jira'dan elle açtıysan:
**Assignee** field'ını bot kullanıcısına çevir (`payment-ai-bot` gibi). Bot 5 saniye
içinde harekete geçer.

## Sonra Ne Olur?

[Bot'u beklerken](waiting-for-bot.md) dosyasına bak.

---

**Yardım gerekiyor mu?**

- Bot'un yapamayacağı şeyler için: [`what-bot-cannot-do.md`](what-bot-cannot-do.md)
- Sıkça sorulan sorular: [`faq.md`](faq.md)
