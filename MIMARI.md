# MIMARI.md

## 1. Yüksek seviye mimari

```text
┌───────────────────────────────────────────┐
│              React Frontend               │
│ Projects • Inventory • Jobs • AI Builder  │
└───────────────────┬───────────────────────┘
                    │ HTTP + SSE
┌───────────────────▼───────────────────────┐
│               FastAPI Backend             │
│                                           │
│ API • Auth • Projects • Jobs • AI • Risk  │
└──────┬─────────────┬─────────────┬────────┘
       │             │             │
       ▼             ▼             ▼
 SQLite/Postgres  Ansible Runner   LLM Provider
       │             │             │
       │             ▼             │
       │        SSH ile node'lar    │
       │                           │
       └──── Artifact ve metadata ─┘
```

## 2. Ana bileşenler

### Frontend

Görevleri:

- Project/inventory ekranları
- Playbook seçimi
- Job launch formu
- Canlı job event görünümü
- Job geçmişi
- AI chat ve artifact preview
- Validation sonuçları
- Diff ve approval ekranı

Frontend execution komutu üretmez. Backend'e yapılandırılmış istek gönderir.

#### Frontend katmanları (T-104, T-203, T-204C)

```text
pages/                Route bileşenleri; yalnızca durum dallanması ve düzen
features/<alan>/
  api.ts              Endpoint çağrıları; URL kurgusu yalnızca burada
  types.ts            İstek/cevap tipleri
  queryKeys.ts        Query key hiyerarşisi
  hooks.ts            useQuery/useMutation sarmalayıcıları + invalidation
  errorMessages.ts    Hata kodu → kullanıcı metni
  components/         Alan bileşenleri
lib/apiClient.ts      fetch sınırı ve `ApiError` dönüşümü
components/           Alan bağımsız bileşenler
```

Sayfa bileşenleri doğrudan `fetch` çağırmaz ve `queryClient` ile uğraşmaz;
cache invalidation kuralları `hooks.ts` içinde tek yerde tanımlıdır.

Hata zarfının `details` alanı kullanıcıya **ham JSON olarak gösterilmez**.
`errorMessages.ts` yalnızca bilinen alanları (`project_id`, `is_active`,
`reason`) tip korumasından geçirip metne veya bağlantıya çevirir.

Sunucudaki mutlak path'ler arayüzde birleştirilmez: playbook yolları API'nin
döndürdüğü göreli hâliyle gösterilir.

Her domain kendi `errorMessages.ts` dosyasını taşır. Project ve inventory hata
kodları örtüşmez ve aynı kod farklı domainlerde farklı eylem gerektirir; tek bir
ortak eşleyici bu farkı silerdi. Ortak olan `StatusMessage` gibi alan bağımsız
bileşenler `components/` altındadır.

**Inventory ekranları (T-203).** İki route eklenir:

```text
/inventories              Kayıt listesi
/inventories/{id}         Metadata + grup/host görünümü
```

Detay sayfası iki bağımsız sorgu kullanır: kayıt metadata'sı
(`GET /api/inventories/{id}`) ve içerik (`GET /api/inventories/{id}/hosts`).
Ayrım bilinçlidir — içerik okuma sunucuda ayrı bir süreç çalıştırır ve
başarısız olabilir; dosya ayrıştırılamasa bile kaydın kendisi görünür kalır.
İçerik sorgusunda otomatik retry **kapalıdır**: ayrıştırılamayan bir dosya için
sessizce üç kez daha süreç başlatmanın kullanıcıya faydası yoktur.

Host değişkenleri backend'den **maskelenmiş** gelir (GUVENLIK.md bölüm 9) ve
arayüz onları geldiği gibi gösterir; maskeyi açmaya veya bir değer üretmeye
çalışmaz. Maskeli değer yalnızca maskenin kendisiyle ve görünür bir "gizlendi"
etiketiyle basılır. İç içe yapılar da backend tarafından özyinelemeli
maskelendiği için JSON olarak gösterilmeleri güvenlidir.

**Ping arayüzü (T-204C).** Inventory detay sayfasına ayrı bir "Erişilebilirlik
testi" bölümü eklenir. Bölüm inventory metadata'sına bağlıdır ve içerik
sorgusundan **bağımsızdır**: dosya ayrıştırılamasa bile ping denenebilir.

```text
features/inventories/components/
  PingSection.tsx      Durum makinesi, senkron kilit, token ömrü
  PingPlanPanel.tsx    Onay planının güvenli alanları
  PingResultPanel.tsx  Terminal sonuç: özet + host tablosu
  PingErrorPanel.tsx   Ping hata bildirimi
```

`InventoryDetailPage` tek büyük durum bileşenine dönüştürülmez; yalnızca
`PingSection`'ı inventory kimliğiyle **key'leyerek** yerleştirir. Kimlik
değişince bileşen yeniden kurulur, böylece bir inventory'nin planı, onayı veya
sonucu bir sonraki ekrana taşınmaz.

**UI akışı iki aşamalıdır ve backend sözleşmesini birebir izler:**

```text
idle → previewing → preview_ready ─┬─ (Onayla) → confirming → result
                                   └─ (Vazgeç)  → canceling  → idle
herhangi bir arıza → error → (Yeni önizleme oluştur) → idle
```

Durumlar ayrık bir birleşimdir (discriminated union); çakışan iki eylemin aynı
anda "açık" olması tip düzeyinde imkânsızdır. `confirming` sırasında plan
okunabilir kalır ama Onayla/Vazgeç/Önizle kontrollerinin hiçbiri render
edilmez. Onay görünür olduğunda odak onay butonuna taşınır.

`disabled` tek başına yeterli değildir: React'in bir sonraki render'ını bekler
ve hızlı çift tıklama arasındaki pencerede ikinci bir istek çıkabilir. Bu
yüzden her handler ilk iş olarak **senkron** bir `inFlightRef` kilidi alır.

**Ping action'ları bilinçli olarak TanStack mutation'ı değildir.** `useMutation`
her çağrıyı `MutationCache`'e yazar: `variables` istek gövdesini, `data` cevabı
taşır — ping akışında ikisi de onay token'ıdır. `reset()` bu kaydı silmez,
yalnızca observer'ı ayırır ve varsayılan garbage collection süresini başlatır;
ayrıca istek sürerken bileşen unmount olursa `mutate()` callback'leri hiç
çalışmayabilir ve `reset()` güvencesi tümüyle devre dışı kalır. Bu yüzden
`hooks.ts` ping için durum tutmayan imperative bir yüzey (`usePingActions`)
verir. Çözüm "cache'i sonradan temizlemek" değil, token'ı o cache'e **hiç
sokmamaktır**.

Token'ın ömrü buna göre dardır: preview cevabından alınıp private bir `useRef`
içinde tutulur, confirm/cancel isteği gönderilmeden **önce** ref temizlenir ve
değer yalnızca HTTP gövdesinde kısa süre yaşar. Token DOM veya görünür metne,
URL/query/hash'e, query veya mutation cache'ine, `localStorage`/`sessionStorage`
alanına ve log satırlarına **girmez**. Unmount'ta ref temizlenir; unmount'tan
sonra çözülen bir istek ne ref'e ne state'e yazar. React cleanup'ında
güvenilmez bir fire-and-forget iptal isteği başlatılmaz — kullanılmayan preview
state'i sunucuda TTL ile temizlenir.

