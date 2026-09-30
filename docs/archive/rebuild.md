# Web tools rebuild

Eve devam notu. Demo branch’ine (`feature/grounded-web-research`) dokunma.
İş branch’i: **`feature/web-tools-rebuild`** (grounded @ `7e513be`).

Yarın sunum/demo: **`feature/grounded-web-research`** (SearXNG+Crawl4AI, mevcut).
Bu branch rebuild — controller henüz kodda değil.

## Kilit kararlar (stack)

- **SearXNG (arama) + Crawl4AI (tek sayfa fetch)**. Firecrawl yok. İkinci tarayıcı yok.
- CAMEL `SearchToolkit` / Browser bağlanmaz. Neden kendi katman: CAMEL snippet araması; bu ürün sayfa açıp grounded `data_path` / artifact üretir.
- SSRF, bütçe tavanı, `html_extract`, tablo diski — kalır.

## Bugün bitti (temizlik)

- Parked `DEEP_RESEARCH` capability + `deep_research.py` + testler + Research Agent bağı çıktı. Anket niyeti hâlâ `detect_deep_research` → `WEB_SEARCH` floor.
- `url_discovery` log alias kalktı; gerçek ağ araması log’da yalnız `web_search`.
- Docs/test: `web_acquire` / Scrapy artık aktif API değil. Yüzey `web_search` / `web_read`.

Kalacak: `web_search.py`, `searxng/`, `crawl4ai_engine.py`, `html_extract.py`, `web_security.py`, tablo `data_path` + `transform_table`, `job_tool_calls` / JSONL.

## Sorun (neden controller)

Akışın 3 kutusu sağlıklı: SearXNG link bulur → model URL seçer → Crawl4AI scrape eder.

Kırılan yer **durma**. Bugün ReAct + yumuşak “If this page answers, write FINAL ANSWER”:

1. Doğru sayfa okundu.
2. Model teyit için başka URL / yeni arama açtı.
3. Son tool sonucu kazandı; önceki kanıt sohbet dikkatinde kayboldu (diskte duruyor).
4. Daha fazla arama.

Hit/miss, homepage, listing, “yeterince iyi mi?” diye kural üstüne kural **yazılmayacak**. Her canlı kırılmada yeni sayfa sınıfı eklemek unbounded.

Durma **semantic değil structural**. Kod faz keser. Model kaynak ve cevap yargılar.

## Sıradaki iş: phase controller

Model: ne araştırılacak, hangi URL anlamlı, cevap nasıl yazılacak.
Kod: akış devam mı, bitti mi.

```
SEARCH → SELECT → FETCH → ANSWER → END
```

Teknik olarak hiç başarılı fetch yoksa **bir** ikinci tur:

```
SEARCH_1 → SELECT → FETCH
  en az bir başarılı fetch → ANSWER
  hiç yok                  → SEARCH_2 → SELECT → FETCH → ANSWER
```

İkinci search koşulu: `successful_fetches == 0`. Başarı = HTTP/render + extract dolu (metin veya tablo). “Bu sayfa soruyu cevaplıyor mu?” yok.

### Fazlar

1. **SEARCH** — Model yalnız sorgu üretir. SearXNG arar, aday listesi döner. Sıra SearXNG’nin; kod re-rank etmez.
2. **SELECT** — Model tool çağırmaz. Structured çıktı: açılacak 2–3 URL (aday listesinden, birebir kopya).
3. **FETCH** — Kod Crawl4AI ile açar. Teknik başarılar `evidence[]`’e **append-only** eklenir. İçerik kararı yok (listing mi, doğru entity mi, homepage mi — SELECT/ANSWER model işi; SSRF/uydurma URL altyapı olarak kalır).
4. **ANSWER** — Web tool yok. Context: kullanıcı sorusu + `evidence[]`. Model yalnız bundan yazar. Excel: tam tablo `data_path` → artifact tool; satırlar modele değil.

`last_web_result` yok. Son zayıf sayfa önceiyi ezemez.

### Execution budget (tavan, durma değil)

Örnek: max **2 search**, max **6 fetch**. Kontrolsüz dönmesin diye. Relevance kararı vermez.

### CAMEL / ReAct

Bunu ReAct döngüsünün *içine* gömme. ReAct her tur “tool veya yaz” der; durmamız faz kesmek.

Uygulama: her faz ayrı model çağrısı.

- SEARCH: structured query (web tool bağlı olmasın, veya yalnız arama kodda).
- SELECT: toolsuz, `urls: [...]`.
- FETCH: kod, model yok.
- ANSWER: toolsuz, yalnız evidence.

`web_search` / `web_read` agent’a açık kalırsa model yine kaçar. Controller açıkken model-facing web tool bağlama.

Küçük yol (aynı fikir): tek agent, 3 `astep` — query → url[] → toolsuz cevap; ortada sen fetch. Hit-kilidi / sayfa taksonomisi `WebTools`’a yazma.

### Edge case (kabul, yeni kural değil)

- Seçilen 2–3 URL hepsi 200 OK ama yanlış entity / eski yıl → ANSWER zayıf kanıttan yazar veya “bunu buldum” der. Kalite SELECT+ANSWER yargısı.
- Anket (birden fazla kaynak): SELECT kotası iş tipine göre (örn. 3 vs 5), yeni sayfa sınıfı değil.
- Kullanıcı literal URL verdi: SEARCH atla, FETCH → ANSWER.
- Retry: yalnız sıfır teknik başarı. İkinci SELECT’e “öncekiler fail, başka host” demek yeter.

### Bilerek yapılmayacak

- “Bu sayfa yeterince iyi mi?” kapısı
- Homepage/listing/hub/isabet taksonomisi ile durma
- Soft “FINAL ANSWER now” ile ReAct’ten çıkmayı beklemek
- Demo gecesi `feature/grounded-web-research` üzerine bu controller

## Checklist

- [x] Temizlik: DEEP_RESEARCH, url_discovery, web_acquire docs/test
- [x] Bu tasarım (`rebuild.md`)
- [x] Phase controller: SEARCH → SELECT → FETCH → ANSWER
- [x] `evidence[]` append-only; ANSWER toolsuz
- [x] İkinci search yalnız `successful_fetches == 0`
- [x] Budget tavanı (2 search / 6 fetch)
- [x] Literal URL: SEARCH skip
- [x] Agent’a `web_search`/`web_read` bağlı değilken controller çalışır (ReAct kaçışı yok)
- [ ] (sonra) İnce facade: search port = SearXNG, fetch port = Crawl4AI, loop = controller

## Şu an neredesin

Controller yazıldı. `backend/web_controller.py` fazı keser; SearXNG/Crawl4AI aynı.

**Single:** tools=None ChatAgent. Coordinator/Workforce/web controller/artifact-intent yok. `runtime_block` yalnız tarih + dil.

**Workforce:** SEARCH/SELECT/ANSWER `CURRENT DATE` alır. Job başında bir artifact-intent LLM (regex fallback). Handoff yalnız `wants_artifact and data_path`. Pad hâlâ Document tool’ları için; handoff’u tetiklemez.

Eski execution router (`request_routing.py`, `ROUTER_MODE`, `decide_execution`) silindi. UI mode = strateji.

Demo branch’ine dokunulmadı.

Doğrulama: backend pytest (`--ignore=vendor`, iki live deselect).
