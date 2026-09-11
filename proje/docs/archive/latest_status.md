# latest_status — session handoff (2026-08-18 / 2026-08-19)

Ofise geçerken context kaybı olmasın diye bu dosya. Cursor'a şunu okutabilirsin:
> `latest_status.md` oku, `feature/web-tools-rebuild` branch'indeyiz, kaldığımız yerden devam et.

**Güncelleme:** 2026-08-19 ~06:35 (UTC+3)

---

## Neredeyiz

| Alan | Değer |
|------|--------|
| **Aktif branch** | `feature/web-tools-rebuild` |
| **Dokunma** | `feature/grounded-web-research` (demo branch) |
| **Son commit** | `77e9692` — structured planning + FILES-primary routing |
| **Push durumu** | `77e9692` remote'ta olmayabilir — ofiste `git pull` / `git log origin/feature/web-tools-rebuild` ile doğrula |
| **Referans dokümanlar** | `rebuild.md`, `docs/STATUS.md` (18 Ağustos sabah oturumu) |
| **Venv** | `.venv/bin/python3` |

---

## Kilit mimari kararlar (session boyunca sabit)

1. **Single** = hızlı chatbot: `tools=None`, Coordinator/Workforce/artifact classifier yok.
2. **Multi/Workforce** = job başında artifact-intent classifier + deterministic **web phase controller**.
3. Web controller ReAct içine gömülmez; Research `astep` wrap. Model-facing `web_search`/`web_read` yok.
4. **Document controller rebuild** planlandı, **henüz yapılmadı** — sunum öncesi Excel bind + upload fix tercih edildi.
5. CAMEL FileToolkit kullanılmayacak — Docling + openpyxl + Carbone kalır.
6. **FILES primary vs ambient** (19 Ağustos sabah, `77e9692`):
   - `WEB yok + FILES` → Document-owned FILES-primary stream
   - `WEB + FILES` → FILES ambient
   - Default Research assignment kaldırıldı; web controller task WEB caps'e göre bind

---

# SALI 18 AĞUSTOS — Tüm değişiklikler (kronolojik)

## Sabah / gece yarısı (00:00 – 01:30)

### `a5dd3f1` — Streaming donması + marka rengi
**Sorun:** Her SSE `token_delta` (~4 karakter) React state'i güncelliyordu → O(n²) render, UI donuyordu.

**Değişiklikler:**
- `frontend/src/App.jsx` — `requestAnimationFrame` ile saniyede max ~60 güncelleme
- `frontend/src/index.css`, `tailwind.config.js` — iki tonlu marka aksanı (`--accent-secondary`: mavi→mor)
- `Composer.jsx`, `Sidebar.jsx`, `button.jsx` — accent uygulaması

### `c59ccae` — `web_fetch.py` ölü kod temizliği
**Sorun:** Crawl4AI refactor'ünden (`8842827`) beri eski static-HTTP katmanı hiç çağrılmıyordu (~1700 satır).

**Değişiklikler:**
- `backend/web_fetch.py` — 1091 → ~700 satır; sadece Crawl4AI sonrası tablo/kayıt şekillendirme kaldı
- `backend/test_web_fetch.py` — büyük sadeleştirme
- `backend/test_golden_regressions.py`, `test_execution_reliability.py` — güncellendi
- `backend/test_web_security.py` — yeni

### `07ae5c0` — SearXNG DuckDuckGo fix (Diyanet arama kök nedeni)
**Sorun:** "Diyanet Ankara namaz vakitleri" 7 denemede başarısızdı. Bing alakasız sonuç, duckduckgo config'te yanlışlıkla kapalıydı.

**Değişiklikler:**
- `searxng/settings.yml` — `duckduckgo: disabled: false`
- Canlı doğrulandı: tek denemede doğru URL

### `70b3b18` — `docs/STATUS.md` yeniden yazıldı
Önceki STATUS Scrapy/`web_acquire` dönemine aitti; 18 Ağustos sabah durumu için baştan yazıldı.

---

## Gündüz (09:34 – 14:32)

### `5b2e85b` — Kısmi worker cevapları kaybolmasın
**Sorun:** Worker kısmi gerçekleri bulup `failed=true` dönünce CAMEL retry'a giriyor, kullanıcı hiçbir şey görmüyordu.

**Değişiklikler:**
- `backend/camel_service.py` — `_patched_parse_structured_response`: substantive output varsa `failed=false`
- `backend/web_tools.py` — bütçe/limit iyileştirmeleri
- `backend/test_worker_out_of_turns.py` — **yeni** (Research out-of-turns recovery)
- `backend/test_web_tools.py` — genişletildi
- `frontend/src/App.jsx` — küçük UI düzeltmesi

