# DORAnsible Kurulum ve Geliştirme Ortamı

Bu belge, temiz bir Linux controller üzerinde repository'yi çalıştırma,
yapılandırma, doğrulama ve yedekleme adımlarını açıklar.

## 1. Desteklenen çalışma modeli

- Controller: Linux (Ubuntu 22.04/24.04 önerilir)
- Python: 3.11+
- Node.js: 20+
- Veritabanı: varsayılan SQLite
- Backend: `127.0.0.1:8000`
- Frontend geliştirme sunucusu: `127.0.0.1:5173`
- Playbook worker: aynı backend prosesi içinde tek thread
- Ansible bağlantısı: controller'dan hedeflere SSH

Ansible Windows control node'u desteklemediği için tam ürün akışı Windows'ta
desteklenmez. Frontend build araçları Windows'ta çalışabilse de inventory,
ping ve runner Linux controller gerektirir.

## 2. Sistem paketleri

Ubuntu örneği:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssh-client
```

Node.js 20+ ve npm'i kurumunuzun onaylı paket kaynağından kurun. Sürümleri
kontrol edin:

```bash
python3 --version
node --version
npm --version
ssh -V
```

## 3. Repository

```bash
git clone https://github.com/yusufaydinn/doransible.git DORAnsible
cd DORAnsible
git status --short --branch
```

Runtime verisi `app-data/` altında oluşur ve Git tarafından izlenmez.

## 4. Backend kurulumu

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
```

Kurulum kontrolü:

```bash
ansible --version
ansible-inventory --version
ansible-runner --version
```

Backend başlatma:

```bash
uvicorn app.main:create_app \
  --factory \
  --reload \
  --host 127.0.0.1 \
  --port 8000
```

Health:

```bash
curl -fsS http://127.0.0.1:8000/health
```

`app.main:app` kullanılmaz; modül global ASGI app nesnesi değil
`create_app()` factory'si sunar. Uvicorn komutunda `--factory` zorunludur.

## 5. Frontend kurulumu

Ayrı terminal:

