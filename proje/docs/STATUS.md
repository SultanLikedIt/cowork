# Durum

Kısa handoff. **Tam devir teslim rehberi: [`../HANDOVER.md`](../HANDOVER.md)** —
kurulum, mimari, kullanım, test ve güncel açık listesi orada.
Eski oturum notları: [`archive/latest_status.md`](archive/latest_status.md),
[`archive/current_status.md`](archive/current_status.md),
tasarım geçmişi: [`archive/rebuild.md`](archive/rebuild.md), [`archive/NOTES.md`](archive/NOTES.md).

| Alan | Değer |
|---|---|
| Repo | `SultanLikedIt/cowork` (güncel). Devralınan kod: 11.09.2026 (`0836338`); köken `bsaltalii/agent-webapp` |
| Branch | **`main`** — son birleşen: PR #13 Coding Agent (24.09.2026). Özellik işleri kendi dalında + PR ile gelir |
| Executor | `AGENT_EXECUTOR=langgraph` (kod default'u `camel` — env'de yazılmalı) |
| Portlar | Arayüz 8081, API 8001, AgentSearch 3939 (container içi backend 8000) |
| Modlar | Single = tools-free chat; Workforce = artifact intent + web phase controller |
| Web | SEARCH → SELECT → FETCH → ANSWER (`web_controller.py`); model-facing `web_search`/`web_read` Research'te yok |
| Artifact | Canonical kaynak: artifact-intent LLM → `apply_artifact_intent` → effective capabilities |
| Upload | Docling; modeller `backend/vendor/docling-models` (veya `DOCLING_ARTIFACTS_PATH`) |
| Reranker | SELECT = BGE `bge-reranker-v2-m3` (in-process CPU, `reranker.py`); ağırlıklar `backend/vendor/bge-reranker-v2-m3` (veya `RERANKER_MODEL_PATH`); yoksa LLM SELECT |

## Son tamamlanan işler

- **Coding Agent (PR #13, 23–24.09.2026):** kodu gerçekten çalıştırır
  (yaz → çalıştır → düzelt → gerçek çıktıyı anlat), yüklenen Excel/CSV'yi
  analiz eder, matplotlib grafiklerini sohbette gösterir ve PDF/Word'e gömer.
  Grafikler için backend imajı yeniden build edilmeli. Ayrıntı:
  [coding-agent.md](coding-agent.md)
- Bundan önce main'e girenler: Knowledge Agent / belge kütüphanesi (#8, #9),
  PowerPoint tipli slaytlar (#10), uzun yapıştırılan isteği brief sayma (#11),
  yüklenen dosyadan üretilen dosyada kaynak (#12), GitHub (#6), Jira (#5),
  Reddit + Hacker News, Outlook okuma/gönderme, zamanlanmış görevler

- **Outlook e-posta (faz 1, 18.09.2026):** zamanlanmış görev sonuçları klasik
  Outlook masaüstünden e-postayla gider. Köprüde (`scripts/local_computer_bridge.py`)
  token korumalı `/outlook/send`, backend'de `outlook.py` + `scheduling/delivery.py`,
  migration `0010`, görev editöründe **Email** bölümü. Gerçek gönderim bu makinede
  uçtan uca doğrulandı. Ayrıntı: [outlook.md](outlook.md)
- **Zamanlanmış görev zincirleri:** bir görev başka bir görevin çalışmasından
  sonra, koşullu olarak (başarı / değişiklik / her zaman) çalışır; üst görevin
  raporu ve dosyaları aktarılır
- LangGraph executor'a geçiş (`execution/adapter.py` anahtarı, `langgraph_runtime/`)
- Web fazı sertleştirme: `unwrap_document_source` (sohbete yazılan HTML sayfası
  düz yazıya çevrilir, Document yine dosyayı üretir), `drop_echoed_request`
  (cevap kullanıcının kendi cümlesiyle başlamaz)
- Belge çıktısı düzeltmeleri: şablonda tablo stili yoksa Word'ün tamamen
  düşmemesi, PPTX'in `summary` yerine `body` okuması, Excel'in düzyazıdaki
  gerçek markdown tabloyu tercih etmesi
- Stage bazlı süre ölçümü (`model_config.UsageTracker.duration_seconds`)
- Test izolasyonu: `conftest.py` `DATA_DIR`'i geçici dizine **zorlar**
  (eskiden `setdefault` idi ve container'da `/data`'ya yazıyordu)

## Bilinen açıklar

- **Outlook:** yalnız aynı makine ve yalnız **klasik** Outlook (yeni Outlook
  uygulamasında COM yok). Köprü kapalıysa / PC uykudaysa e-posta bekler, ~6 saat
  denenir, sonra `failed`. Gönderim sırasında backend çökerse aynı e-posta tekrar
  gidebilir (en az bir kez teslim)
- Ajanın kendi yazdığı e-posta (taslak olarak Outlook'ta açma), gelen kutusu
  okuma ve takvim **henüz yok** — sonraki fazlar
- Kaynak başlığında site navigasyonu kalabiliyor (`_TITLE_CHROME_RE` yalnız
  consent/cookie metnini keser)
- `_looks_like_web_access_refusal` kelime listesine bakıyor; "Maalesef bu isteğin
  her iki kısmını da yerine getiremiyorum" süzgeçten geçiyor — asıl sinyal
  dosyanın üretilip üretilmediği olmalı
- İki docx'te "içerik A, biçim B" ayrımı kırılgan
- Deep anahtarı iş sürerken kilitlenmiyor
- Artifact intent LLM false positive → Document Agent chat-only isteklerde
  görünebilir (`expand_plan_for_coverage`)
- Docling model cache yoksa upload 422 (`docling_conversion_failed`)
- `camel_service.py` hâlâ büyük; monkeypatch çekirdeği ayrı refactor

## Test

```bash
# Container içinde (pytest image'da yok, her --build sonrası kurulur)
docker compose exec backend pip install pytest pytest-asyncio
docker compose exec backend python -m pytest tests -q

# Host venv ile
cd backend
python -m pytest tests -q
```

Son ölçüm: **652 geçti, 0 kaldı** (container içinde, `AGENT_EXECUTOR=langgraph`).

CI (`.github/workflows/backend-tests.yml`) yalnız `main`'e push'ta koşar.