### `7e513be` — Homepage / alakasız tablo grounded sayılmasın
**Sorun:** Ana sayfa veya yanlış tablo "kanıt" sayılıp cevap üretiliyordu.

**Değişiklikler:**
- `backend/camel_service.py` — structured recovery / handoff sıkılaştırması
- `backend/web_tools.py` — tablo/sayfa kalite sinyalleri
- `backend/test_workforce_structured_recovery.py` — genişletildi

---

## Öğleden sonra (16:46) — Session başlangıcı (evde)

### `71825ea` — DEEP_RESEARCH temizliği + `rebuild.md`
**Bağlam:** Ofisten `rebuild.md` handoff geldi; phase controller henüz kodda yoktu.

**Silinen / temizlenen:**
- `backend/deep_research.py` (646 satır)
- `backend/test_deep_research.py` (629 satır)
- `DEEP_RESEARCH` capability, Research Agent bağı
- `url_discovery` log alias, eski `web_acquire` doküman referansları

**Eklenen:**
- `rebuild.md` — SEARCH → SELECT → FETCH → ANSWER tasarım spec'i
- `docs/web-execution.md` güncellendi

**Kalan:** `detect_deep_research()` — anket cümlesi → `WEB_SEARCH` floor (ayrı deep research yolu değil)

---

## Akşam (18:00 – 21:15) — Web phase controller implementasyonu

