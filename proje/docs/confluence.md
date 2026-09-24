# Confluence okuma

Ajanın şirket Confluence'ında **yalnız izin verilen space'lerde** sayfa arayıp
okumasını ve cevaba kanıt olarak katmasını sağlar. "İzin politikası ne diyor",
"deploy runbook'unu özetle", "ENG'de bu hafta güncellenen sayfalar",
"onboarding adımlarını Excel yap" gibi istekler bu yolu kullanır.

**Sadece okur.** Sayfa oluşturma, düzenleme, yorum yazma yoktur;
`backend/confluence_client.py` yalnız `GET` gönderir.

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
| `confluence_client.py` | REST istemcisi: yalnız GET, tek uç (`/rest/api/content/search`), izin listesi denetimi, hata sınıflandırma |
| `confluence_query.py` | CQL kurma, bağlantı ayrıştırma, storage → metin |
| `confluence_controller.py` | PLAN→SEARCH→SELECT→READ→ANSWER, notlar |
| `confluence_api.py` | `/integrations/confluence/status` ve `/recheck` |

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
- Atlassian Cloud'un v1 REST API'si (`/wiki/rest/api/content/search`)
  kullanılıyor. Atlassian bu ucu kaldırırsa istemcinin v2'ye taşınması
  gerekir.
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
