# Başlangıç rehberi — sıfırdan çalışır hale

Bu dosya **her şeyi baştan sona** anlatır: kurulum, günlük kullanım, açılabilir
özellikler (web araştırması, OCR, zamanlanmış görevler, Outlook e-posta,
"bilgisayarımda aç"), doğrulama komutları ve sık takılınan yerler.

- Projeyi ilk kez görüyorsan: bu dosya + [`README.md`](README.md) yeter.
- Kodu değiştireceksen: [`HANDOVER.md`](HANDOVER.md) (13 bölüm) asıl rehberdir.
- Konu bazlı ayrıntı: [`docs/`](docs/README.md).

---

## 0. Ne gerekiyor

| Gereken | Not |
| --- | --- |
| **Docker Desktop** | Windows'ta WSL2 açık olmalı |
| **İnternet** | Build sırasında PyPI, npm, Playwright Chromium, PyTorch iner |
| **Model erişimi** | OpenAI uyumlu endpoint: adres + model adı + anahtar |
| Disk | ~8 GB |
| RAM | 8 GB altı zorlanır |
| *(opsiyonel)* NVIDIA GPU | Yalnız PaddleOCR-VL profili için |
| *(opsiyonel)* Windows + **klasik** Outlook | Yalnız e-posta göndermek için |

İlk build **15–30 dakika** sürer. Sonraki başlatmalar saniyeler.

---

## 1. Kurulum (ilk kez)

```powershell
cd <repo-klasoru>
copy .env.example .env
notepad .env
```

`.env` içinde **zorunlu** üç satır:

```env
MODEL_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=deepseek/deepseek-v4-flash
MODEL_API_KEY=sk-...
```

Sonra:

```powershell
docker compose up --build
```

Dört servis ayağa kalkar. Arayüz: <http://127.0.0.1:8081>

---

## 2. Çalıştığını doğrula

```powershell
curl http://127.0.0.1:8081/                      # arayüz      -> 200
curl http://127.0.0.1:8001/docs                  # API/Swagger -> 200
curl http://127.0.0.1:3939/health                # AgentSearch -> healthy
docker compose ps                                # hepsi Up
```

İlk deneme sırası:

1. Workspace oluştur.
2. **Single** ile "2+2 kaç" → düz cevap.
3. **Workforce**'a geç → "İstanbul hakkında kısa bir PDF hazırla" → dosya üretilir.

---

## 3. Servisler ve portlar

| Servis | Adres | Ne yapar |
| --- | --- | --- |
| frontend | <http://127.0.0.1:8081> | React arayüz (nginx) |
| backend | <http://127.0.0.1:8001> | FastAPI + ajanlar (container içinde 8000) |
| agent-search | <http://127.0.0.1:3939> | Arama sidecar'ı |
| searxng | yayınlanmaz | Docker ağı içi arama motoru |
| paddleocr-vl-api | yayınlanmaz | OCR (yalnız `COMPOSE_PROFILES=paddleocr`) |
| *(host)* local computer bridge | 127.0.0.1:8765 | Dosya açma + Outlook e-posta (Docker **dışında**) |

Portlar 8081/8001 bilerek seçildi: 8080/8000'i kullanan başka bir yığınla yan
yana çalışabilsin diye.

---

## 4. Her gün ne başlatılır

| Ne | Komut / işlem | Gerekli mi |
| --- | --- | --- |
| Yığın | `docker compose up -d` (klasör: repo kökü) | Her zaman |
| Köprü | `python scripts\local_computer_bridge.py --token <token>` | Yalnız "bilgisayarımda aç" ya da Outlook e-posta kullanıyorsan |
| Outlook | Klasik Outlook açık ve hesabı tanımlı | Yalnız e-posta gönderilecekse |

Bilgisayar kapalıyken zamanlanmış görev **çalışmaz**; açılışta
`SCHEDULE_CATCHUP_WINDOW_HOURS` (6 saat) içindeki bir kaçan çalışma bir kez telafi
edilir, daha eskisi `missed` yazılır.

Durdurma:

```powershell
docker compose down        # veri kalır
docker compose down -v     # her şey silinir (DB, workspace'ler)
```

---

## 5. Açılabilir özellikler

Hepsi `.env` ile açılır/kapanır. Değişiklikten sonra: `docker compose up -d`
(yalnız env değiştiyse yeniden derlemeye gerek yok).

### 5.1 Web araştırması

Varsayılan açık (`AGENT_SEARCH_ENABLED=true`). Workforce modunda çalışır:
SEARCH → SELECT → FETCH → ANSWER. Ayrıntı: [`docs/web-execution.md`](docs/web-execution.md).

