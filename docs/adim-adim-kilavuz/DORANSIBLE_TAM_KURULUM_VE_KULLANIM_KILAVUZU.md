# DORAnsible Tam Kurulum ve Kullanım Kılavuzu

Bu belge, numaralı kurulum ve kullanım rehberlerinin tek dosyada birleştirilmiş
sürümüdür. Temiz bir Ubuntu controller kurulumu, DORAnsible yapılandırması,
hedef hazırlığı ve web arayüzündeki çalışma akışını kapsar.

## İçindekiler

1. Kılavuzun kapsamı ve uygulama akışı
2. Sistem resmi ve temel kavramlar
3. Ubuntu ve gereksinimler
4. Repository'yi indirme
5. Backend kurulumu
6. Frontend kurulumu
7. İlk açılış ve sağlık kontrolü
8. Hedef makine ve SSH
9. Project ve Inventory
10. Ping
11. Playbook çalıştırma
12. Sonuç ve geçmiş
13. Örnek uçtan uca senaryo
14. Kapatma, yedekleme ve güncelleme
15. Sorun giderme
16. Güvenlik sınırları ve sözlük

---

## 1. Kılavuzun kapsamı ve uygulama akışı

Ana kurulum yolu Ubuntu 24.04 LTS controller, aynı controller üzerinde backend,
worker ve frontend süreçleri, SSH erişimli ayrı bir Ubuntu hedef ve tek güvenilir
operatör modelini esas alır.

Kurulumdan önce controller üzerinde `sudo` yetkisi, GitHub repository erişimi,
hedef makinenin adresi, hedef SSH hesabı ve gerektiğinde hedef konsol erişimi
hazır olmalıdır.

İlk kurulum sırası:

1. Controller gereksinimlerini kurun.
2. Repository'yi indirin ve kullanılacak sürümü doğrulayın.
3. Backend ve frontend bağımlılıklarını kurun.
4. Uygulama veri dizini ile environment ayarlarını hazırlayın.
5. Backend/worker ve frontend'i başlatıp sağlık kontrollerini yapın.
6. Hedefi SSH erişimine hazırlayın.
7. Project ve Inventory oluşturup Ping çalıştırın.
8. Execution planını inceleyip Check veya Normal kipte çalıştırın.
9. Job durumunu, recap'i ve bounded Ansible çıktısını inceleyin.

Komutun çalıştırılacağı yer her adımda belirtilir. “Repository kökü”;
`backend/`, `frontend/`, `docs/` ve `scripts/` dizinlerini içeren DORAnsible
dizinidir. “Backend dizini” ve “Frontend dizini” bunun altındaki ilgili
dizinlerdir. “Hedefte” denmeyen komutlar controller üzerinde çalıştırılır.

`<HEDEF_IP>`, `<KULLANICI>` ve `<REPOSITORY_URL>` gibi örnek değerleri kendi
ortamınıza göre değiştirin. Parola, token, private key içeriği veya başka bir
secret'ı belgeye, komut geçmişine ya da repository'ye eklemeyin.

DORAnsible gerçek hedeflere SSH ile bağlanıp playbook çalıştırır. Normal kip
hedefi gerçekten değiştirebilir; Check kip mutlak yan etkisizlik garantisi
vermez. İlk çalıştırmayı snapshot alınabilen veya yeniden oluşturulabilen bir
Ubuntu test makinesinde doğrulayın. Seçilen Project/Inventory/playbook,
execution planı veya SSH kimliği beklediğiniz değerlerle eşleşmiyorsa Normal
kipte devam etmeyin.

---

## 2. Sistem resmi ve temel kavramlar

DORAnsible; project ve inventory kayıtlarını, erişilebilirlik testini, onaylı
playbook çalıştırmasını ve sonucu web arayüzünde birleştirir. Bugünkü üründe
AI endpoint'i veya otomatik AI remediation yoktur.

```text
Tarayıcı → React frontend → FastAPI + SQLite + worker
                                      |
                                      v
                            ansible-runner → SSH hedefleri
```

