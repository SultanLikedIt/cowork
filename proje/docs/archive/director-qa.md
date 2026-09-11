# Direktör Soru Bankası — `clean-rebuild`

Bu cevaplar ezber metni değil, konuşma iskeletidir. Her cevapta önce mevcut
durumu, sonra gerekiyorsa hedef durumu söyleyin.

## A. 30 saniyelik temel cevaplar

### 1. Bu sistem tam olarak ne yapıyor?

Kullanıcının prompt ve dokümanlarını tek agent veya dinamik bir CAMEL
Workforce ile çalıştırıyor; planı, agent atamalarını ve sonucu canlı
gösteriyor, sonucu dört formatta export ediyor.

### 2. Mimariyi tek cümlede nasıl tarif edersin?

React/Vite frontend'in REST ve SSE ile konuştuğu, FastAPI içinde in-memory job
state ve CAMEL adapter'ı bulunan modüler bir demo backend'idir.

### 3. Üretime hazır mı?

Hayır; güçlü bir demo/pilot çekirdeği. Üretim için auth, kalıcı state, durable
queue, execution isolation, test/CI, retention ve observability katmanları
gerekiyor.

### 4. En güçlü tarafı ne?

Model, CAMEL, event ve UI sınırları iyi ayrılmış; aynı ürün akışında hem düşük
maliyetli Single Agent hem de dinamik Workforce sunuyor.

### 5. En kritik risk ne?

Dynamic recovery worker'ın local code/command execution tool'u. Üretim/pilot
öncesi kaldırılmalı veya gerçek sandbox ve policy broker arkasına alınmalı.

## B. Ürün ve kullanım

### 6. Neden Single Agent ve Workforce diye iki mod var?

Her görev multi-agent overhead'ini hak etmiyor. Single Agent basit görevlerde
daha hızlı ve ucuz; Workforce karmaşık, uzmanlaşan veya paralelleşen işlerde
değer üretiyor.

### 7. Modu sistem mi seçiyor?

Hayır. `clean-rebuild` sürümünde seçim kullanıcıya açıkça bırakılmıştır;
otomatik complexity routing yoktur.

### 8. Multi-agent her zaman daha kaliteli mi?

Hayır. Daha fazla model çağrısı daha fazla latency/maliyet ve hata yüzeyi
demektir. Kalite kazanımı görev yapısına bağlıdır ve evaluation dataset'iyle
ölçülmelidir.

### 9. Ana kullanıcı yolculuğu nedir?

Prompt yaz → gerekirse dosya ekle → Single/Workforce seç → submit et → task ve
agent progress'ini canlı izle → final sonucu kopyala veya export et.

### 10. Kullanıcı feedback'i sisteme gidiyor mu?

Hayır. Thumbs up/down bugünkü UI'da local state'tir; backend feedback endpoint'i
ve öğrenme/evaluation pipeline'ı yoktur.

## C. Workforce ve agent davranışı

### 11. Dört agent sırayla mı çalışıyor?

Hayır. Dört sabit agent bir capability pool'dur. Task Planner dinamik subtasks
üretir, Coordinator bunları uygun worker'lara atar; her job tüm agent'ları
kullanmak zorunda değildir.

### 12. Sabit agent'lar hangileri?

Research, Document, Review ve Outline Agent.

### 13. Task graph nasıl oluşuyor?

CAMEL Task Planner root task'ı alt görevlere böler. Coordinator atama sırasında
dependency'leri üretir; hazır ve bağımsız task'lar paralel çalışabilir.

### 14. Bu sabit pipeline mı?

Hayır. `WorkforceMode.AUTO_DECOMPOSE` kullanılıyor; task yapısı request'e göre
LLM tarafından dinamik üretiliyor.

### 15. Kalite kontrol nasıl yapılıyor?

Worker çıktısından sonra CAMEL Task Agent quality score, issue listesi ve
recovery strategy üretir. Recovery retry, replan, reassign, decompose veya
create-worker olabilir.

### 16. Yeni agent ekleme nasıl çalışıyor?

Kullanıcının eklediği manual agent session memory'sinde tutulur. Çalışan
Workforce'a eklenmez; sonraki multi-agent job başlarken yeni Workforce'a
register edilir.

### 17. Dynamic worker nedir?

