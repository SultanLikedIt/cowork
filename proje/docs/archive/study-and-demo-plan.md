# Çalışma, Prova ve Demo Planı — `clean-rebuild`

Amaç: Sunumda mimariyi ezberden değil, request akışını izleyerek anlatmak ve
direktörün teknik/ürün/güvenlik sorularına mevcut durum ile hedef durumu
karıştırmadan cevap vermek.

## 1. Bu gece çalışma planı

### Blok 1 — 35 dakika: Büyük resim

Okuma:

1. `README.md`
2. `docs/architecture-presentation.md`
3. `docs/architecture-analysis.md` bölümleri 1–5

Çıktı:

- Sistemi 30 saniyede anlat.
- Single ve Workforce farkını tabloya bakmadan söyle.
- "Sabit pipeline mı?" sorusuna doğru cevap ver.

Kontrol cümlesi:

> "Workforce, dört agent'ın sırayla çalıştığı sabit pipeline değil; Planner'ın
> dinamik task graph ürettiği ve Coordinator'ın capability pool'a atama yaptığı
> AUTO_DECOMPOSE akışıdır."

### Blok 2 — 50 dakika: Backend request akışı

Şu sırayla kodu aç:

1. `backend/main.py:340-496` — API schema ve job state
2. `backend/main.py:499-620` — snapshot ve event reducer
3. `backend/main.py:653-778` — execution ve submit
4. `backend/main.py:780-929` — cancel, export, workers, SSE
5. `backend/camel_service.py:207-308` — Workforce construction
6. `backend/camel_service.py:315-382` — Single Agent

Kendi kendine çiz:

```text
POST /run-pipeline
  → _JOBS + _JobControl
  → asyncio.create_task(_run_job)
  → single: run_single_agent
    veya
    multi: build_workforce + process_task_async
  → event queue
  → _apply_event
  → SSE
```

Bilmen gereken gerçek:

- Ayrı worker process/durable queue yok.
- Job state memory'de.
- Her multi job için yeni Workforce kuruluyor.
- Single event'leri sentetik, multi event'leri CAMEL callback'inden.

### Blok 3 — 45 dakika: CAMEL ve agent mekaniği

Okuma:

1. `backend/camel_service.py:34-123` — fixed pool
2. `backend/camel_service.py:126-204` — manual worker
3. `backend/camel_service.py:207-305` — model/tool wiring
4. `backend/vendor/camel-source/camel/societies/workforce/workforce.py`
   - Constructor/default mode: yaklaşık `166-304`
   - Decomposition: yaklaşık `2167-2245`
   - Assignment/scheduling: yaklaşık `3586-4145`
   - Dynamic worker: yaklaşık `3700-3890`
5. `backend/vendor/camel-source/camel/toolkits/code_execution.py:33-179`

Çıktı:

- Planner, Coordinator, Worker ve Task Agent rollerini anlat.
- Recovery seçeneklerini say.
- Dynamic worker riskini saklamadan anlat.
- Neden sync CAMEL çağrılarının API event loop'u için risk olduğunu açıkla.

### Blok 4 — 40 dakika: Upload, güvenlik ve air-gap

Okuma:

1. `backend/workspace.py:14-230` — limit ve ZIP validation
2. `backend/workspace.py:232-394` — workspace construction
3. `backend/workspace.py:396-544` — prompt preview ve helper'lar
4. `backend/model_config.py`
5. `backend/tool_call_log.py`
6. `backend/requirements.txt`

Ezberlenecek sayılar:

- 10 kaynak/job
- 20 MiB/dosya
- 50 MiB toplam
- 1.000 ZIP entry
- 200 text file
- 1 MiB/file
- 10 MiB extracted total
- 60.000 karakter prompt context
- 8.000 karakter/file preview

Kontrol cümlesi:

> "Workspace tool sınıfı güvenli olarak yazılmış ama current integration'da
> hiçbir agente bağlı değil; bugün agent yalnız bounded preview görüyor."

### Blok 5 — 35 dakika: Frontend ve final-answer davranışı

Okuma:

