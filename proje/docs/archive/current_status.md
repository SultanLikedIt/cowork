# Current Status — 19 Ağustos 2026

## Bugün Yapılanlar

### Bug Fixes
- **Conversation navigation kırıktı**: Sidebar'dan ve "Pick up where you left off" bölümünden sohbete tıklanınca sayfa değişmiyordu. Sebep: `setComposerTransitioning` önceki refactor'da kaldırılmış ama `App.jsx`'te iki yerde hâlâ çağrılıyordu → `ReferenceError` handler'ı sessizce öldürüyordu. Kaldırıldı.
- **Code block indentation bozuluyordu**: `_strip_internal_file_links` içindeki `re.sub(r"[ \t]{2,}", " ", cleaned)` regex'i fenced code block'ların içindeki indentation'ı da tek boşluğa çeviriyordu (`    if n` → ` if n`). Code block'lar placeholder ile korunup regex sonrası geri konularak düzeltildi. (`backend/workforce/answer_format.py`)
- **Syntax highlighting renkleri görünmüyordu** (iki ayrı sorun):
  1. `hljs-keyword`, `hljs-string` vb. CSS kuralları `@layer components` içindeydi → Tailwind purge bunları HTML'de statik referans bulamadığı için siliyordu (highlight.js runtime'da `dangerouslySetInnerHTML` ile inject ediyor). Kurallar `@layer` dışına taşındı.
  2. `.code-block-pre code { color: #e6edf3 }` specificity'si (`0,2,1`) `.hljs-keyword` (`0,1,0`)'dan yüksekti → tüm token renkleri eziliyordu. Default renk `.code-line-content`'e taşındı.
- **`highlightAuto` eşiği çok yüksekti**: Fibonacci kodu relevance=5 döndürüyordu ama eşik `>5`'ti → auto-detect hiç çalışmıyordu. `>=3` yapıldı ve fallback olarak python highlight eklendi.

### UI/UX Redesign
- **WorkspacePicker (Landing Page)**: Düz liste → workspace card'ları (icon tile, hover shadow/arrow animasyonu), "New workspace" dashed-border kart, animated gradient glow arka plan (CSS-only), feature chip'leri (Multi-agent, Code execution, Persistent threads), recent conversations bordered container.
- **RightPanel (Execution Panel)**: Düz task list → bordered task card'ları (state'e göre renk: running=mavi, failed=kırmızı), expand/collapse butonu (uzun açıklamalar kesilmiyor artık), agent card'ları (icon tile, status badge, task count göstergesi), tool activity card'ları.
- **HomeView**: Inline agent text → agent roster card'ları (icon tile + description), starter chip'ler hover'da icon scale animasyonu, fade-in-up entrance animasyonu.
- **Composer**: Max-width 43rem → 46rem (ThreadView dahil), send butonu hover scale + active shrink micro-animasyonu.
- **Genel**: Fade-in-up animasyonu view geçişlerinde, workspace picker animated gradient background.

### CI/CD
- **GitHub Actions**: `playwright install --with-deps chromium` adımı kaldırıldı. Hiçbir test gerçek browser başlatmıyor — tüm crawl4ai testleri fake engine kullanıyor. crawl4ai/playwright paketleri hâlâ pip ile kuruluyor (import chain için gerekli).

## Mevcut Mimari Durumu

### Fixed Workers (3 adet)
- **Research Agent**: Web search, kaynak sentezi
- **Document Agent**: Analiz, özetleme, doküman oluşturma
- **Coding Agent**: Kod yazma, debug, CI/CD, code execution (CAMEL CodeExecutionToolkit)

### Dynamic Worker
- Coordinator gerekli gördüğünde runtime'da yeni worker oluşturabiliyor (fallback korunuyor)

### Routing
- `artifact_intent.classify_artifact_intent` → `needs_code_execution` alanı eklendi
- `_role_for_subtask`: code-only task → "Coding Agent"'a yönleniyor
- Domain guard: Research ve Document Agent'ta var, Coding Agent'ta yok

### Code Execution
- Backend: CAMEL `CodeExecutionToolkit` (`execute_code`, `execute_command`)
- Frontend `/execute` endpoint: `code_exec.CodeExecutionTools` (sandboxed)
- Frontend: highlight.js syntax highlighting, line numbers, Run/Copy butonları, error line highlighting

### Streaming
- SSE via `EventSource` + RAF coalescing (mevcut ama kullanıcı düzgün çalışmadığını bildirdi — henüz investigate edilmedi)

## Yapılması Planlanan / Bilinen Sorunlar

1. **Streaming response düzgün çalışmıyor** — Kullanıcı bildirdi, henüz root cause araştırılmadı. SSE bağlantısı var ama davranış net değil.
2. **Error recovery** — Network hatası veya backend timeout durumunda retry butonu yok.
3. **Conversation persistence** — Sayfa yenilendiğinde mevcut sohbet URL routing ile korunuyor ama localStorage/DB persistence yok.
4. **Mobile responsive** — Layout uyumu kontrol edilmedi.
5. **Code execution dil desteği** — Şu anda sadece Python, JavaScript/bash eklenebilir.

## Branch Durumu
- `main` ve `feature/optimistic-chat-submit` aynı commit'te (`5387c5c`) — fast-forward merge yapıldı
- Feature branch silinmedi, remote'da duruyor

## Test Durumu
- Backend: 364 test passing
- Frontend: render.test.mjs + strategy.test.mjs tümü passing
- CI: `python -m pytest -q` (Chromium install olmadan)

## Önemli Dosyalar (Sık Değişen)
- `backend/camel_service.py` — Worker tanımları, routing, system prompt'lar
- `backend/capabilities.py` — Capability detection, tool binding
- `backend/workforce/answer_format.py` — Cevap formatlama, code block koruması
- `backend/artifact_intent.py` — LLM intent classification
- `backend/main.py` — FastAPI endpoints, /execute endpoint
- `frontend/src/App.jsx` — Global state, routing, conversation flow
- `frontend/src/components/ChatExchange.jsx` — CodeBlock, syntax highlighting
- `frontend/src/components/WorkspacePicker.jsx` — Landing page
- `frontend/src/components/RightPanel.jsx` — Execution panel
- `frontend/src/components/HomeView.jsx` — In-workspace home
- `frontend/src/index.css` — Tailwind + custom styles, hljs token colors
