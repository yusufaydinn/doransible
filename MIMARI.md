# DORAnsible Mimarisi

Bu belge repository'de teslim edilen ve bugün çalışan MVP mimarisini açıklar.
Gelecek genişlemeler yalnız 10. bölümde ayrıca işaretlenir; uygulanmamış bir
servis, tablo veya endpoint güncel mimarinin parçası gibi gösterilmez.

Kaynak yorumlarında kullanılan `ADR-xxx` izleme kimliklerinin konu ve güncel
belge karşılıkları için [mimari karar dizinine](docs/KARAR_DIZINI.md) bakın.
Güvenlik sınırlarının bağlayıcı açıklaması [GUVENLIK.md](GUVENLIK.md), kod
katmanları ve katkı kuralları ise
[geliştirici rehberindedir](docs/GELISTIRICI_REHBERI.md).

## 1. Yüksek seviye mimari

```text
Tarayıcı
  React + TypeScript + TanStack Query
                 |
                 | HTTP / JSON
                 v
FastAPI uygulaması
  route → schema → service → SQLAlchemy
        |                         |
        |                         v
        |                       SQLite
        |                         + app-data/
        |
        +-- inventory parse/ping → ayrı Ansible child process
        |
        +-- pending PLAYBOOK Job → tek arka plan worker
                                      |
                                      v
                              ansible-runner child
                                      |
                                      | SSH
                                      v
                              yönetilen host'lar
```

Frontend SSH bağlantısı veya Ansible süreci başlatmaz. API plan ve onay
sözleşmesini uygular; worker yalnız atomik olarak rezerve edilmiş pending
playbook Job'larını çalıştırır. Ping execution, playbook worker kuyruğundan
ayrı ve sınırlandırılmış bir Ansible ad-hoc sürecidir.

MVP çalışma modeli:

- tek güvenilir operatör ve yapılandırmadan gelen sabit aktör etiketi,
- tek Linux controller,
- tek playbook worker ve aynı anda en fazla bir aktif PLAYBOOK Job,
- çok hostlu inventory,
- Check ve Normal execution mode,
- SQLite ile kalıcı project, inventory, Job ve execution-plan kayıtları,
- dosya sisteminde dondurulmuş planlar, çalışma dizinleri ve sonuç belgeleri.

Authentication/RBAC, SSE, scheduler, sürekli monitoring, AI provider çağrısı,
AI üretim endpoint'i, şifreli credential deposu ve çoklu controller
orchestration bugün yoktur.

## 2. Ana bileşenler ve katmanlar

### Frontend

`frontend/src` altında feature tabanlı yapı kullanılır:

```text
features/
├── projects/
├── inventories/
├── executions/
└── jobs/
```

Her feature kendi API fonksiyonlarını, TypeScript tiplerini, query key'lerini,
hook'larını, bileşenlerini ve testlerini taşır. Route seviyesindeki sayfalar
`frontend/src/pages`, ortak UI bileşenleri `frontend/src/components` altındadır.

TanStack Query sunucu state'ini yönetir. Tek kullanımlık preview/plan token'ları
query cache, URL veya kalıcı browser storage'a yazılmaz; ilgili component'in
belleğinde kısa süre tutulur. Job detayı pending/running durumunda HTTP polling
yapar. SSE veya WebSocket bağlantısı yoktur.

### API route katmanı

`backend/app/api/routes` içindeki route'lar ince adaptörlerdir:

1. Pydantic request modelini alır.
2. Session ve Settings dependency'lerini edinir.
3. İlgili service fonksiyonunu çağırır.
4. Public response modelini döndürür.

Route içine SQL, path güvenliği, runner veya authorization kararı yazılmaz.
Route, child process başlatmaz ve uzun transaction tutmaz.

### Schema katmanı

`backend/app/schemas` dış API sözleşmesidir. Güvenlik açısından kritik request
modelleri bilinmeyen alanları reddeder. Örneğin execution isteği kullanıcıdan
serbest argv, extra vars, timeout, forks, tags veya limit almaz.

### Service katmanı

`backend/app/services` domain davranışını uygular:

