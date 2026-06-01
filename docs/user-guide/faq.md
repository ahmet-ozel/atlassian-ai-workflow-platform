# SSS — Sıkça Sorulan Sorular

> 25 soru, kısa cevap. Daha geniş bilgi için ilgili sayfaya yönlendirme bağlantıları.

## Genel

### 1. Bot ne yapar?

Jira task'ına atanırsan ne yapacağını description'dan çıkarır: kod yazar (PR açar), test
çalıştırır, Confluence sayfası günceller, araştırma yapar, transition uygular.
**Merge etmez ve sayfa silmez.**

### 2. Bot ne kadar sürer?

İkinci comment'te kendi tahminini paylaşır. Tipik: doc update 3-5 dk, kod+test 10-30 dk.
[Detay: `waiting-for-bot.md`](waiting-for-bot.md#süre-tahmini).

### 3. Bot maliyeti ne kadar?

İkinci comment'te tahmin verir (`$0.41` gibi). Limit'i aşmadan önce onayını ister.
[Detay: §16.6.8](../../MIMARI.md).

### 4. Bot Türkçe mi İngilizce mi konuşur?

Departmanın default diline göre. Türkçe departmanda Türkçe, İngilizce departmanda
İngilizce. [Detay: §16.6.53](../../MIMARI.md).

## Task Açma

### 5. Description ne kadar detaylı olmalı?

Üç soruya cevap verecek kadar: **(1)** ne istiyorsun, **(2)** nereye dokunulsun,
**(3)** nasıl test edilsin? [Örnek: `task-creation.md`](task-creation.md#description-nasıl-yazılır).

### 6. Issue type ne fark eder?

Bot tipini okur ve davranışını ayarlar. Story → AC odaklı, Bug → reproduce + fix +
regression test, Task → serbest, Epic → multi_step.
[Detay: `task-creation.md`](task-creation.md#issue-type-seçimi).

### 7. Hangi repo'lar bot'a açık?

Streamlit Task Creator'daki dropdown'da görünür — departmanına atanmış repo'lar. Yenisi
yoksa AI Admin'e başvur.

### 8. Tek task'ta iki repo değiştirebilir mi?

❌ Hayır. Ayrı task'lar veya Epic + subtask kullan.
[Detay: `what-bot-cannot-do.md`](what-bot-cannot-do.md#multi-repo-task’lar).

## Bekleme & Bildirim

### 9. Bot task'ımı görmedi mi?

5 saniye içinde *"Task alındı"* comment'i yoksa: assignee bot mu, issue durumu uygun
mu, departman tanımlı mı? [Detay: `waiting-for-bot.md`](waiting-for-bot.md#bot-task’ımı-görmedi-mi).

### 10. Bot soru sordu, ne yapayım?

Comment ile cevap ver. Bot 7 güne kadar bekler. Cevap vermek istemiyorsan `[cancel]`
yaz.

### 11. Bot 30 dakika sürdü, normal mi?

`code_change_with_test` workflow'u 10-30 dk normal aralık. 60 dk+ uzunsa AI Admin'e
bildir — prompt veya repo özel sebebi olabilir.

### 12. Bot hata aldıysa nereden öğrenirim?

Jira'da `❌ Hata aldım: ...` comment'i + departmanına göre Slack/email bildirimi.
[Detay: §16.7.17](../../MIMARI.md).

### 13. Bot bittiyse ne göreceğim?

Jira'da `✅ Tamamlandı` comment'i + (varsa) PR linki, Confluence linki, test sonuçları.
Provenance footer `🔎` simgesinin arkasında gizli.

## İterasyon

### 14. `[fix]` nasıl çalışır?

Comment'i `[fix]` ile başlat → bot mevcut PR'a yeni commit atar.
[Detay: `iteration-with-comments.md`](iteration-with-comments.md).

### 15. Kaç kez `[fix]` yapabilirim?

Default 5. Sonrası: yeni task aç.

### 16. PR merge edildikten sonra `[fix]` çalışır mı?

Bot yeni branch + yeni PR açar (`ai/PAY-4211-iter-2`). Önceki branch silinmişse
problem değil.

### 17. `[cancel]` nasıl çalışır?

Comment olarak `[cancel]` yaz → bot temiz şekilde durur, branch siler.

### 18. Bot aynı hatayı tekrarlıyor

Description ve önceki comment'ler çelişebilir. Description'ı netleştir, yeni task aç.

## PR & Merge

### 19. Bot PR'ı merge edebilir mi?

❌ Hayır. **Daima draft PR** açar; sen merge edersin. [Detay: §1 Kural 10](../../MIMARI.md).

### 20. Draft PR'da CI çalışmıyor

Departman config'inde `pr_draft_strategy=open_after_test_pass` aç → bot CI'yi
geçince PR'ı open'a çeker. [Detay: §16.7.18](../../MIMARI.md).

### 21. PR açıldı ama CI fail

Departman `auto_fix_on_ci_fail` açıksa bot otomatik `[fix]` çalıştırır. Aksi halde
sen `[fix]` yaz. [Detay: §16.6.47](../../MIMARI.md).

### 22. Bot Confluence'a yazınca ne olur?

Departmanın space'inde sayfa oluşturur veya günceller. Sayfa silinmişse sana sorar
(yeni sayfa? task'a comment? iptal?). [Detay: §16.7.15](../../MIMARI.md).

## Limit & Güvenlik

### 23. Bot Epic aldı, tüm subtask'ları yapacak mı?

Evet — `multi_step` workflow'uyla sırayla. Bir subtask fail ederse Epic durur, sana
bildirir. [Detay: §16.6.14](../../MIMARI.md).

### 24. Bot kritik dosyaya dokunacaksa ne olur?

Departman config'inde `approval_required_paths` listesindeki path'lerde önce onay ister.
[Detay: §16.7.9](../../MIMARI.md).

### 25. Bot başka departmanın repo'suna erişemez mi?

❌ Hayır. Atlassian seviyesinde yetki sınırı + sistem seviyesinde capability gate. Bot
yanlış dept'e atanırsa anında reddeder. [Detay: §16.6.16](../../MIMARI.md).

---

**Cevabını bulamadın mı?**

- Detaylı mimari: [`MIMARI.md`](../../MIMARI.md)
- AI Admin Slack kanalı (canlı destek)
- Issue olarak repo'da: `ai-admin` ekibi yanıtlar
