# agent-webapp

Tarayıcıdan konuştuğun bir **ajan uygulaması**. Soru sorarsın; sistem ya düz
sohbet eder, ya web'den araştırır, ya yüklediğin belgeyi okur, ya da
**Word / PDF / PowerPoint / Excel / HTML** dosyası üretip (istersen) bilgisayarında
açar.

> 🚀 **Hemen çalıştırmak istiyorsan → [BASLANGIC.md](BASLANGIC.md).**
> Sıfırdan kurulum, günlük kullanım, açılabilir özellikler, sorun giderme.
>
> 📘 **Projeyi devralıyor ve koda gireceksen → [HANDOVER.md](HANDOVER.md).**
> Mimari, kod haritası, test ve bilinen açıkların tamamı orada.

> ### Hangi dal güncel
>
> **`main`** — reponun varsayılan dalı, klonlayınca doğrudan üzerine düşersin.
> `claudefix/pptx-chat-reformat` geliştirmenin yapıldığı dal ve `main` ile aynı
> commit'te; ikisi arasında fark yok.
>
> Repoda `dev/omer-experiment` diye eski bir dal da var — **63 commit geride**,
> kullanma.
>
> `main`, projenin alındığı orijinal repodan (`bsaltalii/agent-webapp`,
> 19 Ağustos 2026) **84 commit öndedir ve hiçbir commit geride değildir**.
> Tarihçenin tamamı: [HANDOVER.md § 1](HANDOVER.md#1-proje-nedir).

---

## Projenin geçmişi — CAMEL'den LangGraph'a

Proje **19 Ağustos 2026**'da `bsaltalii/agent-webapp` reposundan alındı. O
tarihte sistem tamamen **CAMEL-AI** (`camel-ai==0.2.80`) üzerine kuruluydu.

**21 Ağustos 2026**'da LangGraph'a geçiş başladı ve geçiş **bilerek çift yollu**
yapıldı: eski CAMEL kodu silinmedi, LangGraph yolu onun yanına kondu.
`backend/execution/adapter.py` (41 satır) hangisinin çalışacağına karar verir:

```env
AGENT_EXECUTOR=langgraph   # guncel yol -- teslimde bunu kullan
AGENT_EXECUTOR=camel       # eski CAMEL yolu, hala calisir
```

`backend/camel_service.py` (4.700+ satır) hâlâ repoda duruyor. İstersen tek
satırlık env değişikliğiyle eski davranışa dönebilirsin — uygulamanın geri
kalanı `camel_service`'i doğrudan çağırmaz, hep adapter üzerinden geçer.

> ⚠️ **Kod default'u `camel`'dir**, `langgraph` değil — klasör adı ne olursa
> olsun. `.env`'ye yazmazsan eski yol çalışır ve LangGraph'a özel özellikler
> (yüklenen belgeyi şablon olarak kullanma, dosyayı bilgisayarda açma)
> **sessizce kaybolur**; hata vermez, sadece çalışmaz.

---

## Ne var kutuda

| Katman | Teknoloji |
| --- | --- |
| Arayüz | React 18 + Vite + Tailwind |
| Sunucu | FastAPI + Python 3.11 |
| Ajan çalıştırıcı | **LangGraph** (kod default'u: CAMEL 0.2.80) — `AGENT_EXECUTOR` ile seçilir |
| Model | OpenAI uyumlu `/v1/chat/completions` (OpenRouter, Ollama, vLLM, şirket gateway'i…) |
| Web araştırma | SearXNG + AgentSearch sidecar + Crawl4AI/Playwright Chromium |
| Belge okuma | Docling (PDF, DOCX, PPTX, XLSX, HTML, görsel) + PaddleOCR-VL-1.6 OCR (taranmış PDF sayfası / görsel, GPU; yoksa Tesseract yedeği) + CSV / TXT / ZIP |
| Belge üretme | Tipli bloklar → Chromium HTML/CSS (PDF), python-docx (DOCX), python-pptx (PPTX), openpyxl (XLSX), HTML deck; reportlab yedek |
| Zamanlama | Cron / tek seferlik / zincir görevler, değişiklik izleme |
| E-posta | Zamanlanmış sonuçlar klasik Outlook masaüstünden (opsiyonel, Windows) |
| Canlı durum | Server-Sent Events |
| Kalıcılık | SQLite + dosya sistemi |

**Kimlik doğrulama yok.** Bu yığını internete açma — iç ağ / localhost içindir.

---

## İki mod, kullanıcı seçer

Sunucu tahmin etmez; composer'daki anahtar belirler.

| | **Single Agent** | **Workforce (Multi)** |
| --- | --- | --- |
| Ne yapar | Tek sohbet çağrısı | Research + Document + Coding + Assistant işçileri |
| Web | ❌ | ✅ SEARCH → SELECT → FETCH → ANSWER |
| Dosya üretir | ❌ | ✅ docx / pdf / pptx / xlsx / html |
| Yüklenen belge | ❌ | ✅ okur, şablon olarak doldurur |
| Ne zaman | "Python decorator nedir?", hızlı sohbet | "araştır", "pdf ver", "sunum hazırla", "bunu doldur" |

Workforce'ta ayrıca **Deep research** (daha çok açı, daha uzun) ve **Skills**
(nasıl düşünsün: kök neden, risk analizi…) açılabilir.

---

## Hızlı başlangıç (Docker)

Gerekenler: Git, Docker Desktop (Windows'ta WSL2), OpenAI uyumlu bir model
endpoint'i. İlk build **15–30 dk** ve **~6–8 GB** disk ister.

```powershell
git clone https://github.com/omeryavuzarslan/agent-webapp-langgraph.git
cd agent-webapp-langgraph
git submodule update --init --recursive

copy .env.example .env
notepad .env          # MODEL_BASE_URL / MODEL_NAME / MODEL_API_KEY doldur

docker compose up --build
```

> Proje sana **klasör olarak** verildiyse (GitHub erişimin yok veya gerekmiyorsa)
> ilk üç satırı atla — dosyalar zaten elinde, `vendor/agent-search` dahil.
> Doğrudan `.env` adımından başla. Ayrıntı:
> [HANDOVER.md § 2.2](HANDOVER.md#22-kaynağı-al).

| Servis | Adres |
| --- | --- |
| Arayüz | http://127.0.0.1:8081 |
| API + Swagger | http://127.0.0.1:8001/docs |
| AgentSearch | http://127.0.0.1:3939 |
| SearXNG | yayınlanmaz (Docker ağı içi) |

> Host portları **bilerek** 8081/8001 — eski CAMEL yığını (8080/8000) ile yan yana
> çalışabilsin diye. Container içinde backend hâlâ 8000 dinler.

İlk deneme: workspace oluştur → Single ile "2+2 kaç" → Workforce'a geç →
"İstanbul hakkında kısa bir PDF hazırla".

Durdurmak: `docker compose down` (veri kalır) / `docker compose down -v` (her şey silinir).

> ⚠️ Kaynak kod host'tan mount **edilmez**. Backend'de `.py` değiştirdiysen tek
> yol `docker compose up -d --build backend` — ve rebuild o an çalışan job'ları
> öldürür.

---

## Ortam değişkenleri

Zorunlu üçlü:

```env
MODEL_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=deepseek/deepseek-v4-flash
MODEL_API_KEY=...
```

Sık kullanılanlar:

```env
AGENT_EXECUTOR=langgraph        # boşsa camel'e düşer
OUTLOOK_ENABLED=false           # zamanlanmış sonuçları Outlook'tan e-postala
AGENT_SEARCH_ENABLED=true
LOCAL_COMPUTER_ENABLED=false    # "bilgisayarımda aç" köprüsü
DOCLING_ENABLE_OCR=true         # PDF + görsel okuma (PaddleOCR-VL, yoksa Tesseract)
COMPOSE_PROFILES=paddleocr      # PaddleOCR-VL servisleri (NVIDIA GPU ister)
JIRA_ENABLED=false              # şirket Jira'sını okuma (salt okunur), docs/jira.md
GITHUB_TOKEN=                   # GitHub okuma varsayılan açık; token sınırı 60→5000/saat yapar, docs/github.md
```

Tam liste ve açıklamalar: [`.env.example`](.env.example) ve
[HANDOVER.md § 3](HANDOVER.md#3-ortam-değişkenleri-env).

> Compose **repo kökündeki** `.env`'yi okur. Host'ta doğrudan uvicorn
> çalıştırırsan `model_config.py` **`backend/.env`**'yi yükler. İkisi farklı dosya.

---

## Mimari — bir istek uçtan uca

```text
Tarayıcı (React)
    REST  /upload  /run-pipeline  /jobs  /workspaces  /workers  /skills
    SSE   /jobs/{id}/events
        │
        ▼
FastAPI (backend/main.py) ── SQLite (DATA_DIR/app.db)
    upload → Docling → job workspace (sources/ + artifacts/)
        │
        ├─ mode=single → execution.adapter.run_single_agent
        │
        └─ mode=multi  → artifact_intent + turn_resolution
                            → execution.adapter.run_workforce_in_thread
                                Research / Document / Coding / Assistant
                                web_controller: SEARCH → SELECT → FETCH → ANSWER
                                document_model / document_html / openpyxl
                                (opsiyonel) local_computer aç
```

Öne çıkan tasarım kararları:

- **Niyet keyword ile değil LLM ile belirlenir.** `artifact_intent.py` job başına
  bir structured-output çağrısı yapar: dosya isteniyor mu, tipi ne, web gerekiyor
  mu, yüklenen belge şablon mu, bilgisayarda açılacak mı. Regex yalnız yedektir.
- **Web araştırması sabit fazlıdır.** Model "ara" tool'unu keyfî çağırmaz;
  `web_controller.py` planla → ara → seç → aç → cevapla döngüsünü yürütür.
- **Takip turları çözümlenir.** "bunu pdf ver", "ilk üçü kim" cümleleri
  `turn_resolution.py` ile bağımsız isteğe dönüşür.
- **Word ve HTML aynı blok ayrıştırıcıyı kullanır** (`document_export.iter_body_blocks`)
  — formatlar arası içerik farkı olmasın diye.

Ayrıntı: [HANDOVER.md § 5](HANDOVER.md#5-mimari--bir-istek-uçtan-uca).

---

## Depo yapısı

```text
agent-webapp-langgraph/
├── HANDOVER.md             # tam devir teslim rehberi ← buradan başla
├── .env.example            # ortam değişkeni şablonu
├── docker-compose.yml      # frontend + backend + searxng + agent-search
├── backend/                # FastAPI, ajanlar, web, belge üretimi, testler
├── frontend/               # React SPA
├── searxng/                # arama motoru ayarı
├── vendor/agent-search/    # git submodule (sidecar)
├── scripts/                # local_computer_bridge.py
└── docs/                   # konu bazlı operasyonel referanslar
```

Dosya dosya harita: [HANDOVER.md § 6](HANDOVER.md#6-kod-haritası).

---

## API

Tarayıcı `/api` öneki ekler; backend kökü dinler.

| Metod | Yol | Ne |
| --- | --- | --- |
| POST | `/upload` | Dosya yükle (max 10 dosya, 20 MB/dosya, 50 MB toplam) |
| POST | `/run-pipeline` | Job başlat — `mode`: `single` \| `multi` |
| GET | `/jobs/{id}` | Durum + sonuç + usage |
| GET | `/jobs/{id}/events` | SSE canlı ilerleme |
| POST | `/jobs/{id}/cancel` | İptal |
| GET | `/jobs/{id}/artifacts/{path}` | Üretilen dosyayı indir |
| POST | `/jobs/{id}/export/{fmt}` | Cevabı md/txt/docx/pdf yap |
| POST | `/execute` | Kod bloğunu çalıştır |
| GET/POST | `/workers` | İşçi havuzu + manuel agent |
| GET/POST/PATCH/DELETE | `/workspaces` | Workspace |
| GET | `/skills` | Skill kataloğu |

Canlı şema: http://127.0.0.1:8001/docs

---

## Test

```powershell
# Backend (container içinde — pytest image'da yok, her --build sonrası kurulur)
docker compose exec backend pip install pytest pytest-asyncio
docker compose exec backend python -m pytest tests -q

# Frontend
cd frontend
npm test
```

Testler `conftest.py` sayesinde geçici bir `DATA_DIR` kullanır — canlı sohbet
geçmişini kirletmez. Ayrıntı: [HANDOVER.md § 10](HANDOVER.md#10-test-ve-geliştirme-döngüsü).

---

## Docker'sız geliştirme

Backend host Python'da (uvicorn :8000), frontend Vite'de (:5173). `requirements.txt`
tek geçişte kurulmaz — crawl4ai ile camel-ai arasında `psutil` pin çatışması var;
adım adım kurulum sırası [HANDOVER.md § 9](HANDOVER.md#9-dockersız-yerel-geliştirme)'da.

---

## Bilinen sınırlar

- Auth, çoklu kullanıcı, rate limit, upload temizliği — **yok**
- Çalışan job restart'ta `interrupted` olur, devam ettirilemez
- `run_python` sınırlıdır ama gerçek sandbox değildir
- Kaynak başlıklarında bazen site navigasyonu kalabiliyor
- Deep anahtarı iş sürerken kilitlenmiyor

Güncel açık listesi ve nerede düzeltileceği:
[HANDOVER.md § 11](HANDOVER.md#11-sınırlar-ve-bilinen-açıklar).

---

## Belgeler

| Dosya | Ne için |
| --- | --- |
| [BASLANGIC.md](BASLANGIC.md) | **Başlangıç** — kurulum, günlük kullanım, özellikleri açma, sorun giderme |
| [HANDOVER.md](HANDOVER.md) | **Tam rehber** — kurulum, mimari, kullanım, test, açıklar |
| [docs/langgraph-migration.md](docs/langgraph-migration.md) | CAMEL ↔ LangGraph geçişi |
| [docs/web-execution.md](docs/web-execution.md) | Arama + sayfa açma |
| [docs/document-ingestion.md](docs/document-ingestion.md) | Docling upload pipeline |
| [docs/document-generation.md](docs/document-generation.md) | PDF/DOCX/PPTX üretimi |
| [docs/local-computer.md](docs/local-computer.md) | "Bilgisayarımda aç" köprüsü |
| [docs/scheduling.md](docs/scheduling.md) | Zamanlanmış görevler, zincirler, e-posta |
| [docs/outlook.md](docs/outlook.md) | Outlook ile e-posta: kurulum, güvenlik, sorun giderme |
| [docs/social-reach.md](docs/social-reach.md) | Reddit okuma: kurulum, haftalık çerez yenileme, güvenlik |
| [docs/jira.md](docs/jira.md) | Jira okuma: Cloud / Server kurulumu, örnek sorular, VPN, güvenlik |
| [docs/github.md](docs/github.md) | GitHub okuma: depo arama, bağlantı okuma, token, sınırlar, güvenlik |
| [docs/knowledge.md](docs/knowledge.md) | Workspace belge arama (Knowledge Agent): nasıl çalışır, ölçümler, sınırlar |
| [docs/coding-agent.md](docs/coding-agent.md) | Coding Agent: kodu çalıştırır, Excel/CSV analiz eder, grafik çizip rapora gömer |
| [docs/agent-search.md](docs/agent-search.md) | AgentSearch sidecar |
| [docs/README.md](docs/README.md) | `docs/` dizin indeksi |
