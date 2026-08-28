# 013 — Kapatma, Yedekleme ve Güncelleme

## 1. Güvenli kapatma

1. Yeni ping veya Job başlatmayın.
2. Running Job'ın terminal duruma gelmesini bekleyin.
3. Frontend terminalinde `Ctrl+C`.
4. Backend terminalinde `Ctrl+C`.
5. Terminallerde normal komut satırının geri geldiğini doğrulayın.

Terminal penceresini doğrudan kapatmak yerine önce `Ctrl+C` kullanın.

## 2. Neyi yedeklemelisiniz?

En az:

```text
app-data/database/app.db
app-data/jobs/
app-data/projects/
app-data/inventories/
app-data/secrets/
app-data/ssh/
```

Harici allowlist ile `/srv/...` altında project veya inventory kullanıyorsanız
onları ayrıca yedekleyin. Yalnız SQLite dosyası Job artifact ve Ansible içeriğini
taşımaz.

## 3. Basit kapalı-servis yedeği

Backend tamamen durduktan sonra repository kökünde:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p "$HOME/DORAnsible-Yedekleri"
tar -czf "$HOME/DORAnsible-Yedekleri/app-data-$(date +%Y%m%d-%H%M%S).tar.gz" app-data
```

Yedek secret içerir. Erişim iznini daraltın:

```bash
chmod 600 "$HOME"/DORAnsible-Yedekleri/*.tar.gz
```

Yedeği e-posta veya herkese açık bulut alanına koymayın.

## 4. Uygulamayı güncelleme

Önce yedek alın ve servisleri kapatın. Repository kökünde:

```bash
git status --short --branch
git pull --ff-only
```

Beklenmeyen yerel değişiklik varsa pull öncesi durun; `git reset --hard` veya
dosya silme komutu kullanmayın.

Backend bağımlılığı ve migration:

```bash
cd backend
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps
alembic upgrade head
```

Frontend bağımlılığı:

```bash
cd ../frontend
npm ci
```

Sonra 006. bölümdeki normal açılış sırasını uygulayın.

## 5. Başka bilgisayara taşıma

1. Yeni controller'a repository'yi clone edin.
2. Python/Node bağımlılıklarını yeniden kurun; `.venv` ve `node_modules` kopyalamayın.
3. Servisler kapalıyken `app-data` yedeğini repository köküne geri yükleyin.
4. Harici project/inventory dizinlerini aynı yola veya kontrollü yeni yola taşıyın.
5. Private key izinlerini `600`, secrets ve ssh klasörlerini `700` yapın.
6. `backend/.env` dosyasını secret-safe kanalla taşıyın veya yeniden oluşturun.
7. `alembic upgrade head` çalıştırın.
8. Health, ping ve Check Job ile doğrulayın.

Absolute path'ler değiştiyse inventory private-key yolları ve kayıtlı
project/inventory path'leri kontrollü yeniden oluşturulmalıdır.
