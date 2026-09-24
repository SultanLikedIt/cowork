# agent-webapp — Devir Teslim Rehberi

Bu dosya projeyi **hiç bilmeyen** birine devretmek için yazıldı. Kurulumdan
mimariye, günlük kullanımdan bilinen açıklara kadar her şey burada. Repodaki
`docs/` klasörü tek tek konuların (Docling, belge üretimi, web, local computer) daha
derin referansıdır; bu dosya onların üstündeki tek giriş noktasıdır.

**Okuma sırası:** Bölüm 1–3 (ne bu, nasıl kurulur) → Bölüm 4 (nasıl kullanılır)
→ gerisi ihtiyaç oldukça.

---

## İçindekiler

0. [Hızlı başlangıç](BASLANGIC.md) — sıfırdan çalışır hale, günlük kullanım, açılabilir özellikler
1. [Proje nedir](#1-proje-nedir)
2. [Kurulum — Docker (önerilen)](#2-kurulum--docker-önerilen)
3. [Ortam değişkenleri (.env)](#3-ortam-değişkenleri-env)
4. [Kullanım — arayüz](#4-kullanım--arayüz)
5. [Mimari — bir istek uçtan uca](#5-mimari--bir-istek-uçtan-uca)
6. [Kod haritası](#6-kod-haritası)
7. [API uçları](#7-api-uçları)
8. ["Bilgisayarımda aç" köprüsü](#8-bilgisayarımda-aç-köprüsü)
9. [Docker'sız yerel geliştirme](#9-dockersız-yerel-geliştirme)
10. [Test ve geliştirme döngüsü](#10-test-ve-geliştirme-döngüsü)
11. [Sınırlar ve bilinen açıklar](#11-sınırlar-ve-bilinen-açıklar)
12. [Sorun giderme](#12-sorun-giderme)
13. [Nereden başlamalı](#13-nereden-başlamalı)

---

## 1. Proje nedir

Kullanıcının tarayıcıda konuştuğu bir **ajan uygulaması**. Soru sorarsın; sistem
ya düz sohbet eder, ya web'den araştırır, ya yüklediğin belgeyi okur, ya da
Word / PDF / PowerPoint / Excel / HTML dosyası üretir.

| Parça | Teknoloji | Ne işe yarar |
| --- | --- | --- |
| Arayüz | React 18 + Vite + Tailwind | Sohbet, workspace, dosya yükleme, canlı ilerleme |
| Sunucu | FastAPI (Python 3.11) | Ajan orkestrasyonu, arama, belge yazma, indirme |
| Arama | SearXNG + AgentSearch sidecar | Web keşfi (harici Google/Bing API yok) |
| Sayfa açma | Crawl4AI + Playwright Chromium | Gerçek tarayıcı, JS çalışır |
| Belge | Chromium (HTML→PDF), python-docx, python-pptx, openpyxl, reportlab (yedek) | PDF / DOCX / PPTX / XLSX / HTML |
| Kalıcılık | SQLite + dosya sistemi | Sohbet, job, artifact |

Kullanıcı **çalışma modunu kendisi seçer** — sunucu tahmin etmez:

- **Single Agent** — tek sohbet çağrısı. Hızlı, ucuz. Web yok, dosya üretmez.
- **Workforce (Multi)** — araştırma + belge + kod işçileri. "PDF ver", "sunum
  hazırla", "araştır" buradan gider.

### Hangi repo, hangi dal — KÖKEN VE TARİHÇE

Bu en çok karıştırılan konu, o yüzden en başta. **Tek bir güncel repo var:**

| Repo | Ne | Durum |
| --- | --- | --- |
| `omeryavuzarslan/agent-webapp-langgraph` | **Bu repo.** Geliştirmenin sürdüğü yer | ✅ **GÜNCEL — bunu kullan** |
| `bsaltalii/agent-webapp` | Projenin alındığı orijinal repo | 🗄️ Arşiv, 19 Ağustos 2026'da kaldı |

Ortalıkta `omeryavuzarslan/agent-webapp` adında eski bir CAMEL fork'u daha
görebilirsin — **kullanma.** `bsaltalii/main` ile birebir aynı commit'te
(`c13b5c9`) ve içinde bu repoda olmayan hiçbir şey yok.

**Güncel dal: `main`** — reponun varsayılan dalı budur, klonladığında
doğrudan üzerine düşersin. Ekstra bir `checkout` gerekmez.

```powershell
git clone https://github.com/omeryavuzarslan/agent-webapp-langgraph.git
git branch -vv     # * main
```

Ölçülmüş durum (2026-09-04):

- `main`, projenin alındığı `bsaltalii/main`'den **84 commit önde, 0 commit
  geride** — yani orijinalde burada olmayan hiçbir şey yok
- `claudefix/pptx-chat-reformat` — geliştirmenin yapıldığı dal, `main` ile
  **aynı commit'te**. `main` bu daldan oluşturuldu; ikisi arasında fark yok
- `dev/omer-experiment` — reponun eski varsayılan dalı, **63 commit geride**.
  Kullanma; yalnız geçmiş için duruyor

**Nasıl buraya gelindi:**

1. **19 Ağustos 2026** — Proje `bsaltalii/agent-webapp`'ten alındı. O tarihte
   sistem tamamen **CAMEL-AI** (`camel-ai==0.2.80`) üzerine kuruluydu; Single
   Agent bir `ChatAgent`, Workforce ise CAMEL'in kendi `Workforce` sınıfıydı.
2. **21 Ağustos 2026** — LangGraph'a geçiş başladı
   (`a65e7e3 Add dual-path LangGraph Single Agent behind AGENT_EXECUTOR flag`).
   Geçiş **bilerek çift yollu** yapıldı: eski CAMEL kodu silinmedi, yeni
   LangGraph yolu onun yanına kondu ve ikisi arasında `AGENT_EXECUTOR` env
   değişkeni seçim yapar.
3. **21 Ağustos – 3 Eylül 2026** — 84 commit: LangGraph Workforce supervisor,
   web faz kontrolcüsü, belge üretimi düzeltmeleri, deep research, local
   computer köprüsü, AgentSearch sidecar.
4. **24 Eylül 2026** — CAMEL tamamen kaldırıldı: `camel_service.py`
   (4.700+ satır), CAMEL Single yolu, `AGENT_EXECUTOR` anahtarı, `camel-ai`
   bağımlılığı ve vendor'daki CAMEL kaynağı silindi. Tek çalıştırıcı
   `backend/execution/langgraph_runtime/`. Geçiş notları:
   [`docs/archive/langgraph-migration.md`](docs/archive/langgraph-migration.md).

Eski repolarda kalan, bu dala hiç girmemiş birkaç terk edilmiş deney dalı var
(`bsaltalii/recovery/orchestration-stable-web` — 7 commit "Routing V2" denemesi,
`bsaltalii/feature/triall_webtools_rebuild` — 2 commit). Bunlar 20 Ağustos'ta
bırakıldı ve sonraki 84 commit'lik iş onların yerine geçti. Referans dışında
ihtiyaç duyulması beklenmiyor.

### Çalıştırıcı

Single ve Workforce, LangGraph grafında çalışır: `execution/langgraph_runtime/`
(`single.py`, `workforce.py`). Seçilecek başka bir yol yoktur.

### Portlar

8080/8000'i kullanan başka bir yığınla **yan yana** çalışabilsin diye host
portları farklıdır:

| Servis | Adres |
| --- | --- |
| Arayüz | http://127.0.0.1:8081 |
| API | http://127.0.0.1:8001 |
| AgentSearch | http://127.0.0.1:3939 |
| SearXNG | yayınlanmaz (Docker ağı) |

Container **içinde** backend hâlâ 8000 dinler; host'a **8001** olarak açılır.

---

## 2. Kurulum — Docker (önerilen)

### 2.1 Makinede ne olmalı

| Araç | Neden | Not |
| --- | --- | --- |
| Git | Kaynak + submodule | `vendor/agent-search` ayrı repo |
| Docker Desktop | Tüm yığın | Windows'ta WSL2 açık olsun; image `linux/amd64` |
| Model gateway | Ajanın beyni | OpenAI uyumlu `/v1/chat/completions` |
| (opsiyonel) Python 3.11 | Test, local computer bridge | Docker-only kullanımda şart değil |
| (opsiyonel) Node 20 | Frontend'i Docker dışında derlemek | |

- **Disk:** klon ~1 GB; image'lar (PyTorch CPU, Docling, Chromium) **+6–8 GB**.
- **İlk build:** `docker compose up --build` **15–30 dakika** sürebilir.
- **RAM:** 8 GB altı sıkışır. Compose'daki `shm_size: 1gb` Chromium içindir, düşürme.
- **Ağ:** build sırasında PyPI, npm, Playwright CDN ve PyTorch wheel index gerekir.

### 2.2 Kaynağı al

İki yol var. **Hangisiyle geldiysen sadece onu uygula.**

---

#### A) Elinde teslim klasörü varsa — git gerekmez

Sana bir klasör verildiyse (içinde `BURADAN-BASLA.md`, `proje/` ve
`eigent-karsilastirma/` varsa) **kaynağı almak için yapman gereken hiçbir şey
yok.** Projenin tamamı `proje/` altında, `vendor/agent-search` dahil.

```powershell
cd <klasorun-yolu>\proje
dir            # backend, frontend, docker-compose.yml, HANDOVER.md gorunmeli
```

Bu yolda **git kurmana bile gerek yok**:

- `git clone` — gerek yok, dosyalar zaten elinde
- `git submodule update --init` — gerek yok, `vendor/agent-search` dolu
  (kontrol: `dir vendorgent-search` — `app`, `adapters`, `Dockerfile`
  görünmeli. Boşsa yanlış bir kopya almışsın demektir)

Doğrudan [2.3 `.env` koy](#23-env-koy) adımına geç.

> Bu yol gerçekten denendi: teslim klasöründen `docker compose build` +
> `up -d` yapıldı, dört servis de ayağa kalktı, `POST /run-pipeline` ile
> gerçek bir iş 3.2 saniyede cevap döndü. Git hiç kullanılmadı.

**Bu yolun tek sınırı:** klasörde commit geçmişi (`.git`) yok. Kod çalışır,
ama "bu satır neden böyle" sorusunun cevabını `git log`'dan bulamazsın.
Geliştirmeye devam edeceksen aşağıdaki B yolunu tercih et.

---

#### B) GitHub'dan klonlayacaksan

```powershell
git clone https://github.com/omeryavuzarslan/agent-webapp-langgraph.git
cd agent-webapp-langgraph
git submodule update --init --recursive
```

**Submodule adımını atlama** — atlarsan AgentSearch container'ı boş context
ile build sırasında patlar.

Klonlama seni doğrudan `main`'e bırakır; güncel dal odur, ekstra `checkout`
gerekmez. Kontrol etmek istersen:

```powershell
git branch -vv                 # * main
git log -1 --oneline
```

Repoda `dev/omer-experiment` diye eski bir dal daha görürsün; o **63 commit
geride** ve bu belgeleri içermez. Ona geçme.

---

#### Her iki yolda da internet gerekir

Kaynak nereden gelirse gelsin, `docker compose build` sırasında şunlar
internetten iner: PyPI paketleri, npm paketleri, Playwright Chromium, PyTorch
CPU wheel'ları, (ilk kullanımda) Docling model ağırlıkları. Yani "GitHub'a
erişimim yok" sorun değil, ama "internetim yok" sorun.

### 2.3 `.env` koy

Repo **köküne** `.env` koy — Docker Compose bunu otomatik okur. Şablon repoda:
[`.env.example`](.env.example).

```powershell
copy .env.example .env
notepad .env
```

En az şunlar gerçek olmalı:

```env
MODEL_BASE_URL=https://senin-gateway/v1
MODEL_NAME=kullandigin-model
MODEL_API_KEY=anahtar
```

Compose **bilerek** model default'u yazmaz; boş bırakırsan model çağrıları
sessizce düşer. (Kodun içindeki `http://localhost:11434/v1` + `qwen2.5:3b`
default'u yalnız Python tarafındadır, compose'ta değil.)

### 2.4 Çalıştır

```powershell
docker compose up --build
```

Arka planda:

```powershell
docker compose up -d --build
docker compose logs -f backend
```

| Compose servisi | Host | İçeride |
| --- | --- | --- |
| `frontend` | http://127.0.0.1:8081 | nginx :80 |
| `backend` | http://127.0.0.1:8001 | uvicorn :8000 |
| `searxng` | yayınlanmaz | :8080 (Docker ağı) |
| `agent-search` | http://127.0.0.1:3939 | :3939 |

`depends_on` sıralama verir ama **healthy beklemez** — ilk saniyelerde arama
boş dönebilir, birkaç saniye bekle.

### 2.5 Çalıştığını doğrula

```powershell
curl http://127.0.0.1:8001/docs
curl http://127.0.0.1:3939/health
```

Sonra tarayıcıda http://127.0.0.1:8081 → workspace oluştur → Single ile kısa bir
soru → Workforce'a geç → "İstanbul hakkında kısa bir PDF hazırla".

### 2.6 Durdur / temizle / yeniden derle

```powershell
docker compose down            # durdur, veri kalır
docker compose down -v         # HER ŞEYİ sil (sohbet DB + upload'lar)
docker compose up -d --build backend frontend   # kod değişince
```

> **Kritik:** kaynak kod host'tan **mount edilmez**. Bir `.py` dosyasını
> kaydetmek çalışan container'ı değiştirmez; `docker compose cp` de çalışan
> uvicorn'u yenilemez. Tek yol `--build`, ve rebuild o anda `running` olan
> job'ları öldürür.

### 2.7 Docling model ağırlıkları (artık imajın içinde)

Docling'in düzen/tablo ağırlıkları (~506 MB) `backend/Dockerfile` içinde build
sırasında iniyor, yani Docker yolunda yapman gereken bir şey yok. Bu adım
`COPY . .` satırının **üstünde** duruyor; Python kodunu değiştirdiğinde
yarım gigabayt yeniden inmesin diye.

Klasörü elle doldurmak istersen (backend'i host'ta çalıştırıyorsan):

```powershell
docling-tools models download layout tableformer --output-dir backend\vendor\docling-models
```

**İki model adını mutlaka yaz.** Düz `docling-tools models download` varsayılan
seti indirir; o set RapidOCR ağırlıklarını `modelscope.cn` üzerinden çekmeye
çalışır, oraya erişim zaman aşımına uğrayınca komut (ve Docker build'i) komple
düşer. Üstelik bu pipeline'ın hiç kullanmadığı modelleri de indirir: 506 MB
yerine 1.2 GB.

Ağırlıklar yoksa sistem çalışmayı sürdürür: ilk PDF/görselde Hugging Face'den
indirir (~45 sn) ve dışarı ağ erişimi ister.

Ayrıntı: [`docs/document-ingestion.md`](docs/document-ingestion.md).

### 2.8 BGE reranker ağırlıkları (önerilir — web SELECT gecikmesi)

Web SELECT adımı, her arama turunda LLM çağrısı yerine yerel
`BAAI/bge-reranker-v2-m3` cross-encoder'ı kullanır (`backend/reranker.py`).
Ağırlıklar yoksa otomatik olarak LLM SELECT'e düşer. Bir kere indir, sonra
backend imajını yeniden build et:

```powershell
huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir backend\vendor\bge-reranker-v2-m3
```

Kapatmak için `RERANKER_ENABLED=false`; CPU thread sayısı `RERANKER_THREADS` (varsayılan 4).

### 2.8 Kurulum checklist

- [ ] Kaynak yerinde: klasörle geldiyse `vendorgent-search` dolu, klonladıysan
      `git submodule update --init --recursive` çalıştı
- [ ] Kökte `.env` var; üç `MODEL_*` alanı dolu
- [ ] `docker compose up --build` hatasız bitti
- [ ] http://127.0.0.1:8081 ve http://127.0.0.1:8001/docs açılıyor
- [ ] Single kısa soruya cevap veriyor
- [ ] Workforce PDF/DOCX üretebiliyor
- [ ] (isteniyorsa) local computer bridge ayakta

---

## 3. Ortam değişkenleri (.env)

Tam şablon: [`.env.example`](.env.example). Özet:

| Değişken | Zorunlu | Ne yapar |
| --- | --- | --- |
| `MODEL_BASE_URL` | ✅ | OpenAI uyumlu endpoint; sondaki `/v1` önemli |
| `MODEL_NAME` | ✅ | O endpoint'in beklediği model id |
| `MODEL_API_KEY` | ✅ | Gateway anahtarı (Ollama için gelenek: `ollama`) |
| `AGENT_SEARCH_ENABLED` | — | `false` yaparsan yalnız SearXNG |
| `AGENT_SEARCH_BASE_URL` | — | Sidecar adresi |
| `SEARXNG_BASE_URL` | — | Host'ta backend çalıştırırken gerekir |
| `LOCAL_COMPUTER_ENABLED` | — | "Bilgisayarımda aç" köprüsü |
| `LOCAL_COMPUTER_URL` | — | Varsayılan `http://host.docker.internal:8765` |
| `LOCAL_COMPUTER_TOKEN` | — | Köprünün Outlook uçları için paylaşılan sır; boşsa e-posta kapalı |
| `OUTLOOK_ENABLED` | — | Zamanlanmış sonuçları Outlook'tan e-postayla gönder (Windows + klasik Outlook) |
| `OUTLOOK_AGENT_ACTION` | — | Sohbette istenen e-posta: `draft` (varsayılan, Outlook'ta taslak) \| `send` |
| `OUTLOOK_READ_ENABLED` | — | Ajan posta kutusunu okuyabilsin (Gelen Kutusu + Gönderilmiş Öğeler, salt okunur). Varsayılan `false`; `OUTLOOK_ENABLED` de gerekir |
| `JIRA_ENABLED` / `JIRA_URL` | — | Şirket Jira'sını okuma (salt okunur). Cloud: `JIRA_USERNAME` + `JIRA_API_TOKEN`; Server/DC: `JIRA_PERSONAL_TOKEN` ([`docs/jira.md`](docs/jira.md)) |
| `GITHUB_ENABLED` / `GITHUB_TOKEN` | `true` / — | Herkese açık GitHub okuma (salt okunur). Token opsiyonel ama önerilir: tokensiz sınır tüm backend için saatte 60 istek ([`docs/github.md`](docs/github.md)) |
| `SCHEDULING_ENABLED` | — | Zamanlanmış görevler (varsayılan `true`); diğer `SCHEDULE_*` için `.env.example` |
| `SCHEDULE_DELIVERY_MAX_ATTEMPTS` | — | Gönderilemeyen e-posta için deneme sayısı (varsayılan 10, ~6 saat) |
| `DOCLING_ENABLE_OCR` | — | Compose'ta `true`; taranmış PDF ve görsel yüklemelerini okur. `false` → taranmış PDF boş, görsel reddedilir |
| `COMPOSE_PROFILES` | — | `paddleocr` → PaddleOCR-VL servislerini de başlatır (NVIDIA GPU ister) |
| `OCR_ENGINE` | — | `paddleocr_vl` (varsayılan) \| `tesseract` \| `easyocr` \| `rapidocr` |
| `OCR_FALLBACK_ENGINE` | — | PaddleOCR-VL çalışmazsa kullanılan Docling motoru; varsayılan `tesseract` (imajda kurulu) |
| `PADDLEOCR_VL_URL` | — | Compose'ta `http://paddleocr-vl-api:8080`; boşsa her zaman yedeğe düşer |
| `PADDLEOCR_VL_TIMEOUT_SECONDS` | — | Varsayılan 240; `DOCLING_PARSE_TIMEOUT_SECONDS`'tan küçük tut ki yedeğe zaman kalsın |
| `OCR_LANGS` | — | Varsayılan `tur,eng` (Docling/yedek motorun kendi dil kodları; PaddleOCR-VL dil ayarı almaz) |
| `OCR_FORCE_FULL_PAGE` | — | Docling motorları: `true` = her sayfa OCR'lanır (yavaş); varsayılan yalnız görsel bölgeler |
| `DOCLING_PARSE_TIMEOUT_SECONDS` | — | Upload parse zaman aşımı; compose'ta 300 (OCR sayfa başına saniyeler sürer) |
| `WEB_REQUEST_TIMEOUT_SECONDS` | — | Sayfa açma zaman aşımı |
| `DOCUMENT_BLOCKS` | — | Varsayılan `true`; PDF/DOCX raporları tipli bloklarla yazılır |
| `DOCUMENT_PDF_ENGINE` | — | `chromium` (varsayılan) veya `reportlab` |
| `DOCUMENT_PDF_TIMEOUT_SECONDS` | — | Chromium PDF basma zaman aşımı (varsayılan 45) |
| `DOCUMENT_LLM_REASONING_EFFORT` | — | Document LLM çağrısı için OpenRouter `reasoning.effort` (`low`/`medium`/`high`); boş = sağlayıcı varsayılanı |
| `DOCUMENT_LLM_PROVIDER_SORT` | — | Document LLM çağrısı için OpenRouter sağlayıcı sıralaması (`throughput`/`latency`/`price`); boş = varsayılan |
| `CODING_LLM_REASONING_EFFORT` / `CODING_LLM_PROVIDER_SORT` | — | Coding Agent çağrıları için aynı iki ayar; boş bırakılması önerilir ([`docs/coding-agent.md`](docs/coding-agent.md)) |
| `CODING_LLM_TIMEOUT_SECONDS` | — | Coding Agent'ın tek model çağrısı için üst sınır (varsayılan 150); aşılırsa kesilip bir kez yeniden denenir |
| `DATA_DIR` | — | Docker'da `/data`; yerelde `backend/` |
| `PYTEST_USE_REAL_DATA_DIR` | — | **Test için canlı DB. Kullanma.** |

> **İki ayrı `.env` var, karıştırma.** Docker Compose **repo kökündeki** `.env`'yi
> okur. Host'ta doğrudan uvicorn çalıştırırsan `model_config.py` `load_dotenv` ile
> **`backend/.env`**'yi yükler. Yerel geliştirmede dosyayı ikisine de koy.

`.env` `.gitignore`'da; `.env.example` bilerek istisna. Anahtarı commit'leme.

---

## 4. Kullanım — arayüz

Adres (Docker): http://127.0.0.1:8081 — Vite dev: http://localhost:5173.
**Login yok.**

### 4.1 Ekranlar ve URL yapısı

- `/` — workspace seçici
- `/w/{workspaceId}` — o workspace'in ana sayfası
- `/w/{workspaceId}/c/{conversationId}` — tek bir sohbet

Sayfa yenilendiğinde aynı sohbette kalırsın (`frontend/src/routing.js`).

### 4.2 Kavramlar

| Kavram | Anlamı |
| --- | --- |
| **Workspace** | Klasör gibi; sohbetleri gruplar |
| **Conversation** | Bir sohbet dizisi |
| **Job** | O sohbetteki tek bir gönderim; kendi dizini var (`sources/`, `artifacts/`) |
| **Mode** | Single / Workforce — kullanıcı seçer |
| **Deep research** | Yalnız Workforce'ta; daha çok açı, daha çok sayfa, daha uzun |
| **Skills** | "Kim çalışsın" değil, "nasıl düşünsün" (kök neden, risk…) — sistem prompt'una eklenir |

### 4.3 Composer (alttaki kutu) — 5 kontrol

1. **Metin** — herhangi bir dil (Türkçe yoğun test edildi)
2. **Single / Workforce** anahtarı
3. **Deep research** (yalnız Workforce)
4. **Skills** seçici
5. **Ataç** — PDF, Word, Excel, CSV, TXT, ZIP, görsel (PNG/JPG/TIFF/BMP/WEBP — OCR ile okunur).
   En fazla **10 dosya**, dosya başına **20 MB**, toplam **50 MB**

Gönderince iyimser bir job kartı görünür; gerçek id gelince SSE bağlanır.
Sağ panelde işçiler, tool çağrıları, süre ve token sayısı akar.

### 4.4 Hangi mod ne zaman

**Single** — "Python decorator nedir?", "şu metni kısalt", hızlı sohbet.
Dosya **üretmez**, web **açmaz**. Güncel kur / haber / "pdf ver" için değil.

Single bittikten sonra bazen "bunu Workforce ile yapabilirim" önerisi çıkar —
bu da regex değil, aynı intent modeliyle sınıflandırılır.

**Workforce** — gerçek regresyon cümleleri:

```text
İstanbul 1 haftalık namaz vakitleri pdf olarak ver
Kanuni Sultan Süleyman dönemi html ve pptx sunum verip açar mısın
Bu PDF'in içeriğini yüklediğim docx şablonuna yaz
deniz kaplumbağaları hakkında görselli powerpoint
guncel dolar
Python script yaz ve çalıştır
1 ile 10000 arasındaki asal sayıların toplamı kaç? kodla hesapla
aylık %3 faizle 100.000 TL 24 ayda ne olur, ay ay tablo olarak excel ver
(Excel ekli) bu excel'deki satışları bölgelere göre analiz et, en çok ciro yapan 3 ürünü söyle
(Excel ekli) bölgelere ve aylara göre analiz et, grafikli bir pdf rapor hazırla
```

### 4.5 Çıktıyı nerede görürsün

- Sohbette markdown (başlık, tablo, kaynak linkleri)
- İndirilebilir dosya chip'i — `artifacts/` altındaki gerçek dosya
- Sağ panel: hangi işçi, hangi tool, kaç token, kaç saniye
- Kaynaklar tam `https://` URL olarak; başlıktaki çerez/banner çöpü ayıklanır

Birden fazla format istendiyse (pptx + html) **hepsi** üretilmeli. "Aç" dersen
ve köprü açıksa **hepsi** açılmaya çalışılır, yalnız sonuncusu değil.

### 4.6 Yüklenen belge davranışı

- "Özetle / analiz et" → okur, yeni dosya üretmesi şart değil
- "Bunu doldur / şirketime uyarla / şablon olarak kullan" → `reuses_uploaded_document`
- "Bu PDF'in içeriğini docx ver" → içerik PDF'ten, biçim Word; ikisi karışmaz
- ZIP: metin dosyaları açılır; path traversal ve şifreli entry reddedilir

### 4.7 Devam turları

"ilk üçü kim?", "bunu pdf ver", "bir de powerpoint" gibi cümleler önceki tura
`turn_resolution.py` ile bağlanır. **Format değiştirmek bile Workforce ister** —
Single bir tabloyu dosyaya yazamaz.

### 4.8 İptal

Çalışan işte iptal düğmesi var. İşbirlikçidir: yeni adım başlamaz, ama dönmekte
olan bir model çağrısı birkaç saniye daha sürebilir.

### 4.9 Kod bloğu çalıştırma

Cevaptaki Python bloklarında Run düğmesi (`POST /execute`). Workspace'e
hapsedilmiş, zaman sınırlı interpreter — **güvenlik duvarı değil**.

Workforce'ta buna gerek kalmadan **Coding Agent kodu kendisi çalıştırır**:
yazar → çalıştırır → hata olursa düzeltir → gerçek çıktıyı açıklar. Cevapta
görünen kod çalışan koddur, çıktı gerçekten basılan çıktıdır. Yüklenen
Excel/CSV'yi analiz eder, grafik çizer, grafikleri rapora gömer. Denenecek
cümleler: § 4.4; ayrıntı: [`docs/coding-agent.md`](docs/coding-agent.md).

### 4.10 Manuel agent (kaldırıldı)

Arayüzdeki "Add agent" 24.09.2026'da CAMEL ile birlikte kaldırıldı: yalnız
CAMEL yolunda çalışıyordu, LangGraph'ta listede görünüp hiçbir işe
atanmıyordu. Eski kayıtlar `manual_workers` tablosunda durur, okunmaz.

### 4.11 Ne beklememeli

- Hesap, paylaşım linki, çok kullanıcılı yetki — yok
- Telefonda cilalı layout — kontrol edilmedi
- Her Workforce işinin 10 saniyede bitmesi — araştırma dakikalar sürer, deep daha uzun
- Modelin her seferinde aynı dosya adını / aynı slayt sayısını üretmesi
- İnternetsiz web araştırması — SearXNG dışarı çıkar

---

## 5. Mimari — bir istek uçtan uca

```text
Tarayıcı (React)
    REST  /upload  /run-pipeline  /jobs  /workspaces  /workers  /skills
    SSE   /jobs/{id}/events
        │
        ▼
FastAPI (backend/main.py)
    SQLite  DATA_DIR/app.db
    upload  → Docling  → job workspace (sources/ + artifacts/)
        │
        ├─ mode=single  → langgraph_runtime.single.run_single_agent
        │
        └─ mode=multi   → artifact_intent + turn_resolution
                             langgraph_runtime.workforce.run_workforce_in_thread
                               LangGraph supervisor
                                 Research / Document / Coding / Assistant
                                 web_controller: SEARCH → SELECT → FETCH → ANSWER
                                 document_model / document_html / openpyxl
                                 (opsiyonel) local_computer aç
```

### 5.1 Workforce işçileri

`execution/langgraph_runtime/workforce.py`:

| Rol | İş |
| --- | --- |
| Research Agent | SearXNG/AgentSearch + sayfa açma + kanıt notları |
| Mail Agent | Outlook posta kutusunu okur (opsiyonel); kanıt toplar, cevabı yazmaz |
| Jira Agent | Şirket Jira'sında arar, kayıtları okur (opsiyonel, salt okunur); kanıt toplar, cevabı yazmaz |
| GitHub Agent | Herkese açık GitHub'da depo arar; bağlantısı verilen depo / dosya / issue / PR'ı okur (salt okunur); kanıt toplar, cevabı yazmaz |
| Knowledge Agent | Workspace'e yüklenen belgelerde arar (FTS5 + BGE reranker); `[K#]` atıflı özet toplar, cevabı yazmaz ([`docs/knowledge.md`](docs/knowledge.md)) |
| Document Agent | Word/PDF/PPTX/HTML/Excel yazma, şablon doldurma |
| Coding Agent | Kodu yazar, **çalıştırır**, hata varsa düzeltir, gerçek çıktıyı açıklar; yüklenen Excel/CSV'yi analiz eder, grafik (PNG) çizer. Yazdığı xlsx/csv istenen dosyaysa Document atlanır; rapor istendiyse Document grafikleri PDF/Word'e gömer ([`docs/coding-agent.md`](docs/coding-agent.md)) |
| Assistant | Genel; "bilgisayarımda aç" dahil |

### 5.2 Niyet sınıflandırması

`artifact_intent.py` — Workforce job başına **bir** LLM çağrısı, pydantic
structured output. Regex değil; regex yalnız yedektir.

- `wants_artifact` + `artifact_type` (docx, pptx, pdf, xlsx, html, code…)
- `needs_web_research`
- `needs_code_execution`
- `reuses_uploaded_document` — yüklenen dosyayı şablon / doldurulacak belge say
- `opens_on_user_computer` — "aç", "açar mısın", "sonra aç"

Yüklenen belgeyi doldurmak **kod işi değildir**; Coding'e sapmasın diye kural var.

### 5.3 Web araştırması

Model "google'a git" tool'u çağırmaz. `web_controller.py` sabit fazlar yürütür:

1. **PLAN** — soruyu açılara böl
2. **SEARCH** — her açı için sorgu
3. **SELECT** — hangi URL açılacak
4. **FETCH** — Crawl4AI (gerçek Chromium, JS çalışır)
5. **ANSWER** — kanıttan düz yazı; Kaynaklar listesini sunucu ekler

```text
web_search → AgentSearch :3939 (açıksa) → SearXNG
          → SearXNG doğrudan (yedek)
web_read  → crawl4ai_engine.py   (SSRF kontrolü: web_security.py)
```

Deep research açı tabanını yükseltir (`MIN_PLAN_ANGLES_DEEP` vb.). `DEEP_RESEARCH_MODE=iter` ile bunun yerine her turda tek bir kaynaklı raporu baştan yazan döngü çalışır (bkz. `docs/web-execution.md`). Kanıt bütçesi
Document'a gider; derin + çok formatlı işte not kırpılması raporu zayıflatmasın
diye ayrı tavan vardır.

Cevap **sohbet metnidir**, dosyanın kaynağı değil. Model HTML sayfasını sohbete
yazarsa `unwrap_document_source` onu düz yazıya çevirir ve Document yine dosyayı
üretir. Cevap kullanıcının kendi cümlesiyle başlıyorsa `drop_echoed_request`
o satırı atar.

### 5.4 Dosyalar

**Giriş (upload):** PDF, DOCX, XLSX, CSV, TXT, ZIP, görsel. Docling PDF/DOCX/PPTX/XLSX/HTML'i
ve görselleri kanonik JSON + markdown + tablo CSV'ye çevirir. Taranmış sayfalar ve görseller
PaddleOCR-VL-1.6 ile okunur (GPU'da; tablo/düzen çok iyi, ama `ı`→`i`, `ğ`→`g` hatası yapar); metin katmanı olan PDF sayfaları Docling'in birebir metniyle okunur; servis yoksa Tesseract'a düşer (Türkçe + İngilizce; ölçümler ve WSL ayarları: [`docs/document-ingestion.md`](docs/document-ingestion.md)). Büyük belge model context'ine
gömülmez; önizleme + yol gider. Yüklenenler `sources/`, üretilenler `artifacts/` —
birbirini ezmez.

**Çıkış (artifact):**

| Tür | Kim yazar |
| --- | --- |
| PDF | Tipli bloklar → `document_html` (HTML/CSS, Chromium); yedek: reportlab (`document_model` / `document_export`) |
| DOCX | Tipli bloklar → `document_docx` (PDF ile aynı tasarım; yedek `document_model`); yüklenen şablon: `document_export` doldurma / düzenleme |
| PPTX | Tipli slaytlar (`slide_model`) → `document_pptx` (tablo, grafik, konuşmacı notu, sığdırma); yedek: `document_export.build_pptx` |
| XLSX / CSV / JSON | openpyxl / pandas; düzyazıdaki markdown tablo tercih edilir |
| HTML sunum | Tipli bloklar varsa `document_html.render_deck` (rapor ile aynı tasarım); yoksa `workforce._html_deck_from_payload` |

Word'de tablo stili şablonda yoksa işin tamamı düşmez; var olan stile iner veya
stilsiz yazar.

### 5.5 Canlı durum (SSE)

`GET /jobs/{id}/events` — `text/event-stream`. Nginx ve Vite tamponlamayı kapatır.
Olaylar aktivite gösterir (`searching`, `opening`, `reading`, `building`, `done`);
modelin iç monoloğu değildir.

### 5.6 Kalıcılık

SQLite (`backend/db.py` + `backend/migrations/`): job'lar, workspace,
conversation, mesaj özeti, tool çağrıları, skills, `deep_research` bayrağı,
manuel işçiler, konuşmadaki dosya katalogu (takip job'ı eski artifact'i bulsun).

Process ile ölenler: canlı workforce nesnesi, SSE kuyruğu, iptal bayrağı. Açılışta
`running` / `queued` job'lar `interrupted` işaretlenir.

### 5.7 Güvenlik — dürüstçe

- **Auth yok.** Bu yığını internete açma; iç ağ / localhost.
- `run_python` workspace + süre + temizlenmiş env ile sınırlı — gerçek sandbox değil
- Web: yalnız public HTTP(S), SSRF kontrolü (iç IP / localhost reddedilir)
- Local computer: allow-list dizin (varsayılan Downloads), yalnız aynı makine

---

## 6. Kod haritası

```text
agent-webapp-langgraph/
├── docker-compose.yml      # frontend + backend + searxng + agent-search
├── .env.example            # ortam değişkeni şablonu
├── HANDOVER.md             # bu dosya
├── searxng/settings.yml    # arama motoru ayarı
├── vendor/agent-search/    # git submodule
├── scripts/                # local_computer_bridge.py
├── frontend/               # React SPA
├── backend/                # FastAPI + ajanlar
├── docs/                   # konu bazlı operasyonel ayrıntı
└── .github/workflows/      # backend pytest
```

### Backend — giriş noktaları

| Dosya | Görev |
| --- | --- |
| `main.py` | FastAPI: upload, run-pipeline, jobs, SSE, workspace, export, execute |
| `db.py` | SQLite, migrasyonlar, conversation/job |
| `model_config.py` | `MODEL_*` env, UsageTracker (token + süre) |
| `execution/langgraph_runtime/single.py` | LangGraph Single |
| `execution/langgraph_runtime/workforce.py` | LangGraph Workforce (asıl işçi grafı) |
| `execution/langgraph_runtime/openai_chat.py` | Chat Completions, retry, truncation |

### Niyet, yönlendirme, yetenek

| Dosya | Görev |
| --- | --- |
| `artifact_intent.py` | Workforce: dosya / web / kod / şablon / aç / **e-posta gönder** (`sends_email`, `email_to`) / **posta oku** (`reads_email`) |
| `turn_resolution.py` | Takip cümlesini bağımsız istek yapar |
| `capabilities.py` | web_search, web_fetch, files, code_exec, artifacts |
| `capability_routing.py` | DIRECT / WORKFORCE + structural merge |
| `workforce_plan.py` | Plan / coverage |
| `skills.py` | Built-in skill katalogu |

### Web

| Dosya | Görev |
| --- | --- |
| `web_controller.py` | SEARCH→SELECT→FETCH→ANSWER; HTML unwrap; echo başlık kesme |
| `web_search.py` | AgentSearch + SearXNG |
| `agent_search_client.py` | Sidecar HTTP |
| `web_tools.py` | Modele görünen `web_search` / `web_read` |
| `crawl4ai_engine.py` | Playwright sayfa açma |
| `html_extract.py` | Trafilatura / Parsel |
| `web_security.py` | SSRF / public network kontrolü |
| `image_fetch.py` | Görsel indirme; boş/düz renk reddi |

### Belge ve çıktı

| Dosya | Görev |
| --- | --- |
| `document_ingestion.py` | Docling parse + cache |
| `document_export.py` | Markdown→DOCX/PDF/PPTX iskeleti, tablo, stiller |
| `artifacts.py` | `create_*_artifact` tool'ları |
| `document_model.py` | Tipli belge blokları + DOCX / reportlab PDF çizici |
| `document_html.py` | Blokları HTML/CSS'e çevirir, Chromium ile PDF basar; HTML sunum destesi |
| `document_docx.py` | Blokları PDF ile aynı tasarımda Word'e çizer |
| `slide_model.py` | PPTX tipli slaytları: ayrıştırma, sadeleştirme, rakam/alıntı doğrulama |
| `document_pptx.py` | Slaytları .pptx'e çizer; yazıyı ölçüp sığdırır, uzun tablo/listeyi dengeli böler |
| `document_verify.py` | Üretilen PDF/DOCX'i teslimden önce yeniden açıp kontrol eder |
| `workspace.py` | sources/artifacts sınırları, dosya araçları |
| `code_exec.py` | Sınırlı Python (workspace, ağ yok, temizlenmiş env, süre sınırı) |
| `code_controller.py` | Coding Agent fazları: WRITE → RUN → FIX → ANSWER; kod/çıktı bloklarını sunucu ekler; `CHART:` satırları |
| `code_data.py` | Yüklenen veri dosyalarını Coding'e tanıtır: yol, okuma çağrısı (ayraç/kodlama), sayfalar, sütunlar, ilk satırlar |
| `local_computer.py` | Host'ta dosya açma |

### Destek

| Dosya | Görev |
| --- | --- |
| `workforce_callback.py` | Workforce thread'inin olaylarını SSE kuyruğuna taşır |
| `workforce/answer_format.py` | Son cevabın temizlenmesi + yazma efekti, sohbet özeti |
| `tool_call_log.py` | Tool audit |
| `stream_protocol.py` | Model metninden tool-call işaretlerini temizler |
| `outlook.py` | Outlook köprü istemcisi, e-posta gövdesi, posta okuma istemcisi, `/integrations/outlook` |
| `mail_controller.py` | Posta kutusu fazları: PLAN→SEARCH→SELECT→READ, alıntı çiti, ek indirme |
| `jira_client.py` / `jira_query.py` / `jira_controller.py` | Jira: REST istemcisi (Cloud + Server/DC), model alanları → JQL, PLAN→SEARCH→SELECT→READ→ANSWER |
| `github_client.py` / `github_query.py` / `github_controller.py` | GitHub: REST istemcisi (yalnız GET, ETag önbelleği), bağlantı ayrıştırma + doğrulama, PLAN→SEARCH→SELECT→READ→ANSWER; web araştırmasındaki github.com adresleri de buradan okunur |
| `knowledge_index.py` / `knowledge_controller.py` / `knowledge_api.py` | Workspace belge arama: pasaj indeksi (`knowledge.db`), PLAN→SEARCH→RANK→EXPAND→ANSWER, kütüphane uçları |
| `scheduling/` | Zamanlanmış görevler: cron, runner, zincirler, izleme, e-posta (`delivery.py`) |
| `runtime_context.py` | Job context |
| `golden_prompts.py` | Gerçek kullanıcı cümleleriyle regresyon |

Vendor dizinleri: `docling-models/` ve `bge-reranker-v2-m3/` (ağırlıklar,
gitignore).

### Frontend

| Dosya | Görev |
| --- | --- |
| `src/main.jsx` | React mount |
| `src/App.jsx` | Global state, submit, SSE, workspace |
| `src/api.js` | `/api` fetch |
| `src/routing.js` | `/w/.../c/...` |
| `src/stages.js` | Job durumları / sekmeler |
| `components/WorkspacePicker.jsx` | İlk ekran |
| `components/HomeView.jsx` | Workspace ana sayfası, starter'lar |
| `components/ThreadView.jsx` | Sohbet |
| `components/ChatExchange.jsx` | Mesaj, kod bloğu, başlık promote |
| `components/Composer.jsx` | Yazma kutusu, mode, deep, skills, upload |
| `components/Sidebar.jsx` | Sohbet listesi |
| `components/RightPanel.jsx` | İşçiler, tool, usage |
| `components/TopBar.jsx` | Üst çubuk |
| `nginx.conf` | SPA + `/api` proxy, SSE, 50m upload |

Görsel standart: `.claude/skills/design-quality/`.

### Bir Workforce job'ında okuma sırası

1. `main.py` → `_run_job`
2. `turn_resolution` (takip turuysa)
3. `classify_artifact_intent`
4. `execution.adapter.run_workforce_in_thread`
5. `langgraph_runtime/workforce.py` — rol seç; Research `web_controller`,
   Document `document_export` / `artifacts`
6. Sonuç `job["result"]` + artifact path'ler → SSE → `ChatExchange`

### Dokunurken dikkat

- Model çağrısı `openai_chat.py` veya `model_config.py` üzerinden — rastgele SDK dağıtma
- "Kullanıcı dosya istedi mi" sorusu `artifact_intent.py`'nin işi, regex yığınının değil
- Web kalitesi `web_controller.py` + testleri
- Slide/Word ayrışması `document_export.iter_body_blocks` — HTML/PPTX kendi
  heading detector'ını yeniden yazmasın
- `DATA_DIR`: testte `conftest.py` izole eder, canlı container `/data`
- Kod yorumları çoğu zaman **canlı bir bug'ı** anlatır; silme

---

## 7. API uçları

Backend kök path'lerini dinler; tarayıcı `/api` öneki ekler (nginx/Vite soyar).

| Metod | Yol | Ne |
| --- | --- | --- |
| POST | `/upload`, `/uploads` | Dosya yükle |
| POST | `/run-pipeline` | Job başlat (`mode`: `single` \| `multi`) |
| GET | `/jobs`, `/jobs/{id}` | Durum |
| GET | `/jobs/{id}/events` | SSE canlı ilerleme |
| POST | `/jobs/{id}/cancel` | İptal |
| GET | `/jobs/{id}/artifacts/{path}` | Üretilen dosyayı indir |
| POST | `/jobs/{id}/export/{fmt}` | Cevabı md/txt/docx/pdf yap |
| POST | `/execute` | Kod çalıştır (UI'daki Run) |
| GET | `/workers` | İşçi havuzu |
| GET/POST/PATCH/DELETE | `/workspaces` | Workspace |
| DELETE | `/conversations/{id}` | Sohbet sil |
| GET | `/skills` | Skill listesi |
| GET/POST/PATCH/DELETE | `/scheduled-tasks` | Zamanlanmış görevler, çalıştırma geçmişi (`docs/scheduling.md`) |
| GET | `/integrations/outlook/status` | E-posta hazır mı (köprü, token, Outlook) |
| POST | `/integrations/outlook/test` | Test e-postası |
| GET | `/integrations/jira/status` | Jira bağlı mı (yapılandırma, erişim, kullanıcı); `POST .../recheck` önbelleksiz |
| GET/POST/DELETE | `/workspaces/{id}/documents` | Workspace belge kütüphanesi; `GET /workspaces/{id}/knowledge/search?q=` ham arama |

OpenAPI/Swagger: http://127.0.0.1:8001/docs

Elle deneme:

```json
POST /run-pipeline
{ "topic": "Kısa bir merhaba", "mode": "single" }
```

Workforce için `"mode": "multi"`, derin için `"deep_research": true`.

---

## 8. "Bilgisayarımda aç" köprüsü

Kullanıcı "pdf ver sonra aç" deyince dosya **kullanıcının** Word/Preview'ında
açılır. Docker'ın GUI'si yoktur; host'ta küçük bir HTTP köprüsü gerekir.

1. `.env`: `LOCAL_COMPUTER_ENABLED=true`
2. Host'ta (container'da değil):

```powershell
python scripts\local_computer_bridge.py
curl http://127.0.0.1:8765/health
```

3. Backend'i yeniden başlat.

Sadece **aynı makine** — uzak tarayıcıdan açılmaz. Dosya allow-list dizini
(varsayılan Downloads) dışına yazılmaz. Ayrıntı:
[`docs/local-computer.md`](docs/local-computer.md).

### Aynı köprü: Outlook ile e-posta

Köprüde bir de `/outlook/send` ucu var: zamanlanmış görev sonuçları klasik
Outlook masaüstü uygulamasından e-postayla gönderilir (Microsoft Graph / Azure
kaydı gerekmez). Gönderen hesap, köprünün çalıştığı makinede Outlook'ta açık olan
profildir.

1. `.env`: `OUTLOOK_ENABLED=true` ve `LOCAL_COMPUTER_TOKEN=<rastgele değer>`
2. Host'ta: `pip install pywin32`, sonra köprüyü **aynı token ile** başlat:
   `python scripts\local_computer_bridge.py --token <değer>`
3. Arayüz: Scheduled → görev → **Email** → alıcı → **Send test email**

Token yalnız Outlook uçları için zorunludur: köprü kimlik doğrulamasız dinlediği
için, token olmasa tarayıcıda açık herhangi bir sayfa `127.0.0.1:8765`'e istek
atıp kullanıcı adına e-posta gönderebilirdi. Kurulum, güvenlik modeli, otomatik
başlatma ve sorun giderme: [`docs/outlook.md`](docs/outlook.md).

Köprü container'ın içine konamaz: Linux container'ın Windows oturumuna, GUI'ye
veya COM'a erişimi yoktur.

---

## 9. Docker'sız yerel geliştirme

Üretim gibi tam yığın için Bölüm 2. Bu bölüm backend'i host Python'da,
frontend'i Vite'de çalıştırmak içindir. **Portlar farklıdır:** Vite 5173,
uvicorn 8000; Vite `/api`'yi 8000'e proxy'ler (`frontend/vite.config.js`).

### Backend

```powershell
cd backend
py -3.11 -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ardından tarayıcı ve test araçları:

```powershell
python -m playwright install chromium
pip install "matplotlib==3.11.2" pytest pytest-asyncio pypdf
```

Web sayfaları ve PDF'ler bu Playwright Chromium ile açılır/basılır.

Chromium yoksa PDF otomatik olarak reportlab ile üretilir.

`backend/.env` yaz (Bölüm 3'teki iki-`.env` uyarısı), sonra:

```powershell
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend

```powershell
cd frontend
npm ci
npm run dev
```

http://localhost:5173 → `/api` → `http://127.0.0.1:8000`.
Backend başka yerdeyse: `BACKEND_URL=http://127.0.0.1:8001 npm run dev`

### Arama servisleri

Single sohbet yalnız modeli ister. Workforce araştırma **SearXNG** ister. En
kolayı arama servislerini Docker'da bırakmak:

```powershell
docker compose up -d searxng agent-search
```

Host backend için:

```env
SEARXNG_BASE_URL=http://127.0.0.1:8080
AGENT_SEARCH_BASE_URL=http://127.0.0.1:3939
AGENT_SEARCH_ENABLED=true
```

Compose SearXNG'yi host'a **yayınlamaz** (`expose` only). Host'tan vurmak için
`docker-compose.yml`'ye geçici `ports: ["8080:8080"]` ekle, ya da backend'i de
Compose'ta çalıştır.

Ağsız (air-gap) kurulum desteklenmiyor; bağımlılıklar PyPI'den kurulur.

---

## 10. Test ve geliştirme döngüsü

CI: `.github/workflows/backend-tests.yml` — `main` push ve backend path'li PR.
Gerçek Chromium **yok**; crawl testleri sahte engine kullanır.

### Backend testleri

Host'ta venv yoksa **container içinde** koş:

```powershell
docker compose exec backend pip install pytest pytest-asyncio pypdf
docker compose exec backend python -m pytest tests -q
```

`pytest` image'da yok ve **her `--build` sonrası tekrar kurulmalı**.

Host venv varsa:

```powershell
cd backend
venv\Scripts\Activate.ps1
python -m pytest tests -q
```

Tek dosya:

```powershell
python -m pytest tests/test_document_export.py tests/test_web_controller.py -q
```

Notlar:

- `pytest.ini`: `testpaths = tests`, `vendor` yok sayılır
- Git Bash `docker compose exec` içindeki `/tmp/...` yollarını Windows'a çevirir.
  Engelle: `MSYS_NO_PATHCONV=1`
- `conftest.py` `DATA_DIR`'i geçici dizine **zorlar** (setdefault değil) — testler
  canlı sohbet geçmişini kirletmesin diye. `PYTEST_USE_REAL_DATA_DIR=1` ile bunu
  bozabilirsin; **production volume'da yapma**
- Model / ağ isteyen test yok sayılır veya mock'lanır; gateway kapalıyken suite
  yeşil kalmalı

### Frontend testleri

```powershell
cd frontend
npm test
```

`routing.test.mjs`, `render.test.mjs`, `strategy.test.mjs`, `result.test.mjs`,
`api.test.mjs` — Node, tarayıcı yok.

### Elle duman testi

Docker ayaktayken sırayla:

1. Single: "2+2 kaç"
2. Workforce: kısa bir araştırma + "kaynaklı özet"
3. "şu konuyu pdf ver"
4. "pptx ve html hazırla"
5. Yüklenen docx: "bu şablonu X konusuyla doldur"
6. (köprü açıksa) "pdf ver sonra aç"

### Kod değiştirirken

- Frontend: Vite anında; Docker image için `docker compose up -d --build frontend`
- Backend Python: yerelde `--reload`; **Docker'da mutlaka rebuild**
- `docker-compose.yml` / Dockerfile: her hâlükârda rebuild

### Log

```powershell
docker compose logs -f backend
```

Logger adı: `agent_webapp.stabilization` — retry, truncation, intent kararları.
Dosya logları `DATA_DIR/logs/` (Docker: volume `/data`).

---

## 11. Sınırlar ve bilinen açıklar

### Bilerek yok

- Kullanıcı hesabı, yetki, rate limit
- Çok process'li kuyruk / yatay ölçek
- Upload TTL / otomatik temizlik (disk zamanla dolar)
- Production health/readiness probe (compose'ta `restart: unless-stopped` var)
- Tam güvenlik sandbox'ı (`run_python` yumuşak sınır)
- Air-gap'te Crawl4AI + Chromium + Docling modelleri

### Çalışan iş restart'ta ölür

DB'deki sohbet kalır; `running` / `queued` job'lar açılışta `interrupted` olur.
Devam ettirme yok — kullanıcı yeniden gönderir.

### Bilinen açıklar (teslim anı)

Kodda duruyor, **henüz bitmedi**:

1. **"Yapamam / maalesef yerine getiremiyorum" bazen süzgeçten geçip kullanıcıya
   ulaşıyor.** `_looks_like_web_access_refusal` kelime listesine bakıyor; asıl
   sinyal **dosyanın üretilip üretilmediği** olmalı.
2. **İki docx'te "içerik A, biçim B" ayrımı zayıf** — şablon seçimi kırılgan.
3. **CI yalnız `main`'e push'ta ve backend'e dokunan PR'da koşar**
   (`.github/workflows/backend-tests.yml`). Dal üstünde çalışırken testleri
   elle koş. Son ölçüm (2026-09-24, CAMEL kaldırıldıktan sonra): **1409
   geçti, 0 kaldı**. Aradaki fark silinen, yalnız CAMEL yolunu test eden
   dosyalardır.
4. **OpenRouter sağlayıcısı OpenInference** deepseek-v4-flash cevabının
   tamamını `reasoning` alanına yazıp `content`'i boş bırakıyor (finish=stop).
   Coding Agent bunu yakalıyor (`complete_chat(reasoning_fallback=True)` +
   150 sn sınır + bir yeniden deneme); **Research / Document / Assistant
   yakalamıyor** — o sağlayıcıya düşen bir Document çağrısı 400 sn bekleyip
   boş döndü. Kalıcı çözüm adayı: OpenRouter'da `provider.ignore`.
5. **Coding Agent'ın kod çalıştırması gerçek sandbox değildir** — süreç
   seviyesinde sınır (`code_exec.py` başındaki not). Güvenilmeyen girdi için
   ayrı, ağsız bir container gerekir.

### Yakın geçmişte düzeltilenler (regresyon olursa buraya bak)

- Tablo stili şablonda yoksa Word çıktısının tamamen düşmesi
- PPTX tek slayt kalırken HTML'in dolu gelmesi (PPTX `body` okumalı, `summary` değil)
- Excel'in "Bölüm | İçerik" iskeletine düşmesi (düzyazıdaki gerçek markdown tablo tercih edilir)
- Sohbete stylesheet yazılması (`unwrap_document_source`; Document yine çalışır)
- Başlığın kullanıcının kendi cümlesi olması (`drop_echoed_request`; `**Başlık**` promote edilir)
- Testlerin canlı `/data`'ya sohbet yazması (`conftest.py` izolasyonu)
- CI'ın `langgraph`'ı hiç kurmaması — suite collection'da 4 hatayla düşüyor,
  647 testin hiçbiri çalışmıyordu (workflow'a Dockerfile'daki kurulum adımı eklendi)
- Kaynak başlığında site menüsü kalması (`_clean_source_title`, nav kelimeleri) ve
  Deep anahtarının iş sürerken kilitlenmemesi — teslimdeki açık 1 ve 4
- Coding Agent'ın kodu çalıştırmadan çıktı tahmin etmesi (PR #13): artık gerçekten
  çalıştırıyor. Aynı işte düzeltilenler: rapora kaynak kodun basılması ("yazı çok
  kısa" yedeği), modelin kendi `PdfPages` PDF'ini tasarımlı raporun yerine
  geçirmesi, başarısız hesap için boş PDF yazılması, kod yorumundaki URL'nin
  "Kaynaklar"a girmesi

---

## 12. Sorun giderme

### Model

| Belirti | Bak |
| --- | --- |
| Hemen hata / boş cevap | `.env` `MODEL_*`, gateway `/v1` ekiyle mi |
| "Task timed out" | Model yavaş veya kuyrukta; timeout 600 s |
| Yarıda kesik rapor | Answer `max_tokens`; LangGraph tavanı bir kez 2× dener |
| JSON intent boş dönüyor | Reasoning modeli bütçeyi düşünmeye yakıyor; token tavanları bilinçli yüksek |

### Coding Agent

| Belirti | Bak |
| --- | --- |
| Grafik hiç çizilmiyor, cevap "grafik üretme" diyor | İmajda matplotlib yok: `docker compose up -d --build backend` |
| "Kod Ajanı … kod üretemedi" | Log'da `stage=coding_write` satırı: `provider=OpenInference` + `content empty` → sağlayıcı sorunu (§ 11 açık 4); tekrar gönder |
| Excel analizi Coding'e gitmedi, önizlemeden cevap geldi | İstekte analiz kelimesi yok (analiz, özetle, toplam, grafik, en yüksek…) veya dosya xlsx/csv değil (`capabilities._TABULAR_ANALYSIS_RE`) |
| `.xls` okunamıyor | xlrd kurulu değil; dosyayı `.xlsx` kaydedip yükle |
| Süre paneli | `LLM · code writing`, `Code run`, `LLM · code fix`, `Data file profiling` |

### Docker / port

- Arayüz **8081**, API **8001** — 8080/8000'de başka kopya olabilir
- `bind: address already in use` — eski compose veya yerel uvicorn ayakta
- Frontend açılıyor ama API gitmiyor → backend log; `depends_on` SearXNG'yi
  bekler ama **healthy beklemez**
- İlk build 15–30 dk (torch + playwright) — donmuş değil

### Arama / sayfa

- AgentSearch 500 / boş → `vendor/agent-search` boş mu bak. Klonladıysan
  `git submodule update --init`; teslim klasörüyle geldiyse yanlış/eksik bir
  kopya almışsın, klasörü yeniden iste. Log: `docker compose logs agent-search`
- Hiç sonuç yok → SearXNG dışarı çıkabiliyor mu, `searxng/settings.yml`
- Chromium crash → `shm_size: 1gb` (compose'ta var, düşürme)
- "javascript enable / cookie" başlıklı kaynak → başlık temizliği var ama sayfa
  yine duvar olabilir; `MIN_PROSE_EVIDENCE_CHARS` ince kabukları atar
- SSRF reddi (iç IP / localhost) → **doğru davranış**
- Yalnız SearXNG istiyorsan: `AGENT_SEARCH_ENABLED=false`

### Upload / Docling

- 422 `docling_conversion_failed` → modeller yok; ilk indirme ağ ister
  ([`docs/document-ingestion.md`](docs/document-ingestion.md))
- 422 `ocr_disabled` → görsel yüklendi ama `DOCLING_ENABLE_OCR=false`
- OCR kalitesi Tesseract seviyesinde → PaddleOCR-VL yerine yedek çalışmış
  olabilir. Upload'un `manifest.json` → `parser.fallback_reason` alanına bak
  (`unavailable` = servis kapalı, `timeout`, `incomplete_result` = eksik sayfa).
  `docker compose --profile paddleocr ps` ve
  `docker compose logs paddleocr-vlm-server paddleocr-vl-api`
- `paddleocr-vlm-server` açılmıyor → GPU compute capability ≥ 8.0 mı, NVIDIA
  sürücüsü vLLM imajının CUDA sürümünü destekliyor mu (`nvidia-smi` sağ üst)
- Taranmış PDF / görsel boş geliyor → `DOCLING_ENABLE_OCR=true` mi,
  `docker compose exec backend tesseract --list-langs` içinde `tur` var mı;
  manifest'te `text_empty: true` ise görüntü okunamayacak kadar kötü
- 422 `document_parse_timeout` büyük taramada → `DOCLING_PARSE_TIMEOUT_SECONDS`'ı artır
- Frontend upload listesinde pptx/html yok; bunlar **çıktı** olarak üretilir

### Son işe SQLite'dan bakmak

```powershell
docker compose exec -T backend python -c "import sqlite3; c=sqlite3.connect('file:/data/app.db?mode=ro', uri=True); c.row_factory=sqlite3.Row; d=dict(c.execute('select * from jobs order by created_at desc limit 1').fetchone()); print(d['topic'], d['status'])"
```

---

## 13. Nereden başlamalı

Devralan kişi için ilk gün:

1. Bölüm 2'yi uygula, arayüzü aç, Single ile bir soru sor.
2. Workforce'ta "İstanbul hakkında kısa bir PDF hazırla" çalıştır; sağ paneli
   izle — hangi işçi, hangi tool, kaç token.
3. `backend/main.py` içindeki `_run_job`'ı oku; oradan
   `langgraph_runtime/workforce.py` zincirini takip et.
4. `backend/tests/test_web_controller.py` ve `test_document_export.py`'yi koş —
   sistemin gerçekten neyi garanti ettiğini testler anlatır.
5. Bölüm 11'deki 4 açık maddeden birini seç ve düzelt; hepsinin yeri işaret edildi.

Konu bazlı derin referanslar:

| Dosya | Ne zaman bak |
| --- | --- |
| [`docs/archive/langgraph-migration.md`](docs/archive/langgraph-migration.md) | CAMEL → LangGraph geçiş notları (arşiv) |
| [`docs/web-execution.md`](docs/web-execution.md) | Arama + sayfa açma |
| [`docs/document-ingestion.md`](docs/document-ingestion.md) | Upload / Docling |
| [`docs/document-generation.md`](docs/document-generation.md) | PDF / Word / PowerPoint üretimi |
| [`docs/scheduling.md`](docs/scheduling.md) | Zamanlanmış ve izleme görevleri |
| [`docs/local-computer.md`](docs/local-computer.md) | "Bilgisayarımda aç" köprüsü |
| [`docs/agent-search.md`](docs/agent-search.md) | AgentSearch sidecar |
| [`docs/jira.md`](docs/jira.md) | Jira okuma: kurulum, VPN, güvenlik |
| [`docs/github.md`](docs/github.md) | GitHub okuma: token, sınırlar, güvenlik |
| [`docs/knowledge.md`](docs/knowledge.md) | Workspace belge arama: ölçümler, eval, sınırlar |
| [`docs/backend-reliability-harness.md`](docs/backend-reliability-harness.md) | Deterministik güvenilirlik harness'ı |
