# Reddit okuma (social read)

Ajanın **herkese açık Reddit tartışmalarını** okuyup cevaba kanıt olarak
katmasını sağlar. "Bu telefon hakkında ne diyorlar", "r/LocalLLaMA'da neler
konuşuluyor", "kullanıcı şikayetlerini PDF yap" gibi istekler bu yolu kullanır.

**Sadece okur.** Oy verme, yorum yazma, abone olma, mesaj gönderme yoktur ve
bunların komutu üretilemez (`backend/social/registry.py`, `REDDIT.verbs`).

---

## Neden ekstra kurulum gerekiyor

Reddit'in **anonim erişimi yok**. Ölçüldü: oturumsuz bir arama
`{"ok": false, "error": {"code": "forbidden"}}` döner. Resmî API ise 2025-11'den
beri elle onaylı ve kişisel projelere pratikte verilmiyor.

Dolayısıyla iki parça gerekir:

| Parça | Nerede | Durum |
| --- | --- | --- |
| `rdt-cli` | Backend imajının içinde (Dockerfile) | Otomatik gelir |
| Oturum çerezi | `/data/social-sessions/home/.config/rdt-cli/credential.json` | **Elle** |

---

## Kurulum

### 1. Hesap

**Kendi ana hesabınızı kullanmayın.** Bu araçlar resmî olmayan kazıyıcılardır;
Reddit kullanım şartları otomatik erişime kapalıdır ve hesap kilitlenebilir.
Bunun için açılmış ayrı bir hesap kullanın.

### 2. Çerezi al

Tarayıcıdan otomatik alma **çalışmaz** — Chrome 127+ çerezleri uygulamaya
bağlı şifreleme ile saklar, `rdt login` bunları okuyamaz (Chrome'u kapatmak
da çözmez). Elle alınır:

1. Chrome'a [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   eklentisini kur
2. `reddit.com`'u **o hesapla giriş yapmış** halde aç
3. Cookie-Editor → `reddit_session` → Value'yu kopyala

### 3. Çerezi kaba yaz

```powershell
docker compose exec backend sh -c "mkdir -p /data/social-sessions/home/.config/rdt-cli"
docker compose exec backend sh -c "cat > /data/social-sessions/home/.config/rdt-cli/credential.json" 
```

Sonra şu içeriği yapıştırıp Ctrl+D:

```json
{"cookies": {"reddit_session": "<KOPYALADIGIN_DEGER>"},
 "source": "manual", "username": "<hesap_adi>",
 "modhash": null, "saved_at": 0, "last_verified_at": null}
```

> Değer hesabın **parolasına denktir**. Sohbete, log'a, ekran görüntüsüne
> koymayın. `/data` volume'unda kalır, rebuild'de silinmez.

### 4. Doğrula

```bash
curl http://127.0.0.1:8001/integrations/social/status
```

`"ok": true` ve `"authenticated": true` görmelisiniz. Değilse `detail` alanı
sebebi yazar.

---

## Çerez ~7 günde bir yenilenir

rdt-cli'nin çerezi yaklaşık bir hafta geçerlidir ve normalde tarayıcıdan
kendini yeniler. **Kapta tarayıcı olmadığı için bu asla çalışmaz**, yani
adım 2–3 haftalık bir iştir.

Sistem bunu gizlemez:

- `GET /integrations/social/status` → `cookie_age_days` ve `stale_soon`
- 6 günü geçince `detail` uyarır, `remedy` ne yapılacağını yazar
- Süresi dolduğunda iş **hata vermez**: "Reddit okunamadı, çerez yenilenmeli"
  diye açıkça cevaplar
- `POST /integrations/social/recheck` → 60 sn'lik önbelleği atlayıp hemen bakar

> **Zamanlanmış görevler:** çerez ölünce sosyal okuma yapan görev üst üste
> başarısız olur ve `SCHEDULE_MAX_CONSECUTIVE_FAILURES` (varsayılan 3) sonrası
> devre dışı kalır. Haftalık yenilemeyi atlarsanız görev kendiliğinden durur.

---

## Nasıl çalışıyor