```bash
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Varsayılan API ayarı:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Frontend'i başka cihazdan açacaksanız backend bind adresi, CORS, firewall ve
TLS/reverse proxy sınırlarını ayrıca tasarlayın. MVP varsayılanı yalnız yerel
geliştirmedir.

## 6. Environment ayarları

Backend ayarları `ANSIBLEOPS_` ön ekiyle okunur. JSON liste alanları gerçek
JSON dizisi biçiminde yazılmalıdır.

### Temel

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `ANSIBLEOPS_APP_NAME` | `DORAnsible` | Health/UI görünen adı |
| `ANSIBLEOPS_ENVIRONMENT` | `development` | Ortam etiketi |
| `ANSIBLEOPS_APP_DATA_DIR` | repo `app-data` | DB, plan, run ve artifact kökü |
| `ANSIBLEOPS_DATABASE_URL` | SQLite | Verilirse SQLAlchemy DSN |
| `ANSIBLEOPS_CORS_ORIGINS` | localhost/127.0.0.1:5173 | İzinli frontend origin'leri |
| `ANSIBLEOPS_LOCAL_ACTOR` | `local-single-user` | Authentication olmayan MVP'nin sabit aktör etiketi |

Varsayılan SQLite dosyası:

```text
app-data/database/app.db
```

### Dosya allowlist'leri

| Değişken | Boşsa kullanılan kök |
|---|---|
| `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` | `app-data/projects` |
| `ANSIBLEOPS_INVENTORY_ROOT_ALLOWLIST` | `app-data/inventories` |
| `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST` | `app-data/secrets` |

Örnek:

```dotenv
ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST=["/srv/ansible/projects"]
ANSIBLEOPS_INVENTORY_ROOT_ALLOWLIST=["/srv/ansible/inventories"]
ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST=["/srv/doransible/secrets"]
```

Project'e bağlı inventory genel inventory allowlist'inden değil, bağlı aktif
project'in kendi doğrulanmış kökünden izin alır.

### Inventory ve playbook keşfi

| Değişken | Varsayılan |
|---|---:|
| `ANSIBLEOPS_ANSIBLE_INVENTORY_COMMAND` | `["ansible-inventory"]` |
| `ANSIBLEOPS_INVENTORY_PARSE_TIMEOUT_SECONDS` | `30` |
| `ANSIBLEOPS_INVENTORY_PARSE_MAX_OUTPUT_BYTES` | `5000000` |
| `ANSIBLEOPS_PLAYBOOK_SCAN_MAX_DEPTH` | `12` |
| `ANSIBLEOPS_PLAYBOOK_SCAN_MAX_ENTRIES` | `20000` |
| `ANSIBLEOPS_PLAYBOOK_SCAN_MAX_RESULTS` | `500` |
| `ANSIBLEOPS_PLAYBOOK_SCAN_READ_BYTES` | `65536` |

Komut ayarları shell metni değil JSON argv listesidir:

```dotenv
ANSIBLEOPS_ANSIBLE_INVENTORY_COMMAND=["/opt/doransible/bin/ansible-inventory"]
```

### SSH ve ping

| Değişken | Varsayılan |
|---|---:|
| `ANSIBLEOPS_ANSIBLE_AD_HOC_COMMAND` | `["ansible"]` |
| `ANSIBLEOPS_SSH_HOST_KEY_POLICY` | `strict` |
| `ANSIBLEOPS_SSH_KNOWN_HOSTS_PATH` | `app-data/ssh/known_hosts` |
| `ANSIBLEOPS_SSH_CONNECT_TIMEOUT_SECONDS` | `10` |
| `ANSIBLEOPS_PING_TIMEOUT_SECONDS` | `30` |
| `ANSIBLEOPS_PING_FORKS` | `10` |
| `ANSIBLEOPS_PING_MAX_OUTPUT_BYTES` | `5000000` |
| `ANSIBLEOPS_PING_PREVIEW_TTL_SECONDS` | `300` |
| `ANSIBLEOPS_PING_PREVIEW_CLAIM_STALE_SECONDS` | güvenli alt sınır |
| `ANSIBLEOPS_PING_PREVIEW_MAX_LISTED_HOSTS` | `500` |
| `ANSIBLEOPS_JOB_STALE_SECONDS` | `300` |

`strict`, bilinmeyen host key'i reddeder. `accept_new` ilk anahtarı TOFU ile
kabul eder; doğrulamayı tamamen kapatan seçenek yoktur.

### Execution planı, worker ve runner

| Değişken | Varsayılan |
|---|---:|
| `ANSIBLEOPS_EXECUTION_PLAN_TTL_SECONDS` | `600` |
| `ANSIBLEOPS_EXECUTION_PLAN_STAGING_STALE_SECONDS` | `900` |
| `ANSIBLEOPS_ANSIBLE_RUNNER_COMMAND` | `["ansible-runner"]` |
| `ANSIBLEOPS_PLAYBOOK_RUNNER_TIMEOUT_SECONDS` | `1800` |
| `ANSIBLEOPS_PLAYBOOK_RUNNER_MAX_STDOUT_BYTES` | `5000000` |
| `ANSIBLEOPS_PLAYBOOK_RUNNER_MAX_RAW_BYTES` | `50000000` |
| `ANSIBLEOPS_PLAYBOOK_RUNNER_MAX_EVENTS` | `20000` |
| `ANSIBLEOPS_PLAYBOOK_RUNNER_MAX_RESULT_BYTES` | `1000000` |
| `ANSIBLEOPS_PLAYBOOK_WORKER_LEASE_SECONDS` | `120` |
| `ANSIBLEOPS_PLAYBOOK_WORKER_HEARTBEAT_SECONDS` | `30` |
| `ANSIBLEOPS_PLAYBOOK_WORKER_POLL_SECONDS` | `1` |
| `ANSIBLEOPS_EXECUTION_RUN_STALE_SECONDS` | `2700` |
| `ANSIBLEOPS_EXECUTION_RUN_JANITOR_INTERVAL_SECONDS` | `600` |
| `ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED` | `false` |

Normal ve Check Job'larının gerçekten çalışması için kontrollü kurulumda:

```dotenv
ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
```

Bu ayar bir sandbox değildir. Worker, izinli project'e kaydedilmiş playbook'u
seçilen kipte gerçek hedeflerde çalıştırabilir. Yalnız güvendiğiniz playbook ve
inventory'leri kaydedin.

Sayısal ayarlar sonlu, pozitif ve bazı durumlarda birbirleriyle ilişkili
sınırlara tabidir. Geçersiz ayar sessizce clamp edilmez; Settings oluşturulurken
hata verir.

## 7. Runtime dizinleri

Backend açılışta gerekli dizinleri oluşturur:

```text
app-data/
├── database/
├── projects/
├── inventories/
├── jobs/
├── staging/
├── secrets/
├── ping-previews/
├── execution-plans/
├── execution-runs/
└── ssh/
```

`app-data` dışında project/inventory allowlist'i kullanıyorsanız onların
yedeği ayrı alınmalıdır. Uygulama kayıt sırasında bu dosyaları kendi içine
kopyalamaz.

## 8. Migration

Güncel şemaya yükseltme:

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
```

Mevcut revision:

```bash
alembic current
alembic heads
```

