# 014 — Sorun Giderme

Sorun çözerken önce hangi katmanın bozuk olduğunu ayırın: işletim sistemi,
backend, frontend, inventory parser, SSH, worker veya playbook sonucu.

## 1. `command not found`

| Hata | Çözüm |
|---|---|
| `python3: command not found` | 002. bölümdeki apt paketlerini kurun |
| `node: command not found` | nvm'i yükleyin, terminali yeniden açın, `nvm use --lts` |
| `ansible-inventory: command not found` | Backend `.venv` ortamını etkinleştirin; `requirements.lock.txt` ve uygulamayı 004. bölümdeki komutlarla kurun |
| `uvicorn: command not found` | Backend `.venv` aktif değil veya bağımlılık kurulmadı |

## 2. Sanal ortam aktif değil

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
which python
```

`which python` yolu `backend/.venv/bin/python` ile bitmelidir.

## 3. `Attribute "app" not found in module "app.main"`

Yanlış:

```text
uvicorn app.main:app
```

Doğru:

```bash
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

## 4. Port kullanımda

`Address already in use` görürseniz önce eski backend/frontend terminalini
bulun ve `Ctrl+C` ile kapatın. Rastgele proses öldürmeyin. Hangi prosesin portu
kullandığını görmek için:

```bash
ss -ltnp | rg ':8000|:5173'
```

`rg` kurulu değilse:

```bash
ss -ltnp | grep -E ':8000|:5173'
```

## 5. Frontend açılıyor ama veri gelmiyor

```bash
curl -fsS http://127.0.0.1:8000/health
cat frontend/.env.local
```

Backend çalışmalı ve frontend API adresi `http://127.0.0.1:8000` olmalıdır.
Frontend environment değiştiyse `npm run dev` sürecini yeniden başlatın.

## 6. Project path 403 / path dialogu boş

- Varsayılan project kökü `app-data/projects`.
- Bağımsız inventory kökü `app-data/inventories`.
- Project'e bağlı inventory, seçili project'in altında olmalıdır.
- Symlink veya allowlist dışı çözülen yol reddedilir.
- Backend kullanıcısının parent klasörleri traverse etme izni olmalıdır.

En kolay başlangıç çözümü sample project'i `app-data/projects` altına
kopyalamaktır.

## 7. Inventory parse edilemiyor

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
ansible-inventory -i /TAM/YOL/hosts.yml --list
```

YAML girintisi, dosya biçimi, mutlak path ve project'in aktifliği kontrol edilir.

## 8. Ping unreachable

Sırayla kontrol edin:

1. Hedef açık mı, IP doğru mu?
2. `ping <HEDEF_IP>` ağda izinliyse cevap veriyor mu?
3. `nc -vz <HEDEF_IP> 22` ile SSH portu açık mı? (`netcat-openbsd` gerekebilir.)
4. 007. bölümdeki tam SSH komutu çalışıyor mu?
5. Key yolu mutlak mı ve allowlist altında mı?
6. Key dosya modu `600` mü?
7. DORAnsible `app-data/ssh/known_hosts` kaydı doğru mu?
8. Hedefte Python 3 var mı?

Host yeniden kurulduysa host key değişmiş olabilir. Eski kaydı körlemesine
silmeden önce değişikliğin meşru olduğunu güvenilir kanaldan doğrulayın.

## 9. Become parola istiyor

DORAnsible become parolası göndermez. Hedefte:

```bash
sudo -u automation sudo -n true
```

Başarısızsa hedef sudo politikası hazır değildir. Yalnız laboratuvarda 007.
bölümdeki NOPASSWD adımını uygulayın.

## 10. Job pending kalıyor

```bash
grep ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED backend/.env
```

Değer `true` olmalı ve backend bu ayardan sonra yeniden başlatılmalıdır. Tek
worker başka Job çalıştırıyorsa sıradaki Job bekler.

## 11. Job failed

Önce Job sonuç ekranında hata sınıfını, recap'i ve failed/unreachable task'ları
okuyun. `playbook_failed`, uygulama altyapısının bozuk olduğunu otomatik
göstermez; audit uygunsuzluğu olabilir. `runner_*` ve `result_*` kodlarında
backend terminali ve ilgili `app-data/jobs` artifact'ı secret sızdırmadan
incelenir.

## 12. Kurulum komutu ağ/sertifika hatası veriyor

Kurumsal proxy veya özel CA söz konusu olabilir. Şunları kalıcı çözüm olarak
kullanmayın:

```text
NODE_TLS_REJECT_UNAUTHORIZED=0
pip --trusted-host ...
SSL doğrulamasını kapatma
```

Kurumun CA sertifikasını Ubuntu, pip ve npm trust store'una doğru şekilde
eklemek için sistem yöneticisine başvurun.

## 13. Yardım isterken paylaşılacak bilgi

Secret'ları silerek şunları paylaşın:

```bash
cat /etc/os-release
python3 --version
node --version
npm --version
git status --short --branch
curl -i http://127.0.0.1:8000/health
```

Private key, `.env`, token, inventory secret'ı ve ham Ansible çıktısını doğrudan
paylaşmayın.
