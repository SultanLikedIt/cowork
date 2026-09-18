# Zamanlanmış ve izleme görevleri

Kaydedilmiş bir isteği backend'in kendi başına çalıştırması: cron zamanlamasıyla,
tek seferlik bir saatte, başka bir görevden sonra, ya da yalnız bir şey
değiştiğinde öne çıkan bir izleme olarak. Arayüzde sol menüde **Scheduled** (`/w/:workspaceId/scheduled`).

Her sonuç görevin workspace'inde yeni bir sohbet olarak açılır ve Scheduled
ekranında görünür. İstenirse sonuç ayrıca Outlook'tan e-postayla gönderilir
(aşağıda **E-posta**, kurulum: [outlook.md](outlook.md)). Teams / Slack yok.

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

## Zincirler ve koşullar

Bir görev başka bir görevden sonra çalışabilir: `trigger_kind = "after_task"`,
`upstream_task_id`. Böyle bir görevin kendi saati yoktur (`next_run_at` her zaman
NULL); üst görevin bir çalıştırması bitince tetiklenir. Bir görevin birden çok
takipçisi olabilir (fan-out); "A ve B bitince C" (fan-in) desteklenmez.

```text
[Resmi Gazete kontrolü]  her gün 10:00, research_diff
        │ koşul: change
        ▼
[Mevzuat etki raporu]    Workforce, PDF
        │ koşul: success
        ▼
[Yönetici özeti]         önceki raporun dosyasını okuyup PPTX
```

| Koşul (`condition`) | Takipçi ne zaman çalışır |
| --- | --- |
| `success` (varsayılan) | Üst çalıştırma bir sonuç ürettiyse: `done`, `partial` ya da rapor yazıp değişiklik bulmayan `no_change` |
| `change` | Üst görev bir izlemeyse ve değişiklik bulduysa (`change_detected`). İzleme olmayan üst görevde her `done`/`partial` |
| `always` | Her sonuçta, başarısız olanlar dahil. `skipped_overlap` ve `missed` hariç (ortada bir çalıştırma yok) |

**Akış** (`scheduling/runner.py`):

1. Üst çalıştırma settle olur (`_settle` → `_complete`).
2. Her etkin takipçi için koşul değerlendirilir. Tutmazsa takipçiye
   `skipped_condition` çalıştırması yazılır (okunmuş; `error` alanında neden).
3. Tutarsa takipçiye `status="queued"`, `trigger="chained"`, `parent_run_id`
   ile bir çalıştırma yazılır; girdisi `input_json`'da dondurulur.
4. Tick, settle adımından hemen sonra ve eşzamanlılık sınırı içinde **önce** queued
   çalıştırmaları başlatır, sonra zamanı gelen cron görevlerini. Takipçinin kendi önceki
   çalıştırması sürüyorsa queued çalıştırma bekler.

**Girdi aktarımı:**

- `pass_upstream_result` → üst raporun kaynak listesi ayıklanmış hâli (en fazla
  8.000 karakter) `context_text`'e "ÖNCEKİ GÖREVİN SONUCU" olarak eklenir.
- `pass_upstream_files` → üst işin `artifacts/` dosyaları `main._collect_job_files`
  ile upload olarak kaydedilir ve `file_ids` olur. Takipçi bunları kullanıcının
  eklediği dosya gibi okur (Docling, şablon doldurma dahil). En fazla 10 dosya ve
  50 MB; klasörler (bundle) aktarılmaz. Dışarıda kalanlar çalıştırmanın
  `input.notes` alanına yazılır ve ekranda görünür.

**Korumalar:**

- Döngü reddedilir (A → B → A).
- Zincir derinliği en fazla `SCHEDULE_MAX_CHAIN_DEPTH` (varsayılan 3).
- Bir zincir 24 saatte en fazla `SCHEDULE_MAX_CHAINED_RUNS_PER_DAY` (varsayılan
  10) zincir çalıştırması başlatır.
- Aynı üst çalıştırma bir takipçiyi en fazla bir kez tetikler (unique index).
- Üst ve takipçi aynı workspace'te olmalı.
- Üst görev silinirse takipçi bir sonraki tick'te otomatik duraklatılır. Yeniden
  başlatmak için başka bir üst görev seçilmelidir.

**Run now (takipçide):** Üst görevin son başarılı çalıştırmasının raporu ve
dosyalarıyla çalışır. Hiç başarılı çalıştırma yoksa girdisiz çalışır ve bunu
not olarak yazar.

## Göreve bağlı dosyalar

`attached_files` (API'de `attached_file_ids`): görev oluşturulurken yüklenen
dosyalar her çalıştırmada `file_ids`'e eklenir. Örnek: "Her ay bu Excel
şablonunu güncel kurlarla doldur." Takipçide üst görevden gelen dosyalar bunlara
eklenir (toplam en fazla 10). Silinmiş bir upload çalıştırmayı düşürmez, atlanır.

## E-posta

`delivery`: `{"to": [...], "cc": [...], "on": "success" | "change" | "always", "attach_files": true}`.
Alıcıları görevi oluşturan kişi yazar; model alıcı seçemez.

- `on` zincir koşullarıyla aynı anlamdadır, görevin **kendi** çalıştırmasına
  uygulanır: `success` her rapor, `change` izleme görevinde yalnız değişiklik
  bulunduğunda, `always` başarısız çalıştırmada da (hata bildirimi). `cancelled`
  çalıştırma için hiç e-posta gitmez.
- Run bitince `delivery_status = pending` olur; gönderimi bir sonraki tick yapar
  (`deliver_pending`). Köprü kapalıysa / Outlook açılamıyorsa 1, 5, 15, 30, 60, 60…
  dakika arayla `SCHEDULE_DELIVERY_MAX_ATTEMPTS` (10, ~6 saat) kez denenir, sonra
  `failed`. Yanlış token, geçersiz adres, `OUTLOOK_ENABLED=false` tekrar denenmez.
- En az bir kez gönderim: gönderim sırasında backend çökerse aynı e-posta tekrar
  gidebilir.
- Konu `<görev adı> — <tarih>`; gövde cevabın markdown'ından HTML; oluşturulan
  dosyalar toplam 15 MB'a kadar eklenir, sığmayanlar gövdede listelenir.

## Run durumları

`queued`, `running`, `done`, `partial`, `failed`, `error`, `cancelled`,
`interrupted`, `skipped_overlap`, `missed`, `no_change`, `skipped_condition`.
Okunmamış rozeti yalnız `done`, `partial`, `failed`, `error` için yanar; görev
ekranda seçilince okunmuş sayılır.

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
| GET | `/integrations/outlook/status` | E-posta hazır mı (köprü, token, Outlook) |
| POST | `/integrations/outlook/test` | Test e-postası |

## Kod

| Dosya | Görev |
| --- | --- |
| `backend/scheduling/cron.py` | 5 alanlı cron, saat dilimiyle (croniter yerine: imaj offline wheel'den kuruluyor) |
| `backend/scheduling/store.py` | `scheduled_tasks`, `scheduled_runs` (migration `0008`, `0009`, `0010`) |
| `backend/scheduling/delivery.py` | E-posta ayarı doğrulama, ne zaman gönderilir, yeniden deneme |
| `backend/outlook.py` | Köprü istemcisi, e-posta gövdesi, `/integrations/outlook` |
| `backend/scheduling/runner.py` | Scheduler döngüsü, zincir tetikleme ve sıra |
| `backend/scheduling/chains.py` | Koşullar, döngü/derinlik doğrulaması |
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