Yeni model alanı eklenirse mevcut migration dosyasını değiştirmeyin:

```bash
alembic revision -m "describe change"
```

Migration'ı hem temiz veritabanında hem önceki revision'dan yükseltmede test
edin.

## 9. Test ve kalite

Backend:

```bash
cd backend
source .venv/bin/activate
ruff check .
mypy
pytest
```

Frontend:

```bash
cd frontend
npm run typecheck
npm test -- --run
npm run build
```

Hepsi:

```bash
cd /path/to/repository
source backend/.venv/bin/activate
./scripts/verify.sh
```

Frontend'i atlayarak yalnız backend/güvenlik gate'i:

```bash
SKIP_FRONTEND=1 ./scripts/verify.sh
```

Bağımlılık güvenlik denetimi güncel advisory verisi için ağ ister:

```bash
./scripts/security-audit.sh
```

## 10. Sample project doğrulama

Her sample project kendi README ve offline test script'ine sahiptir. Örnek:

```bash
cd sample-projects/ubuntu-ssh-hardening
PATH="../../../backend/.venv/bin:$PATH" ./tests/run_offline_tests.sh
```

Gerçek host testi offline suite'in yerine geçmez; offline test de lockout,
dağıtım farkı veya ağ davranışını tek başına kanıtlamaz.

## 11. Yedekleme ve taşıma

Güvenli yerel yedek için:

1. Yeni Job başlatmayı durdurun.
2. Aktif Job'ın bitmesini bekleyin.
3. Backend'i kapatın; böylece worker ve SQLite yazmaları durur.
4. `app-data` dizinini erişimi kısıtlı bir hedefe kopyalayın.
5. Harici project/inventory allowlist köklerini ayrıca yedekleyin.
6. Private key'leri Git/e-posta gibi güvensiz kanalla taşımayın.

Geri yüklemede aynı `ANSIBLEOPS_APP_DATA_DIR`, project/inventory yolları ve
anahtar path'leri korunmalı veya kayıtlar kontrollü yeniden oluşturulmalıdır.
Yalnız SQLite dosyasını taşımak Job artifact ve frozen workspace'leri taşımaz.

## 12. Yaygın kurulum hataları

### `Attribute "app" not found in module "app.main"`

Yanlış:

```bash
uvicorn app.main:app
```

Doğru:

```bash
uvicorn app.main:create_app --factory
```

### Backend açılıyor ama Job pending kalıyor

`.env` içinde worker'ı açın ve backend'i yeniden başlatın:

```dotenv
ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
```

### `playbook worker execution attempt failed`

Bu sabit log tek başına kök nedeni içermez. Job detayı/result, controller
artifact'ı, runner binary'si, allowlist, key ve host erişimi sırayla incelenir.
Loglara secret veya ham exception ekleyerek “kolay hata ayıklama” yapılmaz.

### Project path 403

Controller'daki gerçek path'in project allowlist altında olduğunu ve backend
proses kullanıcısının parent dizinleri traverse edebildiğini doğrulayın.
Symlink'in çözüldüğü hedef allowlist dışındaysa reddedilir.

### Inventory parser unavailable

```bash
which ansible-inventory
ansible-inventory --version
```

Farklı venv kullanılıyorsa `ANSIBLEOPS_ANSIBLE_INVENTORY_COMMAND` argv'sini
mutlak binary yoluyla ayarlayın.

### Known host veya key hatası

- Key dosyası SSH key allowlist altında mı?
- Dosya modu dar mı (`chmod 600`)?
- Inventory'de key'in mutlak controller yolu doğru mu?
- `strict` politikasında host key önceden doğrulanmış mı?

## 13. Kurumsal proxy/TLS

`pip` veya `npm` sertifika hatasında TLS doğrulamasını kapatmayın. Kurumun CA
sertifikasını işletim sistemi, Python/pip ve npm trust store'una doğru biçimde
ekleyin. `NODE_TLS_REJECT_UNAUTHORIZED=0`, pip `--trusted-host` veya benzeri
kalıcı doğrulama atlamaları güvenli çözüm değildir.

## 14. Teslim öncesi temizlik

```bash
git status --short --branch
git diff --check
git ls-files | rg '(^|/)(\.env|app-data|node_modules|\.venv)(/|$)' || true
git grep -nE 'BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY|password\s*[:=]' -- . \
  ':(exclude)*.lock' || true
```

Cache, venv, `node_modules`, runtime database, artifact ve secret dosyaları
Git'e eklenmez. Sunum/rapor çıktıları bilinçli teslim artifact'ıysa tutulur;
geçici ekran görüntüsü veya yerel VM dosyası repository'ye konmaz.
