# Workspace belge arama (Knowledge Agent)

Workspace'e yüklenen belgelerde soru sorabilmek: "sözleşmede depozito ne
kadar", "geçen toplantıda Burak'a hangi görev verildi", "yüklediğim iki
teklifin fiyatlarını excel'de karşılaştır". Cevap, bulunan bölümlere
`[K1]`, `[K2]` … etiketiyle atıf yapar; sonuna sunucu **Belge kaynakları**
listesini (dosya, sayfa, bölüm) ekler.

Neden gerekli: LangGraph işçilerinin dosya aracı yok. Yüklenen bir belge
modele yalnız prompt'un ilk `workspace.MAX_CONTEXT_CHARS` (60.000) karakteri
olarak ulaşıyordu; 40. sayfadan sonrası görünmüyordu. Sohbetin dosya çantası
da (`conversation_files`) 10 dosyada en eskisini düşürüyor.

## Basit anlatım: vektör veritabanı olmadan nasıl çalışıyor

**Tek cümleyle:** Her pasajı sayılara çevirip "yakınlığa" göre aramak yerine
(vektör veritabanının yaptığı), pasajları **anahtar sözcük dizininde bulur,
sonra küçük bir yapay zekâ modeline en iyi adayları okutup soruyu gerçekten
cevaplayanları seçtirir.** Yani bu da bir RAG (önce bul, sonra cevapla);
yalnızca "bulma" kısmı farklı.

**Benzetme:** Vektör veritabanı, her sayfanın özünü ezberlemiş bir
kütüphaneci gibidir: ne istediğinizi hangi sözcüklerle anlatırsanız anlatın
bulur. Bu sistem ise kitabın arkasındaki **dizine** bakıp 40 kadar aday
sayfayı çeken, sonra her birini sorunuzla yan yana **dikkatle okuyup**
gerçekten cevap verenleri ayıran bir kütüphanecidir. İkisi de anlama göre
bulur; işi farklı bölüştürürler.

### 1. Belge yüklenince (hazırlık)

- Belge küçük parçalara (pasaj, yaklaşık bir paragraf) bölünür. Her pasaj
  hangi dosyadan, hangi sayfadan ve hangi başlığın altından geldiğini bilir.
- Yazım farkları önemsiz olsun diye metin sadeleştirilir: küçük harf,
  `ı → i`, `ş → s`, `ğ → g`, "Wi-Fi" → "wifi". Bu, taranmış belgelerde OCR'ın
  "ı" yerine "i" okuma hatasını da etkisiz kılar.
- Pasajlar uygulamanın zaten kullandığı SQLite içinde aranabilir bir dizine
  yazılır. Dizin her sözcüğü 3 harflik parçalara ayırır; bu sayede sözcüğün
  bir kısmı da eşleşir: "sözleşme" araması "sözleşmesinde"yi bulur (Türkçe
  ekler için önemli).

### 2. Soru gelince

