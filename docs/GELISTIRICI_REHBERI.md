# DORAnsible Geliştirici Rehberi

Bu belge teslim edilen MVP'nin koduna katkı yapacak geliştiriciler içindir.
Kurulum komutları [Geliştirme Ortamı](gelistirme-ortami.md), kullanıcı akışı
[Kullanıcı Rehberi](KULLANICI_REHBERI.md), ayrıntılı güvenlik kararları ise
[GUVENLIK.md](../GUVENLIK.md) içindedir.

## 1. Ürün sınırı

DORAnsible bugün:

- tek kullanıcı/tek sabit aktör,
- tek controller,
- tek playbook worker,
- çok hostlu inventory,
- Check ve Normal playbook execution,
- kalıcı Job/ping geçmişi

modeliyle çalışır.

Bugünkü kodda authentication/RBAC, scheduler, gerçek zamanlı monitoring, SSE,
AI provider çağrısı, AI üretim endpoint'i, şifreli credential deposu ve çoklu
controller orchestration yoktur. `app/services/ai/__init__.py` yalnız ilerideki
domain sınırı için boş pakettir.

## 2. Teknoloji yığını

### Backend

- Python 3.11+
- FastAPI
- Pydantic v2 / pydantic-settings
- SQLAlchemy 2
- Alembic
- SQLite (varsayılan)
- ansible-core
- ansible-runner 2.4.x
- Pytest, Ruff, MyPy

### Frontend

- React 18
- TypeScript
- Vite
- React Router
- TanStack Query
- Vitest, Testing Library, jsdom

Frontend Job detayı `pending`/`running` durumlarında iki saniyelik HTTP polling
yapar. Liste kendiliğinden polling yapmaz. SSE uygulanmamıştır.

## 3. Repository haritası

```text
backend/
├── app/
│   ├── api/routes/       # İnce HTTP adaptörleri
│   ├── core/             # Settings ve ortak hata sözleşmesi
│   ├── db/               # Engine/session
│   ├── models/           # SQLAlchemy modelleri ve DB invariants
│   ├── schemas/          # Pydantic request/response sözleşmeleri
│   └── services/
│       ├── ansible/      # Inventory/ping subprocess sınırı
│       ├── browse/       # Controller path listesi
│       ├── execution/    # Plan, launch, worker, runner, sonuç
│       ├── inventories/  # Inventory CRUD, parse, ping ve geçmiş
│       ├── jobs/         # Ping artifact/preview ortakları
│       ├── projects/     # Project CRUD ve playbook discovery
│       └── security/     # Path ve redaction
├── alembic/versions/     # Sıralı migration'lar
└── tests/

frontend/src/
├── components/           # Domain dışı ortak UI
├── features/             # executions, inventories, jobs, projects
├── lib/                  # API client, format ve ortak mode tipi
├── pages/                # Route seviyesinde sayfalar
└── test/                 # Fixture/harness
```

## 4. Katman kuralları

### Route

Route yalnız:

- Pydantic girdisini alır,
- dependency ile session/settings edinir,
- servis fonksiyonunu çağırır,
- response modeline dönüştürür.

Path güvenliği, SQL, runner veya authorization mantığı route içine yazılmaz.

### Schema

Pydantic modelleri dış API sözleşmesidir. Güvenlik açısından kritik request
modellerinde `extra="forbid"` kullanılır. Örneğin execution plan isteği yalnız
`mode`, `inventory_id` ve `playbook_path` taşır; `extra_vars`, `limit`, `tags`
ve timeout gibi ikinci kanallar kabul edilmez.

Pydantic biçim/tip doğrular; project-inventory bağı veya dosyanın allowlist
içinde kalması gibi iş kararları servis katmanındadır.

### Service

Servisler:

- domain kararlarını,
- fail-closed path/binding kontrollerini,
- kısa transaction'ları,
- subprocess çağrılarını ve limitleri

uygular. Public servis fonksiyonlarında type hint ve gerekçeyi açıklayan
docstring bulunur.

### Model/veritabanı

Mümkün olan invariants SQL constraint/FK/index ile de korunur. SQLite
bağlantılarında `PRAGMA foreign_keys=ON` her yeni DBAPI bağlantısında açılır.
Migration sırası `backend/alembic/versions` altında kayıtlıdır; mevcut migration
değiştirilmez, yeni şema yeni migration ile eklenir.

