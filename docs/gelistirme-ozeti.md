# agent-webapp — Geliştirme Özeti (yapay zekâya verilecek sunum kaynağı)

Bu dosya, devraldığım projeye **ilk günden bugüne** yaptığım geliştirmeleri
bir yapay zekâya (Claude vb.) **parça parça** aktarıp ondan **sunum metni**
hazırlatmak için yazıldı. Bilgiler git geçmişinden, kodun kendisinden ve
repodaki ölçüm belgelerinden çıkarıldı; her rakamın kaynağı en sondaki
"Metrik kaynakları" tablosunda.

**Nasıl kullanılır:**

1. Önce **PART 0**'ı yapıştır (talimat). Model "hazırım" desin.
2. Sonra PART 1, PART 2A … 2H, PART 3'ü sırayla yapıştır. Her parçanın
   başında modelin o parçayla ne yapacağı yazıyor; model her parçadan sonra
   yalnız kısa bir onay verir.
3. En sonda **PART 4**'ü yapıştır: model tüm sunumu o anda yazar.

Tek seferde de verilebilir; parçalı yapı, uzun sohbetlerde bilginin
kaybolmaması için.

> Durum tarihi: **24 Eylül 2026**. Devralma: **11 Eylül 2026**.

---

## PART 0 — Talimat (önce bunu ver)

> Bu mesajdan sonra sana bir yazılım projesi hakkında bilgi parçaları
> vereceğim (PART 1, PART 2A–2H, PART 3). Her parçayı oku, **yalnız "PART X
> alındı" diye kısa onay ver**, özet veya sunum yazma. PART 4'ü verdiğimde
> hepsini kullanarak **Türkçe bir sunum metni** hazırlayacaksın.
>
> Sunumun kuralları:
>
> 1. **Yapı üç ana bölüm:** (1) Devraldığım proje, (2) Yaptığım geliştirmeler
>    — metriklerle, (3) Gelecek planları. Başa kısa bir "proje nedir", sona
>    kısa bir kapanış eklenebilir.
> 2. **Hedef kitle:** teknik olmayan veya yarı teknik yöneticiler ve ekip
>    arkadaşları. Her geliştirmede önce **"kullanıcıya ne kazandırdı"**, sonra
>    gerekirse tek satır teknik not.
> 3. **Metrikler:** yalnız sana verilen rakamları kullan; yuvarlayabilirsin
>    ama yeni rakam uydurma. **Commit sayısı, PR sayısı, satır sayısı gibi
>    geliştirme istatistikleri kullanma** — metrikler ürünün etkisini
>    anlatmalı (süre, doğruluk, kapsam, kalite).
> 4. **Atıf kuralı:** PART 1'deki "devralınan temel"i benim geliştirmem gibi
>    anlatma. Onu ben yapmadım; ben üzerine inşa ettim.
> 5. **Dürüstlük:** her bölümde bilinen sınırlar varsa kısaca söyle. Rakamlar
>    bu makinede, belirli test setlerinde ölçüldü; öyle sun.
> 6. **Biçim:** her başlık bir slayta sığacak uzunlukta (en fazla 5–6 madde),
>    uzun paragraf yok. Uygun yerlerde tablo kullan (özellikle önce/sonra).

---

## PART 1 — Devraldığım proje

> Model için: bu parçayı "devralınan temel" olarak kaydet. Sunumda
> "Devraldığım proje" bölümünün kaynağı budur; buradakileri geliştirmem gibi
> anlatma.

**Proje nedir:** agent-webapp, tarayıcıdan konuşulan bir yapay zekâ ajan
uygulaması. Kullanıcı soru sorar; sistem sohbet eder, web'de araştırır,
yüklenen belgeyi okur ya da Word/PDF/PowerPoint/Excel/HTML dosyası üretir.
İki mod: **Single** (tek, hızlı sohbet) ve **Workforce** (uzman ajanların
sırayla çalıştığı mod). React arayüz + FastAPI (Python) + SQLite, hepsi
Docker'da; model OpenAI uyumlu herhangi bir LLM (şu an OpenRouter üzerinden
deepseek-v4-flash). Türkçe odaklı. Kimlik doğrulama yok, iç ağ/localhost için.