- `projects`: project kaydı ve bounded playbook discovery,
- `inventories`: inventory kaydı, parse, ping planı/execution ve geçmiş,
- `browse`: allowlist içindeki controller path'lerinin tek seviye listesi,
- `ansible`: subprocess, SSH hedefi ve snapshot yardımcıları,
- `execution`: plan, token claim, Job state, worker, runner ve sonuç okuma,
- `jobs`: ping preview ve artifact ortakları,
- `security`: path ve redaction yardımcıları.

### Model ve veritabanı katmanı

`backend/app/models` SQLAlchemy modellerini ve veritabanı invariant'larını,
`backend/alembic/versions` sıralı migration'ları taşır. SQLite bağlantılarında
`PRAGMA foreign_keys=ON` her yeni bağlantıda açılır. Uygulama içi kontrolün
yanında kritik mode/status/binding kuralları CHECK, FK veya unique index ile de
korunur.

## 3. Temel veri ve execution akışları

### Project ve playbook discovery

```text
project path
  → absolute/canonical çözüm
  → project root allowlist
  → dizin ve symlink sınırı
  → project kaydı
  → bounded playbook discovery
```

Discovery yalnız aktif project kökünden başlar. Derinlik, girdi sayısı, sonuç
sayısı ve okunan byte sınırlandırılır. API absolute playbook path yerine project
köküne göre relative POSIX path döndürür.

### Inventory parse

```text
inventory kaydı
  → binding/path doğrulaması
  → ayrı ansible-inventory --list süreci
  → timeout + stdout byte sınırı
  → normalize host/grup görünümü
  → secret görünümlü hostvar değerlerinin maskelenmesi
```

Dinamik inventory script'i çalıştırılmaz. API, Ansible'ın ham JSON çıktısını
ve private-key içeriğini kullanıcıya vermez.

### Ping

```text
POST ping/preview
  → inventory ve hedef doğrulaması
  → dondurulmuş snapshot
  → kısa ömürlü token

POST ping
  → token'ı atomik claim
  → yalnız snapshot değerleriyle ansible -m ping
  → bounded/fail-closed parse
  → PING Job ve kalıcı özet
```

Preview hedefe bağlanmaz. Confirm isteği host listesi, modül, timeout veya forks
taşımaz. Ping geçmişi kullanıcı tarafından başlatılan ölçümlerdir; sürekli
monitoring veya uptime motoru değildir.

### Playbook planı ve launch

```text
execution-plan
  → salt-okunur plan önizlemesi

execution-plans
  → project + inventory snapshot
  → manifest + input fingerprint
  → frozen workspace publish
  → kısa ömürlü tek kullanımlık token

executions
  → token, actor, mode ve tüm binding'leri atomik claim
  → pending PLAYBOOK Job
```

Launch route'u runner başlatmaz. Worker pending Job'ı daha sonra sahiplenir.
Mode, project, inventory, playbook, host-key policy, actor ve manifest claim
sırasında yeniden bağlanır. Plan ile execution arasında kaynak project değişse
bile runner dondurulmuş workspace'i kullanır.

### Worker ve runner

```text
startup reconciliation
  → stale Job/run temizliği
  → worker poll
  → pending Job acquire
  → running + lease + heartbeat
  → prepare frozen inputs
  → ansible-runner child process
  → normalize/publish result
  → successful veya failed terminal durum
```

Worker varsayılan kapalıdır ve yalnız
`ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true` ile açılır. Runner ayrı process ve
session'da, allowlist ile sıfırdan kurulmuş environment altında çalışır. Shell
string kullanılmaz. Check mode argv'ye tam bir `--cmdline=--check` ekler;
Normal mode aynı kontrollü argv'den yalnız bu elemanı çıkarır.

Timeout durumunda süreç grubu kontrollü sonlandırılır. Hedefte o ana kadar
yapılmış değişiklikler otomatik geri alınmaz.

## 4. Değişmez güvenlik ve doğruluk sözleşmeleri

- Kullanıcı girdisi shell komut metnine birleştirilmez.
- Path allowlist kontrolü varlık sorgusundan önce uygulanır.
- Project, inventory ve private-key allowlist'leri birbirinden ayrıdır.
- Preview hiçbir gerçek bağlantı veya execution başlatmaz.
- Plan token'ı kısa ömürlü, tek kullanımlık ve actor-bound'dur.
- Mode plan → fingerprint → execution planı → Job → runner argv boyunca
  değişmeden bağlanır.
