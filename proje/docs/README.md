# Docs

Operasyonel referanslar burada. Eski oturum / sunum / analiz notları
[`archive/`](archive/) altında.

Projeyi yeni devraldıysan önce **[`../HANDOVER.md`](../HANDOVER.md)** — kurulum,
mimari, kullanım, test ve bilinen açıkların tamamı orada. Bu klasör onun konu
bazlı derinleştirmesidir.

| Dosya | Ne için |
|---|---|
| [STATUS.md](STATUS.md) | Branch / neredeyiz (kısa handoff) |
| [document-ingestion.md](document-ingestion.md) | Docling upload pipeline + model cache |
| [document-generation.md](document-generation.md) | PDF/DOCX/PPTX üretimi: tipli bloklar, doğrulama, Chromium |
| [web-execution.md](web-execution.md) | SearXNG + AgentSearch + Crawl4AI web path |
| [agent-search.md](agent-search.md) | brcrusoe72/agent-search sidecar |
| [langgraph-migration.md](langgraph-migration.md) | CAMEL ↔ LangGraph geçişi, `AGENT_EXECUTOR` |
| [scheduling.md](scheduling.md) | Zamanlanmış ve izleme görevleri |
| [local-computer.md](local-computer.md) | "Bilgisayarımda aç" host köprüsü |
| [outlook.md](outlook.md) | Outlook ile e-posta: zamanlanmış görev sonuçları |
| [social-reach.md](social-reach.md) | Reddit okuma: kurulum, çerez yenileme, güvenlik |
| [openbot-integration.md](openbot-integration.md) | OpenBot / AG-UI entegrasyonu |
| [backend-reliability-harness.md](backend-reliability-harness.md) | Deterministic reliability harness |

## Archive

| Dosya | Not |
|---|---|
| `NOTES.md` | Deneysel patch geçmişi (kod yorumları buraya işaret eder) |
| `rebuild.md` | Web phase-controller tasarım spec’i (uygulandı) |
| `latest_status.md` | 2026-08-18/19 session handoff |
| `current_status.md` | 2026-08-19 session log (eski `answer_format.py` yoluna atıf yapar) |
| `architecture-*.md` / `.html` | Mimari analiz + sunum |
| `director-qa.md`, `study-and-demo-plan.md` | Demo / Q&A hazırlığı |
