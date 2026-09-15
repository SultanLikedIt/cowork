# agent-webapp — Devir Teslim Rehberi

Bu dosya projeyi **hiç bilmeyen** birine devretmek için yazıldı. Kurulumdan
mimariye, günlük kullanımdan bilinen açıklara kadar her şey burada. Repodaki
`docs/` klasörü tek tek konuların (Docling, belge üretimi, web, local computer) daha
derin referansıdır; bu dosya onların üstündeki tek giriş noktasıdır.

**Okuma sırası:** Bölüm 1–3 (ne bu, nasıl kurulur) → Bölüm 4 (nasıl kullanılır)
→ gerisi ihtiyaç oldukça.

---

## İçindekiler

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

**CAMEL kodu hâlâ duruyor — bu kasıtlı.**

`backend/camel_service.py` (4.700+ satır) silinmedi. İstersen tek satır env
değişikliğiyle eski davranışa dönebilirsin:

```env
AGENT_EXECUTOR=camel      # eski CAMEL yolu
AGENT_EXECUTOR=langgraph  # guncel yol -- teslimde bunu kullan
```

Anahtar `backend/execution/adapter.py` içinde (41 satır, okuması kolay).
Uygulamanın geri kalanı `camel_service`'i doğrudan çağırmaz, hep adapter'ı
çağırır — bu yüzden iki yol yan yana yaşayabiliyor.

> **Ama dikkat:** kod default'u (env boşsa) **`camel`**'dir, `langgraph` değil.
> Klasör adı `agent-webapp-langgraph` olsa bile. `.env`'ye yazmazsan eski yol
> çalışır ve LangGraph'a özel bazı özellikler (`reuses_uploaded_document`,
> `opens_on_user_computer`) **sessizce kaybolur** — hata vermez, sadece
> çalışmaz. "Niye yüklediğim şablonu görmedi?" sorusunun cevabı çoğu zaman budur.

Eski repolarda kalan, bu dala hiç girmemiş birkaç terk edilmiş deney dalı var
(`bsaltalii/recovery/orchestration-stable-web` — 7 commit "Routing V2" denemesi,
`bsaltalii/feature/triall_webtools_rebuild` — 2 commit). Bunlar 20 Ağustos'ta
bırakıldı ve sonraki 84 commit'lik iş onların yerine geçti. Referans dışında
ihtiyaç duyulması beklenmiyor.

### İki executor — özet

| `AGENT_EXECUTOR` | Ne çalışır | Dosya |
| --- | --- | --- |
| `langgraph` | Single + Workforce, LangGraph grafında | `execution/langgraph_runtime/` |
| `camel` (veya boş/bilinmeyen) | Eski CAMEL yolu | `camel_service.py` |

Seçim `backend/execution/adapter.py`'de yapılır. Ayrıntı ve tarihçe için
yukarıdaki köken bölümüne bak.

### Portlar

Eski CAMEL yığınıyla **yan yana** çalışabilsin diye host portları farklıdır:

| Servis | Bu repo | Eski Camel yığını |
| --- | --- | --- |
| Arayüz | http://127.0.0.1:8081 | :8080 |
| API | http://127.0.0.1:8001 | :8000 |
| AgentSearch | http://127.0.0.1:3939 | (ayrı kurulum) |
| SearXNG | yayınlanmaz (Docker ağı) | — |

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
AGENT_EXECUTOR=langgraph
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

### 2.7 Docling model cache (opsiyonel ama önerilir)