CAMEL'in failure/recovery sırasında yeni bir uzman worker üretmesidir. Current
template search, thinking ve local code/command execution tool'larına sahiptir.

### 18. Final answer gerçekten sentezleniyor mu?

Uygulama zorunlu bir synthesis stage tanımlamıyor. CAMEL root result alt görev
sonuçlarını birleştirir; frontend öncelikle dependency graph'ın leaf output'larını
seçer. "Final synthesized answer" UI etiketi her zaman ayrı bir synthesis
agent'ı çağrıldığı anlamına gelmez.

### 19. Agent memory job'lar arasında paylaşılıyor mu?

Hayır. Her multi-agent job için yeni Workforce kurulur ve `share_memory`
default olarak kapalıdır. Cross-job conversational memory yoktur.

### 20. Neden CAMEL kullandık?

Decomposition, coordinator assignment, worker lifecycle, dependency scheduling,
structured output, quality evaluation ve recovery mekaniklerini hazır bir
framework ile alıyoruz. Bedeli framework davranışına ve pinned sürüme bağımlılık.

## D. Model, maliyet ve kalite

### 21. Model nasıl değiştiriliyor?

`MODEL_BASE_URL`, `MODEL_NAME` ve `MODEL_API_KEY` ile OpenAI-compatible gateway
değiştirilir. Agent kurulum kodu değişmez.

### 22. Kodun varsayılan modeli ne?

Local Ollama `http://localhost:11434/v1` üzerinde `qwen2.5:3b`. Deployment
environment değişkenleri bunu override eder.

### 23. Tüm model çağrıları aynı gateway'den mi geçiyor?

Project-owned coordinator, task agent, fixed/manual/dynamic worker ve single
agent aynı `get_model()` factory'sinden model alır.

### 24. Token ve çağrı sayısı ölçülüyor mu?

Evet, provider usage döndürürse input/output/total token ve model call count
job'a eklenir. Provider usage dönmezse wrapper çağrıyı saymıyor; bu bir ölçüm
sınırıdır.

### 25. Maliyet ölçülüyor mu?

Schema'da `estimated_cost_usd` var ama current code bunu doldurmuyor. Hedefte
gateway-reported cost veya merkezî pricing policy ile attribution gerekir.

### 26. Hallucination'ı nasıl azaltıyoruz?

Research Agent güncel/doğrulanabilir bilgi için search kullanacak şekilde
prompt'lanmıştır; gerçek search çağrısı ayrıca loglanır. Ancak bu doğruluk
garantisi değildir. Üretimde citation contract, evaluation set ve gerekirse
human approval gerekir.

### 27. Kaliteyi nasıl ölçeriz?

Görev tipine göre golden dataset; factuality/citation accuracy, task completion,
format compliance, latency, cost ve human rating metrikleriyle Single ve
Workforce'u karşılaştırırız.

### 28. Model servisi kapanırsa ne olur?

Çağrı exception üretirse job `error` olabilir veya CAMEL recovery deneyebilir.
Uygulama seviyesinde circuit breaker ve explicit provider retry/deadline
politikası bugün yoktur.

## E. Veri, upload ve workspace

### 29. Hangi dosyalar destekleniyor?

PDF, DOCX, XLSX, CSV, TXT ve ZIP.

### 30. Dosya limitleri nedir?

En fazla 10 kaynak/job, 20 MiB/dosya ve 50 MiB toplam. ZIP'te ayrıca 1.000
entry inspection, 200 text file, 1 MiB/file ve 10 MiB extracted-total limitleri
var.

### 31. ZIP Slip/path traversal nasıl engelleniyor?

Absolute path, `..`, drive prefix, symlink, encrypted entry ve case-insensitive
duplicate path reddediliyor; her hedef path workspace root altında resolve
ediliyor.

### 32. Agent tüm upload'ı okuyabiliyor mu?

Hayır. Prompt'a toplam 60.000, dosya başına 8.000 karaktere kadar preview
ekleniyor. Read-only workspace tool sınıfı mevcut ama current integration'da
agent'a bağlı değil.

### 33. Neden RAG/vector database yok?

Current scope bounded prompt context ile demo akışını doğruluyor. Büyük ve
kalıcı corpus için ingestion, embeddings, retrieval policy, tenant isolation
ve citation katmanı ayrı bir Faz 1/2 kararıdır.

### 34. Upload parser'larının riski nedir?