**Hata metinleri aşama duyarlıdır.** `describePingError(error, stage)` saf bir
fonksiyondur ve `preview | confirm | cancel` bilgisini alır, çünkü aynı hata
kodu adıma göre farklı anlam taşır. `fetch` arızası isteğin gönderilmediğini
değil **cevabın alınamadığını** gösterir; bu yüzden hiçbir adımda "istek
ulaşmadı" denmez. Preview adımı yalnızca yapısal garantiyi verir (bu adım SSH
kurmaz, ping çalıştırmaz) ama planın oluşup oluşmadığını iddia etmez. Cancel
temizliğin tamamlandığını iddia etmez. Confirm ise ping'in başlamış, hatta
tamamlanmış olabileceğini açıkça söyler.

Confirm arızasında **otomatik tekrar yoktur ve aynı token'la yeniden deneme
eylemi sunulmaz**: token en başta claim edildiği için başarısız bir istek de
onu tüketir (ADR-019 Karar 6). Kullanıcının tek güvenli yolu boş forma dönüp
yeni bir önizleme oluşturmaktır ve bu ayrı bir karardır. `PingErrorNotice` bunu
`requiresNewPreview` ile taşır; tek istisna `request_validation_error`'dır,
çünkü o gövde claim'den önce şema doğrulamasında elenir.

Ping `details` alanında da ham JSON gösterilmez. Yalnızca üç alan tip
korumasından geçer: `reason` (yalnız `expired` | `mismatch` | `invalid`),
`stream` (yalnız `stdout` | `stderr`) ve `job_id` (yalnız canonical küçük
harfli UUID). Yanlış tip, dizi, iç içe nesne veya bilinmeyen değer sessizce yok
sayılır ve genel mesaj kullanılır.

Terminal sonuçta `status: "failed"` bir **API hatası değildir**: iş çalışmış ve
sonucu kaydedilmiştir. Bu yüzden hata kutusu değil uyarı kutusu kullanılır,
özet ile host tablosu her iki durumda da gösterilir ve host durumları renkten
bağımsız metin etiketleriyle yazılır (Erişilebilir, Erişilemiyor, Başarısız,
Sonuç alınamadı). Host mesajları yalnız React metni olarak basılır;
`dangerouslySetInnerHTML` kullanılmaz ve `null` mesaj için yapay bir açıklama
üretilmez.

Parse hatalarında `details` ham JSON olarak **gösterilmez**. Yalnızca tip
korumasından geçen alanlar metne çevrilir:

| Alan | Koruma | Kullanım |
|---|---|---|
| `reason` | string | `inventory_path_unavailable` alt durumu |
| `stream` | yalnızca `stdout` \| `stderr` | Boyut aşımının hangi akışta olduğu |
| `parser_message` | string, boş değil | Backend'in temizlediği açıklama |
| `inventory_id` | integer | Kayda bağlantı |

Doğrulamadan geçemeyen bir değer (bilinmeyen `stream`, string olmayan
`parser_message`) sessizce yok sayılır ve genel mesaj kullanılır; ham değer
hiçbir koşulda ekrana yazılmaz.

### API katmanı

Görevleri:

- Request validation
- Authentication
- Authorization, ilerleyen sürüm
- Domain servislerini çağırma
- Standart hata cevapları
- SSE endpoint

Route içinde Ansible veya AI iş mantığı bulunmamalıdır.

**Request validation hatası kullanıcı girdisini geri yansıtmaz.** Pydantic hata
yapısı gönderilen ham değeri `input`, bazen de `ctx` içinde taşır; bu, "secret
hata cevabında yer almaz" kuralını (GUVENLIK.md bölüm 3) doğrudan ihlal ederdi.
Ölçülen örnek: sınırı aşan bir `preview_token` gönderildiğinde `string_too_long`
hatasının `input` alanı token'ın tamamını geri döndürüyordu. Bu yüzden merkezi
handler yalnızca `type`, `loc` ve genel `msg` alanlarını bırakır; sayıları ve
uzunlukları da sınırlıdır. Sanitizasyon tek yerdedir, route'a özel kopyası
yoktur. Standart zarf ve `request_validation_error` kodu değişmemiştir. Ham
request body loglanmaz.

### Project service

- Project köklerini yönetir
- Güvenli path resolution yapar
- Playbook keşfeder
- Project dışına çıkışı engeller
- İleride Git durumunu sağlar

### Inventory service

- Inventory path güvenliğini kontrol eder (T-201)
- Project bağını doğrular (T-201)
- INI/YAML inventory parse eder (T-202)
- Host/group önizlemesi sağlar (T-202)
- Ping onay planı üretir (T-204A)
- Onaylanmış ping'i çalıştırır ve Job'a bağlar (T-204B2)

Servis `app/services/inventories/` altındadır; project servisinden ayrı bir
domain paketidir ve ondan yalnızca "project var mı / aktif mi / kökü nerede"
sorularını sorar.

Üç modüle ayrılmıştır:

```text
service.py       Kayıt yönetimi, path güvenliği, project bağı
parser.py        Alt süreç çağrısı, çıktı sözleşmesi, normalizasyon
ping.py          Ping onay planı (preview) orkestrasyonu — T-204A
ping_confirm.py  Claim → Job → execution → artifact orkestrasyonu — T-204B2
```

`parser.py` veritabanını bilmez; `service.py` alt süreç ayrıntısını bilmez.

### Ansible sınır katmanı

`app/services/ansible/` altındaki modüller domain'den bağımsızdır ve
veritabanını bilmez:

```text
process.py             Sınırlı alt süreç: env daraltması, gerçek zamanlı çıktı
                       sınırı, timeout, terminate→kill, çıktı temizleme
host_patterns.py       Limit (host pattern) yapısal doğrulaması
destinations.py        SSH hedefi ve gösterim host adı pozitif sözleşmesi
inventory_snapshot.py  Hostvar allowlist'i, güvenli snapshot üretimi ve
                       execution öncesi yeniden doğrulama
ssh.py                 Sabit SSH izolasyon argv'si ve kontrollü known_hosts
ping_execution.py      Sabit ad-hoc ping argv'si ve normalize sonuç parser'ı
```

`process.py`, T-202'de inventory parser için yazılan sınırlandırma makinesinin
çıkarılmış hâlidir. T-204 ping akışı aynı sınırlara ihtiyaç duyduğu için kod
**kopyalanmadı**: güvenlik kritik iki kopya zamanla birbirinden ayrışır ve bu,
ADR-015'in bizzat cezalandırdığı hata sınıfıdır.

Bounded ömür session leader ile değil izole process-group ile ölçülür. Leader
önce çıksa bile descendant'lar ilk launch deadline'ına kadar doğal bitiş için
beklenir; timeout/output limitinde grup sonlandırılır. Leader, grup finalize
edilmeden reap edilmez ve iki output reader tek ortak join deadline'ı kullanır.

`app/services/jobs/preview.py` ping preview state'ini yönetir (token üretimi,
atomik yayımlama/claim, TTL, dar kapsamlı temizlik). T-204B1 ile Job modeli,
atomik yaşam döngüsü primitive'leri ve güvenli artifact deposu eklendi.
T-204B2 bu parçaları `app/services/inventories/ping_confirm.py` içinde
birleştirir: claim → doğrulama → Job rezervasyonu → execution → artifact →
terminal geçiş. Job artifact deposunun `cleanup` çağrısı orada iki yeni
ihtiyaca göre daraltıldı — hiç oluşmamış dizin için `missing_ok` no-op'u ve
"yayımlanmış sonuç korundu" durumunun gerçek bir I/O arızasından ayrılması.