### Planlama oturumu (18:00 – 18:17)
- Workforce web işi kalacak (Direct'e çevrilmez)
- `deep_research` commit history ile netleştirildi — zaten kalkmış
- Plan onaylandı: controller yeni modül, ReAct'e gömülmez, demo branch'e dokunulmaz

### Live test bulguları (19:01)
5 canlı test:
1. ✅ Biri iyi çalıştı
2. ❌ "Planning the workflow" 2 dk takıldı (iptal)
3. ❌ Galatasaray golleri — kısmi/boş cevap
4. ❌ 5 makale özeti — tam 5 makale bulunamadı
5. Tablo/grounding sorunları devam ediyor

→ Orchestration/planning ve tablo kalitesi ayrı konular olarak işaretlendi.

### Artifact intent mimari audit (19:13 – 20:03)
Sırayla incelendi, sonra implement edildi (`269bac9` içinde):

| Konu | Karar |
|------|-------|
| Artifact tespiti | Regex primary olmasın → job başında LLM classifier |
| Multilingual | FR/DE/ES + typo (`exsel`) semantik |
| Single mode | Classifier yok, `tools=None` chatbot |
| Multi mode | Classifier bir kez, controller/coordinator tekrar çağırmaz |
| Eski router | `ROUTER_MODE`, `route_request()`, shadow routing silinsin |
| Date grounding | SEARCH/SELECT/ANSWER'a `CURRENT DATE` inject |

### `269bac9` — Ana rebuild commit (21:15)
**En büyük commit.** Web phase controller + routing sadeleştirme + artifact intent + UI download path.

#### Yeni dosyalar
- `backend/web_controller.py` (534 satır) — SEARCH → SELECT → FETCH → ANSWER
- `backend/artifact_intent.py` (193 satır) — `wants_artifact`, `artifact_type` classifier
- `backend/test_web_controller.py` (587 satır)
- `backend/test_artifact_intent.py` (166 satır)

#### Silinen
- `backend/request_routing.py` (456 satır) — `route_request`, `RoutingResult`, `ROUTER_MODE`

#### Web controller davranışı
- SEARCH: model yalnız sorgu; SearXNG arar
- SELECT: toolsuz, 2–3 URL (structured)
- FETCH: kod Crawl4AI; `evidence[]` append-only
- ANSWER: web tool yok; evidence + soru
- Bütçe: max 2 search / 6 fetch; ikinci search yalnız `successful_fetches == 0`
- Literal URL → SEARCH skip
- Research worker: `_web_controller_cfg` + `astep` wrap; model-facing web tool yok
- Assignment matching: controller cfg = web capability (tool dict değil)

#### Artifact intent
- Job başında bir kez: `wants_artifact: bool`, `artifact_type: xlsx|csv|json|docx|pptx|html|code|none`
- Regex yalnız LLM timeout/invalid fallback
- `data_path` tek başına handoff tetiklemez — `wants_artifact` de gerekli
- SEARCH query'ye "excel" sızması engellendi (artifact intent downstream)

#### Routing sadeleştirme
- `backend/capability_routing.py` — büyük sadeleştirme (349 satır silindi)
- UI mode = execution strategy; LLM strategy belirlemez
- Single: Coordinator/Workforce/classifier/controller yok

#### Artifact UI fix
- Workforce xlsx → markdown link yerine **ProducedFiles / modal download**
- `frontend/src/components/ChatExchange.jsx` — artifact attachment render

#### Test suite
- `test_capability_routing.py` — yeniden yazıldı (530→ daha az, yeni mimariye uygun)
- `test_direct_tool_execution.py`, `test_dynamic_worker_tools.py` — güncellendi
- Pytest: **312 passed**, 2 deselected

---

## Gece (21:03 – 23:03)

### Live Excel failure analizi (21:16)
Kullanıcı Excel istedi, trace:
```
web_search → create_json_artifact (ungrounded) → list_artifacts
→ create_text_artifact → create_xlsx_artifact(from_path='null')
```
9 model call, ~10 dk, json/md üretildi, UI "building json" gösterdi.

→ Document tarafı audit tetiklendi; gece yarısı full controller yerine **xlsx-only bind** kararı.

### Document tarafı audit (21:28 – 22:00)
- Document ReAct + 9 read + 11 create tool pad = spagetti
- Upload `[object Object]` hatası (422 detail array)
- Docling + Carbone + custom tools karışık
- CAMEL FileToolkit araştırması → **geçiş önerilmedi**
- **`document_controller.py` planı** çıkarıldı (web analogu) — sunum sonrasına ertelendi
- Sunum kararı: format genişletme değil, **Excel→MD sapmasını yapısal kilitle**

### `a22d2f6` — Excel bind + upload fix (23:03)

**Sorun:** Excel istendiğinde model json/md/text deniyor; `from_path='null'` geçiyordu.

**Değişiklikler:**
- `backend/camel_service.py` — `_document_artifact_include`: xlsx intent'te yalnız `create_xlsx_artifact`
- `backend/artifact_intent.py` — produce vs analyze; `exsel` typo; analyze cümlesi produce'u override etmez
- `backend/artifacts.py` — null/none/empty `from_path` reddi
- `backend/capabilities.py` — artifact tool binding sıkılaştırma
- `frontend/src/api.js` — FastAPI 422 `detail` array → okunabilir string (`[object Object]` fix)
- `frontend/api.test.mjs` — **yeni**
- `backend/test_fixed_worker_tool_contracts.py` — xlsx-only bind testleri
- `backend/test_execution_reliability.py`, `test_artifact_intent.py` — genişletildi

### `81e7777` — Multi-turn context + follow-up (23:03, aynı push)

**Sorun:** Her mesaj yeni job; önceki upload'lar job-scoped kalıyor. "bunu özetle", "az önce yüklediğim dosya" follow-up'ları dosyasız gidiyordu.

**Değişiklikler:**
- `backend/turn_resolution.py` (247 satır) — **yeni** conversation follow-up çözümleme
- `backend/migrations/0006_conversation_files.sql` — conversation file bag (upload'lar thread'e persist)
- `backend/db.py` — file bag CRUD
- `backend/main.py` — pipeline'a turn resolution entegrasyonu; `resolved_request` executor'lara
- Workforce conversation-only follow-up → **Direct execution** (job mode multi kalır, Single'a upgrade yok)
- `backend/test_turn_resolution.py` (171 satır) — **yeni**
- `backend/test_turn_resolution_pipeline.py` (380 satır) — **yeni**

---

## Gece (23:02 – 00:00) — Orchestration audit + uygulama başlangıcı

### Orchestration audit (22:56)
**Sorunlar:**
- Çok hedefli işlerde eksik coverage (3 şehir → 1 research subtask)
- Planning süresi 10s–60s arası değişken (LLM hidden reasoning)
- Assignment coordinator'a düşüyor veya hiç çalışmıyor

**Plan (6 madde, onaylandı):**
1. Structured JSON planner (`workforce_plan.py`)
2. Deterministic contract assignment
3. Quality/synthesis → coverage gerekli
4. Telemetry (`planning_ms`, `assignment_ms`, targets)
5. Decompose entegrasyonu
6. Lifecycle integration test

### Lifecycle bug trace (23:50)
**Semptom:** `subtasks=3`, `targets_planned` dolu → `assignment_ms=0`, `assignment_source=None` → job `4 non-terminal task` ile finalize; Research hiç çalışmıyor.

**Kök neden:** Structured planning `_task_dependencies`'e child ID yazıyordu. CAMEL'de key varlığı = "already assigned" sentinel → `_patched_find_assignee` atlanıyor.

**Fix (ertesi sabah `77e9692`'de commit):**
- `_tasks_from_streams` ve `_repair_initial_plan_coverage`'dan pre-seed kaldırıldı
- Specialist fast-path dual-write dokunulmadı

### Dead code cleanup (gece)
LLM-router test artifaktları silindi:
- fake `_Agent.last_prompt`
- `test_prompt_carries_only_topic_and_file_flag`
- `test_genuinely_independent_work_can_become_workforce`
- obsolete `ROUTING_TIMEOUT_SECONDS` check

---

# ÇARŞAMBA 19 AĞUSTOS — Sabah (~04:30 – 06:35)

## PDF + "bunu özetle" → Research bug

**Audit (04:31):** Uçtan uca trace — routing doğru (`{files}`) ama packing/assignment/controller yanlış.

**Architecture audit (06:02):** FILES primary vs ambient contract önerildi.

**Implement + commit `77e9692` (06:34):**

| Katman | Değişiklik |
|--------|------------|
| `capabilities.py` | `is_files_primary()` |
| `workforce_plan.py` | `needs_web` yalnız job required; FILES-primary stream |
| `camel_service.py` | Assignment, controller bind, repair, completeness, file activity ContextVar |
| Testler | `test_files_primary_routing.py`, lifecycle, plan, assignment contract |

**Test:** 365 passed, 2 deselected

---

## Salı günü commit özeti (hızlı referans)

| Commit | Saat | Özet |
|--------|------|------|
| `a5dd3f1` | 00:04 | Streaming rAF throttle + brand accent |
| `c59ccae` | 00:55 | web_fetch ölü kod temizliği |
| `07ae5c0` | 01:11 | SearXNG duckduckgo fix |
| `70b3b18` | 01:25 | docs/STATUS.md rewrite |
| `5b2e85b` | 09:34 | Partial worker answers + out-of-turns |
| `7e513be` | 14:32 | Homepage/unrelated table grounding |
| `71825ea` | 16:46 | DEEP_RESEARCH silindi + rebuild.md |
| `269bac9` | 21:15 | **Web controller + artifact intent + routing rebuild** |
| `a22d2f6` | 23:03 | Excel-only bind + upload error fix |
| `81e7777` | 23:03 | Turn resolution + conversation file bag |
| `77e9692` | 06:34* | Structured planning + FILES-primary (*19 Ağustos sabah) |

---

## Execution path özeti (güncel)

### Single
```
POST run-pipeline (mode=single) → run_single_agent(tools=None) → doğrudan cevap
```

### Multi / Workforce
```
POST run-pipeline (mode=multi)
  → artifact_intent classify (bir kez)
  → turn_resolution (follow-up varsa)
  → build_workforce
  → workforce_plan.plan_request (25s cap, JSON)
  → expand_plan_for_coverage (FILES-primary / WEB / ARTIFACT)
  → _patched_decompose_task
  → _patched_find_assignee (contract; coordinator yalnız overlap/unknown)
  → Research: web_controller astep (yalnız WEB task'larda)
  → Document: file tools (+ artifact if wants_artifact)
  → synthesize → ProducedFiles UI
```

---

## Bilinen açık konular

### Live doğrulanacak
- [ ] PDF + "bunu özetle" → Document + file tool activity
- [ ] WEB+FILES karşılaştırma → Research + FILES ambient
- [ ] Multi-target (3 şehir / 4 takım) lifecycle sonrası assignment

### Planlanmış, yapılmadı
- [ ] `document_controller.py` (web analogu)
- [ ] JSON/tablonun chat'e sızması (handoff leak)
- [ ] Upload tüm formatlar live doğrulama

### Bilerek ertelenen
- Document format genişletme, CAMEL FileToolkit, camel_service full split

---

## Demo notları

**Güvenilir:** Web→Excel, PDF/XLSX özet (FILES-primary), namaz vakitleri URL→tablo→Excel

**Dikkat:** Word/PPT canlı risk; planning 10–60s değişken (25s cap var)

```bash
docker compose up -d --build
docker compose up -d --pull always --force-recreate searxng
# http://localhost:8080
```

---

## CAMEL assignment contract

- `_task_dependencies` key varlığı = assigned sentinel → planning'de pre-seed etme
- `_assignees` yalnız `_post_ready_tasks` / `_patched_find_assignee` yazsın
- Specialist fast-path kasıtlı pre-seed: `camel_service.py` ~4726–4732

---

## Ofiste devam

```bash
git checkout feature/web-tools-rebuild
git pull
git log -3 --oneline
cat latest_status.md
.venv/bin/python3 -m pytest backend/test_*.py -q \
  --deselect backend/test_decomposition_policy.py::test_live_decomposition \
  --deselect backend/test_dynamic_worker_tools.py::test_task_based_tool_selection
```

`latest_status.md` henüz commit edilmedi — istersen ekle.