PDF/XLSX gibi untrusted içerik sync ve CPU/memory-heavy parse ediliyor. AV scan,
parser isolation ve time/memory guard yok; kontrollü pilotta parsing worker'a
taşınmalıdır.

### 35. Dosyalar ne zaman siliniyor?

Otomatik TTL/cleanup yok. Upload, workspace ve tool logs diskte birikir. Üretim
öncesi retention, quota, encryption ve deletion workflow gereklidir.

## F. API, event ve frontend

### 36. Neden SSE seçildi?

İhtiyaç ağırlıklı olarak server'dan browser'a tek yönlü progress akışı. SSE,
HTTP üzerinde basit reconnect davranışıyla WebSocket'ten daha düşük operasyonel
karmaşıklık sağlar.

### 37. WebSocket ne zaman gerekir?

Pause/resume, human-in-the-loop mesajları, interactive tool approval veya
yüksek frekanslı çift yönlü kontrol gerektiğinde.

### 38. SSE delta mı gönderiyor?

Hayır. Her lifecycle event sonrası tam job snapshot'ı gönderiliyor. Demo için
basit; büyük job/task hacminde event delta + cursor modeli daha verimli olur.

### 39. Browser refresh olursa ne olur?

Backend prosesi ayaktaysa `GET /jobs` ile memory'deki job'lar hydrate edilir ve
aktif job için SSE tekrar açılır. Backend restart olduysa geçmiş yoktur.

### 40. Export nasıl çalışıyor?

Markdown/TXT browser'da oluşturulur. DOCX/PDF için ekranda seçilen exact final
text backend'e gönderilir; `python-docx` veya ReportLab ile binary üretilir.

## G. Güvenlik ve air-gap

### 41. Authentication var mı?

Hayır. CORS localhost ile sınırlı ama CORS authentication değildir. Pilot için
OIDC/SSO, tenant ve RBAC P0/P1 gereksinimidir.

### 42. Prompt injection riski var mı?

Evet. Kullanıcı prompt'u ve upload içeriği model context'ine giriyor. Search ve
özellikle code execution olan agent'larda etkisi büyür. Tool allowlist,
least-privilege, sandbox ve approval policy gerekir.

### 43. Agent terminal komutu çalıştırabilir mi?

Fixed dört worker çalıştıramaz. Fakat recovery ile oluşan dynamic worker'ın
`CodeExecutionToolkit.execute_command` erişimi vardır; bu nedenle current
build production-safe değildir.

### 44. Sistem air-gapped mi?

Backend için pinned Windows wheel bundle ve vendored CAMEL source vardır;
model adapter internal gateway'e yönlenebilir. Fakat tam air-gap için frontend
artifact'ı, internal model, internal/disabled search ve offline runbook gerekir.

### 45. DuckDuckGo air-gap ile çelişmiyor mu?

Evet, gerçek air-gap deployment'ta DuckDuckGo kullanılamaz. Research tool'u
internal search servisiyle değiştirilir veya kapatılır.

### 46. Hassas dokümanlar harici modele gider mi?

Environment harici gateway'e ayarlıysa prompt'a eklenen doküman preview'ları
gateway'e gider. Data residency ve provider policy deployment öncesi
netleştirilmelidir.

### 47. Secret'lar repoda mı?

`.env` ve `.env.*` gitignore'dadır. Current yaklaşım local env file'dır;
production'da secrets manager/managed identity tercih edilmelidir.

### 48. Tool log'larında hassas veri olabilir mi?

Evet. Search args ve raw results JSONL'e yazılır. Redaction, rotation, TTL,
encryption ve erişim politikası bugün yoktur.

## H. Reliability ve scale

### 49. Job state nerede?

FastAPI prosesindeki Python sözlüklerinde. Upload metadata ayrıca diskte
saklanır ama job execution/history kalıcı değildir.

### 50. Backend restart olursa?

Job geçmişi, aktif job state'i ve manual worker'lar kaybolur. Diskteki
upload/workspace kalabilir.

### 51. Birden fazla Uvicorn worker açabilir miyiz?

Doğru çalışmaz. Her proses ayrı `_JOBS` ve subscriber listesi görür. Sticky
session geçici bir yama olur; çözüm shared datastore ve durable queue'dur.

### 52. Kaç job paralel çalışabilir?

