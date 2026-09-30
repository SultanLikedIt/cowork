---
marp: true
theme: default
paginate: true
title: Agent Workspace — Mimari ve Üretimleşme Yol Haritası
description: clean-rebuild branch mimari sunumu
---

<style>
section {
  background-color: #f7f5f1;
  background-image: radial-gradient(circle, rgba(31, 29, 26, 0.08) 1px, transparent 1px);
  background-size: 22px 22px;
  border-top: 8px solid #2f6fed;
  color: #1f1d1a;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 25px;
  line-height: 1.32;
  padding: 52px 64px 46px;
}
h1 {
  color: #1f1d1a;
  font-size: 43px;
  letter-spacing: -0.025em;
  margin-bottom: 28px;
}
h2, h3 {
  color: #2f6fed;
}
strong {
  color: #2459bd;
}
code {
  background: #ffffff;
  border: 1px solid #e7e2d9;
  border-radius: 8px;
  color: #1f1d1a;
}
pre {
  background: #ffffff;
  border: 1px solid #e7e2d9;
  border-radius: 14px;
  box-shadow: 0 2px 10px rgba(31, 29, 26, 0.06);
}
table {
  background: rgba(255, 255, 255, 0.94);
  border-radius: 12px;
  font-size: 20px;
  overflow: hidden;
}
th {
  background: #eaf0fc;
  color: #1f1d1a;
}
blockquote {
  background: rgba(255, 255, 255, 0.92);
  border-left: 6px solid #22a06b;
  border-radius: 0 12px 12px 0;
  color: #1f1d1a;
  padding: 10px 20px;
}
section::after {
  color: #8b857c;
}
</style>

# Agent Workspace

## Mimari, çalışma modeli ve üretimleşme yol haritası

`clean-rebuild` · React + FastAPI + CAMEL-AI

**Ana mesaj:** Güçlü bir agent demo çekirdeğimiz var; üretime geçişin ana işi
execution'ı güvenli, kalıcı ve ölçeklenebilir bir platform katmanına taşımak.

<!--
Konuşmacı notu:
30 saniyelik açılış:
"Ürün kullanıcıya iki çalışma biçimi sunuyor: hızlı işler için tek agent,
karmaşık işler için dinamik bir agent workforce. Bugün önce çalışan mimariyi,
sonra güçlü tarafları, dürüst riskleri ve üretime geçiş planını göstereceğim."
-->

---

# 1. Hangi problemi çözüyoruz?

- Kullanıcı tek bir prompt ile araştırma, doküman üretimi, inceleme ve
  yapılandırma işleri başlatıyor.
- Doküman veya ZIP workspace ekleyebiliyor.
- Basit işte tek agent, karmaşık işte uzman worker havuzu seçebiliyor.
- Planı, çalışan agent'ı ve sonucu canlı izliyor.
- Sonucu Markdown, TXT, Word veya PDF olarak alıyor.

**Ürün değeri:** Tek bir chat cevabı değil, izlenebilir bir iş yürütme deneyimi.

<!--
Not:
Burada teknolojiyle başlamayın. Önce kullanıcı değerini anlatın.
"İzlenebilir" kelimesini event'ler ve tool audit ile bağlayacağız.
-->

---

# 2. Bugünkü sistem tek bakışta

```text
Browser / React
   │  REST + SSE
   ▼
FastAPI
   ├── Upload + isolated workspace
   ├── In-memory job state
   ├── Single Agent ─────────────────┐
   └── CAMEL Workforce               │
         ├── Task Planner            ├── Model Gateway
         ├── Coordinator             │
         ├── Fixed/manual workers    │
         └── Dynamic recovery worker ┘
```

- Frontend: React 18 + Vite
- Backend: FastAPI + Python 3.11
- Framework: CAMEL-AI 0.2.80
- Live state: Server-Sent Events
- Model: OpenAI-compatible endpoint

<!--
Not:
Bu bir mikroservis sistemi değil. Bugün tek backend prosesi içinde çalışan
modüler monolit + async job mimarisi. Bunu saklamadan söyleyin.
-->

---

# 3. Uçtan uca request akışı