| Terim | Açıklama |
|---|---|
| Controller | Backend, worker, Ansible ve dosyaların bulunduğu Linux makine |
| Yönetilen host | Ansible'ın SSH ile bağlandığı hedef |
| Project | Playbook, role ve ilgili dosyaların klasörü |
| Inventory | Host, grup, SSH adresi/kullanıcısı/key yolunu tanımlar |
| Playbook | YAML biçimindeki otomasyon adımları |
| Task | Tek bir kontrol veya değişiklik işlemi |
| Job | Onaylanmış playbook çalıştırmasının kalıcı kaydı |
| Check | Runner'a `--check` eklenen kip |
| Normal | `--check` olmadan gerçek Ansible davranışı |

Check mümkün olduğunda simülasyon yapar fakat yan etkisizlik garantisi değildir.
Normal hedefi gerçekten değiştirebilir ve otomatik rollback garantisi yoktur.

Ürün tek güvenilir operatör, tek controller ve tek worker içindir. Login/RBAC,
scheduler, alarm, Vault/become parolası ve uygulama-yönetimli credential deposu
yoktur.

---

## 3. Ubuntu ve gereksinimler

### İşletim sistemi

Ana yol Ubuntu 24.04 LTS'tir. Resmî kurulum:

<https://documentation.ubuntu.com/desktop/en/24.04/tutorial/install-ubuntu-desktop/>

Kurulum disk verisini silebilir; önce yedek alın. Windows doğrudan Ansible
controller değildir. Windows kullanıcısı ayrı Ubuntu makine/VM kullanmalıdır.

### Sistem paketleri

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssh-client curl ca-certificates
```

```bash
python3 --version
git --version
ssh -V
curl --version
```

Python en az 3.11 olmalıdır. Ubuntu 24.04 normalde Python 3.12 sağlar.

### Node.js

Yeni kurulumda güncel LTS Node.js kullanın. Proje minimumu Node 20+ olsa da
EOL sürüm kurulmaz. Güncel LTS ve nvm komutu:

- <https://nodejs.org/en/download>
- <https://nodejs.org/en/about/previous-releases>

26 Ağustos 2026 tarihinde resmî Node indirme sayfasında gösterilen nvm komutu
aşağıdadır. Sayfadaki sürüm daha yeniyse resmî sayfadaki komutu kullanın:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh | bash
\. "$HOME/.nvm/nvm.sh"
```

Ardından:

```bash
nvm install --lts
nvm use --lts
node --version
npm --version
```

Kurumsal TLS hatasında doğrulamayı kapatmayın; kurum CA'sını trust store'a
ekletin.

Resmî başvuru:

- <https://git-scm.com/install/linux>
- <https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html>
- <https://docs.ansible.com/projects/ansible/latest/os_guide/intro_windows.html#using-windows-as-the-control-node>

---

## 4. Repository'yi indirme

```bash
mkdir -p "$HOME/Projeler"
cd "$HOME/Projeler"
git clone https://github.com/yusufaydinn/doransible.git DORAnsible
cd DORAnsible
```

Özel repository için GitHub erişimi gerekir. Personal access token'ı komuta
veya belgeye yazmayın.

```bash
pwd
ls
git status --short --branch
git switch main
git pull --ff-only
```

`backend`, `frontend`, `sample-projects`, `docs` ve `scripts` görünmelidir.
Size özel release/tag/commit verilmişse onu kullanın. Aksi durumda GitHub
repository'sinin varsayılan `main` dalını kullanın. Beklenmeyen yerel değişiklik
varsa güncelleme öncesi durun.

---

## 5. Backend kurulumu

```bash
cd "$HOME/Projeler/DORAnsible/backend"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps
```

Kilit dosyası doğrulanmış bağımlılık sürümlerini kurar; son komut uygulama
paketini bağımlılıkları yeniden çözmeden editable olarak ekler.

Kontrol:

```bash
python --version
ansible --version
ansible-inventory --version
ansible-runner --version
```

Environment oluşturun:

```bash
cp .env.example .env
nano .env
```

Dosyanın sonuna ekleyin:

```dotenv
ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
```

`.env` içine key, parola veya token yazmayın.

Veritabanı:

```bash
alembic upgrade head
alembic current
```

Backend:

```bash
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Bu terminal açık kalır. Ayrı terminalden:

```bash
curl -fsS http://127.0.0.1:8000/health
```

API dokümanı: `http://127.0.0.1:8000/docs`.