Kodda global/per-user limit yok. Her submit bir `asyncio` task yaratır;
Workforce ayrıca ready subtasks'ı paralel gönderebilir. Production'da bounded
queue, worker concurrency ve provider rate limit gerekir.

### 53. API event loop neden bloklanabilir?

Current CAMEL 0.2.80 decomposition, coordinator assignment ve quality analysis
içinde synchronous `ChatAgent.step()` çağrıları kullanır. Multi execution API
prosesinden ayrı worker'a taşınmalıdır.

### 54. Cancellation güçlü mü?

Cooperative. Scheduling'i durdurur ama başlamış model/provider veya Python
çağrısını zorla öldürmez. Single Agent çağrısı tamamlanana kadar devam edebilir.

### 55. Timeout var mı?

CAMEL task bekleme default'u 600 saniyedir; uygulama seviyesinde total job
deadline ve explicit provider timeout/retry policy yoktur.

### 56. Event subscriber yavaşsa ne olur?

Subscriber başına `asyncio.Queue` unbounded'dır. Slow client memory baskısı
oluşturabilir; bounded buffer/heartbeat/cursor hedef mimariye eklenmelidir.

## I. Test, deployment ve operasyon

### 57. Test kapsamı ne?

Automated unit/integration/e2e suite yok. Manuel upload script'i var. İnceleme
sırasında source compile, API smoke, frontend build, DOCX ve ZIP security
smoke'ları geçti.

### 58. Demo öncesi açık blocker var mı?

Local virtualenv'de `ddgs` ve `reportlab` eksik bulundu. Bu halde Research
search ve PDF export fail olur; environment requirements'tan yeniden
kurulmalıdır.

### 59. CI/CD var mı?

Hayır. Repo içinde application CI workflow, lint/test gate veya deployment
manifest'i yoktur.

### 60. Backend dependency'leri reproducible mı?

Direct requirements exact pinlidir; Python 3.11 Windows wheel bundle repodadır.
Ancak hash verification, signed artifact ve SBOM yoktur.

### 61. Frontend air-gap kurulabilir mi?

Package lock vardır ama node_modules/prebuilt dist/offline npm cache repoda
yoktur. Tam air-gap release için frontend artifact stratejisi gerekir.

### 62. Observability ne seviyede?

Task event'leri, job duration, timing log, token usage ve search tool audit var.
Metrics endpoint, traces, SLO, central log ve cost dashboard yok.

### 63. CAMEL upgrade nasıl yapılır?

Pinned 0.2.80 source ve wheel kontrollü yükseltilir; decomposition, structured
output, recovery, callback ve tool smoke/evaluation set'i yeniden çalıştırılır.

## J. Yol haritası ve karar

### 64. Yarın tek bir şey düzeltsek ne olur?

Dynamic worker'daki code/command execution'ı kapatır ve dev environment'i
requirements ile yeniden kurup demo smoke yaparız.

### 65. Kontrollü pilot için minimum nedir?

SSO/RBAC, persistent job state, durable bounded workers, internal model/search
policy, upload retention/scan, timeout/idempotency ve automated tests.

### 66. Başarı KPI'ları ne olmalı?

Task completion, factual/citation accuracy, human acceptance, p50/p95 latency,
token/cost per successful task, cancellation/failure rate ve kullanıcı zaman
tasarrufu.

### 67. İlk mimari karar ne olmalı?

Hedef seviye netleşmeli: demo mu, kontrollü pilot mu, production mı? Bu karar
security, persistence, egress ve operasyon yatırımını belirler.

### 68. Direktörden hangi kararları istiyoruz?

Onaylı model/data boundary, search politikası, code-execution politikası,
retention süresi, ilk kullanıcı grubu ve başarı KPI'ı.

## K. Zor sorulara cevap kalıbı

Bir eksik sorulduğunda şu kalıbı kullanın:

> "Bugünkü `clean-rebuild` sürümünde **mevcut durum** şu. Bunun **etkisi** şu.
> Kontrollü pilot/üretim için **önerdiğimiz değişiklik** şu ve yol haritasında
> Faz X'te."

Kaçınılacak cevaplar:

- "Framework hallediyor."
- "Multi-agent olduğu için daha iyi."
- "Air-gapped çünkü wheel'lar repoda."
- "CORS var, güvenli."
- "Async olduğu için ölçeklenir."
- "Agent terminale erişemez."