1. Kullanıcı prompt ve isteğe bağlı kaynakları gönderir.
2. Backend upload'ları doğrular, metin çıkarır, job workspace'i oluşturur.
3. `POST /run-pipeline` job'ı memory'de yaratır ve hemen `job_id` döner.
4. Single Agent veya Workforce arka planda çalışır.
5. Agent lifecycle event'leri job state'e uygulanır.
6. Her güncelleme SSE ile browser'a tam snapshot olarak gider.
7. Frontend task planını, agent atamalarını, usage'ı ve final output'u gösterir.

```text
Prompt → Validate → Queue in-process → Execute → Events → SSE → Result
```

<!--
Not:
"Queue in-process" ifadesi bilinçli. Durable queue varmış gibi anlatmayın.
Job submit HTTP request'ini bekletmiyor ama execution aynı proses içinde.
-->

---

# 4. Neden iki mod var?

| | Single Agent | Workforce |
|---|---|---|
| Akış | Tek model çağrısı | Planla, ata, çalıştır, kalite kontrol |
| Latency | Düşük | Daha yüksek/değişken |
| Maliyet | Düşük | Daha yüksek |
| Uzmanlaşma | Yok | Var |
| Paralellik | Yok | Bağımsız task'larda mümkün |
| Kullanım | Basit/doğrudan iş | Karmaşık/çok adımlı iş |

**Karar kullanıcıda:** Sistem bugün otomatik mod seçmiyor.

<!--
Not:
"Multi-agent her zaman daha iyi" demeyin. Bu tasarımın güçlü tarafı gereksiz
orchestration maliyetini kullanıcıya zorlamaması.
-->

---

# 5. Workforce nasıl çalışıyor?

```text
Root task
   │
   ▼
Task Planner ──► Dinamik subtasks + olası dependencies
   │
   ▼
Coordinator ──► En uygun worker'a atama
   │
   ▼
Workers ──────► Paralel veya dependency sıralı execution
   │
   ▼
Task Agent ───► Quality score + recovery kararı
```

Recovery seçenekleri: retry, replan, reassign, decompose, create worker.

**Önemli:** Bu sabit dört-stage pipeline değildir; task graph her istek için
dinamik üretilir.

<!--
Not:
Direktör "Dört agent sırayla mı çalışıyor?" diye sorarsa:
"Hayır. Dört sabit uzman bir capability pool. Planner görevleri, Coordinator
atamaları belirliyor; hepsinin her job'da kullanılması garanti değil."
-->

---

# 6. Agent havuzu ve tool sınırları

### Sabit worker'lar

- Research Agent — web search
- Document Agent — toolsuz
- Review Agent — toolsuz
- Outline Agent — toolsuz

### Genişleme

- Manual agent'lar: session boyunca tutulur, sonraki job'lara eklenir.
- Dynamic worker: recovery sırasında CAMEL tarafından yaratılabilir.

### Bugünkü kritik risk

Dynamic worker template'inde local code ve command execution vardır.
Bu üretim güvenlik sınırı değildir.

<!--
Not:
Bu riski gizlemeyin. Güven veren cevap:
"Fixed agent'larda terminal yok. Dynamic recovery template'inde var; pilot
öncesi kaldırmak veya gerçek sandbox'a taşımak P0 maddemiz."
-->

---

# 7. Upload ve workspace güvenliği

### Uygulanan kontroller

- 10 kaynak/job, 20 MiB/dosya, 50 MiB toplam
- ZIP path traversal ve absolute path rejection
- Symlink, encrypted entry ve duplicate path rejection
- File count, per-file ve extracted-total limitleri
- `.git`, `node_modules`, build/cache ve binary içerik ignore
- Her job için ayrı workspace

### Gerçek erişim modeli

Agent prompt'una en fazla 60.000 karakter bounded preview eklenir.
Kodda read-only workspace helper'ları vardır; henüz agente tool olarak bağlı
değildir.

<!--
Not:
"Agent tüm ZIP'i okuyabiliyor" demeyin. Doğru cevap: bounded preview görüyor.
Tool sınıfı hazır ama integration tamamlanmamış.
-->

---

# 8. Canlı durum ve izlenebilirlik

