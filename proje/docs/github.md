# GitHub okuma

Ajanın **herkese açık GitHub'da** depo arayıp okumasını ve cevaba kanıt olarak
katmasını sağlar. "En popüler Rust web framework'leri", "bu hafta trend olan
Python projeleri", "https://github.com/astral-sh/uv ne işe yarıyor",
"facebook/react ile vuejs/core'u karşılaştır" gibi istekler bu yolu kullanır.

**Sadece okur.** Yıldız verme, fork, issue/PR açma, yorum yazma yoktur;
`backend/github_client.py` içinde bunları yapabilecek bir fonksiyon da yoktur
ve gönderilen tek HTTP metodu `GET`'tir.

Bu faz yalnız **github.com'daki herkese açık depoları** kapsar. Özel depolar,
kod araması ve yazma işlemleri sonraki fazlara bırakıldı.

---

## Ne yapabilir

| İstek türü | Örnek | Ne olur |
| --- | --- | --- |
| **Keşif** | "en popüler python cli araçları", "langchain alternatifleri" | Depo araması; sonuç tablosu (depo, açıklama, yıldız, dil, son push) |
| **Trend** | "bu hafta trend olan rust projeleri", `github.com/trending/rust` | GitHub'ın trend sayfasının API'si yok; **yakın zamanda açılmış ve en çok yıldız alan** depolardan yaklaşık çıkarılır, cevapta bu belirtilir |
| **Depo bağlantısı** | `https://github.com/astral-sh/uv` | Depo bilgileri + README + kök dizin listesi |
| **Dosya bağlantısı** | `.../blob/main/src/app.py` | Dosyanın içeriği (512 KB'a kadar); "bu kodu açıkla / çevir" için kodun kendisi de sonraki işçiye gider |
| **Dizin bağlantısı** | `.../tree/main/src` | Dizin listesi |
| **Issue / PR bağlantısı** | `.../issues/123`, `.../pull/45` | Başlık, açıklama, son yorumlar; PR ise birleşti mi, kaç dosya, +/- satır |
| **Adıyla depo** | "facebook/react ile vuejs/core karşılaştır" | Yalnız kullanıcının **kendi yazdığı** `sahip/depo` adları okunur; model depo uyduramaz |

Diğer işçilerle birleşir: "langchain alternatiflerini Excel yap" → GitHub Agent
listeyi çıkarır, Document Agent Excel'i yazar.

**Web araştırmasında da kullanılır:** Research Agent bir github.com adresini
açmak istediğinde sayfa Chromium ile render edilmez, aynı API okuyucusundan
geçer (README veya dosyanın kendisi gelir, menü/navigasyon çöpü gelmez). API
okuyamazsa (ör. sınır dolduysa) normal tarayıcı yoluna düşer.

---

## Kurulum

Hiçbir şey gerekmez: özellik **varsayılan olarak açıktır** ve token olmadan
çalışır. Ama tokensiz sınır düşüktür, o yüzden token önerilir.

### Sınırlar

| | Token yok | Token var |
| --- | --- | --- |
| Genel istek | saatte **60** | saatte 5.000 |
| Arama | dakikada 10 | dakikada 30 |

Tokensiz sınır **IP başına** ve backend'i kullanan herkes için ortaktır. Bir
depo özeti ~3 istek, bir arama 1 istektir — yani tokensiz saatte kabaca 15-20
depo okunabilir. Sınır dolunca ajan bunu, ne zaman açılacağını ve çözümünü
söyler; uydurma cevap vermez.

### Token üret (önerilir, ~2 dakika)

1. <https://github.com/settings/personal-access-tokens/new>
   (Settings → Developer settings → Personal access tokens → **Fine-grained tokens**)
2. **Token name:** `agent-webapp-read`, **Expiration:** istediğiniz süre
3. **Repository access:** **Public repositories** (salt okunur)
4. **Permissions:** hiçbir şey ekleme — herkese açık okuma izin gerektirmez
5. Üret, `github_pat_...` değerini kopyala

### `.env`'ye yaz (repo kökü)

```env
GITHUB_ENABLED=true
GITHUB_TOKEN=github_pat_...
```

Kapatmak için `GITHUB_ENABLED=false`. GitHub Enterprise Server için
`GITHUB_API_URL=https://sunucu/api/v3`.

### Backend'i yeniden başlat ve doğrula

```powershell
docker compose up -d --build backend
curl http://127.0.0.1:8001/integrations/github/status
```

```json
{"ok": true, "authenticated": true, "core_remaining": 4998, "search_remaining": 30, "detail": "Bağlı (token ile): ..."}
```

Token ekledikten hemen sonra 60 saniyelik önbelleği atlamak için:
`curl -X POST http://127.0.0.1:8001/integrations/github/recheck`

---

## Nasıl çalışır

`backend/github_controller.py` sabit fazlar yürütür, tıpkı Jira ve web
araştırması gibi; model "GitHub'a git" diye keyfî bir tool çağırmaz:

1. **PLAN** — yalnız kullanıcının cümlesinden arama planı (anahtar kelime, dil,
   yıldız eşiği, tarih aralığı). Kullanıcının yazdığı bağlantılar plan ne derse
   desin okunur.
2. **SEARCH** — `/search/repositories`
3. **SELECT** — ayrıntı gerekiyorsa hangi depoların açılacağı
4. **READ** — depo bilgisi, README, kök dizin; dosya / dizin / issue / PR
5. **ANSWER** — okunanlardan kısa özet; sonraki işçiye (Assistant / Document)
   özet + tablo + bağlantılar gider

| Dosya | Görev |
| --- | --- |
| `github_query.py` | Bağlantı ayrıştırma, ad/yol doğrulama, arama metni |
| `github_client.py` | REST istemcisi: yalnız GET, sabit host, sınır/hata sınıflandırma, ETag önbelleği |
| `github_controller.py` | PLAN→SEARCH→SELECT→READ→ANSWER, notlar, web okuyucu kancası |
| `github_api.py` | `/integrations/github/status` ve `/recheck` |

Aynı depo kısa sürede tekrar okunursa (ör. takip sorusu) cevap önbellekten
gelir; 10 dakikadan eskiyse ETag ile doğrulanır — GitHub `304` cevabını
sınırdan düşmez.

---

## Güvenlik

- **Yalnız okur.** İstemcide `GET` dışında metot yok; testler bunu doğrular.
- **Sabit host.** İstekler yalnız `GITHUB_API_URL`'e gider. Yol parçaları
  doğrulanır (`..`, `\`, kontrol karakteri reddedilir) ve URL-kodlanır; model
  çıktısı yalnız arama sorgusunun `q` değerine ulaşır. Yönlendirme yalnız aynı
  API hostunda izlenir (yeniden adlandırılmış depolar için), token başka hosta
  taşınmaz.
- **README'ler yabancıların metnidir.** README'ler ve kod yorumları, yapay
  zeka ajanlarına talimat bırakmak için bilinen bir yerdir. Okunan her şey
  `untrusted_content` ile alıntı çitine alınır; talimat benzeri metin
  işaretlenir ve kullanıcıya bildirilir. README'de geçen hiçbir adres açılmaz,
  hiçbir komut çalıştırılmaz, e-posta alıcısı buradan gelemez.
- **Token log'a yazılmaz.** `GitHubConfig` token'ı `repr`'den gizler; hata
  mesajları yalnız durum kodu ve GitHub'ın kendi mesajından oluşur.
- Bu uygulamada giriş yoktur; token herkese açık okuma yetkisindedir, yani
  arayüze erişen biri bu token'ın okuyabileceğinden fazlasını okuyamaz.

---

## Sorun giderme

| Belirti | Neden / çözüm |
| --- | --- |
| "GitHub istek sınırı doldu" | Tokensiz 60/saat bitti. `GITHUB_TOKEN` ekleyin ya da mesajdaki süre kadar bekleyin |
| "GitHub token'ı reddedildi (401)" | Token yanlış veya süresi dolmuş; yenisini üretin ya da satırı boşaltın |
| "bulunamadı (özel, silinmiş ya da adı yanlış)" | Depo özel olabilir — bu faz yalnız herkese açık depoları okur |
| Sağ panelde GitHub Agent yok | `GITHUB_ENABLED=false` ya da `GITHUB_API_URL` geçersiz; `/integrations/github/status`'a bakın |
| Trend listesi github.com/trending ile birebir değil | Beklenen: trend sayfasının API'si yok, liste yaklaşık hesaplanır |
| `AGENT_EXECUTOR=camel` iken GitHub Agent çalışmıyor | GitHub Agent yalnız LangGraph yolunda var. CAMEL yolunda github.com adresleri yine de web okuyucusu üzerinden API ile okunur |
