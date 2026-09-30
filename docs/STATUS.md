# Durum

Kısa handoff. **Tam devir teslim rehberi: [`../HANDOVER.md`](../HANDOVER.md)** —
kurulum, mimari, kullanım, test ve güncel açık listesi orada.
Eski oturum notları: [`archive/latest_status.md`](archive/latest_status.md),
[`archive/current_status.md`](archive/current_status.md),
tasarım geçmişi: [`archive/rebuild.md`](archive/rebuild.md), [`archive/NOTES.md`](archive/NOTES.md).

| Alan | Değer |
|---|---|
| Repo | `SultanLikedIt/cowork` (güncel). Devralınan kod: 11.09.2026 (`0836338`); köken `bsaltalii/agent-webapp` |
| Branch | **`main`**. Özellik işleri kendi dalında + PR ile gelir. Açık: PR #22 Jira / Confluence onaylı yazma (`confluence-write`) |
| Executor | LangGraph (`execution/langgraph_runtime/`); CAMEL 24.09.2026'da kaldırıldı |
| Portlar | Arayüz 8081, API 8001, AgentSearch 3939 (container içi backend 8000) |
| Modlar | Single = tools-free chat; Workforce = artifact intent + web phase controller |
| Web | SEARCH → SELECT → FETCH → ANSWER (`web_controller.py`); model-facing `web_search`/`web_read` Research'te yok |
| Artifact | Canonical kaynak: artifact-intent LLM → `apply_artifact_intent` → effective capabilities |
| Upload | Docling; modeller `backend/vendor/docling-models` (veya `DOCLING_ARTIFACTS_PATH`) |
| Reranker | SELECT = BGE `bge-reranker-v2-m3` (in-process CPU, `reranker.py`); ağırlıklar `backend/vendor/bge-reranker-v2-m3` (veya `RERANKER_MODEL_PATH`); yoksa LLM SELECT |

## Son tamamlanan işler

- **Jira / Confluence onaylı yazma (PR #22, 28.09.2026):** Jira'da kayıt aç,
  yorum ekle, durum değiştir; Confluence'ta yeni sayfa ve sayfaya yorum. Ajan
  önerir, cevabın altındaki onay kartında **Onayla** ile yazılır
  (`jira_actions.py`, `confluence_actions.py`, ortak `integration_actions.py`,
  migration `0013`, `ApprovalCard.jsx`). Varsayılan kapalı:
  `JIRA_WRITE_ENABLED`, `CONFLUENCE_WRITE_ENABLED`. Ortak niyet
  sınıflandırıcısına dokunulmadı. Ayrıntı: [jira.md](jira.md#yazma-onaylı),
  [confluence.md](confluence.md#yazma-onaylı)
- **CAMEL kaldırıldı (24.09.2026):** `camel_service.py` (4.7k satır), CAMEL
  Single yolu, `AGENT_EXECUTOR` anahtarı, `camel-ai` bağımlılığı ve vendor'daki
  CAMEL kaynağı (2141 dosya) silindi. Niyet, takip turu ve sohbet özeti
  çağrıları `openai_chat.complete_chat` üzerinden gider. Manuel agent
  (arayüzde "Add agent") kaldırıldı; LangGraph'ta zaten hiçbir işe
  atanmıyordu. Backend imajı yeniden build edilmeli (Dockerfile artık
  bağımlılıkları PyPI'den kuruyor).
- **Coding Agent düzeltmeleri (24.09.2026):** Coding işi sürerken arayüzün
  boş kalması (#14); hesaplama isteklerinin (faiz tablosu, toplam,
  istatistik) Coding'e yönlenmesi (#15); sınıflandırıcı zaman aşımında bir
  kez yeniden deneme ve yedek kuralların hesaplamayı tanıması (#17).
- **Geliştirme özeti:** [gelistirme-ozeti.md](gelistirme-ozeti.md) —
  devralmadan bugüne, sunum hazırlatmak için.
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
- Outlook takvim **henüz yok** (e-posta taslağı ve gelen kutusu okuma var:
  [outlook.md](outlook.md))
- Jira / Confluence yazma açıkken, giriş olmadığı ve portlar tüm ağ
  arayüzlerinde açık olduğu için aynı ağdaki biri de değişiklik onaylayabilir
  (HANDOVER § 11, madde 6)
- Kaynak başlığında site navigasyonu kalabiliyor (`_TITLE_CHROME_RE` yalnız
  consent/cookie metnini keser)
- `_looks_like_web_access_refusal` kelime listesine bakıyor; "Maalesef bu isteğin
  her iki kısmını da yerine getiremiyorum" süzgeçten geçiyor — asıl sinyal
  dosyanın üretilip üretilmediği olmalı
- İki docx'te "içerik A, biçim B" ayrımı kırılgan
- Deep anahtarı iş sürerken kilitlenmiyor
- Docling model cache yoksa upload 422 (`docling_conversion_failed`)
- `main.py` (~3.8k satır) ve `langgraph_runtime/workforce.py` (~6.7k satır)
  büyük; bölünmeleri sıradaki iş

## Test

```bash
# Container içinde (pytest image'da yok, her --build sonrası kurulur)
docker compose exec backend pip install pytest pytest-asyncio pypdf
docker compose exec backend python -m pytest tests -q

# Host venv ile
cd backend
python -m pytest tests -q
```

Son ölçüm (28.09.2026, PR #22 dalında): **1546 geçti, 0 kaldı** (container içinde).

CI (`.github/workflows/backend-tests.yml`) yalnız `main`'e push'ta koşar.
