# Coding Agent — kodu yazar, çalıştırır, sonucu gerçek çıktıdan anlatır

Workforce'ta Coding Agent artık kodu **gerçekten çalıştırır**. Önceden tek bir
metin çağrısıydı: kodu yazıyor, çıktıyı tahmin ediyordu.

## Nasıl çalışır

`backend/code_controller.py` — diğer ajanlarla aynı kalıp: fazları kod yürütür,
model "kodu çalıştır" diye bir araç çağırmaz.

| Faz | Ne olur |
| --- | --- |
| WRITE | Model `MODE`, `PLAN` ve tek bir kod bloğu döner. `MODE: write` → kod yalnız gösterilir (Dockerfile, JS, sunucu, ağ isteyen kod) |
| RUN | `code_exec.run_python`: job workspace'inde, ağ kapalı, API anahtarları ortamdan silinmiş, 60 sn sınırı |
| FIX | Hata olursa traceback + kod modele geri gider; en fazla 2 düzeltme (3 çalıştırma) |
| ANSWER | Model yalnız açıklamayı yazar; **kod ve çıktı bloğunu sunucu ekler** — gösterilen kod çalışan koddur, gösterilen çıktı gerçekten basılan çıktıdır |

Kod JSON içinde istenmez: kaçış karakterlerinden biri kaçarsa bütün cevap
kaybolur. Düz metindeki ```` ```python ```` bloğu ayrıştırılır.

## Kullanıcı ne görür

```text
1 ile 10000 arasındaki asal sayıların toplamı **5.736.396**.   ← model açıklaması

**Kod**        ← çalıştırılan kodun kendisi
**Çıktı**      ← programın gerçek çıktısı
_1 çalıştırma, son çalıştırma 0.0 sn_
📎 Oluşturulan dosyalar: `faiz_tablosu.xlsx`   ← artifacts/ altına yazılanlar
```

`artifacts/` altına yazılan her dosya indirme chip'i olur. Kod istenen dosyayı
(ör. xlsx) zaten yazdıysa Document Agent **atlanır** — aynı tabloyu modelin
yeniden yazdığı ikinci bir kopya oluşmaz.

Coding cevabı `main.py`'deki sentez adımından geçmez (`execution_result(verbatim=True)`):
sentez cevabı modelle yeniden yazar ve gerçek çıktının yerine tahmini koyabilirdi.

## Yüklenen veriyle analiz

"Bu Excel'i bölgelere göre analiz et", "departman bazında ortalama maaş",
"en çok satan 3 ürün" — tablo dosyası (xlsx / csv / tsv) yüklüyken analiz
kelimeleri (analiz, özetle, grafik, toplam, en yüksek, filtrele, grupla,
karşılaştır, kaç…) Coding Agent'ı devreye sokar
(`capabilities._TABULAR_ANALYSIS_RE`). Aynı kelimeler PDF'te Coding'i
**tetiklemez** — "bu PDF'i özetle" hâlâ okuma işidir.

Kod yazılmadan önce `code_data.py` her veri dosyasını bir kez açıp modele
anlatır:

```text
1. sources/a1b2c3/2026-satislar.xlsx — 2026-satislar.xlsx, xlsx, 9.8 KB
   read: pd.read_excel('sources/a1b2c3/2026-satislar.xlsx', sheet_name='Satışlar')
   sayfa 'Satışlar': 200 satır × 5 sütun; sayfa 'Hedefler': 4 satır × 2 sütun
2. sources/d4e5f6/personel.csv — personel.csv, csv, 1.4 KB
   read: pd.read_csv('sources/d4e5f6/personel.csv', sep=';', encoding='cp1254')