```text
artifact_intent  reads_social  → capabilities.SOCIAL_READ
                                    ↓
workforce        ROLE_SOCIAL ("Social Agent")  ← kanıt toplar, cevabı YAZMAZ
                                    ↓
social_controller  PLAN → SEARCH → SELECT → READ
                     │       │        │        └─ rdt read  (gönderi + yorumlar)
                     │       │        └────────── hangileri açılsın (LLM)
                     │       └─────────────────── rdt search / sub / popular
                     └─────────────────────────── SADECE kullanıcı isteğini okur
                                    ↓
untrusted_content.render  → tahmin edilemez işaretçili alıntı bloğu
```

| Dosya | Görev |
| --- | --- |
| `backend/social/registry.py` | Hangi komutlar var, argümanlar nasıl doğrulanır |
| `backend/social/runner.py` | Süreç sınırı: temizlenmiş env, shell yok, bütçeler |
| `backend/social/parse.py` | Upstream JSON → kayıt (iki ayrı şekil) |
| `backend/social/health.py` | Kurulu mu, oturum var mı, çerez kaç günlük |
| `backend/social_controller.py` | Faz sırası ve bütçeler |
| `backend/untrusted_content.py` | Yabancı metni alıntılama katmanı |

---

## Güvenlik

**Prompt injection.** Reddit'e herkes yazabilir; gönderi ve yorumlar modele
talimat vermeye çalışabilir. Savunma dört katmanlı:

1. Alıntı bloğunun kapanış işaretçisi her blokta **rastgeledir**, gönderi
   tahmin edip erken kapatamaz
2. **PLAN fazı hiçbir çekilen metni görmez** — gönderi aramayı yönlendiremez
3. SELECT'e giden başlıklar da temizlenir (başlık da saldırganın yazdığı metin)
4. Görünmez karakterle gizlenmiş talimatlar (Unicode tag, bidi, zero-width)
   çözülür, bildirilir ve metinden çıkarılır

İçerik **silinmez, işaretlenir**: "Reddit'te prompt injection hakkında ne
konuşuluyor" meşru bir sorudur.

**Yapamayacakları.** Gönderi e-posta alıcısı ekleyemez (alıcılar yalnızca
kullanıcının kendi yazdığı adreslerden gelir), içindeki bağlantılar
açılmaz, komut çalıştırılmaz, Reddit'e hiçbir şey yazılmaz.

**Kimlik bilgisi.** Çerez yalnızca `rdt` alt sürecine geçer; `MODEL_API_KEY`,
`LOCAL_COMPUTER_TOKEN` gibi değişkenler o sürece **verilmez**. Hata
mesajlarında çerez değeri varsa maskelenir.

---

## Sorun giderme

| Belirti | Sebep / çözüm |
| --- | --- |
| `"detail": "'rdt' bulunamadı"` | İmaj eski. `docker compose up -d --build backend` |
| `"authenticated": false` | Çerez yok/bozuk. Adım 2–3'ü tekrarla |
| `forbidden` | Çerezin süresi doldu (en sık sebep) |
| `rate_limited` | Çok sık istek. Bekle; `SOCIAL_MAX_CONCURRENT` düşür |
| Sonuçlar alakasız | `top` sıralaması zaman aralığı ister; sistem yoksa 1 yıl uygular |
| Türkçe sorgu boş dönüyor | Reddit ağırlıklı İngilizce; PLAN fazı sorguyu İngilizceye çevirir |

Son iş ne yaptı:

```bash
docker compose exec backend python -c "
import sqlite3, json
db = sqlite3.connect('/data/app.db')
row = db.execute(\"select usage from jobs order by created_at desc limit 1\").fetchone()
print(json.dumps(json.loads(row[0]), indent=2, ensure_ascii=False))"
```

`reads_social` alanı bu işin Reddit okuyup okumadığını söyler.

---

## Sınırlar

- **Sadece Reddit.** Facebook/Instagram/X yok: tek çalışan yolları masaüstünde
  açık ve giriş yapılmış bir Chrome gerektiriyor (OpenCLI), kap içinde
  mümkün değil. Bunlar için `scripts/local_computer_bridge.py` genişletilmeli.
- `rdt-cli` resmî değil ve upstream 2026-03'ten beri güncellenmiyor; sabit bir
  commit'e pinlenmiştir. Reddit arayüzünü değiştirirse bozulur —
  `/integrations/social/status` bunu görünür kılar, iş sessizce yanlış cevap
  vermez.
- Arama **anahtar kelime** tabanlıdır, anlamsal değil.
