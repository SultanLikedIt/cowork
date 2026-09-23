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

## Ortam ve sınırlar

- Python 3.11 + pandas, numpy, scipy, openpyxl. Başka paket kurulamaz.
- Ağ yok (`code_exec._GUARD`). Ağ isteyen iş `MODE: write` ile kullanıcıya verilir.
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