- Aktif PLAYBOOK Job için gerekli execution-plan bağı DB'de de zorunludur.
- Tek worker aynı anda yalnız bir PLAYBOOK Job çalıştırır.
- API exception, DSN, controller path, secret veya subprocess ham hatasını
  doğrudan kullanıcıya taşımaz.
- Yapılandırılmış result/event alanları sanitize edilir ve şema ile doğrulanır.
- `ansible_output` ayrı bir display yüzeyidir; bounded'dır fakat sanitize veya
  secret-free garantisi yoktur.
- Check mode mutlak yan etkisizlik, Normal mode otomatik rollback garantisi
  vermez.

## 5. Dosya sistemi yapısı

Repository kaynak yapısı:

```text
backend/             FastAPI, modeller, servisler, migration ve testler
frontend/            React/TypeScript UI ve testler
sample-projects/     Ubuntu SSH/UFW audit ve remediation örnekleri
docs/                Kullanıcı, geliştirici ve işletim belgeleri
scripts/             Kalite ve dependency-audit komutları
```

Runtime kökü varsayılan olarak repository kökündeki `app-data/` dizinidir:

```text
app-data/
├── database/        SQLite veritabanı
├── projects/        varsayılan project allowlist kökü
├── inventories/     varsayılan standalone inventory kökü
├── jobs/            kalıcı Job sonuç belgeleri
├── staging/         atomik publish geçici alanı
├── secrets/         varsayılan private-key allowlist kökü
├── ping-previews/   kısa ömürlü ping snapshot'ları
├── execution-plans/ frozen execution workspace'leri
├── execution-runs/  geçici runner çalışma dizinleri
└── ssh/             managed known_hosts
```

Bu dizin Git tarafından izlenmez. Uygulama runtime alt dizinlerini 0700 izinle
oluşturur. Secret ve private key'ler repository'ye eklenmez. Harici allowlist
altındaki project/inventory kaynakları kayıt sırasında otomatik kopyalanmaz;
execution hazırlığında gerekli içerik ayrıca dondurulur.

## 6. Veritabanı modeli

Güncel şema Alembic `0008_add_execution_mode` revision'ına kadardır.

### `projects`

Project adı, canonical controller path'i, aktiflik ve zaman damgalarını tutar.
Silme işlemi kaynak dosyayı silmez; kayıt pasife alınır.

### `inventories`

Project'e bağlı veya standalone inventory kaydını, path'i ve zaman damgalarını
tutar. Project binding nullable olsa da servis ve API akışına özgü kurallar
ayrıca uygulanır.

### `execution_plans`

Hazırlanmış planın token hash/prefix'i, actor, project/inventory/playbook,
mode, fingerprint, manifest, workspace ve prepared/claimed/expired yaşam
döngüsünü taşır. Ham token saklanmaz.

### `jobs`

PING ve PLAYBOOK Job'larının ortak kimlik, status, actor, zaman, result ve hata
alanlarını taşır. PLAYBOOK Job'ı execution planı, project, playbook, inventory
ve mode bağlarını içerir. Running playbook Job'ında worker, heartbeat ve lease
alanları bulunur; terminal geçişte lease alanları temizlenir.

Aktif PLAYBOOK Job tekilliği partial unique index ile korunur. Mode/status ve
execution-plan bağı CHECK constraint'lerle de doğrulanır.

Event'ler ayrı bir `job_events` tablosunda tutulmaz. Sanitize edilmiş bounded
event listesi ve display çıktısı, doğrulanan kalıcı Job result belgesindedir.
AI provider/generation veya validation tabloları bugünkü şemada yoktur.

## 7. Public API yüzeyi

Kanonik, makine tarafından üretilen sözleşme çalışan backend'in
`/openapi.json` çıktısıdır.