1. `frontend/src/App.jsx:78-208` — hydration ve SSE
2. `frontend/src/App.jsx:212-450` — submit/upload/cancel
3. `frontend/src/stages.js:103-221` — task ve final answer türetme
4. `frontend/src/components/TaskFlowView.jsx`
5. `frontend/src/components/RightPanel.jsx`

Bilmen gereken:

- SSE her event'te full snapshot taşır.
- Browser refresh, backend ayaktaysa jobs'u hydrate eder.
- UI'daki feedback backend'e gitmez.
- "Final synthesized answer" ayrı bir synthesis agent garantisi değildir.
- Home'daki dekoratif agent isimleri backend pool'uyla tam uyumlu değildir.

### Blok 6 — 45 dakika: Soru-cevap turu

`docs/director-qa.md` dosyasını şu şekilde çalış:

1. Soruyu sesli oku.
2. Cevabı kapat.
3. Önce tek cümle cevap ver.
4. Sonra "mevcut durum → etki → hedef çözüm" sırasıyla genişlet.
5. Takıldığın soruları işaretle ve ilgili kodu tekrar aç.

Öncelikli sorular:

- 11, 14, 18, 24, 32, 41, 43, 44, 49, 51, 53, 57, 58, 64.

## 2. Yarın sabah 45 dakikalık plan

### İlk 15 dakika — Teknik preflight

```bash
git branch --show-current
git status --short
backend/venv/bin/python -m py_compile \
  backend/main.py \
  backend/camel_service.py \
  backend/model_config.py \
  backend/workspace.py
cd frontend && npm run build
```

Beklenti:

- Branch: `clean-rebuild`
- Python compile: exit 0
- Vite build: success

### Sonraki 15 dakika — Demo smoke

Virtualenv'in direct requirements ile aynı olduğunu kontrol et:

```bash
backend/venv/bin/python -c "import ddgs, reportlab; print('demo deps ok')"
```

Sonra backend ve frontend'i ayrı terminallerde başlat:

```bash
cd backend
source venv/bin/activate
python -m uvicorn main:app
```

```bash
cd frontend
npm run dev
```

Kontrol:

- Home açılıyor.
- `/api/workers` dört fixed worker döndürüyor.
- Single demo sonuçlanıyor.
- Workforce demo task planı ve agent ataması gösteriyor.
- PDF export iniyor.
- Search gerekiyorsa tool call sayısı görünüyor.

### Son 15 dakika — Sunum provası

- 30 saniyelik açılış
- 5 dakikalık ana anlatım
- 2 zor soru:
  - "Üretime hazır mı?"
  - "Agent terminal komutu çalıştırabiliyor mu?"
- 20 saniyelik kapanış

## 3. Önerilen 8 dakikalık anlatım akışı

| Süre | Konu | Slayt |
|---:|---|---:|
| 0:00–0:30 | Problem ve ana mesaj | 1 |
| 0:30–1:15 | Ürün değeri | 2 |
| 1:15–2:00 | Sistem resmi | 3 |
| 2:00–2:45 | Request akışı | 4 |
| 2:45–3:30 | Single vs Workforce | 5 |
| 3:30–4:30 | Planner/Coordinator/Workers/Quality | 6–7 |
| 4:30–5:20 | Workspace + live events | 8–9 |
| 5:20–6:10 | Güçlü yanlar | 10 |
| 6:10–7:00 | Riskler ve air-gap | 11–12 |
| 7:00–7:40 | Hedef mimari/yol haritası | 13–14 |
| 7:40–8:00 | Karar talepleri/kapanış | 15 |

## 4. Demo senaryosu

### Senaryo A — Güvenli ana demo: Single Agent

Prompt:

> "Kurumsal bir agent platformu için teknik karar kaydı hazırla. Amaç,
> alternatifler, karar, gerekçe, riskler ve sonraki adımlar başlıklarını kullan."

Göster:

- Single Agent seçimi
- Hızlı submit
- Running → result
- Usage
- Word export

Bu senaryo search'e bağımlı değildir ve demo riskini azaltır.