```text
CAMEL event / Single synthetic event
               │
               ▼
          asyncio.Queue
               │
               ▼
        In-memory job snapshot
               │
               ▼
              SSE
               │
               ▼
            React UI
```

Görünen/verilen sinyaller:

- Task created, assigned, started, completed/failed
- Assigned agent
- Model call/token usage
- Job duration ve timing breakdown
- Gerçek search tool args/result/error audit'i

<!--
Not:
Single mode event'leri uygulama üretir; multi mode event'leri gerçek CAMEL
callback'lerinden gelir. Search log'u agent'ın kendi anlatımından bağımsızdır.
-->

---

# 9. Mimari olarak güçlü taraflarımız

1. Model provider'ı agent kodundan ayrılmış durumda.
2. Single/multi seçimi performans ve maliyet kontrolü sağlıyor.
3. UI gerçek lifecycle event'lerini gösteriyor.
4. Frontend task sayısını ve worker sayısını sabit varsaymıyor.
5. Search tool çağrısı ground-truth audit ile doğrulanabiliyor.
6. ZIP/workspace sınırları savunmacı tasarlanmış.
7. Backend bağımlılıkları Python 3.11 için pinli ve Windows offline bundle var.
8. CAMEL erişimi tek adapter modülünde toplanmış.

<!--
Not:
Bu slaytta "production-ready" demeyin. "İyi çekirdek ve doğru seam'ler" deyin.
En değerli seam'ler: model_config, camel_service, callback/event adapter.
-->

---

# 10. Bugünkü üretim boşlukları

| Öncelik | Boşluk | Etki |
|---|---|---|
| P0 | Dynamic local command execution | Prompt injection → OS riski |
| P0 | Auth/authorization yok | Job ve result izolasyonu yok |
| P0 | Harici model/search policy'si | Air-gap ve data residency riski |
| P1 | Job state memory'de | Restart kaybı, replica çalışmaz |
| P1 | Durable queue/concurrency quota yok | Kaynak ve maliyet kontrolsüz |
| P1 | Sync orchestration çağrıları | Event loop bloklanabilir |
| P1 | Test/CI yok | Regression riski |
| P1 | Retention/cleanup yok | Disk ve hassas veri birikir |

**Sonuç:** Demo güçlü; kontrollü pilot için platform sertleştirmesi gerekiyor.

<!--
Not:
Riskleri iş etkisiyle anlatın. Kod detayı sorulursa backup/Q&A'ya geçin.
-->

---

# 11. Air-gap gerçeği

### Hazır olan

- CAMEL 0.2.80 source repoda
- Python 3.11 Windows wheel bundle repoda
- Model adapter internal OpenAI-compatible gateway'e yönlenebilir

### Tamamlanması gereken

- Frontend prebuilt artifact veya offline npm cache
- Internal model endpoint
- DuckDuckGo yerine internal search veya search disabled
- Signed artifact/SBOM/hash doğrulama
- Offline install ve smoke-test runbook

**Doğru ifade:** Air-gap için tasarlanmış backend paketleme temeli var; tüm
çalışma zamanı zinciri henüz air-gap olarak ürünleştirilmiş değil.

<!--
Not:
Bu cümle kritik. Current developer config'in external olabilmesi, code seam'in
çalıştığını gösterir ama air-gap garantisi deployment policy ile gelir.
-->

---

# 12. Hedef üretim mimarisi

```text
User
  │
SSO / API Gateway / Rate Limit
  │
Stateless FastAPI ──────► Postgres (jobs/tasks)
  │                      Object Store (uploads)
  ├──► Durable Queue ───► Isolated Worker Pool
  │                           │
  │                           ├── Internal Model Gateway
  │                           └── Policy Tool Broker
  │                                  ├── Internal Search
  │                                  └── Ephemeral Sandbox
  └──► Event Bus ───────► SSE

Logs + Metrics + Traces + Audit
```

Ana değişiklik: Agent execution API prosesinden ayrılır.

<!--
Not:
Hedef mimariyi "hemen mikroservis yapalım" diye anlatmayın. Amaç execution
izolasyonu, durable state ve policy. Teknoloji isimleri ikincil.
-->