| Method | Path | İşlev |
|---|---|---|
| GET | `/health` | Servis sağlığı |
| POST, GET | `/api/projects` | Project oluşturma ve listeleme |
| GET, DELETE | `/api/projects/{project_id}` | Project detayı ve pasife alma |
| GET | `/api/projects/{project_id}/playbooks` | Bounded playbook discovery |
| POST, GET | `/api/inventories` | Inventory oluşturma ve listeleme |
| GET | `/api/inventories/{inventory_id}` | Inventory detayı |
| GET | `/api/inventories/{inventory_id}/hosts` | Normalize host/grup görünümü |
| POST | `/api/inventories/{inventory_id}/ping/preview` | Ping planı hazırlama |
| POST | `/api/inventories/{inventory_id}/ping/preview/cancel` | Ping preview iptali |
| POST | `/api/inventories/{inventory_id}/ping` | Token ile ping execution |
| GET | `/api/inventories/{inventory_id}/ping-runs` | Kalıcı ping geçmişi |
| GET | `/api/controller-paths` | Allowlist içi path browse |
| POST | `/api/projects/{project_id}/execution-plan` | Salt-okunur playbook planı |
| POST | `/api/projects/{project_id}/execution-plans` | Frozen plan hazırlama |
| POST | `/api/projects/{project_id}/executions` | Token claim ve pending Job |
| GET | `/api/jobs` | Filtreli/cursor'lı Job listesi |
| GET | `/api/jobs/{job_id}` | Job özeti |
| GET | `/api/jobs/{job_id}/result` | Doğrulanmış Job sonucu |

Aktör body, header veya query'den alınmaz. Tek kullanıcılı MVP'de
`Settings.local_actor` kullanılır. Token'lı cevaplarda `Cache-Control: no-store`
uygulanır.

## 8. Frontend veri akışı

Frontend route'ları project, inventory ve Job sayfalarına ayrılır. Project
detayı plan/prepare/launch akışını, inventory detayı host görünümü, ping ve ping
geçmişini, Job sayfaları filtreli liste, özet ve result görünümünü sunar.

Job listesi `(created_at DESC, id DESC)` keyset cursor kullanır. Sonraki cursor
sunucunun cevabından aynen alınır; UI son satırdan yeni cursor türetmez. Filtre
değişince ilk sayfaya dönülür.

Job detayı pending/running iken iki saniyelik HTTP polling yapar. Terminal
durumda polling durur. Sonuç görünümü:

- sanitize edilmiş recap ve event alanlarını,
- sınıflandırılmış public hata kodlarını,
- kullanıcı uyarısıyla kapalı bir ayrıntı alanındaki bounded
  `ansible_output` metnini

gösterir. Output React tarafından düz metin olarak render edilir; HTML olarak
yorumlanmaz.

## 9. Concurrency, recovery ve temizlik

- Launch, plan claim ve pending Job rezervasyonunu tek transaction'da yapar.
- Worker acquire işlemi koşullu atomik UPDATE kullanır.
- Lease ve heartbeat, yaşayan worker sahipliğini kanıtlar.
- Açılış reconciliation'ı yarım plan publish'lerini ve stale Job'ları ele alır.
- Execution-run janitor yalnız uygulamanın yönettiği kökte, bounded ve
  symlink-follow etmeyen silme uygular.
- Terminal publish/finish başarısızlığında transaction rollback edilir ve
  bilinmeyen başarı uydurulmaz.
- Ping preview ve execution-plan token'ları ayrı store ve yaşam döngüleridir.

SQLite MVP varsayılanıdır. Model/transaction tasarımı SQLAlchemy üzerinden
yürütülür; başka veritabanına geçiş otomatik uyumluluk garantisi değildir ve
ayrı migration/integration doğrulaması gerektirir.

## 10. Gelecek entegrasyon sınırları

Aşağıdakiler mevcut ürün özelliği değildir:

- AI ile playbook üretme, açıklama veya sonuç analizi,
- YAML/syntax/lint/diff/risk onay hattı,
- SSE veya WebSocket event yayını,
- scheduler ve sürekli filo monitoring,
- authentication/RBAC ve multi-tenant kullanım,
- birden çok controller veya AWX backend'i.

AI daha sonra eklenirse önerilen sınır:

```text
AI taslağı
  → ayrı staging artifact
  → deterministik doğrulamalar
  → insan incelemesi ve açık onay
  → mevcut frozen plan/token/Job hattı
```

AI provider execution endpoint'ine doğrudan yetki alamaz, secret görmez ve
Normal execution'ı kendi kararıyla başlatmaz. Scheduler, çoklu worker,
credential türü veya dışa açık kullanım da mevcut trusted-operator sınırını
sessizce genişletemez; ayrı tehdit modeli ve migration gerektirir.
