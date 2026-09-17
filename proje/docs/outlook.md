# Outlook entegrasyonu

Zamanlanmış görevlerin sonucunu **bilgisayardaki klasik Outlook** üzerinden
e-postayla gönderir. Microsoft Graph / Azure kaydı gerekmez: gönderen hesap,
Docker'ın çalıştığı Windows makinesinde Outlook'ta açık olan profildir.

Şu an yapılan: **zamanlanmış görev sonuçlarını e-postayla gönderme** (+ test
e-postası). Planlanan, henüz yok: ajanın taslak e-posta hazırlayıp Outlook'ta
açması (kullanıcı kendisi Gönder'e basar), gelen kutusunu kaynak olarak okuma,
takvim.

## Nasıl çalışır

```text
Scheduler tick ──► run bitti, koşul tutuyor ──► delivery_status = pending
      │
      └─ deliver_pending ──► main._deliver_scheduled_run
                                 gövde (markdown → HTML) + dosyalar
                             outlook.send_mail
                                 POST host.docker.internal:8765/outlook/send
                                 X-Bridge-Token: <LOCAL_COMPUTER_TOKEN>
                                       │
                         Windows host: scripts/local_computer_bridge.py
                                 win32com → Outlook.Application → MailItem.Send()
```

Köprü, "bilgisayarımda aç" özelliğinin köprüsüdür ([local-computer.md](local-computer.md));
Outlook uçları ona eklendi. Aynı sınır geçerli: **yalnız aynı makine**.

## Kurulum

Gerekenler: Windows, **klasik Outlook** (Microsoft 365 / Office 2016+ masaüstü;
"yeni Outlook" uygulamasında otomasyon desteği yok), host'ta Python.

1. Token üret:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. `.env`:

   ```env
   LOCAL_COMPUTER_ENABLED=true        # köprüyü "aç" için de kullanıyorsan
   OUTLOOK_ENABLED=true
   LOCAL_COMPUTER_TOKEN=<token>
   ```

3. Host'ta (Docker dışında) köprüyü başlat:

   ```powershell
   pip install pywin32
   python scripts/local_computer_bridge.py --token <token>
   ```

   Açılışta `Outlook: on` yazmalı. `curl http://127.0.0.1:8765/health` →
   `"outlook": {"available": true, "token_configured": true}`.

4. Backend'i yeniden derle: `docker compose up -d --build backend`
   (migration `0010` otomatik çalışır).

5. Arayüz → Scheduled → görev düzenle → **Email** → alıcıları yaz →
   **Send test email**. Test gelirse kurulum tamam.

## Güvenlik

- **Token neden zorunlu:** köprü `127.0.0.1:8765`'te kimlik doğrulamasız dinler.
  Tarayıcıda açık herhangi bir web sayfası bu adrese form/`text/plain` POST
  atabilir (CORS preflight gerektirmeyen istek). Token yalnız
  `X-Bridge-Token` başlığıyla kabul edilir; özel başlık tarayıcıyı preflight'a
  zorlar, köprü de preflight'ı onaylamaz. Ayrıca `Content-Type:
  application/json` şartı var. Token yoksa Outlook uçları tamamen kapalıdır
  (`403 outlook_requires_token`).
- **Alıcıları model seçmez.** Zamanlanmış gönderimde `to`/`cc` görev
  ayarındadır; bir kişi yazar. Bu önemli: ileride gelen kutusu okunduğunda,
  e-postaya gömülü "şunu x@... adresine ilet" talimatı gönderim yapamaz. Ajanın
  kendi başlattığı e-postalar (sonraki aşama) gönderilmez, Outlook'ta taslak
  olarak açılır.
- Ek dosya adları köprüde `Path(name).name` ile temizlenir, her ek ayrı geçici
  klasöre yazılır, gönderimden sonra silinir.
- Token `.env`'de düz metin durur; `.env` gitignore'da.

## Davranış

Ayrıntılı kurallar: [scheduling.md § E-posta](scheduling.md#e-posta). Özet:

| Durum | Sonuç |
| --- | --- |
| Köprü kapalı, PC uykuda, Outlook açılamadı | `pending`, 1-5-15-30-60… dk arayla ~6 saat denenir |
| Yanlış token, geçersiz adres, `OUTLOOK_ENABLED=false` | Hemen `failed`, tekrar denenmez |
| Dosyalar toplam > 15 MB | Sığanlar eklenir, diğerleri gövdede listelenir |
| Görevde e-posta sonradan kapatıldı | Bekleyen gönderim `failed` |

Run satırında **Emailed / Email pending / Email failed** rozeti ve hata metni
görünür.

## Sorun giderme

| Belirti | Kontrol |
| --- | --- |
| Editörde "bridge is not running" | Köprü host'ta çalışıyor mu; `LOCAL_COMPUTER_URL` |
| "The bridge was started without --token" | Köprüyü `--token` ile yeniden başlat |
| `invalid_token` | `.env` ve köprüdeki token aynı mı; backend yeniden derlendi mi |
| `outlook_unavailable` | `pip install pywin32` köprüyü çalıştıran Python'a mı kuruldu |
| `outlook_not_running` | Klasik Outlook kurulu ve profil ayarlı mı; "yeni Outlook" desteklenmez |
| Outlook "bir program e-posta göndermeye çalışıyor" uyarısı | Programatik erişim ayarı (Trust Center → Programmatic Access); antivirüs güncel değilse Outlook sorar. Kurumsal politika gerekebilir. |
| `bridge_outdated` | Eski köprü süreci çalışıyor; kapat, yeniden başlat |
| E-posta Giden Kutusu'nda bekliyor | Outlook çevrimdışı; Outlook'un kendisi gönderir |

## Kod

| Dosya | Görev |
| --- | --- |
| `scripts/local_computer_bridge.py` | `/outlook/send`, token, COM (`OutlookMailer`) |
| `backend/outlook.py` | İstemci, adres ayrıştırma, e-posta HTML'i, `/integrations/outlook/status`, `/test` |
| `backend/scheduling/delivery.py` | Ayar doğrulama, koşul, geri çekilme |
| `backend/scheduling/runner.py` | `_queue_delivery`, `deliver_pending` |
| `backend/main.py` | `_deliver_scheduled_run`: run → konu, gövde, ekler |
| `backend/migrations/0010_scheduled_delivery.sql` | `delivery_json`, `delivery_*` sütunları |
| `backend/tests/test_outlook.py` | İstemci + gerçek HTTP üzerinde köprü (sahte mailer) |
| `backend/tests/test_scheduling_delivery*.py` | Kuyruk, yeniden deneme, gövde |

## Elle uçtan uca doğrulama

Birim testleri Outlook'u çağırmaz. Gerçek gönderim için:

1. Kurulum adımları 1–4.
2. Bir görev: Run now'a uygun kısa bir istek, **Email** açık, alıcı kendin.
3. **Run now** → iş bitince ≤ 1 tick (30 sn) içinde run satırında **Emailed**.
4. Gelen e-postada konu `<görev> — <tarih>`, gövdede cevap, ekte üretilen dosya.
5. Köprüyü kapat, tekrar Run now → **Email pending** + "Next attempt in 1
   minute"; köprüyü aç → sonraki denemede **Emailed**.