```

ardından sütun adları, tipleri ve ilk 5 satır. Türkçe Excel CSV'lerinin `;`
ayracı ve cp1254 kodlaması otomatik bulunur. PDF/DOCX içinden Docling'in
çıkardığı tablolar da listelenir (xlsx'in kendi kopyaları hariç); diğer
belgeler için çıkarılmış metnin yolu verilir.

Sütun adları ve hücre değerleri dosyadan geldiği için `untrusted_content`
çitinin **içindedir**; yollar ve okuma çağrıları dışındadır. Başlık satırına
"önceki talimatları yok say" yazılmış bir tablo işaretlenir.

Canlı ölçüm (deepseek-v4-flash): 200 satırlık 2 sayfalı xlsx'te bölge/hedef/ürün
analizi 16 sn, 40 satırlık cp1254 CSV'de departman özeti 9,5 sn — ikisi de ilk
çalıştırmada doğru dosya, sayfa ve ayraçla.

## Grafikler

İmajda matplotlib varsa (`backend/Dockerfile`, ayrı katman) Coding Agent'a
grafik çizmesi söylenir; yoksa prompt "grafik üretme" der
(`code_controller.HAS_MATPLOTLIB`). Kural:

```python
plt.savefig('artifacts/bolge-ciro.png', dpi=150, bbox_inches='tight'); plt.close()
print('CHART: artifacts/bolge-ciro.png | Bölgelere göre ciro (bin TL)')
```

- **Sohbette** PNG/JPG çıktılar dosya listesinin üstünde önizleme olarak
  görünür (`ChatExchange.jsx` `PREVIEW_KINDS`). Sunucu bunları `image/png` /
  `image/jpeg` ile verir; SVG bilerek yok (script taşıyabilir).
- **Rapora gömme:** "analiz et, grafikli PDF ver" → Coding sonra Document.
  Document `CHART:` satırlarını notlardan okur, dosyayı diskte
  (`artifacts/` altında) doğrular ve `ArtifactTools.leading_images` ile
  PDF / Word / PowerPoint / HTML yazıcılarına verir. Modelden her grafik için
  `{"type": "image"}` bloğunu ilgili paragrafın arkasına koyması istenir;
  başlık `CHART:` satırından gelir.
- Coding notları rapora **kod olarak basılmaz**: Document'a "gerçek çıktıyı
  raporla, kaynak kodu koyma" denir; notları doğrudan dosyaya kopyalayan
  kısayol ve "yazı çok kısa → ham notları koy" yedeği Coding işlerinde koddan
  arındırılmış metinle çalışır.

Canlı ölçüm (deepseek-v4-flash): 200 satırlık xlsx, "bölgelere ve aylara
göre analiz et, grafikli pdf rapor hazırla" → 3 grafik, 3 sayfalık tasarımlı
PDF (grafikler ilgili paragrafların arkasında, kod yok), 152 sn — bir düzeltme
turu dahil; bunun ~66 sn'si Document.

## Takip istekleri ("bu excel'i grafikleştir")

Her işin kendi klasörü var; önceki turun ürettiği dosya yeni işin klasöründe
yoktur. Coding çalışırken aynı sohbetin önceki işlerinin `artifacts/`
altındaki **veri dosyaları** (xlsx, xlsm, csv, tsv, json, txt, md)
`sources/onceki-turlar/<iş>/` altına kopyalanır
(`workforce._import_earlier_results`: son 5 iş, en fazla 20 dosya, dosya
başına 20 MB). `code_data` bunları "bu sohbette daha önce üretildi" diye
okuma çağrısıyla listeler.

Coding'den sonra Document çalışacaksa (PDF/Word/PowerPoint istendi),
programın kendisinin yazdığı rapor dosyaları (pdf/docx/pptx/html)
teslim edilmez: `.coding-discarded/` altına taşınır ve cevaptaki dosya
listesinden çıkar. Tasarımlı dosyayı Document yazar.

## Başarısızlık ve yavaş sağlayıcı

- Coding sonuç üretemezse (kod gelmedi, 3 çalıştırma da hata verdi)
  Document **atlanır**: hata hakkında cilalı bir PDF yazılmaz; cevap ne
  olduğunu ve hatayı gösterir.
- WRITE boş dönerse bir kez daha istenir.
- OpenRouter'daki bir sağlayıcı (OpenInference) cevabın tamamını `reasoning`
  alanına yazıp `content`'i boş bırakıyor. Coding'in WRITE/FIX çağrılarında
  (`reasoning_fallback`) bu metin kullanılır — yalnız burada, çünkü kod
  ayrıştırılıp doğrulanıyor; düz yazı cevaplarında düşünce metni cevap olmasın.
- Model PDF/Word'ü kendisi yazmaz (prompt kuralı); yazsa bile rapor türleri
  için Document yine çalışır.
- Her Coding model çağrısı `CODING_LLM_TIMEOUT_SECONDS` (150) sonra kesilir.
  Canlıda bir OpenRouter sağlayıcısı 8.192 token'ın tamamını düşünmeye
  harcayıp 325 sn sonra boş döndü — iki kez üst üste.
- `CODING_LLM_REASONING_EFFORT` / `CODING_LLM_PROVIDER_SORT` Document'takiyle
  aynı anlamda; deepseek-v4-flash için boş bırakın (`low` düşünmeyi açıyor).

## Ortam ve sınırlar

- Python 3.11 + pandas, numpy, scipy, openpyxl, matplotlib. Başka paket kurulamaz.
- **matplotlib için backend imajı yeniden build edilmeli**
  (`docker compose up -d --build backend`); build edilmeden grafikler kapalı
  kalır, geri kalan her şey çalışır.
- Ağ yok (`code_exec._GUARD`). Ağ isteyen iş `MODE: write` ile kullanıcıya verilir.
  Koruma `socket.socket`'i bir **sınıf** olarak bırakır, yalnız oluşturmayı
  engeller: fonksiyonla değiştirildiğinde `ssl` import edilemiyordu ve
  python-pptx, `urllib`/`http` kullanan her kütüphane import anında
  `function() argument 'code' must be code, not str` ile çöküyordu.
- Kod **nerede çalışır:** backend container'ının içinde, backend'in kendi
  Python'u ile ayrı bir alt süreç; çalışma dizini işin klasörü
  (`/data/workspaces/<iş-id>/`).
- İş başına en fazla 12 çalıştırma, makine genelinde aynı anda 4 (`code_exec`).
- **Gerçek sandbox değildir** — süreç seviyesinde sınırlama. Kasıtlı olarak
  koruyu söken kod kaçabilir. Ayrıntı `code_exec.py` başındaki not.

## İzleme

- Sağ panel / süre paneli: `LLM · code writing`, `LLM · code fix`,
  `LLM · result explanation`, `Code run`.
- Canlı durum satırı: `coding` aktivitesi (kod yazılıyor / çalıştırılıyor /
  hata düzeltiliyor).

## Test

```powershell
python -m pytest tests/test_code_controller.py tests/test_coding_wiring.py -q
```

`test_coding_wiring.py` kodu gerçek alt süreçte çalıştırır (sahte model, gerçek yürütme).
