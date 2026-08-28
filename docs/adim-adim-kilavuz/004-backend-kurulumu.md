# 004 — Backend Kurulumu

Bu bölümün bütün komutları repository kökünden başlar.

## 1. Backend klasörüne girin

```bash
cd "$HOME/Projeler/DORAnsible/backend"
```

Projeyi başka klasöre indirdiyseniz kendi yolunuzu kullanın. Kontrol:

```bash
pwd
ls
```

Çıktıda `pyproject.toml`, `alembic.ini`, `app` ve `tests` görünmelidir.

## 2. Python sanal ortamını oluşturun

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Komut satırının başında `(.venv)` görünür. Bu, bağımlılıkların sistem Python'u
yerine projeye özel alana kurulacağını gösterir.

Yeni terminal açtığınızda sanal ortam otomatik etkinleşmez. Backend komutlarından
önce yeniden şunu çalıştırın:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
```

## 3. Python bağımlılıklarını kurun

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps
```

İlk kurulum komutu FastAPI, SQLAlchemy, Ansible Core, ansible-runner ve test
araçlarını repository'de doğrulanmış tam sürümlerle kurar. Son komut DORAnsible
backend paketini bu bağımlılıkları yeniden çözmeden editable olarak ekler. Sistem
genelinde ayrıca `apt install ansible` yapmanız gerekmez.

Kontrol edin:

```bash
python --version
ansible --version
ansible-inventory --version
ansible-runner --version
```

## 4. Environment dosyasını oluşturun

```bash
cp .env.example .env
nano .env
```

Dosyanın sonuna şu satırı ekleyin:

```dotenv
ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
```

Kaydedip çıkın. Worker kapalı kalırsa plan ve Job oluşur fakat Job `pending`
durumunda bekler.

İlk yerel kurulumda diğer varsayılanları değiştirmeyin. Özellikle `.env`
dosyasına private key, parola veya token yazmayın.

## 5. Veritabanını hazırlayın

Sanal ortam aktif ve hâlâ `backend/` klasöründeyken:

```bash
alembic upgrade head
alembic current
```

İlk komut SQLite şemasını oluşturur/günceller. İkinci komut güncel migration
revision'ını göstermelidir.

## 6. Backend'i başlatın

```bash
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Bu terminali açık bırakın. `Application startup complete` benzeri bir satır
başarılı açılışı gösterir.

Yanlış komut:

```text
uvicorn app.main:app
```

Uygulama global `app` nesnesi değil `create_app()` factory'si kullandığı için
`--factory` zorunludur.

## 7. Hızlı backend kontrolü

Ayrı terminal açın:

```bash
curl -fsS http://127.0.0.1:8000/health
```

JSON içinde `ok` görmelisiniz. Tarayıcıdan API dokümantasyonu:

```text
http://127.0.0.1:8000/docs
```

## 8. Bölüm sonu kontrolü

- [ ] `backend/.venv` oluşturuldu.
- [ ] `ansible`, `ansible-inventory` ve `ansible-runner` sürüm gösteriyor.
- [ ] `backend/.env` var ve worker açık.
- [ ] `alembic upgrade head` hatasız.
- [ ] Health cevabı başarılı.
- [ ] Backend terminali açık kalıyor.