### Senaryo B — Workforce farkını göster

Prompt:

> "Kurumsal kullanım için multi-agent tabanlı doküman analiz platformu tasarla.
> Gereksinimleri araştırma, çözüm mimarisi, güvenlik incelemesi ve rollout
> planına ayır; sonuçta yönetici özeti üret."

Göster:

- Subtask planning
- Dinamik task listesi
- Assigned agent'lar
- Per-task outputs
- Token/model call farkı

Not: Task Planner'ın hangi subtasks'ı/worker'ları seçeceği deterministik
değildir. "Dört fixed agent'ın tamamı görünecek" vaadi vermeyin.

### Senaryo C — Upload

Küçük bir TXT/CSV kullan:

- 1 MiB altı
- Hassas veri içermeyen
- İçeriği önceden bilinen

PDF/XLSX parser ve dış model egress riskini ana demo akışına zorunlu kılmayın.

## 5. Demo fallback planı

### Model gateway cevap vermezse

Söylenecek:

> "Execution external/internal model gateway bağımlılığı taşıyor. UI ve job
> boundary ayakta; gateway failure için production hedefimiz deadline, circuit
> breaker ve retry policy."

Ardından sunumdaki request flow ve backup validation slaydına geç.

### Search çalışmazsa

- Research search'ü ana demo için zorunlu yapma.
- Single scenario'ya dön.
- Search tool logging mimarisini kod üzerinden göster.

### Workforce uzun sürerse

- Single demo'yu tamamlanmış göster.
- Workforce'u running state, task plan ve assigned agent seviyesinde göster.
- Hazır, önceden çalıştırılmış bir job'a güvenme: job history backend restart'ında
  kaybolur.

### PDF export çalışmazsa

- Word/Markdown export göster.
- `reportlab` dependency smoke'unun preflight'ta zorunlu olduğunu belirt.

## 6. Sunumda kullanılacak dil

### Kullan

- "Bugünkü `clean-rebuild` sürümünde..."
- "Demo çekirdeği..."
- "Controlled pilot için..."
- "Process-local state..."
- "Dynamic task graph..."
- "Cooperative cancellation..."
- "Provider-agnostic OpenAI-compatible adapter..."

### Kullanma

- "Production-ready."
- "Tam air-gapped."
- "Dört agent sırayla çalışıyor."
- "Agent'ların terminal erişimi yok."
- "Async olduğu için otomatik ölçeklenir."
- "Framework güvenliği çözüyor."
- "Multi-agent her zaman daha iyi."

## 7. Bir sayfalık ezber kartı

### Stack

React 18 · Vite 5 · FastAPI · Python 3.11 · CAMEL 0.2.80 · SSE ·
OpenAI-compatible model

### State

Jobs/manual workers: memory  
Uploads/workspaces/tool log: local disk  
Database/queue: yok

### Modes

Single: one `astep`  
Multi: decompose → assign → execute → quality/recovery

### Fixed workers

Research · Document · Review · Outline

### P0

Dynamic command execution · auth · egress/data policy · demo dependencies

### P1

Postgres · durable queue · bounded workers · test/CI · retention · timeout

### En güçlü cevap

> "Mimari seam'ler doğru; üretimleşme işi agent UI'dan çok execution
> platformunu sertleştirmek."

## 8. Tamamlanma checklist'i

- [ ] README yalnız `clean-rebuild` anlatıyor.
- [ ] Mimari analiz yalnız `clean-rebuild` anlatıyor.
- [ ] Sunum yalnız `clean-rebuild` anlatıyor.
- [ ] 30 saniye, 2 dakika ve 8 dakika anlatım prova edildi.
- [ ] `ddgs` ve `reportlab` import smoke geçti.
- [ ] Single Agent demo geçti.
- [ ] Workforce demo en az bir kez tamamlandı.
- [ ] Word/PDF export test edildi.
- [ ] Hassas olmayan demo dosyası hazır.
- [ ] Model/search kesintisi fallback anlatımı hazır.
- [ ] Direktör karar soruları kapanış slaydında.