| Adım | Ne olur | Neden |
| --- | --- | --- |
| **a. Anahtar sözcük araması** | Sorudaki sözcükler köklerine kısaltılır ("harcandı" → "harca"), eşleşen en fazla 40 pasaj çekilir. Nadir sözcükler sık olanlardan fazla sayılır. | Hızlı (milisaniye) ve ucuz. Aday bulur ama anlamı bilmez. |
| **b. Yeniden sıralama** | Küçük bir yapay zekâ modeli (web aramasında zaten kullanılan BGE cross-encoder) soruyu ve her adayı **birlikte** okuyup 0–1 arası alaka puanı verir. En iyi ~8 pasaj kalır. | Anlam burada devreye girer: farklı ifadeyi tanır, Türkçe ile İngilizce arasında çalışır. |
| **c. Güvenlik ağı** | En iyi puan zayıfsa (0,5'in altında) ana yapay zekâ modeli soruyu Türkçe ve İngilizce arama sözcüklerine çevirir; a–b tekrar çalışır. | Cevapla ortak sözcüğü olmayan soruları kurtarır: "evden çıkmak" ↔ "fesih", Türkçe belgeye İngilizce soru. |
| **d. Bağlam** | Her bulunan pasajın hemen öncesi ve sonrası eklenir; her sonuç bir etiket alır: [K1], [K2]… | Cevap çoğu zaman eşleşen sözcüklerin hemen yanındadır. |
| **e. Cevap** | Yapay zekâ etiketlere atıf yapan kısa bir özet yazar; son cevap bundan yazılır ve uygulama kaynak listesini (dosya, sayfa, bölüm) ekler. | Her iddia kaynağına kadar izlenebilir. |

### Neden vektör veritabanı yok

| | Vektör veritabanı | Bu yaklaşım |
| --- | --- | --- |
| Ek model | Bir embedding modeli (~2,2 GB RAM) | Yok; zaten yüklü sıralama modeli kullanılıyor |
| Yükleme maliyeti | Her pasaj modelden geçer (büyük dosyada dakikalar) | Düz metin dizinleme (25 dosya 0,2 sn) |
| Ek servis | Genellikle ayrı bir veritabanı | Yeni bir şey yok; silinirse kendini yeniden kuran bir dosya (`knowledge.db`) |
| Alakayı yargılama | Önceden hesaplanmış "anlam özetlerini" karşılaştırır | Soruyla pasajı birlikte okur; pasaj başına daha isabetli |

Sıralama modeli, tek bir pasajı yargılamakta vektör aramasından daha
isabetlidir; ama her pasaja çalıştırılamayacak kadar yavaştır (pasaj başına
~0,2–0,35 sn). Bu yüzden önce anahtar sözcük dizini adayları daraltır.

### Dürüst bedeli

- **Zayıf nokta:** Bir pasajın ne soruyla ne de yapay zekânın yeniden
  yazdığı sorgularla ortak bir sözcüğü/kökü yoksa, anahtar sözcük adımı onu
  hiç bulamaz; sıralama modeli de onu hiç görmez.
- **Ölçüm:** Test setinde yalnız anahtar sözcük araması cevabı %81 oranında
  buldu; yeniden yazma güvenlik ağıyla %96.
- **Not:** Bu test sentetik belgelerle yapıldı. Gerçek şirket belgeleriyle
  yapılan test kaçan cevaplar gösterirse vektör araması **ek bir aday bulma
  yolu** olarak eklenir; sistemin geri kalanı değişmez.

## Nasıl çalışır (teknik)

```text
upload ─► parse_document (Docling) ─► chunks.jsonl (zaten vardı)
             └─► knowledge_index.submit ─► ~200 token'lık pasajlar ─► knowledge.db (FTS5 trigram)
submit_job ─► workspace_documents (kütüphane üyeliği, migration 0012)
          └─► artifact_intent.reads_documents ─► Knowledge Agent (yalnız toplar)
                knowledge_controller: PLAN → SEARCH → RANK → EXPAND → ANSWER
                └─► Assistant / Document cevabı yazar; sunucu kaynak listesini ekler
```

| Dosya | Ne |
| --- | --- |
| `backend/knowledge_index.py` | Pasaj üretimi (Docling chunk'ları birleştirilir, sayfa + başlık korunur), `fold` (ı/İ→i, aksanlar, iç tireler), FTS5 trigram arama, RRF birleştirme, arka plan indeksleme |
| `backend/knowledge_controller.py` | Aşamalar, bütçeler, `[K#]` alıntıları, brief |
| `backend/knowledge_api.py` | Kütüphane uçları + ham arama sondası |
| `backend/migrations/0012_workspace_documents.sql` | Kütüphane tablosu; eski iş eklerinden ve sohbet çantalarından doldurulur |
| `execution/langgraph_runtime/workforce.py` | `ROLE_KNOWLEDGE`, `_run_knowledge`, `_append_document_sources` |
| `frontend/src/components/LibraryPanel.jsx` | Ana sayfadaki **Documents** listesi (Workforce modunda) |

### Arama ayrıntıları

Sade anlatımın teknik karşılıkları: dizin SQLite FTS5 `trigram`
tokenizer'ı, metin önce `knowledge_index.fold` ile sadeleştirilir. Uzun
sorgu sözcükleri 5 ve 7 karakterlik iki önekle aranır (kısa önek bulmayı,
uzun önek tam biçimi öne çıkarır). Sorgu varyantlarının sonuçları reciprocal
rank fusion (RRF) ile birleştirilir, sıralamayı `reranker.score_batched`
yapar. Eşikler: `MIN_RELEVANCE` 0,03, `CONFIDENT_RELEVANCE` 0,5
(`knowledge_controller.py`).

### Uyarlanabilir PLAN

PLAN (LLM) en yavaş adım: DeepSeek reasoning aynı çağrıyı bir sağlayıcıda
1,5 sn'de, bir başkasında 86 sn'de bitirdi, bazen boş döndü. Bu yüzden önce
isteğin kendisi aranıp sıralanır (hızlı yol, ~0,4 sn). En iyi skor
`CONFIDENT_RELEVANCE` (0,5) ve üstündeyse PLAN beklenmez; paralelde başlamış
çağrı iptal edilir. Deep research her zaman PLAN'ı bekler.

### Ne zaman çalışır

- Sınıflandırıcı `reads_documents` der: cevap yüklenen belgelerin **içinde**
  (bir rakam, madde, tarih, karar; belgeler arası karşılaştırma).
- **Ve** aranacak belge vardır: `main._knowledge_files_for_job`. İşin kendi
  dosyaları prompt'a zaten tam sığıyorsa (≤ %90 × 60.000 karakter) ve başka
  belge yoksa ajan planlanmaz; Assistant belgeyi doğrudan okur.
- Çalışmaz: "bu belgeyi özetle" (bütün belge okuma), şablon doldurma
  (`reuses_uploaded_document`), belgelerle ilgisi olmayan genel sorular.

Takip turlarında `turn_resolution` da kütüphaneyi görür (`WORKSPACE
DOCUMENTS`), yani "sözleşmede depozito ne kadar" bir önceki cevabın yeniden
söylenmesi sanılmaz.

## Ölçümler (bu makinede, 2026-09-23)

| | Değer |
| --- | --- |
| SQLite | 3.46.1, FTS5 trigram var |
| Reranker | ~0,35 sn/çift @ 240 token, ~0,17 sn @ 120 token; `RERANKER_THREADS` 4→12 hızlandırmıyor |
| Türkçe token | 3,2 karakter/token (XLM-R) → pasaj ≤ 640 karakter |
| İndeksleme | Canlı verinin kopyasındaki 25 yükleme 0,2 sn |

Getirme eval'i (`reliability_harness/knowledge_eval.py`, 3 sentetik belge,
26 soru: TR, EN, diller arası, OCR benzeri):

| Mod | recall | hit@1 | sn/soru |
| --- | --- | --- | --- |
| keyword | 0,81 | 0,77 | ~0 |
| keyword + rerank | 0,81 | 0,77 | 0,36 |
| adaptive (işin yaptığı) | **0,96** | **0,92** | 11,9 (PLAN %42'sinde gerekti) |

Kaçan tek soru, PLAN çağrısının reasoning bütçesini bitirip boş dönmesi.

```powershell
# Model gerektirmeyen modlar
docker compose exec backend python -m reliability_harness.knowledge_eval --modes keyword,rerank
# Gerçek belgelerle (PDF'ler Docling'den geçer)
docker compose exec backend python -m reliability_harness.knowledge_eval --docs /data/eval-docs --cases /data/eval-cases.json
```

Sentetik belgeler gerçeği temsil etmez. **Semantik (embedding) arama
eklenip eklenmeyeceğine gerçek belgelerle bu eval karar vermeli.**

## Eşzamanlılık

Reranker tek model örneği, tek kilit (`_INFER_LOCK`). Web SELECT turu kilidi
~1,2 sn tutar. Belge sıralaması 8'lik partilerle yapılır, partiler arasında
kilit bırakılır; `RANK_BUDGET_SECONDS` (6 sn, deep 10) sonrası kalanlar
anahtar sözcük sırasında kalır. 0,2 sn'den uzun kilit beklemeleri timing
log'a yazılır (`reranker lock wait`).

## API

| Metod | Yol | Ne |
| --- | --- | --- |
| GET | `/workspaces/{id}/documents` | Kütüphane + indeks durumu (`indexed`, `pending`, `missing`, `skipped`, `failed`) |
| POST | `/workspaces/{id}/documents` | Doğrudan kütüphaneye yükle (multipart `files`) |
| DELETE | `/workspaces/{id}/documents/{file_id}` | Kütüphaneden çıkar (yükleme ve sohbetler kalır) |
| POST | `/workspaces/{id}/documents/{file_id}/reindex` | Yeniden indeksle |
| GET | `/workspaces/{id}/knowledge/search?q=` | Ham arama: anahtar sözcük + reranker skorları, model çağrısı yok |

## Davranış kararları

- Sohbet silinince dosyaları kütüphanede **kalır**; kütüphaneden ayrıca
  çıkarılır. Workspace silinince kütüphanesi de gider; başka workspace'te
  olmayan dosyaların pasajları indeksten silinir.
- ZIP ve CSV indekslenmez (`skipped`): tablo sorusu satırları ister, üç pasaj
  değil.
- Belge metni güvenilmez sayılır: alıntılar `untrusted_content` ile çitlenir,
  PLAN yalnız kullanıcının isteğini görür, aşağıya brief gider.
- Atıflar dosya + sayfa olarak yazılır; PDF'i o sayfada açan bağlantı yok.
  `/uploads/{id}` bilerek `attachment` + `sandbox` CSP ile sunuluyor.
- `AGENT_EXECUTOR=camel` yolunda Knowledge Agent yoktur; bayrak sessizce düşer.

## Bilinen açıklar

- Bütün belgeyi özetleme 60.000 karakter sınırında kalıyor (bu özellik
  getirme yapar, özet değil).
- PLAN gecikmesi model sağlayıcısına bağlı; diğer ajanların PLAN
  çağrılarıyla aynı sorun.
- Eval sentetik belgelerle; gerçek, taranmış belgelerle ölçülmedi.