**Devraldığımda zaten vardı:**

- Single / Workforce modları, arayüz, sunucu, veritabanı
- CAMEL → LangGraph çift yollu çalıştırıcı
- 4 ajan: Research, Document, Coding, Assistant
- Web araştırma akışı (planla → ara → seç → aç → cevapla), SearXNG + AgentSearch
- Yapay zekâ ile niyet sınıflandırma (dosya istendi mi, web gerekli mi…)
- Docling ile belge okuma
- Markdown'dan PDF (reportlab) ve Node tabanlı Carbone şablonlarıyla DOCX/PPTX
- "Bilgisayarımda aç" yerel köprüsü, sohbetteki kod bloğu için Run düğmesi
- 7 hazır "skill" (düşünme biçimi), 624 otomatik test

**Devraldığımda eksik veya kırık olanlar:**

- **Coding Agent kodu çalıştırmıyordu:** kodu yazıp çıktısını *tahmin eden*
  tek bir metin çağrısıydı.
- **OCR kapalıydı:** taranmış PDF boş okunuyor, görsel yükleme reddediliyordu.
- **Kritik hatalar:** belge ekli her Workforce işi 0,02 saniyede çöküyordu
  ("bu PDF'i özetle" hiç çalışmıyordu); temiz bir Docker kurulumunda her
  dosya yüklemesi hata (422) veriyordu.
- Kurum içi hiçbir veri kaynağına bağlantı yoktu (e-posta, Jira, GitHub, forum).
- Zamanlanmış görev, e-posta teslimi, grafik, belge kütüphanesi, süre/performans
  paneli, prompt-injection koruması ve yönlendirme doğruluğunu ölçen bir test
  seti yoktu.
- Belgeler tasarımsızdı (düz PDF), PowerPoint araştırma metninin slaytlara
  kesilip biçilmesiydi.

---

## PART 2A — Geliştirmeler: kurumsal veri kaynakları ve yeni ajanlar

> Model için: bu ve sonraki PART 2 parçaları "Yaptığım geliştirmeler"
> bölümünün kaynağıdır. Kaydet, kısa onay ver.

**Ajan sayısı 4 → 10.** Yeni ajanlar bilgi toplar; cevabı en sondaki ajan yazar.

- **Mail Agent (Outlook):** gelen/gönderilmiş postaları okur, ekleri indirip
  belge gibi işler. "Geçen hafta gelen teklifleri karşılaştır ve PDF ver"
  çalışır. Salt okunur: hiçbir mail silinmez, taşınmaz, okundu işaretlenmez.
- **Jira Agent:** şirket Jira'sında arar ve kayıtları okur ("bana atanan
  açık işler", "PROJ-123 ne durumda"). Cloud ve Server/Data Center. Model
  sorgu dili yazmaz, sorgular güvenli şablonlardan kurulur. VPN kapalıysa
  bunu açıkça söyler.
- **Confluence Agent:** şirket wiki'sinde yalnız izin verilen space'lerde
  arar ve sayfaları okur ("izin politikası ne diyor"). Jira hesabını kullanır;
  izin listesi her sorguya eklenir.
- **GitHub Agent:** herkese açık depoları arar; depo, dosya, issue, PR okur.
  Web araştırmasında çıkan GitHub linkleri de API'den okunur.
- **Social Agent (Reddit + Hacker News):** "bu konuda insanlar ne diyor"
  sorularına forum tartışmalarından kanıt toplar.