## 5. API yüzeyi

OpenAPI sözleşmesinin kanonik çıktısı çalışan backend'deki `/docs` ve
`/openapi.json` adresleridir.

| Method | Path | İşlev |
|---|---|---|
| GET | `/health` | Servis sağlığı |
| POST/GET | `/api/projects` | Project oluştur/listele |
| GET/DELETE | `/api/projects/{id}` | Detay/pasife alma |
| GET | `/api/projects/{id}/playbooks` | Güvenli playbook keşfi |
| POST/GET | `/api/inventories` | Inventory oluştur/listele |
| GET | `/api/inventories/{id}` | Inventory detayı |
| GET | `/api/inventories/{id}/hosts` | Normalize host/grup görünümü |
| POST | `/api/inventories/{id}/ping/preview` | Ping onay planı |
| POST | `/api/inventories/{id}/ping/preview/cancel` | Preview iptali |
| POST | `/api/inventories/{id}/ping` | Token'la ping çalıştırma |
| GET | `/api/inventories/{id}/ping-runs` | Kalıcı ping geçmişi |
| GET | `/api/controller-paths` | Allowlist içi tek seviye path browse |
| POST | `/api/projects/{id}/execution-plan` | Durumsuz plan önizleme |
| POST | `/api/projects/{id}/execution-plans` | Frozen plan hazırlama |
| POST | `/api/projects/{id}/executions` | Token claim + pending Job |
| GET | `/api/jobs` | Filtreli/cursor'lı Job listesi |
| GET | `/api/jobs/{id}` | Job özeti |
| GET | `/api/jobs/{id}/result` | Doğrulanmış sonuç belgesi |

Aktör request body/header/query'den alınmaz. Tek kullanıcılı MVP'de
`Settings.local_actor` sabit etiketi kullanılır.

## 6. Project ve inventory güvenlik akışı

Path kontrolünün genel sırası:

```text
ham path
  → mutlak/kanonik çözüm
  → allowlist kontrolü
  → varlık ve tür kontrolü
  → domain bağı
  → DB işlemi
```

Allowlist kontrolü varlık kontrolünden önce gelir; allowlist dışındaki mevcut
ve olmayan yollar aynı generic cevapla reddedilir. Bu, controller dosya sistemi
üzerinde varlık oracle'ı oluşmasını sınırlar.

Project discovery:

- yalnız kayıtlı aktif project kökünden başlar,
- kullanıcı glob/path göndermez,
- derinlik, girdi, sonuç ve okuma byte sınırları vardır,
- relative POSIX path döndürür,
- symlink/kaçış ve okunamayan girdileri güvenli biçimde ele alır.

Inventory parse:

- kendi parser'ımız yerine ayrı `ansible-inventory --list` süreci kullanılır,
- shell kullanılmaz; argv listesi oluşturulur,
- timeout ve stdout byte sınırı vardır,
- ham Ansible JSON'u API'ye verilmez,
- secret görünümlü host değişkenleri maskelenir.

## 7. Ping execution akışı

```text
POST preview
  → inventory ve bağlantı alanlarını doğrula
  → hedef snapshot'ını dondur
  → kısa ömürlü token

POST ping(token)
  → token'ı atomik claim et
  → snapshot'ı kullan
  → ansible -m ping child process
  → host sonuçlarını fail-closed parse et
  → Job + result artifact
```

Confirm isteği inventory path, forks, timeout, modül veya host listesi taşımaz.
Bu değerler ayarlardan ve dondurulmuş preview'dan gelir. Ansible 2.19'un
`UNREACHABLE` öncesi tanı bloğu yalnız kanonik yapısıyla kabul edilir; yabancı
metin genel olarak fail-closed invalid output üretir.

Ping Job'ları playbook worker'ından geçmez ve mode'ları DB invariant'ıyla
`check`tir. Kalıcı ping geçmişi host mesajlarını veya ham çıktıyı içermez.

## 8. Playbook plan ve onay akışı

```text
1. execution-plan
   - project/inventory/playbook/mode doğrulaması
   - inventory hedef görünümü
   - state ve token yok

2. execution-plans
   - project + inventory snapshot
   - manifest/fingerprint
   - atomic publish
   - kısa ömürlü tek kullanımlık token

3. executions
   - token + bütün binding'ler atomik claim
   - pending PLAYBOOK Job
   - route runner başlatmaz
```