Her yeni backend terminalinde önce:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
```

---

## 6. Frontend kurulumu

Backend'i kapatmadan yeni terminal açın:

```bash
cd "$HOME/Projeler/DORAnsible/frontend"
npm ci
cp .env.example .env.local
npm run dev
```

`.env.local` şu API adresini taşımalıdır:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Tarayıcıdan `http://127.0.0.1:5173` adresini açın. Genel bakış,
Project'ler, Inventory'ler ve Çalıştırmalar menülerini görmelisiniz.

Frontend environment'a hiçbir secret yazmayın; `VITE_` değerleri tarayıcıya
gönderilebilir.

---

## 7. İlk açılış ve sağlık kontrolü

Her normal kullanımda bağımlılıkları tekrar kurmazsınız.

Terminal 1:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Terminal 2:

```bash
cd "$HOME/Projeler/DORAnsible/frontend"
npm run dev
```

Kontroller:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:5173`

Runtime veri repository kökündeki `app-data/` altında tutulur:

```text
database/app.db     SQLite
projects/           varsayılan project kökü
inventories/        bağımsız inventory kökü
secrets/            private key dosyaları
ssh/known_hosts     DORAnsible SSH host key kayıtları
jobs/               Job sonuç/artifact'ları
execution-plans/    kısa ömürlü dondurulmuş planlar
execution-runs/     runner çalışma alanları
```

`app-data` klasörünü silmek yerel kayıt ve geçmiş kaybıdır. Kılavuz yalnız
localhost'ta çalıştırır; login/RBAC olmayan MVP'yi `0.0.0.0` ile ağa açmayın.

---

## 8. Hedef makine ve SSH

İlk hedef ayrı bir Ubuntu test VM'si olmalıdır.

### Hedefte

```bash
sudo apt update
sudo apt install -y openssh-server python3 sudo
sudo systemctl enable --now ssh
ip -br address
sudo adduser automation
sudo usermod -aG sudo automation
```

IP'yi not edin. Audit/hardening için disposable laboratuvarda:

```bash
sudo visudo -f /etc/sudoers.d/doransible-automation
```

Satır:

```text
automation ALL=(ALL) NOPASSWD: ALL
```

```bash
sudo chmod 440 /etc/sudoers.d/doransible-automation
sudo visudo -cf /etc/sudoers.d/doransible-automation
```

Bu geniş sudo yalnız laboratuvar içindir.

### Controller'da key

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p app-data/secrets
chmod 700 app-data/secrets
ssh-keygen -t ed25519 -f app-data/secrets/doransible_demo -C doransible-demo
chmod 600 app-data/secrets/doransible_demo
chmod 644 app-data/secrets/doransible_demo.pub
ssh-copy-id -i app-data/secrets/doransible_demo.pub automation@<HEDEF_IP>
```

Private key içeriğini inventory'ye veya Git'e yazmayın.

### DORAnsible known_hosts

DORAnsible `~/.ssh/known_hosts` yerine kendi dosyasını kullanır:

```bash
mkdir -p app-data/ssh
chmod 700 app-data/ssh
ssh-keyscan -p 22 <HEDEF_IP> > /tmp/doransible-host-key
ssh-keygen -lf /tmp/doransible-host-key
```

Fingerprint'i hedef konsolu veya güvenilir ayrı kanalla karşılaştırın. Eşleşirse:

```bash
cp /tmp/doransible-host-key app-data/ssh/known_hosts
chmod 600 app-data/ssh/known_hosts
```

`ssh-keyscan` tek başına host kimliğini doğrulamaz.

### Uçtan uca SSH testi

```bash
ssh -F /dev/null \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$PWD/app-data/ssh/known_hosts" \
  -o StrictHostKeyChecking=yes \
  -i "$PWD/app-data/secrets/doransible_demo" \
  automation@<HEDEF_IP> \
  'python3 --version && sudo -n true && echo DORANSIBLE_SSH_OK'
```

Parola sorulmadan `DORANSIBLE_SSH_OK` görünmelidir.

---

## 9. Project ve Inventory