- **Knowledge Agent:** workspace'e yüklenen belgelerde arar (PART 2E).
- Arayüzde ajanlar marka logolu kartlarla görünür; yalnız kuruluysa.
- **Onaylı yazma (Jira + Confluence):** "KAN'da task aç", "KAN-12'ye yorum
  ekle", "KAN-7'yi Done'a çek", "wiki'de yeni sayfa aç", "şu sayfaya yorum
  yaz". Ajan değişikliği **yalnız önerir**; cevabın altında ne yazılacağını
  gösteren bir onay kartı çıkar, Jira / Confluence'a ancak kullanıcı
  **Onayla**'ya basınca yazılır. Proje, tür, durum geçişi ve sayfa önce
  Jira / Confluence'a karşı doğrulanır; kayıt anahtarı ve sayfa yalnız
  kullanıcının yazdığı olabilir. Canlı testte 3 Jira değişikliği (kayıt, yorum,
  durum) ve 4 Confluence yazması (2 sayfa, 2 yorum) onaylanıp geri okundu;
  aynı onay ikinci kez reddedildi; okuma / taslak isteklerinde kart çıkmadı
  (4/4). 43 yeni otomatik test. Varsayılan kapalı (`JIRA_WRITE_ENABLED`,
  `CONFLUENCE_WRITE_ENABLED`).
- Diğer entegrasyonlar **salt okunur** (Outlook okuma, GitHub, Reddit / HN).

---

## PART 2B — Geliştirmeler: veri analizi yapan Kod Ajanı

> Model için: kaydet, kısa onay ver. Sunumda en güçlü demo konusu budur.

**Önce:** kodu yazıp çıktıyı tahmin ediyordu. **Şimdi:**

- **Kodu gerçekten çalıştırıyor:** yazar → çalıştırır → hata alırsa hatayı
  görüp düzeltir (en fazla 2 kez) → gerçek sonucu açıklar.
- **Gösterilen sonuç uydurma olamaz:** cevaptaki kod ve çıktıyı model değil
  sistem ekliyor; görünen kod çalışan kod, görünen çıktı gerçek çıktı.
- **Excel / CSV analizi:** "Bu Excel'i bölgelere göre analiz et",
  "departman bazında ortalama maaş". Dosyanın sayfaları, sütunları, Türkçe
  CSV'lerin ayracı ve kodlaması önceden çıkarılıp ajana veriliyor.
- **Grafikler:** sohbette önizleme; "grafikli PDF rapor" istenince tasarımlı
  rapora ilgili paragrafın arkasına gömülüyor.
- **Hesaplamalar doğru ajana gidiyor:** faiz/kredi tabloları, toplamlar,
  istatistik artık tahmin edilmiyor, kodla hesaplanıyor.
- **Güvenli çalışma:** ağ kapalı, API anahtarları görünmez, süre sınırlı,
  yalnız o işin klasörü.

| Metrik (canlı ölçüm) | Değer |
| --- | --- |
| Basit hesap (ör. asal sayı toplamı) | ~10 sn |
| 40 satırlık Türkçe CSV'de departman özeti | 9,5 sn, ilk denemede doğru okuma |
| 200 satırlık 2 sayfalı Excel'de bölge/hedef/ürün analizi | 16 sn, ilk denemede doğru sayfa ve sütun |
| Aynı Excel → 3 grafikli, 3 sayfalık tasarımlı PDF rapor | ~2,5 dk |
| Hesaplama isteklerinin Kod Ajanı'na yönlenmesi (4 istek × 3 deneme) | %42 → **%100** |

---

## PART 2C — Geliştirmeler: otomasyon ve e-posta

> Model için: kaydet, kısa onay ver.

- **Zamanlanmış görevler:** cron, tek seferlik veya **bir sayfa değişince**
  çalışan görevler; ayrı "Scheduled" ekranı ve görev düzenleyici.
- **Görev zincirleri:** bir görev, diğeri bittikten sonra koşullu çalışır
  (başarıda / değişiklikte / her zaman); önceki görevin raporu ve dosyaları
  aktarılır.
- Bilgisayar kapalıyken kaçan çalışma açılışta bir kez telafi edilir.
- **E-posta teslimi:** sonuçlar klasik Outlook masaüstünden gönderilir (Azure
  kaydı gerekmez); gönderilemezse ~6 saat yeniden denenir.
- Sohbette "sonucu ali@sirket.com'a gönder" → Outlook'ta **taslak** açılır;
  gönderme kararı kullanıcıda.
- Alıcılar yalnız kullanıcının kendi yazdığı adreslerdir; hatalı adres
  sessizce atlanmaz, kullanıcıya söylenir.