**Inventory parse sınırı (T-202, ADR-017).** INI/YAML söz dizimi uygulamada
yeniden yazılmaz; Ansible'ın kendi parser'ı `ansible-inventory --list -i <path>`
komutuyla **ayrı bir süreç** olarak çağrılır:

- Komut argüman listesidir; shell kullanılmaz (GUVENLIK.md bölüm 5).
- `stdin` kapalıdır: parola soran bir alt süreç askıda kalmaz.
- Çalışma dizini boş bir geçici dizindir ve `ANSIBLE_CONFIG` oradaki **boş**
  dosyaya sabitlenir; kullanıcının `ansible.cfg` dosyaları okunmaz.
- `ANSIBLE_INVENTORY_ENABLED=ini,yaml` — `script` eklentisi kapalıdır, yani
  dinamik inventory çalıştırılamaz (ADR-009 ile tutarlı).
- Üst sürecin environment'ı **aktarılmaz**; yalnızca sayılı değişken geçer.
- stdout ve stderr pipe'lardan okunur ve **diske hiç yazılmaz**. Her akışın
  kendi üst sınırı vardır (stdout: `INVENTORY_PARSE_MAX_OUTPUT_BYTES`,
  varsayılan 5 MB; stderr: 64 KiB) ve sınır aşıldığı **anda** alt süreç
  sonlandırılır — sürecin doğal olarak bitmesi beklenmez. Sınır bu yüzden aynı
  anda hem bellek hem disk sınırıdır.
- Timeout **ayrı** bir korumadır ve boyut sınırının yerine geçmez: hiç çıktı
  üretmeden asılı kalan süreci sonlandırır.

Ham `ansible-inventory` JSON'u API cevabı olarak dönmez: sürüme bağlı, grup
merkezli ve maskelenmemiştir. Servis onu kararlı sıralı, host merkezli ve
maskelenmiş bir gösterime çevirir.

### Runner service

- `ansible-runner` çağrısını hazırlar
- Job artifact dizinini oluşturur
- Event handler kullanır
- Status ve return code günceller
- Süreç iptalini yönetir
- Timeout uygular
- Secret redaction katmanını çağırır

### Job service

- Job yaşam döngüsünü yönetir
- Veritabanı kaydı oluşturur
- Event özetlerini saklar
- Artifact referanslarını tutar
- Frontend'e normalize çıktı sağlar

### Validation service

Alt servisler:

- YAML validator
- Lint runner
- Syntax checker
- Check-mode runner
- Diff parser
- Risk classifier

Validation gerçek execution'dan ayrı job türü olarak düşünülebilir.

### AI service

Provider-independent arayüz:

```python
class AIProvider:
    async def generate_artifact(...): ...
    async def summarize_job(...): ...
    async def review_ansible(...): ...
```

AI service:

- Prompt template seçer
- Secret içermeyen context oluşturur
- Provider'a istek atar
- Structured output doğrular
- Artifact path'lerini kontrol eder
- Cache kullanır
- Provider hatasını domain hatasına çevirir

### Risk engine

İlk sürüm deterministik çalışır.

Girdi:

- Artifact type
- Module isimleri
- Task isimleri
- Hedef dosya yolları
- Service state
- Reboot
- User/group değişiklikleri
- SSH/firewall/sudo anahtar kelimeleri

Çıktı:

```json
{
  "risk": "high",
  "reasons": [
    "SSH configuration is modified",
    "Service reload is requested"
  ],
  "required_controls": [
    "syntax_check",
    "check_mode",
    "diff",
    "explicit_approval"
  ]
}
```

### Secret service

- Secret metadata yönetir
- Şifreleme/deşifreleme sınırını tutar
- Prompt ve log redaction uygular
- Secret değerleri frontend'e geri döndürmez

---

## 3. Job veri akışı

```text
Kullanıcı job formunu gönderir
→ API isteği doğrular
→ Project ve inventory path kontrol edilir
→ Job kaydı pending oluşturulur
→ Runner service işi başlatır
→ Job running olur
→ Runner event'leri event handler'a gelir
→ Event'ler normalize edilir
→ SSE ile frontend'e yayınlanır
→ Özet metadata saklanır
→ Runner tamamlanır
→ return code ve status kaydedilir
→ Recap oluşturulur
→ Gerekirse AI Run Analyzer çağrılabilir
```

AI Run Analyzer otomatik çağrılmak zorunda değildir. MVP 1'de kullanıcı butonuyla veya yalnızca failed job için yapılandırılabilir.

---

## 4. AI artifact akışı

```text
Kullanıcı isteği
→ Artifact türü seçilir
→ Gerekli somut bilgiler toplanır
→ Secret içermeyen project context hazırlanır
→ AI provider çağrılır
→ Structured response parse edilir
→ Dosya path'leri doğrulanır
→ İçerik preview edilir
→ Kullanıcı dosyaları seçer
→ Geçici workspace'e yazılır
→ Validation pipeline
→ Sonuç ve diff gösterilir
→ Kullanıcı project'e uygulamayı onaylar
→ Gerçek project dosyaları değiştirilir
```

Üretilen dosyalar doğrudan aktif project'e yazılmamalıdır. Önce staging workspace kullanılmalıdır.

---

## 5. Dosya sistemi yapısı

Örnek çalışma verisi:

```text
app-data/
├── database/
│   └── app.db
├── projects/
│   └── <project-id>/
├── inventories/
│   └── <inventory-id>/
├── jobs/
│   └── <job-id>/
│       └── result.json
├── staging/
│   └── <generation-id>/
├── ping-previews/
│   └── <sha256-token>/
│       ├── meta.json
│       └── inventory-targets.yml
├── secrets/
└── ssh/
    └── known_hosts
```

Kullanıcı mevcut project dizinini sisteme register edebilir. Uygulama tüm project'i kendi klasörüne kopyalamak zorunda değildir. Ancak path allowlist uygulanmalıdır.

---

## 6. Veritabanı başlangıç tabloları

### projects

- id
- name
- path, normalize edilmiş kanonik yol
- path_key, `path`'ten türetilen karşılaştırma anahtarı, unique
- description
- is_active
- created_at
- updated_at

Sütun adı veri modeli sözleşmesinde `is_active` olarak sabitlenmiştir.

Duplicate koruması `path` üzerinde değil `path_key` üzerindedir. Anahtar
`os.path.normcase` ile üretilir; böylece Windows'un case-insensitive dosya
sistemi semantiği veritabanı seviyesinde uygulanır. `path_key` bağımsız
atanabilir bir alan değildir: her INSERT/UPDATE öncesinde `path`'ten
yeniden türetilir, dışarıdan zorlanan değer duplicate korumasını aşamaz.

### inventories

- id
- project_id, nullable, `projects.id`'ye FK (`ON DELETE RESTRICT`)
- name
- path, normalize edilmiş kanonik dosya yolu
- source_type, `ini` | `yaml`
- created_at
- updated_at