Mode, project, inventory, playbook, actor, host-key policy ve manifest bağları
claim sırasında tekrar eşleşir. Response alanları request'ten değil claim
edilmiş plandan türetilir. Token URL/query yerine request body'dedir ve
`Cache-Control: no-store` kullanılır.

## 9. Worker ve runner

Worker `ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true` olmadan thread başlatmaz.
Açıkken genel akış:

```text
reconcile stale Job'lar
  → stale execution run janitor
  → worker poll
  → pending Job acquire
  → running + lease/heartbeat
  → runner child
  → result publish
  → successful/failed + lease temizliği
```

Tek worker aynı anda tek playbook Job çalıştırır. Lease ve heartbeat DB
satırındadır. Süreç açılışında stale `running` Job'lar uzlaştırılır; aktif
çalışma dizinleri janitor tarafından korunur.

Runner:

- `ansible-runner` CLI'ı ayrı process/session olarak çağırır,
- API prosesinin environment'ını doğrudan miras almaz; allowlist environment
  sıfırdan kurulur,
- shell string kullanmaz,
- Check için exact bir `--cmdline=--check` ekler,
- Normal için aynı argv'den yalnız bu elemanı çıkarır,
- timeout'ta süreç grubunu kontrollü sonlandırır,
- stdout, raw artifact, event ve result için ayrı limitler uygular.

Geçersiz runtime mode, run dizini veya child process oluşturulmadan önce
reddedilir.

## 10. Workspace ve artifact modeli

Hazırlama sırasında project/inventory içerikleri dondurulur ve manifest ile
bağlanır. Böylece kullanıcı planı gördükten sonra kaynak dosya değişse bile
launch serbestçe güncel dizini yeniden okuyup farklı bir iş çalıştırmaz.

Runtime genel olarak şu ayrımı taşır:

```text
execution-plans/   hazırlanmış frozen planlar
execution-runs/    geçici runner çalışma alanı
jobs/              yayımlanmış kalıcı sonuç/artifact
```

Kalıcı result belgesi boyut ve şema bakımından doğrulanır. UI event yüzeyi
yalnız `event`, `host`, `task`, `changed`, `failed` alanlarını taşır. Raw event
JSON'u, task args, `event_data.res`, environment ve argv UI'ya açılmaz.

`ansible_output`, runner event nesnelerinin yalnız top-level `stdout`
alanlarından event sırasıyla üretilen ve UTF-8 sınırında en fazla 128 KiB
tutulan görüntüdür. Redaction uygulanmaz; güvenli log olarak yorumlanamaz.

## 11. Job okuma ve geçmiş

Job listesi keyset cursor kullanır:

```text
(created_at DESC, id DESC)
```

`before_created_at` ve `before_job_id` birlikte verilmelidir. Filtreler:

- `project_id`
- `status`
- `mode`

Okuma sorgusu yalnız Job satırına güvenmez. Job, plan, project, inventory,
actor, mode ve immutable plan binding'leri INNER JOIN/WHERE ile birlikte
doğrulanır. Claim edilmiş plan daha sonra TTL temizliğiyle `expired` olsa bile
`claimed_at` dolu, Job `successful`/`failed` terminal durumda ve diğer bütün
binding'ler doğruysa geçmiş görünür kalır. Hiç claim edilmemiş expired planlar
ve tutarsız aktif Job'lar fail-closed elenir.

Bu davranış `backend/tests/test_execution_job_history_persistence.py` ile
gerçek launch → terminal Job → TTL sweep → list/detail/result zincirinde
regresyona karşı korunur.

## 12. Hata sözleşmesi

Domain hataları `AppError` alt sınıflarıyla sabit kod ve sanitize edilmiş mesaj
taşır. Pydantic `422` hatalarından ham kullanıcı girdisi çıkarılır. UI backend
`details`, exception, path veya subprocess metnini doğrudan göstermek yerine
bilinen kodları sabit kullanıcı metinlerine eşler.

Önemli ayrım:

- HTTP/runner/çıktı arızası
- güvenilir terminal sonuçta playbook failure/unreachable

aynı şey değildir. `playbook_failed`, güvenilir recap'in problem bildirdiğini
söyler; kök nedenin kesin sınıflandırması değildir.

## 13. Frontend mimarisi

Her feature genel olarak şunları içerir:

