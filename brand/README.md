# Kurumsal şablon

Ajanın ürettiği her dosya bu klasördeki markayı giyer: PDF ve Word raporu,
PowerPoint, HTML sunum ve Excel başlık satırı.

| Dosya | Ne |
| --- | --- |
| `brand.json` | Şirket adı, renkler, yazı tipi, logo dosyasının adı |
| `logo.png` | Logo (PNG veya JPEG, en fazla 2 MB). Word ve PowerPoint SVG almaz |

## brand.json

```json
{
  "name": "",
  "primary": "#001489",
  "secondary": "#DA291C",
  "font": "",
  "logo": "logo.png"
}
```

| Alan | Etkisi |
| --- | --- |
| `primary` | Ana renk: başlıklar, tablo başlıkları, bağlantılar, slayt vurguları, grafik ilk seri. Şu an PANTONE Reflex Blue C (`#001489`) |
| `secondary` | İkinci renk: kapak çizgisi, "RAPOR" / "SUNUM" etiketi, alt başlıklar, grafik ikinci seri. Şu an PANTONE 485 C (`#DA291C`) |
| `name` | Doluysa sayfa altına başlığın önüne yazılır ("Şirket · Rapor başlığı"). Boşsa yalnız başlık |
| `font` | Word ve PDF'in yazı tipi. Boşsa Calibri (Word) / Liberation Sans (PDF). PDF'te yazı tipi container'da yüklü değilse yedeğe düşer. PowerPoint her zaman Calibri kalır: slayta sığdırma hesabı Calibri'nin ölçüleriyle yapılıyor |
| `logo` | Bu klasördeki logo dosyasının adı. Dosya yoksa belgeler logosuz çıkar |

Pantone renklerinin ekran karşılıkları Pantone'un yayınladığı sRGB
değerleridir. Baskıda birebir tutması için matbaanın kendi profilini kullanın.

## Değişiklik nasıl uygulanır

Klasör backend container'ına salt okunur bağlıdır (`docker-compose.yml`,
`./brand:/app/brand:ro`). Renk veya logo değişince yeniden build gerekmez,
yalnız yeniden başlatma yeterli:

```powershell
docker compose restart backend
```

Markayı kapatmak için `.env`'ye `BRAND_DIR=` (boş) yazıp backend'i yeniden
başlatın; belgeler yerleşik görünüme döner. `brand.json` bozuksa belge üretimi
durmaz: backend log'a bir uyarı yazar ve yerleşik görünümü kullanır.
