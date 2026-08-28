# DORAnsible

**DOR = Deploy · Orchestrate · Report**

DORAnsible, tek bir Ansible controller üzerinde çalışan; project ve inventory
kayıtlarını, erişilebilirlik testlerini, onaylı playbook çalıştırmalarını ve
kalıcı sonuç geçmişini web arayüzünde birleştiren self-hosted bir uygulamadır.

Bu repository teslim edilen MVP'yi içerir. Yapay zekâ entegrasyonu tasarım ve
yol haritası seviyesindedir; bugünkü üründe AI endpoint'i, model çağrısı veya
otomatik AI remediation bulunmaz.

## Mevcut ürün kapsamı

- Controller üzerindeki izinli dizinlerden Ansible project kaydı ve güvenli
  playbook keşfi
- Project'e bağlı veya bağımsız YAML/INI inventory kaydı
- Inventory host/grup görünümü ve secret görünümlü değişkenlerin maskelenmesi
- Allowlist sınırları içinde salt-okunur controller path seçici
- Plan → tek kullanımlık onay → Job zinciri
- Açıkça seçilen **Check (Ansible `--check`)** ve **Normal** çalışma kipleri
- Arka plan worker'ı ve ayrı `ansible-runner` child process'i
- Kalıcı ping geçmişi ve playbook Job geçmişi
- Project/inventory adları, durum/kip filtreleri ve cursor sayfalama
- Host recap'i, sanitize edilmiş event listesi ve sınırlandırılmış ham Ansible
  görüntü çıktısı
- Ubuntu SSH ve UFW için audit/remediation örnek project'leri

## Bilinçli sınırlar

- MVP tek kullanıcılıdır; login, RBAC ve çoklu tenant desteği yoktur.
- Tek controller ve tek playbook worker kullanılır. Inventory birden çok host
  içerebilir; bu, çoklu controller orchestration değildir.
- Sürekli monitoring, scheduler, alarm ve drift motoru yoktur. Ping geçmişi,
  kullanıcı tarafından başlatılan zaman damgalı ölçümlerdir.
- Şifreli credential deposu ve become-parolası desteği yoktur. Private key
  controller dosya sisteminde, ayrı bir allowlist altında tutulur.
- Ham Ansible görüntü çıktısı sanitize edilmiş veya secret-free kabul edilmez.
- Check mode yan etkisizlik garantisi değildir; playbook'un kendi davranışı da
  incelenmelidir.
- Docker, Kubernetes ve AWX zorunlu değildir ve bu MVP'nin çalışma yolunda
  kullanılmaz.

## Mimari özet

```text
Tarayıcı (React + TypeScript)
            |
            | HTTP / JSON
            v
FastAPI + Pydantic ---- SQLite
            |
            | pending Job / lease / heartbeat
            v
       Tek worker
            |
            v
 ansible-runner child process
            |
            | SSH
            v
    Yönetilen host'lar
```

Frontend doğrudan SSH bağlantısı kurmaz. FastAPI API sözleşmesini ve iş
kurallarını uygular; Pydantic istek/cevap biçimini doğrular. Onaylanan plan bir
Job'a dönüşür, worker Job'ı sahiplenir ve `ansible-runner` hedeflere SSH ile
bağlanır. Ayrıntı için [MIMARI.md](MIMARI.md) ve
[geliştirici rehberi](docs/GELISTIRICI_REHBERI.md) kullanılmalıdır.

## Gereksinimler

- Linux controller (Ubuntu 22.04/24.04 önerilir)
- Python 3.11 veya üzeri
- Node.js 20 veya üzeri ve npm
- Hedeflerde SSH, Python 3 ve playbook gerektiriyorsa `NOPASSWD` sudo

Ansible, Windows'u control node olarak desteklemez. Frontend başka bir
cihazdan açılabilse de backend ve Ansible süreçleri controller üzerinde
çalışır; UI'daki dosya yolları controller'a aittir.

## Hızlı başlangıç