```text
api.ts          HTTP sözleşmesi
types.ts        Backend response/request tipleri
queryKeys.ts    TanStack cache kimliği
hooks.ts        Query ve eylem orkestrasyonu
components/     Görünüm
__tests__/      Kullanıcı davranışı testleri
```

Kurallar:

- API hataları `apiClient` üzerinden normalize edilir.
- Tek kullanımlık plan token'ları TanStack cache/localStorage/URL'ye konmaz;
  component belleğinde tutulur ve launch öncesi temizlenir.
- Mode kaynağı: preview form state, prepare preview planı, launch prepared plan.
- Anlam yalnız renkle verilmez; rozet/metin/event kodu korunur.
- Native form/dialog elemanları tercih edilir; yeni UI dependency'si eklemek
  için gerçek ihtiyaç gerekir.
- Stale async path browse cevabı AbortController + request id ile görünümü
  ezemez.

## 14. Test stratejisi

### Backend

```bash
cd backend
ruff check .
mypy
pytest
```

Test sınıfları:

- unit/service testleri,
- API/OpenAPI sözleşme testleri,
- gerçek SQLite constraint/migration testleri,
- stub subprocess testleri,
- yalnız localhost kullanan runner gate'leri,
- fail-closed negatif ve mutation testleri.

Production hostlara test suite içinden bağlanılmaz.

### Frontend

```bash
cd frontend
npm run typecheck
npm test -- --run
npm run build
```

Testing Library testleri DOM implementation ayrıntısı yerine kullanıcıya
görünen metin, erişilebilir rol ve request body/query sözleşmesini ölçer.
Güvenlik açısından önemli class/attribute davranışı gerektiğinde doğrudan da
kilitlenir.

### Bütünleşik kontrol

Backend venv aktifken repository kökünden:

```bash
./scripts/verify.sh
```

Dependency vulnerability audit ağ gerektirdiği için ayrı script'tir:

```bash
./scripts/security-audit.sh
```

## 15. Yeni özellik ekleme sırası

1. Kanonik belgeler ve ilgili ADR/güvenlik sınırını okuyun.
2. API request/response sözleşmesini ve forbidden alanları yazın.
3. Domain servisini route'tan bağımsız uygulayın.
4. Gerekli DB invariant ve yeni Alembic migration'ını ekleyin.
5. API route'unu ince adaptör olarak bağlayın.
6. Frontend tip/API/query key/hook/component sırasını izleyin.
7. Mutlu yol, hata, authorization/path ve stale/concurrency testlerini ekleyin.
8. Hedefli testlerden sonra full verify çalıştırın.
9. Kullanıcı, geliştirici, environment ve güvenlik belgelerini aynı commit
   zincirinde güncelleyin.

## 16. Kod inceleme kontrol listesi

- [ ] Kullanıcı girdisi shell string'e girmiyor.
- [ ] Path allowlist kontrolü varlık kontrolünden önce.
- [ ] Request model bilinmeyen alanları gerektiğinde reddediyor.
- [ ] Route'ta iş mantığı/SQL yok.
- [ ] Token/secret URL, log, cache veya response'a gereksiz girmiyor.
- [ ] Subprocess timeout ve output limiti var.
- [ ] Transaction dış I/O boyunca açık tutulmuyor.
- [ ] Worker ownership ve terminal geçişleri atomik.
- [ ] UI kök neden uydurmuyor ve anlamı yalnız renkle vermiyor.
- [ ] Runtime verisi/secret Git'e eklenmiyor.
- [ ] Migration ileri uygulanabiliyor.
- [ ] `git diff --check`, Ruff, MyPy, Pytest, TypeScript, Vitest ve build temiz.

## 17. Gelecek genişleme noktaları

AI daha sonra eklenecekse mevcut execution yoluna doğrudan bağlanmamalıdır.
Önerilen sınır:

```text
AI taslak/inceleme
  → staging artifact
  → deterministik YAML/syntax/lint kontrolleri
  → kullanıcı diff/plan incelemesi
  → mevcut prepare/token/Job hattı
```

LLM secret görmemeli, risk kararının tek kaynağı olmamalı ve otomatik Normal
execution başlatmamalıdır. Monitoring/scheduler ve çoklu worker da ayrı
tasarım dilimleridir; mevcut tek worker varsayımı sessizce genişletilmemelidir.