`project_id` **nullable**'dır: inventory bir project'e bağlı olabileceği gibi
project'ten bağımsız ve yeniden kullanılabilir de olabilir. Project kayıtları
soft delete edildiği için FK `RESTRICT`'tir; fiziksel silme beklenmez.

FK'nin gerçekten uygulanması için SQLite'ta bağlantı başına
`PRAGMA foreign_keys=ON` gerekir; SQLite kısıtları varsayılan olarak
**uygulamaz**. PRAGMA `create_db_engine` içinde engine'in `connect` olayına
bağlanır, yani uygulamanın açtığı her bağlantıda etkindir. PostgreSQL'de böyle
bir adım gerekmez; FK'ler koşulsuz uygulanır (ADR-004).

`source_type` native olmayan bir Enum'dur: VARCHAR + CHECK olarak render edilir.
Böylece hem SQLite hem PostgreSQL'de aynı davranır ve geçersiz bir değer
uygulama katmanı atlansa bile veritabanına yazılamaz. `ini`/`yaml` dışındaki
biçimler (dinamik inventory script'i dâhil) MVP 1 kapsamı dışındadır.

Project'in aksine `path` üzerinde **unique index yoktur**: aynı inventory
dosyası farklı project'lere bağlı veya farklı adlarla birden çok kez
kaydedilebilir. Bu bilinçli bir T-201 kapsam kararıdır; duplicate koruması
ihtiyacı ortaya çıkarsa ayrı görev olarak ele alınır.

### jobs

- id
- job_type (`ping` | `playbook`)
- status (`pending` | `running` | `successful` | `failed` | `canceled`)
- inventory_id
- project_id, nullable
- playbook_path
- limit_pattern
- requested_by
- artifact_path
- return_code
- started_at
- finished_at
- created_at

`running` kayıt için `started_at` zorunluluğu CHECK constraint'tir. Aynı
inventory üzerinde `pending`/`running` ping çoğaltmasını Python'daki ön sorgu
değil, SQLite ve PostgreSQL'de aynı predicate'i üreten partial unique index
engeller. Ping servisinin normal durum yolu yalnız
`pending → running → successful|failed|canceled` biçimindedir; pending stale
kaydı yalnız koşullu recovery UPDATE'iyle failed yapılır.

Yayımlanmış `jobs/<id>/result.json` operatör incelemesi için korunur. Cleanup
yalnız result bulunmayan boş/yarım dizindeki bilinen geçici dosyaları siler;
terminal DB commit'i veya rename sonrası dizin fsync'i başarısız olsa bile
görünür sonuç silinmez.

### job_events

MVP yaklaşımı:

- id
- job_id
- sequence
- event_type
- host
- task
- changed
- failed
- stdout_excerpt
- created_at

Tam raw event artifact dosyasında tutulabilir.

### ai_providers

- id
- provider_type
- name
- base_url
- model
- secret_reference
- enabled

### ai_generations

- id
- provider_id
- generation_type
- request_summary
- response_status
- staged_path
- risk_level
- created_at

### validations

- id
- generation_id veya project file referansı
- validation_type
- status
- output_excerpt
- artifact_path
- created_at

---

## 7. API taslağı

### Project

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}
DELETE /api/projects/{id}
GET    /api/projects/{id}/playbooks
```

`DELETE` **soft delete**'tir: fiziksel project dosyalarına dokunmaz, yalnızca
kaydı `is_active = false` yapar. Böylece geçmiş job'ların project referansı
korunur. Kayıt `GET /api/projects/{id}` ile hâlâ okunabilir; varsayılan listede
görünmez, `?include_inactive=true` ile listelenir. İşlem idempotenttir.

Duplicate koruması `is_active` durumundan bağımsızdır: pasif bir kaydın path'i
tekrar kaydedilemez, `409 project_already_exists` döner ve mesaj kaydın pasif
olduğunu, `details` ise `project_id`'yi bildirir. Pasif kaydı tekrar aktif etme
(PATCH) MVP 1'de yoktur.

`POST` sırasında uygulanan kontrol sırası (GUVENLIK.md bölüm 4):

```text
normalize (422 invalid_path)
→ allowlist (403 path_not_allowed)
→ var mı / dizin mi (422 path_not_found | path_not_a_directory)
→ duplicate (409 project_already_exists)
```

Varlık kontrolü allowlist kontrolünden **sonra** yapılır; aksi hâlde endpoint,
izin verilmeyen bir path için "var/yok" bilgisi sızdıran bir dosya sistemi
sondası olurdu. İzin verilen kökler `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` ile
yapılandırılır; tanımsızsa yalnızca `app-data/projects` kabul edilir.

`GET /api/projects/{id}/playbooks` yalnızca aktif project'lerde çalışır ve
path/glob parametresi almaz. Keşif başında veritabanındaki path yeniden
normalize edilip allowlist'e karşı **tekrar** doğrulanır: kayıt anındaki
kontroller kalıcı bir garanti değildir, dosya sistemi sonradan değişebilir.
Aday sınıflandırması uzantı + dizin adı + dosya adı + hafif yapısal içerik
sezgisiyle yapılır; Ansible semantiği T-402'de doğrulanır. Ayrıntı:
[docs/gelistirme-ortami.md](docs/gelistirme-ortami.md#playbook-keşfi)

### Inventory

```text
GET    /api/inventories
POST   /api/inventories
GET    /api/inventories/{id}
GET    /api/inventories/{id}/hosts
POST   /api/inventories/{id}/ping/preview         (T-204A, uygulandı)
POST   /api/inventories/{id}/ping/preview/cancel  (T-204A, uygulandı)
POST   /api/inventories/{id}/ping                 (T-204B2, uygulandı)
```

İlk üç endpoint inventory **dosyasının içeriğini okumaz**; yalnızca güvenli
metadata yönetirler (T-201). Erişilebilirlik testi T-204'e aittir.

#### Ping: iki aşamalı onay sözleşmesi (T-204A)

GUVENLIK.md bölüm 2 ve 7 gereği gerçek execution öncesinde kullanıcı yetkili
planı görmeli ve **açıkça onaylamalıdır**. Ansible ping uzak hostta geçici
modül dosyası ve süreç oluşturur; bu gerçek execution'dır. Bu yüzden akış
ikiye ayrılmıştır:

```text
POST .../ping/preview   → plan + tek kullanımlık token      (SSH YOK)
POST .../ping           → yalnızca token ile; dondurulmuş
                          snapshot üzerinde çalışır          (T-204B2)