### 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps
cp .env.example .env
alembic upgrade head
```

Gerçek playbook Job'larını işleyecek worker varsayılan olarak kapalıdır.
Kontrollü yerel kurulumda `backend/.env` içine şunu ekleyin:

```dotenv
ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
```

Backend'i başlatın:

```bash
uvicorn app.main:create_app \
  --factory \
  --reload \
  --host 127.0.0.1 \
  --port 8000
```

Kontrol adresleri:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`

### 2. Frontend

Ayrı bir terminalde:

```bash
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Arayüz: `http://127.0.0.1:5173`

### 3. İlk project

Varsayılan izinli kökler şunlardır:

```text
app-data/projects
app-data/inventories
app-data/secrets
```

Örnek project'i `app-data/projects` altına kopyalayın veya
`ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` ile kendi controller dizininizi açıkça
izinli hâle getirin. Private key içeriğini inventory'ye yazmayın; anahtarı
`app-data/secrets` altında tutup inventory'de yalnız mutlak dosya yolunu
kullanın.

Uygulamanın ekran bazlı kullanımı için
[Kullanıcı Rehberi](docs/KULLANICI_REHBERI.md), bütün ayarlar için
[Geliştirme Ortamı](docs/gelistirme-ortami.md) belgesine bakın.

## Runtime verisi ve yedekleme

`app-data/` Git tarafından izlenmez ve varsayılan olarak şunları içerir:

```text
app-data/
├── database/app.db
├── projects/
├── inventories/
├── secrets/
├── jobs/
├── ping-previews/
├── execution-plans/
└── execution-runs/
```

Job geçmişinin kalması için aynı `ANSIBLEOPS_APP_DATA_DIR` ve veritabanı
kullanılmalıdır. Temiz bir yedek alınırken backend/worker durdurulmalı;
veritabanı ile `jobs`, project, inventory ve gerekli secret dosyaları birlikte,
erişimi kısıtlı bir konuma kopyalanmalıdır. Secret ve runtime verileri hiçbir
zaman Git'e eklenmemelidir.

## Doğrulama

Backend sanal ortamı aktifken repository kökünden:

```bash
./scripts/verify.sh
```

Tek tek çalıştırmak için:

```bash
cd backend
ruff check .
mypy
pytest
alembic upgrade head
```

```bash
cd frontend
npm run typecheck
npm test -- --run
npm run build
```

Ağ erişimi gerektiren bağımlılık denetimi ayrı çalışır:

```bash
./scripts/security-audit.sh
```

## Repository yapısı

```text
backend/             FastAPI, SQLAlchemy, worker ve Ansible servisleri
frontend/            React, TypeScript, Vite kullanıcı arayüzü
sample-projects/     SSH/UFW audit ve remediation örnekleri
docs/                Kanonik kullanıcı/geliştirici/işletim belgeleri
scripts/             Test, kalite ve dependency audit komutları
app-data/            Yerel runtime verisi; Git dışında
```

## Dokümantasyon

Kanonik giriş noktası: [docs/README.md](docs/README.md)

- [Adım adım kurulum ve kullanım](docs/adim-adim-kilavuz/README.md)
- [Tek dosyalık birleşik kılavuz](docs/adim-adim-kilavuz/DORANSIBLE_TAM_KURULUM_VE_KULLANIM_KILAVUZU.md)
- [Kullanıcı Rehberi](docs/KULLANICI_REHBERI.md)
- [Geliştirici Rehberi](docs/GELISTIRICI_REHBERI.md)
- [Kurulum ve yapılandırma](docs/gelistirme-ortami.md)
- [Mimari](MIMARI.md)
- [Güvenlik modeli](GUVENLIK.md)
- [Örnek Ansible project'leri](sample-projects/README.md)

Bu teslim repository'sinde yalnız ürün kaynakları, testler, örnek project'ler
ve güncel işletim belgeleri tutulur. Runtime verileri ile kişisel geliştirme
kayıtları repository kapsamı dışındadır.
