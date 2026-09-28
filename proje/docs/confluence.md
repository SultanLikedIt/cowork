# Confluence okuma ve onaylı yazma

Ajanın şirket Confluence'ında **yalnız izin verilen space'lerde** sayfa arayıp
okumasını ve cevaba kanıt olarak katmasını sağlar. "İzin politikası ne diyor",
"deploy runbook'unu özetle", "ENG'de bu hafta güncellenen sayfalar",
"onboarding adımlarını Excel yap" gibi istekler bu yolu kullanır.

**Varsayılan olarak yalnız okur.** `CONFLUENCE_WRITE_ENABLED=true` ile iki
değişiklik açılır — **yeni sayfa** ve **sayfaya yorum** — ikisi de yalnız izinli
space'lerde ve arayüzde **onay kartı** ile: Confluence'a yalnız kullanıcı
Onayla'ya bastığında yazılır. Mevcut bir sayfa düzenlenmez, taşınmaz, silinmez.
Ayrıntı: [Yazma (onaylı)](#yazma-onaylı).

---

## Ne yapabilir

| İstek türü | Örnek | Ne olur |
| --- | --- | --- |
| **İçerik sorusu** | "izin politikası ne diyor" | Arama → en ilgili sayfalar açılır → sayfa adı verilerek özet |
| **Liste** | "ENG'de son güncellenen sayfalar" | Sayfa tablosu (başlık, space, tarih, değiştiren); sayfalar açılmaz |
| **Bağlantı** | `https://site.atlassian.net/wiki/spaces/ENG/pages/123/...` | O sayfa doğrudan okunur (space izinliyse) |
| **Dosyaya çevirme** | "onboarding adımlarını Excel yap" | Confluence Agent okur, Document Agent dosyayı yazar |

Server/DC bağlantıları da tanınır: `.../pages/viewpage.action?pageId=123` ve
`.../display/SPACE/Sayfa+Adi`. Yalnız yapılandırılan Confluence adresindeki
bağlantılar okunur; başka bir sunucudaki bağlantı yok sayılır.

---

## Kurulum

Confluence **Jira hesabını kullanır**: Atlassian Cloud'da aynı e-posta ve API
token her iki ürüne de açılır, Confluence adresi de `JIRA_URL + /wiki` olarak
kendiliğinden bulunur. Jira zaten kuruluysa yalnız iki satır yeter:

```env
CONFLUENCE_ENABLED=true
CONFLUENCE_SPACES=ENG,HR
```

`CONFLUENCE_SPACES` **zorunludur**. Ajan yalnız bu space'lerde arar ve okur;
liste boşsa özellik kapalı sayılır. Space anahtarı Confluence'ta space'in
adresinde görünür (`/wiki/spaces/ENG/...` → `ENG`).

### Confluence Jira'dan ayrı bir sunucudaysa (Server / Data Center)

```env
CONFLUENCE_URL=https://confluence.sirket.com
CONFLUENCE_PERSONAL_TOKEN=...      # Confluence profil > Personal Access Tokens
```

Kullanıcı adı ya da token Jira'dakinden farklıysa `CONFLUENCE_USERNAME`,
`CONFLUENCE_API_TOKEN` ile ayrıca verilebilir. Her `CONFLUENCE_*` değeri boşsa
`JIRA_*` karşılığı kullanılır.

### Doğrula

```powershell
docker compose up -d backend
curl http://127.0.0.1:8001/integrations/confluence/status
```

```json
{"ok": true, "user": "Ad Soyad", "spaces": ["ENG", "HR"], "detail": "Bağlı (Ad Soyad); izinli space'ler: ENG, HR."}
```

Ayar değiştirdikten hemen sonra 60 saniyelik önbelleği atlamak için:
`curl -X POST http://127.0.0.1:8001/integrations/confluence/recheck`

---

## Nasıl çalışır

`backend/confluence_controller.py` sabit fazlar yürütür (Jira ile aynı yapı):

1. **PLAN**: yalnız kullanıcının cümlesinden tipli alanlar çıkarılır (anahtar
   kelime, space, başlık, tarih aralığı, içerik mi liste mi). Model CQL
   yazmaz.
2. **SEARCH**: `confluence_query.build_cql` sabit kalıplardan CQL üretir; her
   değer tırnaklı bir değişmezdir ve **space izin listesi her sorguya eklenir**.
3. **SELECT**: çok eşleşme varsa hangi sayfaların açılacağını model seçer
   (yalnız temizlenmiş başlıkları görür).
4. **READ**: sayfa `id = X AND space in (izinli liste)` sorgusuyla okunur;
   gövde (storage XHTML) okunabilir metne çevrilir.
5. **ANSWER**: sayfalardan sayfa adı verilerek kısa özet; sonraki işçiye
   (Assistant / Document) özet + tablo + bağlantılar gider.

| Dosya | Görev |
| --- | --- |
| `confluence_client.py` | REST istemcisi: okuma tek uçtan (`/rest/api/content/search`), izin listesi denetimi, hata sınıflandırma; onaydan sonra `create_page` / `add_comment` |
| `confluence_query.py` | CQL kurma, bağlantı ayrıştırma, storage → metin |
| `confluence_controller.py` | PLAN→SEARCH→SELECT→READ→ANSWER, notlar |
| `confluence_api.py` | `/integrations/confluence/status` ve `/recheck`; onay kartının `/actions/{id}/apply` ve `/cancel` uçları |
| `confluence_actions.py` | Onaylı yazma: tanıma, öneri + doğrulama, Markdown → storage, onayda yazma |
| `integration_actions.py` | Jira ile ortak onay / vazgeç döngüsü (`integration_actions` tablosu) |

---

## Yazma (onaylı)

`.env`'de `CONFLUENCE_WRITE_ENABLED=true` (ve backend yeniden başlatılır).
Token'ın o space'te sayfa oluşturma ve yorum yetkisi olmalı. Workforce modunda:

```text
Confluence'ta SALES space'ine 'Haftalık satış toplantısı' sayfası oluştur: katılımcılar Ali ve Ayşe; kararlar: ...
'Sales' sayfasının altına 'Q4 hedefleri' adında bir sayfa aç, içinde kısa bir yapılacaklar listesi olsun
bu raporu wiki'ye yeni sayfa olarak ekle                    (önceki cevap sayfanın içeriği olur)
https://site.atlassian.net/wiki/spaces/SALES/pages/123 sayfasına yorum ekle: onaylandı
'Template - Business review' sayfasına yorum yaz: bu şablonu Q4 için kullanalım
```

Yeni sayfada **cevabın kendisi sayfanın taslağıdır**: sohbette sayfanın
içeriğini görürsünüz, altında kart çıkar (space, başlık, üst sayfa ve içeriğin
Confluence'taki görünümü). **Onayla** → sayfa / yorum oluşur, kart bağlantı
verir; **Vazgeç** → hiçbir şey yazılmaz.

Nasıl çalışır (`backend/confluence_actions.py`, Jira ile aynı yapı):

1. **Tanıma** — istek Confluence'ı / wiki'yi anıyorsa, bu Confluence'ta bir
   sayfa bağlantısı içeriyorsa, izinli bir space anahtarını ya da izinli
   space'lerdeki bir sayfanın başlığını geçiriyorsa küçük bir model çağrısı
   değişikliğin türünü söyler (`create_page` / `add_comment` / `none`).
   Ortak niyet sınıflandırıcısına dokunulmadı.
2. **Çalışma** — Assistant'a yeni sayfada "cevabın sayfanın taslağı" denir
   (verilmeyen bilgi uydurmaz); yorumda "onaya sunulacak" denir. Kod
   çalıştırılmaz.
3. **Öneri + doğrulama** — space izin listesinde mi; o başlıkta sayfa zaten
   var mı; üst sayfa ya da yorumlanacak sayfa **kullanıcının bağlantısını
   verdiği ya da başlığını yazdığı** bir sayfa mı (model sayfa uyduramaz).
   Markdown burada Confluence storage biçimine çevrilir; **ham HTML kapalı**
   olduğundan gövde bir makro ya da script taşıyamaz.
4. **Onay** — kayıttaki değişiklik aynen gönderilir; iki tık iki sayfa açamaz.
   Cloud'da API v2 (`/wiki/api/v2/pages`, `/footer-comments`), Server/DC'de
   v1 (`/rest/api/content`).

Bilerek olmayanlar: **mevcut sayfayı düzenleme** (Confluence her düzenlemede
gövdenin tamamını değiştirir; tek bir onaylı düzenleme insanların yazdığını
silebilir), taşıma, silme, ek yükleme, etiket. Zamanlanmış görevler değişiklik
önermez.

> ⚠️ Bu uygulamada giriş yoktur ve portlar tüm ağ arayüzlerinde açıktır.
> Yazma açıkken aynı ağdaki biri de öneri oluşturup onaylayabilir; yalnız
> kendi makinenizde kullanıyorsanız portları `127.0.0.1:` ile sınırlayın.

---

## Güvenlik

- **Space izin listesi üç yerde uygulanır:**
  1. Her CQL sorgusuna eklenir. İzin listesini içermeyen bir sorgu istemci tarafından hiç gönderilmez.
  2. Confluence'tan dönen sonuçlarda izinli olmayan space'lerin sayfaları atılır.
  3. Kullanıcı başka bir space'teki sayfanın bağlantısını yapıştırsa bile sayfa okunmaz; kullanıcıya nedeni söylenir.
- **Hesabın kendi yetkisi de geçerli:** token'ın göremediği bir sayfayı ajan
  da göremez. İzin listesi bunun *üstüne* ek bir daraltmadır.
- **Sayfa metni talimat değildir.** Okunan her sayfa `untrusted_content` ile
  alıntı çitine alınır; talimat benzeri metin işaretlenir. Sayfada geçen
  hiçbir adres açılmaz, hiçbir komut çalıştırılmaz.
- **Sabit host, yönlendirme yok.** İstekler yalnız yapılandırılan adrese
  gider; yönlendirme (genelde SSO sayfası) izlenmez, token başka hosta
  taşınmaz. Token log'a ve hata mesajına yazılmaz.
- Her okuma, iş kaydında `read_confluence` araç çağrısı olarak görünür
  (hangi CQL, hangi sayfalar).

---

## Sınırlar

- Ekler (attachment), yorumlar ve sayfa geçmişi okunmaz; yalnız sayfanın
  güncel gövdesi.
- Makro çıktıları (Jira tablosu, dahil edilen sayfa gibi) okunmaz; makronun
  içindeki düz metin ve kod blokları okunur.
- Okuma, Atlassian Cloud'un v1 REST API'sini (`/wiki/rest/api/content/search`)
  kullanıyor. Atlassian bu ucu kaldırırsa okumanın v2'ye taşınması gerekir
  (yazma zaten v2'de).
- Yeni sayfa arama dizinine birkaç saniye / dakika içinde düşer; hemen
  ardından "şu sayfayı özetle" denirse bulunamayabilir.
- Kısa bağlantılar (`/wiki/x/AbCd`) tanınmaz; sayfanın tam adresi
  yapıştırılmalı.

---

## Sorun giderme

| Belirti | Neden / çözüm |
| --- | --- |
| Sağ panelde Confluence Agent yok | `CONFLUENCE_ENABLED`, `CONFLUENCE_SPACES` ya da kimlik bilgisi eksik; `/integrations/confluence/status`'daki `detail` neyin eksik olduğunu söyler |
| "yönlendirdi (HTTP 302)" | Adres yanlış (Cloud'da `.../wiki` olmalı) ya da SSO token girişine izin vermiyor |
| "kimlik bilgileri reddedildi (401)" | Cloud: e-posta + id.atlassian.com API token; Server/DC: Confluence PAT |
| "adı çözümlenemedi" / "bağlanılamadı" | VPN kapalı ya da `CONFLUENCE_URL` yanlış |
| "izinli listede değil" | İstenen space `CONFLUENCE_SPACES`'ta yok; bilerek böyle, listeye eklenmeli |