```

T-204A preview ve cancel'ı, T-204B1 Job/SSH/process/artifact/ping execution
primitive'lerini, **T-204B2 ise public confirm endpoint'ini ve claim →
execution orkestrasyonunu** uygulamıştır. T-204C bu sözleşmenin arayüzünü
eklemiştir (bkz. bölüm 2, "Ping arayüzü"); T-204 tamamlanmıştır.

`preview` cevabı:

```json
{
  "preview_token": "…43 karakter base64url…",
  "expires_at": "2026-07-31T09:19:02Z",
  "plan": {
    "inventory": {"id": 1, "name": "prod", "binding": "standalone",
                  "project_id": null, "project_name": null},
    "operation": "ansible.builtin.ping",
    "operation_effect": "…SSH bağlantısı kurulur; uzak hostta geçici modül "
                        "dosyaları ve süreç oluşabilir…",
    "limit": "webservers",
    "host_count": 12,
    "hosts": ["web01", "…"],
    "hosts_truncated": false,
    "connection": "ssh",
    "host_key_policy": "strict",
    "become": false
  }
}
```

Plan **yalnızca güvenli alanları** taşır: host adları vardır; adres, kullanıcı,
private key yolu ve diğer hostvar'lar **yoktur**. `host_count` liste kırpılsa
bile her zaman kesindir. Sunucudaki dosya yolu da taşınmaz.

`operation_effect` metni bilinçli olarak mutlak güvence **vermez**; "hiçbir
değişiklik yapılmaz" demek yanlış olurdu.

**Preview pipeline'ı — özgün inventory yalnızca bir kez okunur:**

```text
limit doğrulama (422 ping_invalid_limit)
→ kayıtlı path'i kullanım anında yeniden doğrula (403/404/409)
→ Phase 1 : ansible-inventory --list -i <ÖZGÜN>      (ini,yaml)
→ ham JSON üzerinde hostvar allowlist + SSH hedef doğrulaması
→ Snapshot A (grup topolojisi)                        [geçici workdir, 0600]
→ Phase 1b: --limit, Snapshot A üzerinde              (yalnızca yaml)
→ kesin hedef kümesi
→ Snapshot B (yalnızca hedefler)
→ meta + snapshot digest
→ atomik publish
```

Phase 1b'nin özgün dosyaya değil snapshot'a uygulanması bilinçlidir: plan ile
çalıştırma arasında inventory veya `group_vars` değişse bile hedef kümesi ve
güvenlik incelemesi geçersizleşmez (TOCTOU). Snapshot A'nın ayrıştırılabilir
olduğu Phase 1'de kanıtlandığı için Phase 1b'deki **her** arıza limite
atfedilir ve `422 ping_invalid_limit` üretir; Ansible'ın metni, çıkış kodu ve
traceback'i kullanıcıya hiç gösterilmez.

Snapshot okuyan adımlarda yalnızca `yaml` inventory eklentisi etkindir.
Ölçülen sebep: `ini` eklentisi JSON metnini ayrıştırmaya çalışıp başarısız
olmadan **önce** paylaşılan inventory nesnesine `{` adında hayalet bir host
ekler.

**Preview state.** `app-data/ping-previews/<sha256(token)>/` altında
`meta.json` ve `inventory-targets.yml` tutulur (dizin 0700, dosyalar 0600).
Sunucuda **token değil yalnızca özeti** adres olarak kullanılır. State önce
`building-<32 hex>` adlı bir dizinde tam olarak hazırlanıp fsync edilir, sonra
atomik `rename` ile yayımlanır; yarım state hiçbir zaman geçerli preview olarak
görünmez. Claim de `rename` iledir: iki eşzamanlı istekten yalnızca biri kazanır.

Snapshot A kalıcı state'e **kopyalanmaz**; yalnızca üretim sırasındaki geçici
workdir'de bulunur.

**Bütün preview dosya sistemi işlemleri descriptor-relative'dir.** Kök bir kez
`O_DIRECTORY | O_NOFOLLOW` ile açılır ve publish, claim, discard ile sweep aynı
kök descriptor'ına göre (`dir_fd`) ilerler; ikinci bir path-tabanlı kopya
**yoktur**. Kökün kendisi symlink ise işlem fail-closed biçimde `500
ping_preview_unavailable` üretir. Alt dizin açıldıktan sonra descriptor'ın
`fstat()` kimliği, isimdeki girdinin `stat(..., follow_symlinks=False)` kimliğiyle
karşılaştırılır: `O_NOFOLLOW` yalnızca açma anındaki symlink'i reddeder, bu
kontrol ise açmadan sonra yapılan bir değiş-tokuşu yakalar. Güvenli
primitive'ler bulunmayan bir platformda zayıf bir fallback'e **düşülmez**.

Gerekçesi ölçülmüştür: eski path-tabanlı uygulamada yayımlanmış bir `<digest>`
dizini dışarıyı gösteren bir symlink ile değiştirildiğinde claim symlink'i
`.claimed-<uuid>` adına taşıyor, dış hedefe `claim.json` yazıyor ve temizlik
dışarıyı etkiliyordu. `Path.is_dir()` / `Path.is_symlink()` gibi arka arkaya
yapılan kontroller güvenlik garantisi **sayılmaz**.

**Claim, state'i döndürmeden önce dört şeyi doğrular:** meta'nın zorunlu
alanları, son kullanma zamanı, planın bağlandığı `inventory_id` ile aktör, ve
snapshot'ın `hmac.compare_digest` ile karşılaştırılan SHA-256 özeti. Biri
tutmazsa state tüketilmiş sayılır, temizlenir ve `409 ping_preview_invalid`
döner; token tekrar kullanılabilir bırakılmaz. Meta zorunlu alanları:
`schema_version`, `created_at`, `expires_at`, `inventory_id`, `requested_by`,
`limit`, `host_count`, `host_key_policy`, `operation`, `snapshot_sha256`.
Secret, private key yolu ve hostvar değeri meta'ya **yazılmaz**.

`requested_by` değeri `ANSIBLEOPS_LOCAL_ACTOR` ayarından gelir. MVP 1 tek
kullanıcılıdır (ADR-011) ve gerçek authentication yoktur; bu yüzden uygulama OS
kullanıcı adı veya istemci IP'si **üretmez** — ikisi de doğrulanmamış bir
kimliğe doğrulanmış görünümü verirdi.

`cancel` **token doğrulaması açısından** her durumda `204` döner — bilinmeyen,
biçimsiz, süresi geçmiş, eşleşmeyen veya kullanılmış token da. Aksi hâlde cevap
farkı bir token'ın var olup olmadığını sızdırırdı. Kullanılmış token'ın dizini
silindiği için "kullanılmış" ile "hiç var olmamış" ayırt **edilemez**; bu yüzden
`already_used` diye bir garanti verilmez ve `reason` yalnızca `expired`,
`mismatch` veya `invalid` olur.

Bunun tek istisnası **altyapı arızasıdır**: izin, I/O, kök güvenliği, meta okuma
veya temizlik başarısızlığı `500 ping_preview_unavailable` üretir ve `204` ile
örtülmez. `rename`'in her hatası da 409 sayılmaz — kaynak gerçekten yoksa token
bilinmiyordur, `PermissionError` ve `EIO` ise altyapı hatasıdır. Beklenmeyen
içerikli bir dizin güvenlik gereği silinmeden korunabilir; ancak işlem o zaman
başarılı gösterilmez. Hata cevabı dosya sistemi yolu, token, stdout/stderr veya
exception metni taşımaz.

**Hostvar allowlist (fail-closed).** Snapshot'a yalnızca `ansible_host`,
`ansible_port`, `ansible_user`/`ansible_ssh_user`,
`ansible_ssh_private_key_file`/`ansible_private_key_file` ve
`ansible_python_interpreter` taşınır. `ansible_connection` yalnızca değeri tam
olarak `ssh` ise kabul edilir ve snapshot'a **yazılmaz**. Bunların dışındaki her
`ansible_*` — bilinmeyenler ve `ansible_become*` dâhil —
`422 ping_inventory_unsafe` üretir. `ansible_` ile başlamayan kullanıcı
değişkenleri sessizce kopyalanmaz, hata üretmez.

`ansible_password` ve `ansible_ssh_pass` **desteklenmez**: Credential service
yoktur ve parolayı ikinci bir geçici dosyaya kopyalamak ani süreç/host
çökmesinde düz metin kalıntı bırakabilir. Bu sınıfta değişken **adı bile**
dışarı verilmez; genel bir "desteklenmeyen credential yöntemi" mesajı döner.

`details` yalnızca host adı (veya güvenli `host_index`) ve değişken **adını**
taşır; değer hiçbir koşulda yer almaz. Gösterim host adı doğrulamadan geçemezse
adı basılmaz, `host_index` kullanılır.

**SSH hedef doğrulaması.** Shell kullanmamak OpenSSH option injection'ını tek
başına çözmez: Ansible hedefi `ssh` argv'sine `--` ayıracı olmadan ekler ve
lider `-` orada bir seçenektir. Bu yüzden etkin hedef (`ansible_host`, yoksa
inventory host adı) pozitif bir sözleşmeyle kabul edilir — geçerli DNS adı,
IPv4 veya IPv6. Lider `-`, `user@host`, path benzeri değerler, boşluk ve
kontrol karakterleri reddedilir.

#### Ping: confirm sözleşmesi (T-204B2)

`POST /api/inventories/{id}/ping` gövdesi **yalnızca** `preview_token` taşır ve
`extra="forbid"`tir. Limit, timeout, forks, modül, modül argümanı ve inventory
path'i istemciden alınmaz: çalıştırılan iş, onaylanan plandır.

Uygulanan sıra:

```text
preview claim (atomik, tek kullanımlık)
→ meta/snapshot bütünlüğü + private key yeniden doğrulaması
→ inventory kaydı yalnız FK/project metadata'sı için okunur
→ execution workspace (0700) + snapshot dosyası (0600) + known_hosts
→ stale kurtarma + aktif Job ön kontrolü
→ canonical UUID4
→ T1: pending Job flush → artifact dizini → commit
→ T2: koşullu pending → running → commit
→ (açık transaction yokken) ansible all -i <snapshot> -m ping
→ güvenli parser → atomik result.json
→ T3: koşullu running → terminal → commit
→ workspace temizliği + claim edilen preview'ın discard'ı
```

Üç ayrıntı bilinçlidir:

1. **Claim en başta yapılır.** Sonraki her arıza — aktif Job çakışması dâhil —
   token'ı tüketilmiş bırakır. Aksi hâlde tek-kullanım garantisi yalnızca
   "mutlu yolda" geçerli olurdu.
2. **Workspace ve known_hosts, Job rezervasyonundan öncedir.** Bu iki adımın
   arızası altyapı arızasıdır ve geride pending bir Job veya boş bir artifact
   dizini bırakmamalıdır.
3. **Alt süreç çalışırken açık transaction yoktur.** Ping timeout'u kadar süren
   bir SQLite yazma kilidi uygulamanın geri kalanını bloklardı. Regresyon,
   runner çağrısında `session.in_transaction()` değerini doğrudan ölçer.

Özgün inventory dosyası confirm sırasında **hiç açılmaz**; hedef kümesi ve
bağlantı alanları yalnızca claim edilen snapshot'tan gelir. Preview'dan sonra
dosya değişse, silinse veya izinleri kapansa bile çalıştırılan iş aynıdır.

**Onaylanan host-key politikası execution'a bağlıdır.** Plandaki
`host_key_policy` ile confirm anındaki ayar birebir aynı olmalıdır; aksi hâlde
`409 ping_preview_invalid` (`reason: mismatch`) döner ve hiçbir süreç
başlatılmaz. İki yön de reddedilir: `strict` onaylanmış bir planı `accept_new`
ile çalıştırmak kullanıcının görmediği bir TOFU penceresi açardı, planın eski
değerini kullanmak ise güncel yönetici ayarını sessizce delerdi. Çözüm yeni bir
preview'dır.

Cevap yapısı:

```json
{
  "job_id": "…canonical uuid4…",
  "job_type": "ping",
  "status": "successful",
  "inventory_id": 1,
  "project_id": null,
  "limit": null,
  "return_code": 0,
  "started_at": "…",
  "finished_at": "…",
  "summary": {"total": 1, "reachable": 1, "unreachable": 0,
              "failed": 0, "no_result": 0},
  "hosts": [{"name": "web01", "status": "reachable", "message": null}]
}
```

Job **yalnız** iki koşul birlikte sağlanırsa `successful` olur: return code 0
**ve** beklenen bütün host'lar reachable. Beklenen bir host için hiç sonuç
bloğu görülmediğinde durum `no_result`tur ve sessizce başarı sayılmaz.

Ansible'ın `rc=2`/`rc=4` gibi sonuçları **altyapı hatası değildir**: HTTP 200
döner, Job `failed` olur. Host listesi ada göre deterministik sıralıdır.
`message` yalnız `unreachable` ve `failed` durumlarında doludur.

**Mesajlar bağlantı değerlerini geri taşımaz.** Ölçülen sızıntı: kapalı bir
porta yapılan gerçek ping'de OpenSSH'in metni `connect to host 127.0.0.1 port
1: Connection refused` biçimindedir; yani planın bilinçli olarak vermediği
`ansible_host` ve `ansible_port` değerleri cevaba ve artifact'e geri dönerdi.
Bu yüzden snapshot'taki bağlantı değerleri mesajda maskelenir. Host **adı**
maskelenmez: o zaten planın parçasıdır.

Result artifact'i `app-data/jobs/<uuid>/result.json` altında, 0700/0600
izinlerle ve atomik olarak yayımlanır. İçeriği cevapla aynı alanlar artı
`schema_version`dır. stdout/stderr, hostvar, token, snapshot içeriği, private
key veya inventory yolu, argv, environment ve controller dosya sistemi
ayrıntısı **yazılmaz**.

Timeout, çıktı sınırı, süreç arızası (beklenmeyen istisnalar dâhil) ve geçersiz
çıktı da terminal sonuçtur:
bütün beklenen host'ları `no_result` gösteren güvenli bir artifact yayımlanır,
Job `failed` yapılır ve ancak sonra ilgili hata yükseltilir. Aksi hâlde Job
`running` asılı kalır ve inventory yalnızca stale eşiği dolduğunda tekrar
ping'lenebilirdi.

Ping hata kodları:

| Durum | HTTP | code |
|---|---|---|
| Geçersiz/boş/malformed limit; Phase 1b arızası | 422 | `ping_invalid_limit` |
| Limit hiçbir host ile eşleşmedi | 422 | `ping_no_hosts_matched` |
| Desteklenmeyen bağlantı değişkeni, hedef veya anahtar yolu | 422 | `ping_inventory_unsafe` |
| Preview state yazılamadı/okunamadı/temizlenemedi; kök güvenli değil | 500 | `ping_preview_unavailable` |
| Token bilinmiyor, süresi geçmiş, eşleşmiyor veya kullanılmış | 409 | `ping_preview_invalid` |
| Bu inventory için taze aktif ping Job'u var | 409 | `job_already_running` |
| Job/artifact rezervasyonu veya T1/T2 arızası | 500 | `ping_artifact_unavailable` |
| Sonuç yazılamadı veya Job terminal duruma alınamadı | 500 | `ping_artifact_write_failed` |
| Execution workspace hazırlanamadı/temizlenemedi | 500 | `ping_snapshot_unavailable` |
| known_hosts hazırlanamadı | 500 | `ping_known_hosts_unavailable` |
| `ansible` süreci başlatılamadı | 503 | `ansible_unavailable` |
| Ping zaman aşımına uğradı | 504 | `ping_timeout` |
| Çıktı sınırı aşıldı | 502 | `ping_output_too_large` |
| Çıktı beklenen biçimde değil | 502 | `ping_invalid_output` |

`job_already_running` cevabındaki `details`, çatışan Job'un kimliğini **yalnız
güvenilir biçimde okunabildiğinde** taşır: kayıt bu arada terminal duruma
geçmiş olabilir. `ping_artifact_write_failed` cevabındaki `details` yalnız
`job_id`, `ping_output_too_large` cevabındaki `details` yalnız `stream`
taşır.

Path ve parser hataları T-202'nin mevcut kodlarıyla döner; ping onları yeniden
adlandırmaz.

`GET /api/inventories/{id}/hosts` (T-202) inventory içeriğini döndürür ve
**path veya komut parametresi almaz**: okunacak dosya yalnızca veritabanındaki
kayıttan belirlenir.

Kayıt kullanım anında yeniden doğrulanır — kayıt anındaki kontroller kalıcı bir
garanti değildir:

```text
normalize
→ güvenlik sınırı (standalone: inventory allowlist,
                   bağlı: project allowlist + aktif project kökü)
→ dosya hâlâ var mı / dosya mı  (409 inventory_path_unavailable)
→ parser
→ normalize + redaction
```

Cevap yapısı:

```json
{
  "inventory_id": 1,
  "groups": [{"name": "web", "hosts": ["web01"]}],
  "hosts": [
    {
      "name": "web01",
      "groups": ["all", "web"],
      "variables": {"ansible_host": "10.0.0.10", "api_token": "***"}
    }
  ]
}
```

Grup üyeliği `children` kenarları izlenerek **geçişli** hesaplanır; sıralama
ada göre deterministiktir. Host değişkenlerinde secret anahtarları ve secret
görünümlü değerler maskelenir (GUVENLIK.md bölüm 9).

Parse hataları standart zarfta döner:

| Durum | HTTP | code |
|---|---|---|
| `ansible-core` yok, çalıştırılamıyor **veya çöküyor** | 503 | `inventory_parser_unavailable` |
| Süre aşımı | 504 | `inventory_parse_timeout` |
| Çıktı sınırı aşıldı (stdout veya stderr) | 502 | `inventory_parse_output_too_large` |
| Çıktı beklenen JSON değil | 502 | `inventory_parse_invalid_output` |
| Dosya ayrıştırılamadı (içerik hatası) | 422 | `inventory_parse_failed` |
| Kayıtlı dosya silinmiş / dosya değil | 409 | `inventory_path_unavailable` |

Sıfırdan farklı çıkış kodu **iki farklı arızayı** temsil eder ve ayrıştırılır:
stderr bir Python traceback'i içeriyorsa parser'ın kendisi çökmüştür (bozuk
kurulum, uyumsuz yorumlayıcı, desteklenmeyen platform) ve 503 döner. Kullanıcıya
"dosyanız ayrıştırılamadı" demek yanıltıcı olurdu; ayrıca o stderr'de yorumlayıcı
iç yapısı bulunur ve **hiç** gösterilmez.

`inventory_parse_failed` cevabındaki `details.parser_message` **ham stderr
değildir**: mutlak yollar `<path>` ile değiştirilir, traceback çerçeveleri
silinir, secret biçimleri maskelenir ve metin kırpılır.

`inventory_parse_output_too_large` cevabı `details.stream` alanıyla hangi akışın
sınırı aştığını bildirir: `"stdout"` (sonuç çok büyük) veya `"stderr"` (parser
sınırsız hata metni üretiyor). Sınır süreç çalışırken uygulandığı için bu hata,
alt süreç durdurulduktan sonra ve **doğal bitişi beklenmeden** döner; boyut
aşımı hiçbir zaman timeout olarak raporlanmaz.

`GET /api/inventories` isteğe bağlı `project_id` filtresi alır. Filtre
verilmezse standalone kayıtlar dâhil hepsi listelenir.

`POST` sırasında geçerli güvenlik sınırı, project bağı istenip istenmediğine
göre değişir (ADR-015). Kontrol sırası her iki akışta da GUVENLIK.md bölüm 4'e
uyar.

**Standalone** (`project_id` verilmedi):

```text
normalize (422 invalid_path)
→ inventory allowlist (403 path_not_allowed)
→ dosya var mı / dosya mı (422 path_not_found | path_not_a_file)
```

**Project'e bağlı:**

```text
normalize (422 invalid_path)
→ dosya project allowlist'inde mi (403 path_not_allowed)
→ project var mı (404) / aktif mi (409 project_inactive)
→ project kökü project allowlist'inde mi (403 path_not_allowed)
→ dosya project kökünün içinde mi (403 inventory_path_outside_project)
→ dosya var mı / dosya mı (422 path_not_found | path_not_a_file)
```

Genel allowlist kontrolü project sorgusundan **önce** gelir. Aksi hâlde izin
verilen alanın tamamen dışındaki bir path için dönen 403/404 farkı, project
kaydının var olup olmadığını sızdıran bir oracle olurdu; yetkisiz bir istemci
project id'lerini bu farkla tarayabilirdi. Bu yüzden izinsiz bir path, project
`4242` de olsa var olan bir project de olsa **aynı** cevabı alır.

Varlık kontrolü **her iki akışta da en sonda** yapılır. Aksi hâlde endpoint,
izin verilmeyen bir path için "var/yok" bilgisi sızdıran bir dosya sistemi
sondası olurdu; sınırın dışındaki bir path için var olan ve olmayan dosya
bilinçli olarak **aynı** 403 cevabını üretir.

Path izin verilen alanın **içindeyse** project kaydının yokluğu artık
gizlenecek bir bilgi değildir; o durumda `404 not_found` döner.

İki allowlist birbirinin yerine geçmez:

- Project allowlist'i standalone akışını genişletmez; project kökü altında
  duran her dosya kendiliğinden kaydedilebilir bir inventory sayılmaz.
- Inventory allowlist'i project akışını genişletmez; bağ istendiği anda
  geçerli sınırlar project allowlist'i ve ardından project kökünün kendisidir.

Bağ kurulurken project'in kayıtlı path'i yeniden normalize edilip project
allowlist'ine karşı **tekrar** doğrulanır: kayıt anındaki kontroller kalıcı bir
garanti değildir.

| Ayar | Kapsam | Varsayılan |
|---|---|---|
| `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` | Project kökleri | `app-data/projects` |
| `ANSIBLEOPS_INVENTORY_ROOT_ALLOWLIST` | Standalone inventory dosyaları | `app-data/inventories` |
| `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST` | Inventory'de gösterilen private key dosyaları | `app-data/secrets` |
| `ANSIBLEOPS_LOCAL_ACTOR` | Preview/onay kaydındaki `requested_by` etiketi | `local-single-user` |

Üçüncü liste T-204A ile eklendi ve diğer ikisinden ayrıdır: bir project veya
inventory kökü altında duran her dosya kendiliğinden kullanılabilir bir SSH
anahtarı sayılmaz. Bu değer controller üzerinde **dosya okutur**; doğrulanmadan
geçirilseydi `/root/.ssh/id_rsa` gibi bir yol denenerek varlık ve okunabilirlik
bilgisi sızdırılabilirdi.

İki varsayılan da `ensure_app_data_dirs` tarafından oluşturulan dizinlerdir;
yani varsayılan yapılandırma her iki akış için de kullanılabilir bir kök
bırakır.

### Job

```text
GET    /api/jobs
POST   /api/jobs
GET    /api/jobs/{id}
GET    /api/jobs/{id}/events
GET    /api/jobs/{id}/stream
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/analyze
```

### AI

```text
GET    /api/ai/providers
POST   /api/ai/providers
POST   /api/ai/generate
GET    /api/ai/generations/{id}
POST   /api/ai/generations/{id}/validate
POST   /api/ai/generations/{id}/apply
```

Apply endpoint açık onay bilgisini ve validation sonucunu kontrol etmelidir.

---

## 8. Canlı event tercihi

MVP 1 için SSE önerilir.

Neden:

- Veri yönü ağırlıklı olarak backend'den frontend'e.
- WebSocket'e göre daha basit.
- Tarayıcı otomatik reconnect desteğine sahiptir.
- Job log akışı için yeterlidir.

WebSocket ileride çift yönlü interaktif terminal veya gelişmiş kontrol gerekirse değerlendirilebilir. Genel terminal özelliği ürün kapsamı dışındadır.

---

## 9. Concurrency

MVP 1:

- Tek process içinde kontrollü background task veya küçük worker mekanizması
- Aynı anda sınırlı job
- Global concurrency ayarı
- Project veya inventory bazında kilit
- Aynı staging alanına eşzamanlı yazmayı engelleme

İleride:

- PostgreSQL
- Harici queue
- Ayrı runner workers

Ancak distributed queue MVP 1'e alınmamalıdır.

---

## 10. AWX entegrasyon sınırı

MVP 3'te `ExecutionBackend` interface oluşturulabilir:

```python
class ExecutionBackend:
    async def launch_job(...): ...
    async def get_status(...): ...
    async def stream_events(...): ...
    async def cancel_job(...): ...
```

Implementasyonlar:

- `LocalRunnerBackend`
- `AWXBackend`

MVP 1 kodu mümkün olduğunca bu ayrımı kolaylaştırmalı; ancak sırf gelecekte AWX gelecek diye gereksiz abstraction yazılmamalıdır.

---

## 11. Planlanan sunucu onboarding ve erişilebilirlik izleme mimarisi

**Durum:** Planlandı, uygulanmadı. Bu bölüm EPIC 3B için bağlayıcı mimari
sınırları tanımlar; model, migration, API, scheduler veya frontend'in mevcut
olduğunu iddia etmez. EPIC 3A'nın güvenli Job/Runner ve cancel yaşam döngüsü
tamamlanmadan bu akış uygulamaya alınmayacaktır.

Planlanan veri ve kontrol akışı:

```text
Onboarding wizard
  -> dar onboarding API'si
  -> credential referansı + host-key güven kaydı
  -> yönetilen hedef/inventory bağı
  -> mevcut ping preview/confirm ile bağlantı testi

Dayanıklı monitor schedule
  -> mevcut Job/Runner kuyruğu ve concurrency sınırı
  -> sonlu Ansible ping execution'ı
  -> normalize gözlem + sınırlı geçmiş
  -> durum projeksiyonu API'si
  -> bounded polling kullanan filo dashboard'u
```

### 11.1 Kimlik ve veri ayrımı

Inventory host alias'ı değişebilir ve aynı fiziksel hedef birden fazla
inventory'de bulunabilir. Bu nedenle alias global sunucu kimliği sayılmaz.
T-308, kalıcı hedef kimliği ile inventory bağının kesin modelini ve duplicate
politikasını karara bağlayacaktır.

Model aşağıdaki kavramları birbirinden ayırmalıdır:

- hedefin kararlı kimliği ve görüntüleme adı,
- inventory/group üyeliği,
- credential'a değeri okunamayan referans,
- host-key güven durumu ve doğrulanan fingerprint,
- periyodik kontrol politikası,
- aktif kontrol, son deneme ve son geçerli gözlem,
- sınırlı sonuç geçmişi ve ilgili Job kimliği.

### 11.2 Onboarding sınırı

Tarayıcı serbest Ansible hostvar, SSH argümanı, private-key değeri veya
filesystem path'i göndermez. Backend pozitif sözleşmeyle ad, hedef, port ve
kullanıcı gibi dar alanları kabul eder; uygulama yönetimli inventory
materyalini yalnız kendi data root'u altında üretir.

Uygulama yeni bir ED25519 credential üretebilir ve **yalnız public key'i**
kullanıcıya verir. Public key'in hedef sunucuya kurulması için ya önceden var
olan güvenilir bir yönetim kanalı gerekir ya da kullanıcı sunucu konsolunda
verilen adımları uygular. Uygulama, elinde böyle bir kanal yokken bu adımı
“tam otomatik ve güvenli” diye sunamaz.

Aynı şekilde uzak sunucudan host key okumak kimliği kanıtlamaz. Strict akışta
kullanıcı fingerprint'i sunucu konsolu veya başka bağımsız kanaldan
karşılaştırıp onaylar. `accept_new` seçeneği korunursa ayrı ve görünür bir TOFU
kararıdır.

### 11.3 Monitor yürütme sınırı

Monitor ayrı bir SSH veya subprocess motoru kurmaz. Her ölçüm, EPIC 3A'daki
dayanıklı Job/worker yaşam döngüsü üzerinden mevcut güvenli snapshot, SSH,
process-group, timeout, output-limit, artifact ve redaction sınırlarını yeniden
kullanır.

- Sürekli açık SSH oturumu tutulmaz.
- Kontrol aralığının alt sınırı ve global concurrency limiti vardır.
- Aynı hedef için iki kontrol üst üste binmez.
- Restart sonrası program dayanıklı kayıttan yüklenir; kaçırılmış periyotlar
  toplu bir kontrol fırtınasına dönüşmez.
- Scheduler, queue veya Runner arızası hedefin kapalı olduğunu kanıtlamaz;
  durum `unknown` veya gözlem yaşına göre `stale` olur.
- `reachable` yalnız dar Ansible ping'in o anda SSH, Python ve modül yürütmeyi
  başardığını gösterir; servis sağlığı veya genel makine sağlığı değildir.

### 11.4 Kullanıcıya canlı görünüm

MVP 1 filo ekranı kısa fakat sınırlandırılmış HTTP polling ile güncellenir.
Sekme görünür değilken polling azaltılır veya durur. Job ayrıntısında EPIC
3A'nın SSE event akışı yeniden kullanılabilir; yalnız durum kartlarını
güncellemek için sürekli WebSocket veya sürekli SSH bağlantısı kurulmaz.

Filo ekranı son geçerli gözlem ile son kontrol denemesini ayrı gösterir ve
`reachable`, `unreachable`, `degraded`, `checking`, `unknown`, `stale`
durumlarını yalnız renge dayanmadan sunar. Dashboard polling'i yalnız durum
projeksiyonunu okur; yeni ping Job'u başlatmaz. Gelişmiş uyarı kanalları,
flapping/debounce ve uzun dönem trend analizi MVP 2 kapsamındadır.