İlk upload parse'ı, ağırlıklar yoksa Hugging Face'den indirir. Kalıcı cache
(host'ta Docling kuruluysa, bir kere):

```powershell
docling-tools models download --output-dir backend\vendor\docling-models
```

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
- [ ] Kökte `.env` var; üç `MODEL_*` alanı dolu; `AGENT_EXECUTOR=langgraph`
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
| `AGENT_EXECUTOR` | — | `langgraph` \| `camel` (boşsa `camel`) |
| `AGENT_SEARCH_ENABLED` | — | `false` yaparsan yalnız SearXNG |
| `AGENT_SEARCH_BASE_URL` | — | Sidecar adresi |
| `SEARXNG_BASE_URL` | — | Host'ta backend çalıştırırken gerekir |
| `LOCAL_COMPUTER_ENABLED` | — | "Bilgisayarımda aç" köprüsü |
| `LOCAL_COMPUTER_URL` | — | Varsayılan `http://host.docker.internal:8765` |
| `DOCLING_ENABLE_OCR` | — | Compose'ta `true`; taranmış PDF ve görsel yüklemelerini okur. `false` → taranmış PDF boş, görsel reddedilir |
| `OCR_ENGINE` | — | `tesseract` (varsayılan, imajda kurulu) \| `easyocr` \| `rapidocr` |
| `OCR_LANGS` | — | Varsayılan `tur,eng` (motorun kendi dil kodları) |
| `OCR_FORCE_FULL_PAGE` | — | `true` = her sayfa OCR'lanır (yavaş); varsayılan yalnız görsel bölgeler |
| `DOCLING_PARSE_TIMEOUT_SECONDS` | — | Upload parse zaman aşımı; compose'ta 300 (OCR sayfa başına saniyeler sürer) |
| `WEB_REQUEST_TIMEOUT_SECONDS` | — | Sayfa açma zaman aşımı |
| `DOCUMENT_BLOCKS` | — | Varsayılan `true`; PDF/DOCX raporları tipli bloklarla yazılır |
| `DOCUMENT_PDF_ENGINE` | — | `chromium` (varsayılan) veya `reportlab` |
| `DOCUMENT_PDF_TIMEOUT_SECONDS` | — | Chromium PDF basma zaman aşımı (varsayılan 45) |
| `DOCUMENT_LLM_REASONING_EFFORT` | — | Document LLM çağrısı için OpenRouter `reasoning.effort` (`low`/`medium`/`high`); boş = sağlayıcı varsayılanı |
| `DOCUMENT_LLM_PROVIDER_SORT` | — | Document LLM çağrısı için OpenRouter sağlayıcı sıralaması (`throughput`/`latency`/`price`); boş = varsayılan |
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

### 4.10 Manuel agent

Workforce havuzuna isim + açıklama ile agent eklenebilir (**en fazla 10**).
SQLite'da kalır, her Workforce job'ında yeniden bağlanır. Research / Document /
Coding'in yerine geçmez, ek uzmandır.

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
        ├─ mode=single  → execution.adapter.run_single_agent
        │                    camel ChatAgent  VEYA  LangGraph single
        │
        └─ mode=multi   → artifact_intent + turn_resolution
                             execution.adapter.run_workforce_in_thread
                               camel Workforce  VEYA  LangGraph supervisor
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
| Document Agent | Word/PDF/PPTX/HTML/Excel yazma, şablon doldurma |
| Coding Agent | Kod; araştırma/belge işi buraya düşmemeli |
| Assistant | Genel; "bilgisayarımda aç" dahil |

CAMEL yolunda (`camel_service.py`) benzer sabit havuz + coordinator'ın gerekirse
ürettiği dinamik işçi vardır.

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

Deep research açı tabanını yükseltir (`MIN_PLAN_ANGLES_DEEP` vb.). Kanıt bütçesi
Document'a gider; derin + çok formatlı işte not kırpılması raporu zayıflatmasın
diye ayrı tavan vardır.

Cevap **sohbet metnidir**, dosyanın kaynağı değil. Model HTML sayfasını sohbete
yazarsa `unwrap_document_source` onu düz yazıya çevirir ve Document yine dosyayı
üretir. Cevap kullanıcının kendi cümlesiyle başlıyorsa `drop_echoed_request`
o satırı atar.

### 5.4 Dosyalar

**Giriş (upload):** PDF, DOCX, XLSX, CSV, TXT, ZIP, görsel. Docling PDF/DOCX/PPTX/XLSX/HTML'i
ve görselleri kanonik JSON + markdown + tablo CSV'ye çevirir. Taranmış sayfalar ve görseller
Tesseract OCR ile okunur (Türkçe + İngilizce; [`docs/document-ingestion.md`](docs/document-ingestion.md)). Büyük belge model context'ine
gömülmez; önizleme + yol gider. Yüklenenler `sources/`, üretilenler `artifacts/` —
birbirini ezmez.

**Çıkış (artifact):**

| Tür | Kim yazar |
| --- | --- |
| PDF | Tipli bloklar → `document_html` (HTML/CSS, Chromium); yedek: reportlab (`document_model` / `document_export`) |
| DOCX | Tipli bloklar → `document_docx` (PDF ile aynı tasarım; yedek `document_model`); yüklenen şablon: `document_export` doldurma / düzenleme |
| PPTX | python-pptx / `document_export.build_pptx` |
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
| `execution/adapter.py` | camel / langgraph anahtarı |
| `execution/executor_config.py` | `AGENT_EXECUTOR` okuma |
| `camel_service.py` | CAMEL Single + Workforce (büyük dosya) |
| `execution/langgraph_runtime/single.py` | LangGraph Single |
| `execution/langgraph_runtime/workforce.py` | LangGraph Workforce (asıl işçi grafı) |
| `execution/langgraph_runtime/openai_chat.py` | Chat Completions, retry, truncation |

### Niyet, yönlendirme, yetenek

| Dosya | Görev |
| --- | --- |
| `artifact_intent.py` | Workforce: dosya / web / kod / şablon / aç |
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
| `document_verify.py` | Üretilen PDF/DOCX'i teslimden önce yeniden açıp kontrol eder |
| `workspace.py` | sources/artifacts sınırları, dosya araçları |
| `code_exec.py` | Sınırlı Python |
| `local_computer.py` | Host'ta dosya açma |

### Destek

| Dosya | Görev |
| --- | --- |
| `workforce_callback.py` | CAMEL event → queue |
| `tool_call_log.py` | Tool audit |
| `stream_protocol.py` | SSE şekli |
| `runtime_context.py` | Job context |
| `golden_prompts.py` | Gerçek kullanıcı cümleleriyle regresyon |

Vendor dizinleri: `backend/vendor/camel-source/` (CAMEL 0.2.80 kaynağı,
inceleme için), `camel-offline-bundle/` (Windows wheel),
`camel-offline-bundle-linux/` (Docker build cache, gitignore),
`docling-models/` (ağırlıklar, gitignore).

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
| GET/POST | `/workers` | Havuz + manuel agent |
| GET/POST/PATCH/DELETE | `/workspaces` | Workspace |
| DELETE | `/conversations/{id}` | Sohbet sil |
| GET | `/skills` | Skill listesi |

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

`requirements.txt` tek geçişte **crawl4ai ile camel-ai'yi birlikte çözemez**
(psutil pin çatışması: camel-ai `<6` ister, crawl4ai `>=6.1.1`). Dockerfile ile
aynı ek adımlar, bu sırayla:

```powershell
pip install crawl4ai==0.9.2 playwright==1.62.0
python -m playwright install chromium
pip install --no-deps "psutil>=6.1.1"
pip install "langgraph==0.6.7" "ag-ui-protocol>=0.1.15"
pip install pytest pytest-asyncio
```

PDF'ler Playwright Chromium ile basılır (crawl4ai ile aynı tarayıcı). Yerelde bir kez:

```powershell
python -m playwright install chromium
```

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

### Offline wheel (Windows, ağ yok)

```powershell
cd backend
venv\Scripts\python -m pip install --no-index --find-links vendor\camel-offline-bundle -r requirements.txt
```

Crawl4AI / Playwright / Docling / LangGraph bu bundle'da eksik kalabilir. Tam
air-gap ayrı bir proje işidir.

---

## 10. Test ve geliştirme döngüsü

CI: `.github/workflows/backend-tests.yml` — `main` push ve backend path'li PR.
Gerçek Chromium **yok**; crawl testleri sahte engine kullanır.

### Backend testleri

Host'ta venv yoksa **container içinde** koş:

```powershell
docker compose exec backend pip install pytest pytest-asyncio
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
- CAMEL pin `0.2.80` — rastgele yükseltme
- `requirements.txt` ile crawl4ai aynı `pip install -r` satırında olamaz

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

1. **Kaynak başlığında site menüsü kalabiliyor** ("Facebook Instagram Expand…").
   `_TITLE_CHROME_RE` çerez/consent metnini keser, navigasyon çöpünü kesmez.
2. **"Yapamam / maalesef yerine getiremiyorum" bazen süzgeçten geçip kullanıcıya
   ulaşıyor.** `_looks_like_web_access_refusal` kelime listesine bakıyor; asıl
   sinyal **dosyanın üretilip üretilmediği** olmalı.
3. **İki docx'te "içerik A, biçim B" ayrımı zayıf** — şablon seçimi kırılgan.
4. **Deep anahtarı iş sürerken kilitlenmiyor.**
5. **CI yalnız `main`'e push'ta koşar** (`.github/workflows/backend-tests.yml`).
   Dal üstünde çalışırken testleri elle koşmayı unutma — PR açmadan CI seni
   uyarmaz. Son ölçüm: **652 geçti, 0 kaldı**.

### Yakın geçmişte düzeltilenler (regresyon olursa buraya bak)

- Tablo stili şablonda yoksa Word çıktısının tamamen düşmesi
- PPTX tek slayt kalırken HTML'in dolu gelmesi (PPTX `body` okumalı, `summary` değil)
- Excel'in "Bölüm | İçerik" iskeletine düşmesi (düzyazıdaki gerçek markdown tablo tercih edilir)
- Sohbete stylesheet yazılması (`unwrap_document_source`; Document yine çalışır)
- Başlığın kullanıcının kendi cümlesi olması (`drop_echoed_request`; `**Başlık**` promote edilir)
- Testlerin canlı `/data`'ya sohbet yazması (`conftest.py` izolasyonu)
- CI'ın `langgraph`'ı hiç kurmaması — suite collection'da 4 hatayla düşüyor,
  647 testin hiçbiri çalışmıyordu (workflow'a Dockerfile'daki kurulum adımı eklendi)
- Turn-resolution testlerinin `camel_service`'i patch'lemesi — `AGENT_EXECUTOR=langgraph`
  altında, yani uygulamanın gerçekte çalıştığı ayarda, sahte fonksiyon hiç çağrılmıyordu

---

## 12. Sorun giderme

### Model

| Belirti | Bak |
| --- | --- |
| Hemen hata / boş cevap | `.env` `MODEL_*`, gateway `/v1` ekiyle mi |
| "Task timed out" | Model yavaş veya kuyrukta; timeout 600 s |
| Yarıda kesik rapor | Answer `max_tokens`; LangGraph tavanı bir kez 2× dener |
| JSON intent boş dönüyor | Reasoning modeli bütçeyi düşünmeye yakıyor; token tavanları bilinçli yüksek |

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
- Taranmış PDF / görsel boş geliyor → `DOCLING_ENABLE_OCR=true` mi,
  `docker compose exec backend tesseract --list-langs` içinde `tur` var mı;
  manifest'te `text_empty: true` ise görüntü okunamayacak kadar kötü
- 422 `document_parse_timeout` büyük taramada → `DOCLING_PARSE_TIMEOUT_SECONDS`'ı artır
- Frontend upload listesinde pptx/html yok; bunlar **çıktı** olarak üretilir

### Executor karışıklığı

`AGENT_EXECUTOR` yazılmazsa **camel** çalışır. LangGraph-only kwargs
(`reuses_uploaded_document`, `opens_on_user_computer`) camel fallback'inde
**sessizce düşer** — "niye şablonu görmedi" sorusunun cevabı çoğu zaman budur.

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
   `execution/adapter.py` → `langgraph_runtime/workforce.py` zincirini takip et.
4. `backend/tests/test_web_controller.py` ve `test_document_export.py`'yi koş —
   sistemin gerçekten neyi garanti ettiğini testler anlatır.
5. Bölüm 11'deki 4 açık maddeden birini seç ve düzelt; hepsinin yeri işaret edildi.

Konu bazlı derin referanslar:

| Dosya | Ne zaman bak |
| --- | --- |
| [`docs/langgraph-migration.md`](docs/langgraph-migration.md) | Camel ↔ LangGraph geçiş notları |
| [`docs/web-execution.md`](docs/web-execution.md) | Arama + sayfa açma |
| [`docs/document-ingestion.md`](docs/document-ingestion.md) | Upload / Docling |
| [`docs/document-generation.md`](docs/document-generation.md) | PDF / Word / PowerPoint üretimi |
| [`docs/local-computer.md`](docs/local-computer.md) | "Bilgisayarımda aç" köprüsü |
| [`docs/agent-search.md`](docs/agent-search.md) | AgentSearch sidecar |
| [`docs/backend-reliability-harness.md`](docs/backend-reliability-harness.md) | Deterministik güvenilirlik harness'ı |