Örnek SSH audit project'ini runtime alanına kopyalayın:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p app-data/projects
cp -a sample-projects/ubuntu-ssh-audit app-data/projects/
realpath app-data/projects/ubuntu-ssh-audit
realpath app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml
realpath app-data/secrets/doransible_demo
```

Inventory'yi düzenleyin:

```bash
nano app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml
```

Örnek:

```yaml
all:
  children:
    ssh_audit_targets:
      hosts:
        ubuntu-demo:
          ansible_host: 192.168.1.50
          ansible_port: 22
          ansible_user: automation
          ansible_python_interpreter: /usr/bin/python3
          ansible_ssh_private_key_file: /home/kullanici/Projeler/DORAnsible/app-data/secrets/doransible_demo
```

IP ve key yolunu değiştirin. Key içeriğini yazmayın. YAML'da Tab yerine boşluk
kullanın.

Parser kontrolü:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
ansible-inventory -i ../app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml --list
```

UI'da:

1. **Project'ler → Yeni project ekle**.
2. Ad: `Ubuntu SSH Audit Demo`.
3. Path: `realpath app-data/projects/ubuntu-ssh-audit` çıktısı.
4. Kaydedin.
5. **Inventory'ler → Yeni inventory kaydet**.
6. Ad: `Ubuntu Demo Host`, biçim YAML.
7. Project olarak yeni kaydı seçin.
8. Path olarak project içindeki `inventory/hosts.yml` tam yolunu verin.
9. Kaydedin.

Varsayılan allowlist nedeniyle `sample-projects` doğrudan kaydedilmeyebilir;
runtime kopyası `app-data/projects` altında olmalıdır.

---

## 10. Ping

DORAnsible ping, ICMP değildir; SSH ile `ansible.builtin.ping` çalıştırır.

1. Inventory detayını açın.
2. Erişilebilirlik önizlemesini oluşturun.
3. Host adı ve sayısını inceleyin.
4. Tek kullanımlık onayı verin.
5. Sonucu bekleyin.

| Sonuç | Yorum |
|---|---|
| Erişilebilir | SSH ve Ansible ping başarılı |
| Erişilemiyor | Ağ/port/user/key/host key nedenlerinden biri olabilir |
| Başarısız | Bağlantı sonrası modül çalışması hata verdi |
| Sonuç alınamadı | Güvenilir host sonucu üretilemedi |

Ping geçmişi kalıcı kullanıcı ölçümleridir; monitoring veya alarm değildir.

Yardımcı doğrudan Ansible testi:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
ansible all -i ../app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml -m ansible.builtin.ping
```

---

## 11. Playbook çalıştırma

1. Project detayını açın.
2. İlk denemede **Check** seçin.
3. Doğru Inventory'yi seçin.
4. `ubuntu-ssh-audit.yml` seçin.
5. **Planı Oluştur**.
6. Project, inventory, playbook, mode, host listesi ve host key politikasını
   kontrol edin.
7. **Onaya Hazırla** ile içeriği dondurun.
8. Onay metnini okuyun ve kutuyu işaretleyin.
9. **Onayla ve Çalıştır**.

201 cevabı “pending Job yazıldı” anlamına gelir, Job tamamlandı anlamına gelmez.

```text
pending → running → successful
                  └→ failed