### 5.2 Belge okuma ve OCR

Docling varsayılan açık. Taranmış PDF / görsel için:

```env
DOCLING_ENABLE_OCR=true
COMPOSE_PROFILES=paddleocr     # NVIDIA GPU ister; yoksa Tesseract yedeği devrede
```

Ayrıntı: [`docs/document-ingestion.md`](docs/document-ingestion.md).

### 5.3 "Bilgisayarımda aç"

Üretilen dosyayı kendi bilgisayarında varsayılan uygulamada açar.

```env
LOCAL_COMPUTER_ENABLED=true
```

Host'ta (Docker dışında) köprüyü başlat — bkz. § 6. Ayrıntı:
[`docs/local-computer.md`](docs/local-computer.md).

### 5.4 Zamanlanmış görevler

Varsayılan açık. Sol menüde **Scheduled**: cron / tek seferlik / başka görevden
sonra (zincir) / yalnız değişiklikte öne çıkan izleme. Ayrıntı:
[`docs/scheduling.md`](docs/scheduling.md).

### 5.5 Outlook ile e-posta

Zamanlanmış görev sonuçlarını klasik Outlook'tan e-postayla gönderir.

```env
OUTLOOK_ENABLED=true
LOCAL_COMPUTER_TOKEN=<uzun rastgele değer>
```

Token üret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Köprüyü **aynı** token ile başlat (§ 6), sonra arayüzde: Scheduled → görev →
**Email** bölümü → alıcı → **Send test email**.

Kutuyu işaretlemek şart değil: görev metnine "... ve ali@sirket.com'a gönder"
yazarsan sonuç yine o adrese gider. Sohbette (Workforce) aynı cümle, e-postayı
Outlook'ta **taslak** olarak açar; göndermeye kullanıcı karar verir
(`OUTLOOK_AGENT_ACTION=send` ile doğrudan gönderilir). Alıcı olarak yalnız senin
yazdığın adresler kabul edilir.

Gereken: Windows + **klasik** Outlook (Office masaüstü), hesabı tanımlı ve
uygulama açık. "Yeni Outlook" uygulamasında otomasyon desteği **yoktur**.
Ayrıntı ve sorun giderme: [`docs/outlook.md`](docs/outlook.md).

### 5.6 Posta kutusunu okuma

Ajanın **gelen/giden e-postalarını kaynak olarak okuması** ayrı bir izindir:

```env
OUTLOOK_ENABLED=true
OUTLOOK_READ_ENABLED=true
LOCAL_COMPUTER_TOKEN=<köprüyle aynı değer>
```

Açıkken Workforce'ta şunlar çalışır:

```text
gelen maillerime bak
bugün gelen okunmamış mailleri özetle
ahmetten gelen son faturayı özetle
geçen hafta gelen teklifleri karşılaştır ve pdf ver
```

Okunan: **Gelen Kutusu + Gönderilmiş Öğeler**. Ekler iş klasörüne indirilir ve
yüklenmiş dosya gibi (Docling/OCR ile) okunur — "mailde gelen PDF'i özetle"
çalışır. **Salt okunur:** hiçbir mail okundu işaretlenmez, taşınmaz, silinmez.

Köprüyü bu sürümle **yeniden başlatman** gerekir (okuma uçları yeni). Kontrol:
`curl http://127.0.0.1:8765/health` → `outlook.can_read: true`.

