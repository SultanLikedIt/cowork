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

## Nasıl çalışır

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

### RAG mı?

Evet, ama vektör veritabanı yok. Arama **sözcüksel** (SQLite FTS5, trigram):
iç eşleşme Türkçe ekleri karşılar ("sözleşme" → "sözleşmesinde"). Uzun sorgu
sözcükleri 5 ve 7 karakterlik iki önekle aranır ("harcandı" → "harcadığını").
Anlam düzeyindeki sıralamayı zaten uygulamada olan **BGE cross-encoder**
(`reranker.py`) yapar; çok dilli olduğu için İngilizce soru Türkçe pasajı
bulabilir. Soru dili ile belge dili farklıysa **PLAN** çağrısı TR/EN sorgu
varyantları yazar.

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