```

Pending kalırsa worker ayarını kontrol edip backend'i yeniden başlatın.

Check sandbox değildir. Normal ise gerçek değişiklik yapar; dosya/paket/servis,
SSH ve firewall etkilenebilir, bağlantı kesilebilir ve kısmi değişiklik kalabilir.
Check planı Normal Job'a dönüştürülemez; Normal için ayrı plan ve açık onay gerekir.

Audit Job'ının `failed` olması runner arızası olmak zorunda değildir; baseline
uygunsuzluğu playbook tarafından raporlanmış olabilir.

---

## 12. Sonuç ve geçmiş

Job detayında durum, mode, project, inventory, playbook, zamanlar, return code
ve güvenli hata sınıfı gösterilir.

Recap sayaçları:

| Alan | Anlamı |
|---|---|
| ok | Başarılı/zaten uygun task |
| changed | Değişiklik raporlayan task |
| failures | Başarısız task |
| unreachable | Erişilemeyen host |
| skipped | Atlanan task |
| rescued | Rescue ile ele alınan hata |
| ignored | Devam edilmesine izin verilen hata |

Event listesi host, task, event türü ve changed/failed gibi normalize alanları
gösterir; ham result payload'ını açmaz.

Kapalı detay içindeki Ansible görüntü çıktısı en fazla 128 KiB'tır fakat
sanitize/redact edilmiş ve secret-free değildir. Paylaşmadan önce elle inceleyin.

`playbook_failed` güvenilir sonuçta failed/unreachable bildirildiğini gösterir,
tek başına kök neden değildir. `runner_failed` genel/legacy sınıftır.
`runner_timeout` sonrasında hedefte kısmi değişiklik kalabilir. `result_*` ve
`runner_output_invalid`, artifact'ın güvenilir sonuç belgesine dönüşmediğini
gösterir.

**Çalıştırmalar** sayfası durum/mode filtresi, 25 kayıtlık sayfa ve
Sonraki/Önceki cursor gezinmesi sunar. Aynı SQLite ve `app-data` korunduğunda
terminal geçmiş yeniden başlatmada kalır.

---

## 13. Örnek uçtan uca senaryo

### SSH audit

1. Hedef VM'yi ve uygulamayı açın.
2. Ping önizlemesi + onay ile host'u test edin.
3. SSH Audit project'inde Check planı oluşturun.
4. Mode, inventory, playbook ve host listesini kontrol edin.
5. Hazırlayın, onaylayın ve Job'ı başlatın.
6. Recap, task ve hata sınıfını okuyun.

Audit `failed` ise task adından uygunsuz kontrolü bulun.

### SSH remediation — yalnız laboratuvar

```bash
cd "$HOME/Projeler/DORAnsible"
cp -a sample-projects/ubuntu-ssh-hardening app-data/projects/
```

Inventory'yi düzenleyip yeni Project/Inventory olarak kaydedin. Snapshot ve
konsol erişimi hazırlayın. Sıra:

```text
Audit Check
→ Hardening Check
→ Hardening Normal
→ Hardening Normal tekrar (idempotency)
→ Audit Check tekrar
```

UFW daha yüksek lockout riski taşır. Uygulamadan önce ilgili README'leri okuyun:

- [Ubuntu SSH Audit](../../sample-projects/ubuntu-ssh-audit/README.md)
- [Ubuntu SSH Hardening](../../sample-projects/ubuntu-ssh-hardening/README.md)
- [Ubuntu UFW Audit](../../sample-projects/ubuntu-ufw-audit/README.md)
- [Ubuntu UFW Hardening](../../sample-projects/ubuntu-ufw-hardening/README.md)

---

## 14. Kapatma, yedekleme ve güncelleme

### Kapatma

1. Yeni Job başlatmayın.
2. Running Job'ın bitmesini bekleyin.
3. Frontend terminalinde `Ctrl+C`.
4. Backend terminalinde `Ctrl+C`.

### Yedek

Backend kapalıyken:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p "$HOME/DORAnsible-Yedekleri"
tar -czf "$HOME/DORAnsible-Yedekleri/app-data-$(date +%Y%m%d-%H%M%S).tar.gz" app-data
chmod 600 "$HOME"/DORAnsible-Yedekleri/*.tar.gz
```

Yedek secret içerir. Harici allowlist köklerini ayrıca yedekleyin. Yalnız
SQLite dosyasını kopyalamak artifact ve Ansible içeriklerini taşımaz.

### Güncelleme

Önce yedek ve güvenli kapatma:

```bash
git status --short --branch
git pull --ff-only
cd backend
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e . --no-deps
alembic upgrade head
cd ../frontend
npm ci
```

Beklenmeyen yerel değişiklikte durun; `git reset --hard` kullanmayın.

Başka controller'a taşırken repository yeniden clone edilir, bağımlılıklar
yeniden kurulur, kapalı-servis `app-data` yedeği geri yüklenir. `.venv` ve
`node_modules` kopyalanmaz. Absolute path değişiklikleri kontrollü düzeltilir.

---

## 15. Sorun giderme

### Komut bulunamadı

- `node`: nvm kurun, terminali yeniden açın, `nvm use --lts`.
- `ansible-inventory`/`uvicorn`: backend `.venv` etkinleştirin ve bağımlılığı
  kurun.