> ⚠️ Bu, ajanın posta kutunu okumasına izin vermek demektir. E-postaların
> içindeki "şunu şu adrese gönder" gibi cümleler **uygulanmaz** (alıcılar
> yalnız senin yazdığın metinden alınır), ama yine de kapalıyken hiçbir iş
> posta kutusuna erişemez — varsayılan `false` olmasının sebebi budur.
> Ayrıntı: [`docs/outlook.md`](docs/outlook.md#posta-kutusunu-okuma).

### 5.7 Jira ve Confluence

**Okuma.** Şirket Jira'sını ve Confluence'ını okur (Confluence yalnız izin
verilen space'lerde). Atlassian Cloud'da tek hesap ikisine de yeter:

```env
JIRA_ENABLED=true
JIRA_URL=https://sirketiniz.atlassian.net
JIRA_USERNAME=ad.soyad@sirket.com
JIRA_API_TOKEN=...                 # id.atlassian.com > Security > API tokens
CONFLUENCE_ENABLED=true
CONFLUENCE_SPACES=ENG,HR           # ajan YALNIZ bu space'lerde arar, okur ve yazar
```

Kontrol: `curl http://127.0.0.1:8001/integrations/jira/status` ve
`.../integrations/confluence/status` → `"ok": true`.

```text
bana atanan açık jira işlerini listele
KAN-12 ne durumda, son yorumlarda ne konuşulmuş
confluence'ta izin politikası ne diyor
```

**Onaylı yazma** (ayrı anahtarlar, varsayılan kapalı):

```env
JIRA_WRITE_ENABLED=true            # kayıt aç, yorum ekle, durum değiştir
CONFLUENCE_WRITE_ENABLED=true      # yeni sayfa, sayfaya yorum
```

```text
KAN'da yeni task aç: arama kutusu Türkçe karakterle sonuç getirmiyor
KAN-12'ye yorum ekle: test ortamında düzeldi
KAN-7'yi Done'a çek
Confluence'ta ENG'e 'Haftalık toplantı' sayfası oluştur: katılımcılar ...; kararlar ...
'Deploy runbook' sayfasına yorum yaz: adım 3 güncellendi
```

Ajan değişikliği **yalnız önerir**: cevabın altında ne yazılacağını gösteren bir
**onay kartı** çıkar. **Onayla** → yazılır ve kart kayda / sayfaya bağlantı
verir; **Vazgeç** → hiçbir şey yazılmaz. Kayıt anahtarı, üst sayfa ve
yorumlanacak sayfa yalnız senin yazdığın ya da bağlantısını verdiğin olabilir.
Silme, mevcut sayfayı düzenleme, alan düzenleme ve toplu işlem yoktur;
zamanlanmış görevler değişiklik önermez.

Token'ın bu işlemler için yetkisi olmalı — yazmayı açmadan önce hesabın neye
yetkili olduğunu kontrol et. Ayrıntı: [`docs/jira.md`](docs/jira.md#yazma-onaylı),
[`docs/confluence.md`](docs/confluence.md#yazma-onaylı).

> ⚠️ Uygulamada giriş yok ve portlar tüm ağ arayüzlerinde açık. Yazma açıkken
> aynı ağdaki biri de öneri oluşturup onaylayabilir. Yalnız kendi makinende
> kullanıyorsan `docker-compose.yml`'de portları `127.0.0.1:8081:80`,
> `127.0.0.1:8001:8000` biçimine çevir.

---

## 6. Köprü (local computer bridge)

Docker container'ının GUI'si yoktur; host'taki Outlook'u veya varsayılan
uygulamayı **yalnız host'ta çalışan bir süreç** açabilir. Köprü budur. Bu yüzden
container'ın içine konamaz.

```powershell
cd <repo-klasoru>
pip install pywin32                         # yalnız Outlook kullanacaksan
python scripts\local_computer_bridge.py --token <LOCAL_COMPUTER_TOKEN degeri>
curl http://127.0.0.1:8765/health
```

Sağlıklıysa: `"outlook": {"available": true, "token_configured": true}`.

Token yalnız Outlook uçları için zorunludur ve bilerek öyledir: köprü
kimlik doğrulamasız dinler, token olmasa tarayıcıda açık herhangi bir sayfa bu
adrese istek atıp senin adına e-posta gönderebilirdi.

### Otomatik başlatma (her açılışta elle çalıştırmamak için)

**Yol A — Görev Zamanlayıcı (önerilen, çökerse yeniden başlatır).**
PowerShell'i **normal** (yönetici *değil*) aç; Outlook da normal çalıştığı için
yükseltilmiş bir süreç COM'a bağlanamaz:

```powershell
$proje = "<repo-klasoru>"
$py    = (Get-Command pythonw.exe).Source
$token = ((Get-Content "$proje\.env" | Select-String '^LOCAL_COMPUTER_TOKEN=') -split '=',2)[1]

$action  = New-ScheduledTaskAction -Execute $py `
    -Argument "scripts\local_computer_bridge.py --token $token" -WorkingDirectory $proje
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "agent-webapp bridge" -Action $action -Trigger $trigger `
    -Settings $settings -Description "local computer bridge (dosya acma + Outlook e-posta)"
```

Kaldırmak için: `Unregister-ScheduledTask -TaskName "agent-webapp bridge"`.
Not: token böylece Görev Zamanlayıcı'da görünür; istemiyorsan Yol B'deki .bat
dosyasını kullan (token `.env`'de kalır).

**Yol B — Başlangıç klasörü (daha basit, yeniden başlatma yok).**
`scripts\baslat-kopru.bat` diye bir dosya oluştur:

```bat
@echo off
cd /d "%~dp0.."
for /f "tokens=1,* delims==" %%a in ('findstr /b "LOCAL_COMPUTER_TOKEN=" .env') do set LOCAL_COMPUTER_TOKEN=%%b
start "" /min pythonw scripts\local_computer_bridge.py
```

`Win+R` → `shell:startup` → bu .bat için kısayol koy.

Her iki yolda da köprü **senin oturumunda** çalışmalıdır; Windows servisi olarak
(oturum 0) kurulursa Outlook'a erişemez.

---

## 7. Kod değiştirdiysen

Kaynak host'tan mount **edilmez**; container imajın içindeki kopyayı çalıştırır.

```powershell
docker compose up -d --build backend     # backend .py değiştiyse
docker compose up -d --build frontend    # frontend .jsx/.js değiştiyse
```

> Backend yeniden derlemesi o an çalışan job'ları öldürür.

Veritabanı migration'ları backend açılışında kendiliğinden koşar
(`backend/migrations/*.sql`).

Testler:

```powershell
cd backend
python -m pytest tests -q                       # host'ta (langgraph kurulu değilse bazı dosyalar atlanır)
docker compose exec backend python -m pytest tests -q   # container içinde (önce pytest kur)
```

---

## 8. Sorun giderme

| Belirti | Bak |
| --- | --- |
| Arayüz açılıyor ama cevap yok | `.env`'de `MODEL_*` üçlüsü; `docker compose logs -f backend` |
| Yeni özellik arayüzde yok | Frontend yeniden derlenmedi: `docker compose up -d --build frontend`, sonra Ctrl+F5 |
| Kod değişikliği etkisiz | Backend yeniden derlenmedi (§ 7) |
| Taranmış PDF boş geliyor | `DOCLING_ENABLE_OCR=true`; GPU yoksa Tesseract yedeği |
| Zamanlanmış görev çalışmadı | Bilgisayar açık mıydı; `SCHEDULING_ENABLED`; Scheduled ekranında run durumu |
| E-posta gitmiyor | Köprü çalışıyor mu (`:8765/health`), klasik Outlook açık ve hesabı tanımlı mı → [`docs/outlook.md`](docs/outlook.md) |
| Jira / Confluence onay kartı çıkmıyor | `JIRA_WRITE_ENABLED` / `CONFLUENCE_WRITE_ENABLED` açık mı, istek Workforce'ta mı; rebuild sonrası açık sekmede Ctrl+F5 (eski sayfa kartı gösteremez) |
| "Jira / Confluence değişikliği hazırlanamadı: ..." | Satırın devamı sebebi söyler (proje / space belirtilmedi, kayıt anahtarı yazılmadı, o duruma geçiş yok…); isteği düzeltip tekrar gönder |
| Onayla → "Failed" | Kartta Jira / Confluence'ın kendi mesajı yazar; çoğu zaman token yetkisi ya da zorunlu bir alan |
| `docker compose build` registry hatası | Docker Desktop'ın ağı/DNS'i; genelde geçici, tekrar dene |
| Port dolu | 8081/8001/3939 başka bir şey tarafından kullanılıyor olabilir |

Daha uzun liste: [HANDOVER § 12](HANDOVER.md#12-sorun-giderme).

---

## 9. Bilmen gereken sınırlar

- **Kimlik doğrulama yok.** Bu yığını internete açma; iç ağ / localhost içindir.
- Köprü ve Outlook yalnız **aynı makinede** çalışır (backend, tarayıcı ve Outlook
  aynı bilgisayarda).
- Zamanlanmış e-postanın alıcılarını görevi oluşturan kişi yazar; model alıcı
  seçemez.
- Jira / Confluence'a yazma her zaman onay kartından geçer; model tek başına
  hiçbir şey yazamaz. Bir istekte tek değişiklik; silme ve mevcut sayfayı
  düzenleme yoktur.
- Çalışan bir job restart'ta `interrupted` olur, kaldığı yerden devam etmez.

---

## 10. Sonra ne okumalı

| Dosya | Ne için |
| --- | --- |
| [`README.md`](README.md) | Kısa tanıtım, ne var kutuda |
| [`HANDOVER.md`](HANDOVER.md) | Tam rehber: mimari, kod haritası, test, açıklar |
| [`docs/README.md`](docs/README.md) | Konu bazlı belgelerin indeksi |
| [`docs/scheduling.md`](docs/scheduling.md) | Zamanlanmış görevler, zincirler, e-posta kuralları |
| [`docs/outlook.md`](docs/outlook.md) | Outlook kurulumu, güvenlik, sorun giderme |
| [`docs/jira.md`](docs/jira.md) | Jira okuma ve onaylı yazma |
| [`docs/confluence.md`](docs/confluence.md) | Confluence okuma ve onaylı yazma |