---

## PART 2D — Geliştirmeler: belge üretimi (PDF, Word, PowerPoint)

> Model için: kaydet, kısa onay ver.

- **Yapılandırılmış içerik:** model serbest metin değil, tipli bloklar
  üretiyor (başlık, paragraf, tablo, liste, görsel).
- **Tasarımlı PDF:** kapak, içindekiler, vurgulu başlıklar, düzgün tablolar.
- **Word** PDF ile aynı tasarımda; eski Node/Carbone bağımlılığı kaldırıldı.
- **Tasarlanmış PowerPoint:** her slayt tipiyle tasarlanıyor (madde,
  istatistik kartı, tablo, grafik, iki sütun, alıntı) + konuşmacı notları;
  yazı ölçülüp kutuya sığdırılıyor, uzun tablolar dengeli bölünüyor.
  Notlarda dayanağı olmayan rakam/grafik slayta konmuyor.
- **Teslim öncesi doğrulama:** üretilen dosya ayrı bir okuyucuyla yeniden
  açılıp kontrol ediliyor (boş sayfa, eksik bölüm, taşan yazı).
- **Uzun yapıştırılan metin "brief" sayılıyor:** içindeki "PDF", "Kaynak"
  gibi kelimeler artık talimat sanılmıyor (önceden tek istekten 3 ayrı
  dosya + alakasız web araması çıkıyordu).
- **Yüklenen belgeden dosya üretimi** doğrudan o belgeden yapılıyor; gereksiz
  web araması ve belge taraması kalktı (bir örnekte ~10 dakikalık boşa iş).

---

## PART 2E — Geliştirmeler: belge okuma, OCR ve bilgi kütüphanesi

> Model için: kaydet, kısa onay ver.

**OCR (taranmış belge ve görsel okuma):** önceden taranmış PDF boş okunuyor,
görsel reddediliyordu. Şimdi Tesseract (Türkçe + İngilizce) ve GPU varsa
PaddleOCR-VL; servis yoksa otomatik yedeğe düşüyor. Belge okuma modelleri
imaja gömüldü → ilk yüklemedeki ~45 sn bekleme kalktı.

| Belge (sentetik Türkçe tarama) | PaddleOCR-VL | Tesseract |
| --- | --- | --- |
| Tablolu fatura taraması | %92 kelime, tablo 18/20 hücre | %63 kelime, tablo 2/20 |
| Karışık PDF (dijital + taranmış) | %97, tablo 19/20 | %81, tablo 0/20 |

Bilinen sınır: PaddleOCR-VL Türkçe "ı→i", "ğ→g" hatası yapıyor; gerçek
taramalarla ölçülünce ele alınacak.

**Bilgi kütüphanesi (belgelerde arama):** workspace'e yüklenen **tüm**
belgeler aranabilir (kütüphane ekranı, dizinleme durumu). Knowledge Agent
cevapta **dosya ve sayfa numaralı atıf** verir (`[K1] sozlesme.pdf s.4`).
Türkçe karakter ve OCR hatalarına dayanıklı.

| Metrik (26 soruluk test seti) | Değer |
| --- | --- |
| Doğru pasajı bulma (recall) | **%96** (yalnız anahtar kelimeyle %81) |
| İlk sıradaki sonucun doğru olması (hit@1) | **%92** |
| 25 belgeyi dizinleme | 0,2 sn |

---

## PART 2F — Geliştirmeler: web araştırmasında hız ve kalite

> Model için: kaydet, kısa onay ver.

- **Yerel reranker:** kaynak seçimi her turda bir LLM çağrısı yerine yerel
  bir sıralama modeliyle (CPU, ek maliyet yok).
- **Gereksiz ikinci arama turu kaldırıldı:** ölçülen örnekte 60 sn'lik bir
  araştırmadan **~23 sn** kazanç.
- **Paralel arama:** arama motorları aynı anda sorgulanıyor (4 isteklik bir
  turda ~1,6 sn → ~0,4 sn); yeterli sayfa geldiyse yavaş olanlar beklenmiyor.