- `python3`: Ubuntu sistem paketlerini kurun.

Sanal ortam kontrolü:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
which python
```

### Yanlış Uvicorn hedefi

```bash
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

### Port kullanımda

```bash
ss -ltnp | grep -E ':8000|:5173'
```

Eski uygulama terminalini `Ctrl+C` ile kapatın.

### Frontend veri alamıyor

```bash
curl -fsS http://127.0.0.1:8000/health
cat frontend/.env.local
```

Backend açık, API URL doğru olmalıdır.

### Path 403

Project varsayılan olarak `app-data/projects`, bağımsız inventory
`app-data/inventories` altında olmalıdır. Symlink ve allowlist dışı yol
reddedilir.

### Inventory hatası

```bash
ansible-inventory -i /TAM/YOL/hosts.yml --list
```

YAML girintisi, path ve project aktifliği kontrol edilir.

### Ping unreachable

IP, port, SSH servisi, kullanıcı, key yolu/600 izni, DORAnsible known_hosts ve
hedef Python'u sırayla kontrol edin. Host key değişmişse eski kaydı silmeden önce
değişikliğin meşru olduğunu doğrulayın.

### Become parola istiyor

```bash
sudo -u automation sudo -n true
```

DORAnsible become parolası göndermez; hedef politikası önceden hazırlanmalıdır.

### Pending Job

```bash
grep ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED backend/.env
```

`true` olmalı ve backend yeniden başlatılmış olmalıdır.

### Failed Job

Hata sınıfı, recap ve task'ları birlikte okuyun. Audit uygunsuzluğu ile runner
arızasını karıştırmayın. Ham output ve artifact paylaşımında secret'ları çıkarın.

### Kurumsal TLS/proxy

TLS doğrulamasını kapatmayın; kurum CA'sını doğru trust store'lara ekletin.

---

## 16. Güvenlik sınırları ve sözlük

### Güvenlik özeti

- Uygulamayı doğrudan internete açmayın.
- Yalnız güvenilir içerik çalıştırın.
- Private key'i Git, inventory veya frontend environment'a yazmayın.
- Normal kip gerçek değişikliktir; Check sandbox değildir.
- Timeout/kesintide kısmi değişiklik kalabilir.
- Raw görüntü çıktısını paylaşmadan önce inceleyin.
- SSH/UFW değişikliğinde konsol erişimi ve geri dönüş planı hazırlayın.

Git'e eklenmemesi gerekenler:

```text
backend/.env
frontend/.env.local
app-data/
private key dosyaları
gerçek inventory secret'ları
VM diskleri ve runtime artifact'ları
```

Başka kullanıcılara açılmadan önce authentication/RBAC, TLS/reverse proxy,
firewall, non-root service identity, yedekleme, log rotasyonu, secret yönetimi
ve actor isolation tasarlanmalıdır. `uvicorn --reload` ve Vite geliştirme
sunucusu üretim servisi değildir.

### Kısa sözlük

| Terim | Açıklama |
|---|---|
| API | Frontend ile backend arasındaki HTTP/JSON arayüzü |
| Artifact | Job'a ait kontrollü sonuç/çalışma dosyası |
| CORS | İzin verilen tarayıcı origin'lerini sınırlar |
| Cursor | Liste sayfalama konumu |
| Frozen workspace | Onaylanan içeriğin dondurulmuş kopyası |
| Host key | SSH sunucusunun kimlik anahtarı |
| Idempotency | Tekrarda gereksiz değişiklik oluşmaması |
| Lease/heartbeat | Worker'ın running Job sahipliği |
| Migration | Veritabanı şema yükseltmesi |
| Recap | Host başına Ansible sonuç sayaçları |
| Token | Planı kısa ömürlü tek kullanımlık onaya bağlar |

İleri başvuru:

- [Kullanıcı Rehberi](../KULLANICI_REHBERI.md)
- [Kurulum ve Environment](../gelistirme-ortami.md)
- [Geliştirici Rehberi](../GELISTIRICI_REHBERI.md)
- [Mimari](../../MIMARI.md)
- [Güvenlik](../../GUVENLIK.md)
