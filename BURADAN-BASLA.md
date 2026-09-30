# agent-webapp: buradan başla

Bu klasör `agent-webapp` projesinin **tam ve çalışır** hâlidir. Projeyi hiç
bilmiyor olabilirsin; bu dosyayı yukarıdan aşağı takip edersen sistem ayağa
kalkar. GitHub hesabına veya özel bir erişime **ihtiyacın yok**.

---

## Klasörde ne var

| Klasör / dosya | Ne |
| --- | --- |
| `BASLANGIC.md` | **Başlangıç rehberi.** Kurulum, günlük kullanım, açılabilir özellikler, sorun giderme |
| `HANDOVER.md` | **Asıl rehber.** 13 bölüm: mimari, kullanım, test, bilinen açıklar |
| `README.md` | Kısa tanıtım |
| `.env.example` | Ayar şablonu. Kendi `.env` dosyanı bundan yapacaksın |
| `docker-compose.yml` | Bütün servisleri birlikte başlatan Docker ayarı |
| `backend/` | FastAPI sunucu: ajanlar, web araştırması, belge üretimi, testler |
| `frontend/` | React arayüz |
| `docs/` | Konu konu ayrıntılı belgeler (Jira, Confluence, Outlook, belge üretimi, zamanlanmış görevler…) |
| `vendor/agent-search/` | AgentSearch arama servisi, repoya dahil |
| `searxng/` | Arama motoru ayarı |
| `brand/` | Üretilen dosyalardaki kurumsal renk ve logo |
| `scripts/` | Bilgisayarında (Docker dışında) çalışan köprü: "bilgisayarımda aç" ve Outlook |

---

## Proje ne yapıyor

Tarayıcıdan konuştuğun bir ajan uygulaması. Soru sorarsın; sistem ya sohbet
eder, ya web'den araştırır, ya yüklediğin belgeyi okur, ya da Word / PDF /
PowerPoint / Excel / HTML dosyası üretir. Açılırsa şirketin Jira'sını,
Confluence'ını ve Outlook posta kutusunu da okur.

İki ana parça var: **React arayüz** ve **FastAPI sunucu**. Yanlarında iki
yardımcı servis çalışır (SearXNG arama motoru, AgentSearch). Dördü de Docker'da.

---

## Kurulum

### Adım 0: Bilgisayarda ne olmalı

| Gereken | Not |
| --- | --- |
| **Docker Desktop** | Windows'ta WSL2 açık olmalı. <https://docker.com> |
| **İnternet** | Build sırasında paketler inecek (aşağıda açıklandı) |
| **Model erişimi** | OpenAI uyumlu bir endpoint: adres, model adı, API anahtarı |
| Disk | ~8 GB boş yer |
| RAM | 8 GB altı zorlanır |

**Git'e ihtiyacın yok.** Klasörü aldıysan dosyalar zaten elinde.

> **İnternet neden gerekli?** Kod burada, ama Docker build sırasında Python
> paketleri (PyPI), npm paketleri, Playwright Chromium ve PyTorch iniyor.
> Yani "GitHub'a erişimim yok" sorun değil; "hiç internetim yok" sorun.

### Adım 1: Klasöre gir

PowerShell aç ve bu klasöre geç:

```powershell
cd <bu-klasorun-yolu>
dir
```

`backend`, `frontend`, `docker-compose.yml`, `HANDOVER.md` görmelisin.

Bir de şunu kontrol et, boş olmamalı:

```powershell
dir vendor\agent-search
```

`app`, `adapters`, `Dockerfile` görünüyorsa her şey yerinde.

### Adım 2: Ayar dosyasını oluştur

```powershell
copy .env.example .env
notepad .env
```

Açılan dosyada **üç satırı** kendi bilgilerinle doldur:

```env
MODEL_BASE_URL=https://senin-endpointin/v1
MODEL_NAME=kullandigin-model-adi
MODEL_API_KEY=anahtarin
```

Örnek (OpenRouter kullanıyorsan):

```env
MODEL_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=deepseek/deepseek-v4-flash
MODEL_API_KEY=sk-or-...
```

Kaydet, kapat.

### Adım 3: Başlat

```powershell
docker compose up --build
```

**İlk çalıştırma 15-30 dakika sürer.** Donmuş değil; PyTorch, Chromium ve
Docling iniyor. Ekranda akan satırlar normaldir.

Sonraki başlatmalarda saniyeler sürer:

```powershell
docker compose up -d
```

### Adım 4: Çalıştığını doğrula

Tarayıcıda aç:

| Adres | Ne görmelisin |
| --- | --- |
| <http://127.0.0.1:8081> | Uygulamanın arayüzü, workspace kartları |
| <http://127.0.0.1:8001/docs> | API dokümantasyonu (Swagger) |
| <http://127.0.0.1:3939/health> | `{"status":"healthy"...}` |

Sonra arayüzde:

1. Bir workspace oluştur
2. Alttaki kutuya **Single** modunda `2+2 kaç` yaz, gönder
3. Cevap geldiyse model bağlantın çalışıyor
4. **Workforce** moduna geç ve `İstanbul hakkında kısa bir PDF hazırla` dene
5. İndirilebilir bir PDF çıkıyorsa sistem tam çalışıyor

### Adım 5: Durdurmak

```powershell
docker compose down
```

Verilerin (sohbetler, ürettiğin dosyalar) durur. `proje_backend-data` adlı
Docker volume'unda saklanırlar; klasörün adını değiştirsen de kaybolmazlar.

> ⚠️ `docker compose down -v` yazma. Sondaki `-v` bütün sohbet geçmişini ve
> üretilmiş dosyaları **kalıcı olarak siler**.

---

## Sonra ne okumalı

Önce `BASLANGIC.md`: kurulumun tamamı, her gün neyin başlatılacağı,
zamanlanmış görev / Outlook e-postası / OCR / Jira gibi özelliklerin nasıl
açıldığı.

Sonra `HANDOVER.md`, asıl rehber. Sırasıyla:

| Bölüm | Ne anlatır |
| --- | --- |
| § 1 | Proje nedir, LangGraph çalıştırıcı, reponun geçmişi |
| § 2-3 | Kurulum ve ayarların tamamı |
| § 4 | Arayüzün nasıl kullanılacağı, hangi modda ne yapılır |
| § 5 | Mimari: bir istek uçtan uca nasıl işleniyor |
| § 6 | Kod haritası: hangi dosya ne işe yarar |
| § 10 | Testler nasıl koşulur |
| § 11 | **Bilinen açıklar**: neyin bitmediği, dürüstçe |
| § 12 | Sorun giderme |

Tek bir konuya derin bakacaksan `docs/README.md` hangi belgenin neyi
anlattığını listeler.

---

## Bu klasörde bilerek olmayan şeyler

1. **`.env` yok.** İçinde API anahtarı olduğu için hiçbir kopyaya konmaz.
   Adım 2'de kendin oluşturuyorsun.
2. **Commit geçmişi olmayabilir.** Klasörü kopya olarak aldıysan içinde
   `.git` bulunmayabilir. Kod eksiksiz çalışır, ama `git log` ile "bu satır
   neden böyle" diye bakamazsın. Geliştirmeye devam edeceksen projeyi teslim
   eden kişiden GitHub erişimi iste:

   ```text
   https://github.com/SultanLikedIt/cowork
   ```

   Varsayılan dal `main`, güncel olan odur.
