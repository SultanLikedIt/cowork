# Durum

Kısa handoff. **Tam devir teslim rehberi: [`../HANDOVER.md`](../HANDOVER.md)** —
kurulum, mimari, kullanım, test ve güncel açık listesi orada.
Eski oturum notları: [`archive/latest_status.md`](archive/latest_status.md),
[`archive/current_status.md`](archive/current_status.md),
tasarım geçmişi: [`archive/rebuild.md`](archive/rebuild.md), [`archive/NOTES.md`](archive/NOTES.md).

| Alan | Değer |
|---|---|
| Repo | `omeryavuzarslan/agent-webapp-langgraph` (güncel). Köken: `bsaltalii/agent-webapp`, 19.08.2026 |
| Branch | **`main`** — varsayılan ve güncel dal. `claudefix/pptx-chat-reformat` aynı commit'te; `dev/omer-experiment` 63 commit geride |
| Executor | `AGENT_EXECUTOR=langgraph` (kod default'u `camel` — env'de yazılmalı) |
| Portlar | Arayüz 8081, API 8001, AgentSearch 3939 (container içi backend 8000) |
| Modlar | Single = tools-free chat; Workforce = artifact intent + web phase controller |
| Web | SEARCH → SELECT → FETCH → ANSWER (`web_controller.py`); model-facing `web_search`/`web_read` Research'te yok |
| Artifact | Canonical kaynak: artifact-intent LLM → `apply_artifact_intent` → effective capabilities |
| Upload | Docling; modeller `backend/vendor/docling-models` (veya `DOCLING_ARTIFACTS_PATH`) |
| Reranker | SELECT = BGE `bge-reranker-v2-m3` (in-process CPU, `reranker.py`); ağırlıklar `backend/vendor/bge-reranker-v2-m3` (veya `RERANKER_MODEL_PATH`); yoksa LLM SELECT |

## Son tamamlanan işler

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