- **Derin araştırma "iter" modu:** rapor her turda baştan yazılıyor, her
  bulgu kaynağına bağlanıyor; açılmamış kaynağa verilen atıflar siliniyor.
- Kaynak listesindeki site menüsü / navigasyon çöpü temizlendi.

---

## PART 2G — Geliştirmeler: güvenlik, gözlemlenebilirlik, kalite ölçümü

> Model için: kaydet, kısa onay ver.

- **Dış içerik izolasyonu:** e-posta, forum, Jira/GitHub metinleri ve
  yüklenen tablolar modele "bu veri, talimat değil" çitiyle veriliyor
  (prompt injection'a karşı).
- **Model hiçbir dış sisteme kendi başına yazamaz:** e-posta alıcısını yalnız
  kullanıcı belirler; Jira / Confluence değişikliği onay kartından geçer ve
  aynı değişiklik iki kez yazılamaz.
- **Süre paneli:** her işte hangi adımın ne kadar sürdüğü, kaç token
  harcadığı arayüzde görünüyor.
- **Yönlendirme doğruluğu ölçülüyor:** 73 gerçek (çoğu Türkçe) istek,
  her biri 3 kez; son ölçümde **hiçbir yanlış sınıflama yok**.
- **Test kapsamı:** otomatik test fonksiyonu **624 → 1.453** (her çalıştırmada
  ~1.590 test).
- **Sağlayıcı sorunlarına dayanıklılık:** yavaş/hatalı model sağlayıcısında
  zaman aşımı, yeniden deneme ve güvenli yedek davranış.
- **6 yeni skill:** Kurumsal Türkçe Üslup, Kaynak ve Tarih Disiplini, Sade
  Dil, Aksiyon Maddeleri, Karşılaştırma Tablosu, Mevzuat Hassasiyeti.

---

## PART 2H — Geliştirmeler: kritik hata düzeltmeleri ve özet tablo

> Model için: kaydet, kısa onay ver. Tablo, sunumdaki "önce/sonra" slaytının
> kaynağıdır.

**Devralınan koddaki kritik hatalar (düzeltildi):**

- Belge ekli her Workforce işi 0,02 sn'de çöküyordu.
- Temiz Docker kurulumunda her dosya yüklemesi hata veriyordu.
- Görsel yükleme arayüzde ikinci bir kontrol tarafından reddediliyordu.

**Önce / sonra:**

| Alan | Devraldığımda | Şimdi |
| --- | --- | --- |
| Veri kaynakları | Web + yüklenen dosya | + Outlook, Jira, Confluence, GitHub, Reddit, Hacker News, belge kütüphanesi |
| Ajanlar | 4 | 10 |
| Kurum sistemlerine yazma | Yok | Jira: kayıt / yorum / durum · Confluence: sayfa / yorum — onay kartıyla |
| Kod ajanı | Çıktıyı tahmin ediyordu | Çalıştırıyor, düzeltiyor, gerçek sonucu gösteriyor |
| Excel/CSV analizi | Yalnız önizleme metni | Tüm dosya üzerinde hesaplama + grafik |
| Taranmış belge | Okunamıyordu | OCR; tablolu faturada %92 kelime doğruluğu |
| Belgelerde arama | Yok | %96 bulma, sayfa numaralı atıf |
| Otomasyon | Yok | Zamanlanmış görevler, zincirler, e-posta teslimi |
| Belge çıktısı | Düz PDF, şablon Word | Tasarımlı PDF/Word/PowerPoint, teslim öncesi doğrulama |
| Web araştırma | Gereksiz tur, sıralı arama | Örnekte ~23 sn kazanç, paralel arama |
| Güvenlik | Temel SSRF kontrolü | Dış içerik izolasyonu, yazma yalnız kullanıcı onayıyla |
| Ölçüm | Birim testler | Süre paneli, yönlendirme ve arama doğruluk setleri |
| Test | 624 | 1.453 |

---

## PART 3 — Gelecek planları

> Model için: kaydet, kısa onay ver. Sunumun "Gelecek planları" bölümünün
> kaynağıdır; önceliklerle birlikte anlat.

**Kısa vade (güvenilirlik):**

- **Model sağlayıcı seçimi:** OpenRouter bazı çağrıları yavaş ya da hatalı
  bir sağlayıcıya yönlendiriyor (cevabı yanlış alana yazıp boş dönüyor).
  Kod ajanı ve niyet sınıflandırıcıda önlem var; araştırma ve belge ajanları
  için de sağlayıcı dışlama / yeniden deneme.
- Kod çalıştırmayı **ayrı, ağsız bir container'a** taşımak (tam sandbox);
  şu an süreç seviyesinde sınırlı.
- Bilinen açıklar: bazı "yapamam" cevaplarının süzgeçten geçmesi; iki Word
  belgesinde "içerik A, biçim B" ayrımının kırılgan olması.

**Orta vade (kapsam):**

- **Kimlik doğrulama ve kullanıcı hesapları** — sistemin iç ağ dışına
  açılabilmesi için ön koşul.
- Onaylı yazmayı genişletmek: Jira'da atama ve alan düzenleme, Confluence'ta
  mevcut sayfaya bölüm ekleme (onay kartı altyapısı hazır).
- **Outlook takvim** entegrasyonu.
- GitHub'da özel depolar ve kod araması.

**Ölçülerek karar verilecekler:**

- Gerçek şirket belgeleriyle arama testi; kaçan cevap çıkarsa vektör araması
  eklemek.
- Gerçek taramalarla OCR Türkçe karakter düzeltmesi.

---

## PART 4 — Sunumu yaz (en son bunu ver)

> Tüm parçaları aldın. Şimdi PART 0'daki kurallarla sunum metnini yaz.
>
> Önerilen akış (her madde bir slayt, gerekirse ikiye bölebilirsin):
>
> 1. Proje nedir
> 2. **Devraldığım proje:** neler vardı, neler eksik/kırıktı
> 3. **Yaptığım geliştirmeler — genel bakış:** önce/sonra tablosu
> 4. Kurumsal veri kaynakları ve yeni ajanlar
> 5. Veri analizi yapan Kod Ajanı (Excel → grafikli rapor) — metrik tablosuyla
> 6. Bilgi kütüphanesi ve OCR — metriklerle
> 7. Otomasyon ve e-posta
> 8. Belge üretimi: PDF, Word, PowerPoint
> 9. Web araştırma hızı ve kalite
> 10. Güvenlik, ölçüm ve kalite güvencesi
> 11. Düzeltilen kritik hatalar
> 12. **Gelecek planları:** kısa vade / orta vade / ölçülecekler
> 13. Kapanış (bir cümlelik özet)
>
> Hatırlatma: commit/PR/satır sayısı kullanma; yalnız verilen metrikleri kullan.

---

## Metrik kaynakları (sunuma girmez; doğrulama için)

| Metrik | Kaynak |
| --- | --- |
| Kod Ajanı süreleri, grafikli rapor | [`coding-agent.md`](coding-agent.md) (canlı ölçüm, 2026-09-23) |
| Hesaplama yönlendirmesi %42 → %100 | `reliability_harness/intent_cases.json` `compute` etiketi, PR #15 (4 hesaplama örneği × 3; kontroller hariç) |
| Yönlendirme seti, yanlış sınıflama yok | `reliability_harness/intent_eval.py`, 73 örnek × 3 |
| OCR doğruluk tablosu | [`document-ingestion.md`](document-ingestion.md) "Measured quality" |
| Bilgi kütüphanesi recall/hit@1 | [`knowledge.md`](knowledge.md) "Ölçümler" |
| ~23 sn kazanç | `web_controller.py` yorumu (60,4 sn'lik altın fiyatı araştırması) |
| Paralel arama 1,6 → 0,4 sn | `web_search.py` yorumu |
| Test sayıları | `tests/` altındaki test fonksiyonları, devralınan sürüm ile bugünkü `main` |

Bu dosya, önemli bir geliştirme birleştikten sonra güncellenmeli
(PART 2 ilgili bölüm + PART 2H tablosu + PART 3).
