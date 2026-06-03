# Bot'u Beklerken

> Task açıldı, bot çalışıyor. Ne anda ne göreceksin, ne kadar süre normal?

## Zaman Çizelgesi

```
0s     ───  Task bot'a atandı (Jira webhook tetiklenir)
5s     ───  🤖 "Task alındı, analiz ediyorum..."     ← hızlı ack
1-2dk  ───  🤖 "Plan hazır. Tahmini süre: ~12 dk,
                tahmini maliyet: $0.41. Workflow: ..."  ← analiz sonucu
3-30dk ───  Bot çalışıyor (kod yazma, test, output_actions)
            Bu sırada ek comment'ler:
              - "PR draft açıldı: <link>"
              - "Test geçti: 12/12"
              - "Confluence sayfası güncellendi: <link>"
sonunda───  🤖 "✅ Tamamlandı." veya
            🤖 "❌ Hata aldım — ..."  veya
            🤖 "❓ Şu bilgiye ihtiyacım var: ..."
```

## Bot Task'ımı Görmedi mi?

5 saniye içinde *"Task alındı"* comment'i görmediysen:

1. **Assignee'yi kontrol et** — Jira'da issue assignee'si gerçekten bot kullanıcısı mı?
   (`payment-ai-bot` gibi bir hesap)
2. **Issue'nun durumunu kontrol et** — Bot sadece "To Do", "Open", "In Progress"
   durumundaki issue'ları alır. "Done", "Closed", "Resolved" ise webhook'u görmezden gelir.
3. **Departman eşleşmesi** — Issue'nun project key'i bir departmana atanmış mı? Atanmamışsa
   bot sessiz fail eder. AI Admin'e haber ver.
4. **30 saniye bekle** — webhook gecikmesi olabilir. Hâlâ yoksa AI Admin Slack kanalı.

> **Not:** Atlassian webhook'larında nadir kayıp olabiliyor. Sistem `automation-service`
> her 5 dakikada bir "atanmış ama webhook gelmemiş" task'ları tarar ve eksik webhook'ları
> retroaktif başlatır.

## Süre Tahmini

İkinci comment'te bot kendi tahminini paylaşır. Tipik değerler:

| Workflow tipi | Tipik süre |
|---|---|
| Tek satır comment / transition | 1-2 dk |
| Confluence doc update | 3-5 dk |
| Kod yazma (PR aç, test yok) | 5-10 dk |
| Kod + test (`code_change_with_test`) | 10-30 dk |
| Araştırma + Confluence yayınla | 5-15 dk |
| Epic (multi_step, N subtask) | N × subtask süresi |

> Bot tahmini gerçekle eşleşmiyorsa AI Admin haftalık dashboard'da görür ve prompt'u
> kalibre eder.

## "Çok Uzun Sürüyor" Hissi

Bot 30+ dakika tahmin etti ama sen 10 dakika bekleyebilecek durumdasın:

- **İptal et** — Jira'da `[cancel]` yazarak comment ekle. Bot temiz şekilde durur,
  branch'i siler, commit etmez.
- **Workflow'u izle** — Streamlit "Workflows" sayfası → bot şu an hangi step'te?
- **Maliyet endişesi** — bot tahmini maliyetin %70'ine ulaşırsa zaten sana onay sorar.

## Bot Soru Sorduğunda

Bot bazen `🤖 Şu bilgiye ihtiyacım var: ...` der. Bu durumda:

1. **Comment ile cevap ver** (Jira'ya). Bot 7 güne kadar bekler.
2. Cevap bot'u tatmin etmezse tekrar sorar (max 5 kez — sonra `[fix]` döngüsünü
   sonlandırır).
3. Cevap vermek istemiyorsan: `[cancel]` yazarak iptal et.

> Bot'un sorusu net değilse: cevap olarak *"daha açık sor"* yazabilirsin. LLM yeniden
> formüle eder.

## "Bot Bunu Yapamam Dedi" (`out_of_scope`)

Bazı task'lar bot'un kapsamı dışında: production DB değişikliği, müşteri iletişimi,
sözleşme imzalama gibi. Bot bunları reddeder ve alternatif önerir. Detay:
[`what-bot-cannot-do.md`](what-bot-cannot-do.md).

## Beklerken Başka Task'lar Aç

Bir task'ı bot'a atadıktan sonra başka task'lar da açabilirsin — bot paralel çalışır.
Ancak departmanına concurrency limit'i tanımlıdır (default 5 paralel iş —
departman config'inde değiştirilebilir). Limit aşılırsa bot kuyruğa alır, biri bitince
sıradaki başlar.

## Bot Tamamlayınca

`✅ Tamamlandı` comment'inde şunlar olur:

- Yapılan iş özeti
- PR linki (varsa) — **draft** olarak açılır, sen merge edersin
- Confluence sayfa linki (varsa)
- Test sonuçları (varsa)
- Provenance footer (gizli — `🔎` simgesine tıklayarak aç)

> **Önemli:** Bot **asla merge etmez**. PR'ı incele, gerekiyorsa düzeltme iste,
> uygunsa kendin merge et. [Detay: SSS](faq.md#bot-pr’ı-merge-edebilir-mi).

---

**Sonraki adım:**

- Bot çıktısını beğenmedinse → [`iteration-with-comments.md`](iteration-with-comments.md)
- Bot tamamlamadan önce sorun çıktıysa → [`faq.md`](faq.md#troubleshooting)