---

# 13. Yol haritası

### Faz 0 — Demo güvence

- Eksik dev dependencies'i kur ve smoke test et
- Dynamic code/command tool'unu kapat
- UI isimleri, README ve demo script'ini hizala
- İki deterministik demo senaryosu hazırla

### Faz 1 — Kontrollü pilot

- SSO/RBAC, Postgres, durable queue, bounded workers
- Internal model/search policy
- Upload TTL/AV scan
- Automated test + CI + timeout/idempotency

### Faz 2 — Üretim

- HA events, sandbox tool broker
- Evaluation/quality gates
- SLO, cost, tracing ve audit
- SBOM/signed artifacts/data retention

<!--
Not:
Direktör süre sorarsa tahmini çerçeve:
Faz 0: 1 gün, Faz 1: 2–4 hafta, Faz 2: 4–8 hafta.
Ekip/altyapı varsayımına bağlı olduğunu ekleyin.
-->

---

# 14. Direktörden beklenen kararlar

1. İlk hedef **demo**, **kontrollü pilot** veya **production** mı?
2. Onaylı model gateway ve data residency sınırı nedir?
3. Web search izinli mi; değilse internal search kaynağı ne olacak?
4. Agent'lara code execution verilecek mi; hangi sandbox standardıyla?
5. Job ve upload retention süresi nedir?
6. Başarı KPI'ı: latency, maliyet, kalite, tamamlanma oranı veya kullanıcı
   zamanı mı?

**Önerimiz:** Önce kontrollü pilot sınırlarını netleştirip Faz 0 güvence
maddelerini kapatmak.

<!--
Not:
Bu slayt sunumu teknik rapordan karar toplantısına çevirir.
-->

---

# Kapanış

> "Doğru agent deneyimi ve doğru teknik seam'ler mevcut. Şimdi bunu
> güvenilir bir platforma çevirmek için state, execution, security ve
> observability katmanlarını üretim standardına taşıyoruz."

### Üç cümlelik özet

- Single Agent hız; Workforce uzmanlaşma ve dinamik planlama sağlar.
- Bugünkü sistem güçlü bir modüler demo, fakat process-local ve policy-light.
- Yol haritası nettir: önce güvenlik, sonra durability/scale, sonra governance.

<!--
Not:
Soruları alın. İlk cevapta mevcut durum ile hedef durumu mutlaka ayırın.
-->

---

# Backup A — API yüzeyi

| Method | Endpoint | Amaç |
|---|---|---|
| POST | `/upload`, `/uploads` | Kaynak yükleme |
| POST | `/run-pipeline` | Job başlatma |
| GET | `/jobs`, `/jobs/{id}` | Hydration/result |
| GET | `/jobs/{id}/events` | SSE |
| POST | `/jobs/{id}/cancel` | Cooperative cancel |
| POST | `/jobs/{id}/export/{fmt}` | DOCX/PDF |
| GET/POST | `/workers` | Agent pool |

---

# Backup B — Doğrulama durumu

### Geçti

- Backend source compile
- FastAPI import/route smoke
- `/jobs` ve `/workers` API smoke
- React production build
- DOCX export
- ZIP validation ve traversal rejection

### Demo öncesi kapatılacak local environment drift

- `ddgs` eksik → Research search çalışmaz
- `reportlab` eksik → PDF export çalışmaz

Kodda ve offline bundle'da pinleri var; virtualenv yeniden kurulmalı.

---

# Backup C — Bilinçli trade-off'lar

| Karar | Kazanç | Bedel |
|---|---|---|
| SSE | Basit server→client canlı akış | Çift yönlü kontrol yok |
| In-memory state | Hızlı demo geliştirme | Restart/scale yok |
| AUTO_DECOMPOSE | Esnek dinamik plan | Latency, cost, determinism |
| OpenAI-compatible adapter | Provider taşınabilirliği | Ortak özellik setiyle sınırlı |
| Prompt preview | Basit ve bounded context | Büyük workspace'te derin erişim yok |
| Vendored CAMEL | Air-gap/audit | Repo boyutu ve upgrade sorumluluğu |
