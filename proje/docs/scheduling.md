# Zamanlanmış ve izleme görevleri

Kaydedilmiş bir isteği backend'in kendi başına çalıştırması: cron zamanlamasıyla,
tek seferlik bir saatte, ya da yalnız bir şey değiştiğinde öne çıkan bir izleme
olarak. Arayüzde sol menüde **Scheduled** (`/w/:workspaceId/scheduled`).

Dış servis entegrasyonu (e-posta, Teams, Slack) yok: her sonuç görevin
workspace'inde yeni bir sohbet olarak açılır ve Scheduled ekranında görünür.

## Nasıl çalışır

```text
backend başlarken  → Scheduler.start()   (main.py; SCHEDULING_ENABLED + SCHEDULER_AUTOSTART)
her SCHEDULE_TICK_SECONDS:
  reconcile()      → "running" run'ların job'ı bitti mi? bittiyse sonucu yaz
  claim_due()      → zamanı gelen görevler; next_run_at compare-and-set ile ilerletilir
  start_run()      → prompt değişkenleri + önceki özet → main.submit_job(origin="schedule")
```

- **Aynı yol:** Zamanlanmış bir çalıştırma `/run-pipeline` ile aynı `submit_job`
  fonksiyonundan geçer; turn resolution, artifact intent, web araştırması ve
  belge üretimi elle yazılmış bir mesajla birebir aynıdır. Tek fark: "bilgisayarımda
  aç" zamanlanmış işte kapatılır.
- **Her çalıştırma yeni sohbet:** Conversation kaydının başlığı `📅 <görev adı> — <tarih>`
  (sidebar sohbetleri job'ın isteğiyle listeler). Aynı sohbete
  tur eklenmez; turn_resolution ikinci haftanın isteğini takip turu sanıp
  araştırmayı atlayabilirdi. Süreklilik önceki çalıştırmanın özetiyle sağlanır
  (`include_previous_summary`, `context_text` olarak).
- **Restart güvenliği:** `next_run_at` veritabanında. Sunucu kapalıyken kaçan
  çalıştırma `SCHEDULE_CATCHUP_WINDOW_HOURS` içindeyse bir kez telafi edilir,
  değilse `missed` kaydedilir. Restart'ta yarıda kalan iş (`interrupted`) bir kez
  yeniden denenir.
- **Korkuluklar:** Aynı görevin önceki çalıştırması sürüyorsa yenisi
  `skipped_overlap`. Aynı anda en fazla `SCHEDULE_MAX_CONCURRENT` zamanlanmış iş.
  Art arda `SCHEDULE_MAX_CONSECUTIVE_FAILURES` hata olursa görev otomatik
  duraklatılır. Çalıştırmalar arası en az `SCHEDULE_MIN_INTERVAL_MINUTES`, derin
  araştırmada `SCHEDULE_MIN_DEEP_INTERVAL_HOURS`.

## Prompt değişkenleri

Çalıştırma anında görevin saat diliminde çözülür (`scheduling/prompts.py`):

| Değişken | Örnek |
| --- | --- |
| `{tarih}` | 21 Eylül 2026 |
| `{hafta}` | 14–21 Eylül 2026 |
| `{ay}` | Eylül 2026 |
| `{son_calisma}` | 14 Eylül 2026 (ilk çalıştırmada "son 7 gün (...)") |

## İzleme (yalnız değişiklikte öne çıkar)

`watch.strategies`:

| Strateji | Ne yapar | Maliyet |
| --- | --- | --- |
| `url_hash` | Çalıştırmadan önce izlenen sayfaları Crawl4AI ile açar, ana metnin hash'ini öncekiyle karşılaştırır. Değişmediyse iş hiç başlamaz (`no_change`). | Model çağrısı yok |
| `research_diff` | İş bittikten sonra yeni raporu önceki özetle bir model çağrısıyla karşılaştırır; önemli değişiklik yoksa run `no_change` olur ve okunmamış sayılmaz. | 1 kısa model çağrısı |

İkisi birlikte kullanılabilir. İlk çalıştırmada karşılaştıracak bir şey olmadığı
için her zaman tam rapor üretilir (baseline). Tamamen dinamik sayfalarda (sayaç,
canlı veri) `url_hash` her seferinde "değişti" diyebilir; orada `research_diff`
kullanın.

## Run durumları

`running`, `done`, `partial`, `failed`, `error`, `cancelled`, `interrupted`,
`skipped_overlap`, `missed`, `no_change`. Okunmamış rozeti yalnız `done`,
`partial`, `failed`, `error` için yanar; görev ekranda seçilince okunmuş sayılır.

## API

| Metod | Yol | Ne |
| --- | --- | --- |
| GET | `/scheduled-tasks?workspace_id=` | Liste (`unseen_runs`, `upcoming_runs` dahil) |
| POST | `/scheduled-tasks` | Oluştur |
| GET / PATCH / DELETE | `/scheduled-tasks/{id}` | Detay / güncelle / sil |
| POST | `/scheduled-tasks/{id}/pause`, `/resume` | Duraklat / devam |
| POST | `/scheduled-tasks/{id}/run-now` | Hemen çalıştır (`next_run_at` değişmez) |
| GET | `/scheduled-tasks/{id}/runs` | Çalıştırma geçmişi |
| POST | `/scheduled-tasks/{id}/runs/seen` | Okundu işaretle |
| POST | `/scheduled-tasks/preview` | Zamanlama → sonraki 5 çalışma |
| POST | `/scheduled-tasks/parse-schedule` | "her pazartesi 9" → cron (model çağrısı) |

## Kod

| Dosya | Görev |
| --- | --- |
| `backend/scheduling/cron.py` | 5 alanlı cron, saat dilimiyle (croniter yerine: imaj offline wheel'den kuruluyor) |
| `backend/scheduling/store.py` | `scheduled_tasks`, `scheduled_runs` (migration `0008`) |
| `backend/scheduling/runner.py` | Scheduler döngüsü |
| `backend/scheduling/watch.py` | `url_hash`, `research_diff` |
| `backend/scheduling/api.py` | Route'lar |
| `frontend/src/components/ScheduledView.jsx` | Liste, detay, run geçmişi |
| `frontend/src/components/ScheduleEditorDialog.jsx` | Oluştur / düzenle |
| `frontend/src/schedule.js` | Preset ↔ cron, metinler (`schedule.test.mjs`) |

Sohbetteki bir cevabın altındaki takvim düğmesi (**Repeat on a schedule**) o isteği
doldurulmuş bir görev taslağı olarak açar.

## Bilinen sınırlar

- Tek backend process'i varsayılır (compose'taki gibi). Görev alma compare-and-set
  olduğu için iki process aynı görevi iki kez başlatmaz, ama eşzamanlılık sınırı
  process başınadır.
- Auth olmadığı için görevin sahibi yok; workspace'i gören herkes görevi görür.
- Zamanlanmış işler kullanıcı işleriyle aynı model kotasını kullanır.
