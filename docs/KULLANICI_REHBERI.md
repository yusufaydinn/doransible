# DORAnsible Kullanıcı Rehberi

Bu rehber, DORAnsible'ı tek controller üzerinde kullanan profesyonel operatör
içindir. Kurulumun tamamlandığı ve backend ile frontend'in çalıştığı varsayılır.
Kurulum için [Geliştirme Ortamı](gelistirme-ortami.md) belgesine bakın.

## 1. Temel kavramlar

| Kavram | DORAnsible'daki anlamı |
|---|---|
| Controller | Backend, worker, Ansible ve dosyaların çalıştığı makine |
| Tarayıcı | React arayüzünün açıldığı cihaz; controller ile aynı olabilir |
| Yönetilen host | Ansible'ın SSH ile bağlandığı hedef makine |
| Project | Controller üzerindeki bir Ansible project dizini |
| Inventory | Host, grup ve bağlantı değişkenlerini tanımlayan YAML/INI dosyası |
| Playbook | Project içinden güvenli biçimde keşfedilmiş `.yml`/`.yaml` çalışma dosyası |
| Ping | Ansible `ping` modülüyle kullanıcı tarafından başlatılan erişim ölçümü |
| Execution planı | Project, inventory, playbook, hostlar ve kipin çalıştırma öncesi görünümü |
| Job | Onaylanmış bir playbook çalıştırmasının kalıcı yaşam döngüsü kaydı |
| Check | Runner'a `--check` eklenen Ansible kontrol kipi |
| Normal | `--check` eklenmeden gerçek playbook davranışının uygulandığı kip |

Controller path'i tarayıcının indirme/yükleme path'i değildir. Controller ile
tarayıcı ayrı cihazlardaysa UI'daki `/srv/...` veya `/home/...` yolu tarayıcı
cihazında değil backend makinesindedir.

## 2. Oturum öncesi kontrol

1. Backend sağlık adresini açın: `http://127.0.0.1:8000/health`.
2. Cevapta `status: ok` görüldüğünü doğrulayın.
3. Frontend'i açın: `http://127.0.0.1:5173`.
4. Normal mode kullanılacaksa backend worker'ının açık olduğunu doğrulayın:

   ```dotenv
   ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true
   ```

Worker kapalıysa plan ve Job oluşturulabilir, fakat Job `pending` durumda
kalır. Worker sayısı bu MVP'de birdir; bir playbook Job'ı çalışırken sıradaki
Job bekleyebilir.

## 3. Controller dosyalarını hazırlama

Varsayılan yapı:

```text
app-data/
├── projects/       # Ansible project dizinleri
├── inventories/   # Bağımsız inventory dosyaları
└── secrets/       # Private key dosyaları
```

Örnek project'ler doğrudan `sample-projects/` altında versiyonlanır; runtime
kopyaları ise `app-data/projects/` altında tutulabilir:

```bash
cp -a sample-projects/ubuntu-ssh-audit app-data/projects/
```

Alternatif olarak project'in bulunduğu üst dizini backend `.env` dosyasında
allowlist'e ekleyebilirsiniz. Uygulama allowlist dışındaki bir yolu kaydetmez
ve path seçicide göstermez.

Private key'i inventory YAML içine yapıştırmayın. Dosyayı örneğin
`app-data/secrets/ubuntu-demo` altında tutun ve izinlerini sınırlayın:

```bash
chmod 600 app-data/secrets/ubuntu-demo
```

## 4. Project ekleme

1. Üst menüden **Project'ler** sayfasına gidin.
2. **Yeni project** seçeneğini açın.
3. Görünen bir ad yazın.
4. Controller'daki dizin yolunu elle yazın veya **Gözat…** düğmesini kullanın.
5. İsteğe bağlı açıklama ekleyin.
6. **Project'i kaydet** düğmesine basın.

### Path dialogu

- Dialog yalnız backend allowlist'inin içini gerçek controller dosya
  sisteminden listeler; klasör isimleri hardcoded değildir.
- Project seçiminde klasör seçilir.
- **Aç** bir alt klasöre girer, **Geri** bir üst izinli seviyeye döner,
  **Seç** yolu forma aktarır.
- Dialog recursive bir dosya tarayıcısı değildir ve allowlist kökünün üstüne
  çıkmaz.
- Alan elle düzenlenebilir. Elle girilen yol da backend tarafından aynı
  güvenlik kontrollerinden geçirilir.

Project kaydı dosyaları oluşturmaz, kopyalamaz veya değiştirmez. Yalnız mevcut
controller dizinine referans oluşturur. Project'i pasife almak da diskteki
dosyaları silmez.

## 5. Inventory oluşturma

1. Üst menüden **Inventory'ler** → **Yeni inventory** yolunu açın.
2. Görünen adı yazın.
3. Biçimi `YAML` veya `INI` seçin.
4. Gerekliyse bağlı project'i seçin.
5. Controller'daki inventory dosyasını elle girin veya **Gözat…** ile seçin.
6. Kaydedin.

Bağlı inventory yalnız seçilen aktif project'in kendi doğrulanmış kökü içinde
olabilir. Bağımsız inventory ise `ANSIBLEOPS_INVENTORY_ROOT_ALLOWLIST` içinde
olmalıdır.

### Örnek YAML inventory

```yaml
all:
  children:
    demo_targets:
      hosts:
        ubuntu-demo:
          ansible_host: 192.0.2.10
          ansible_port: 22
          ansible_user: automation
          ansible_python_interpreter: /usr/bin/python3
          ansible_ssh_private_key_file: /absolute/controller/path/to/key
```

`192.0.2.0/24` dokümantasyon ağıdır; gerçek hedef adresinizle değiştirin.

### Inventory ekranında maskeleme

Host değişkenlerinde secret görünen anahtarlar/değerler `***` olarak
gösterilir. Bu, inventory dosyasının diskte güvenli olduğu anlamına gelmez;
dosyanın erişim izinleri ayrıca korunmalıdır. Private key içeriği inventory'de
bulunmamalıdır.

## 6. SSH host key hazırlığı

Varsayılan politika `strict`tir. Hedefin host key'i controller'ın bilinen
hostlar dosyasında yoksa bağlantı reddedilir. Parmak izini güvenilir ayrı bir
kanaldan doğruladıktan sonra controller'dan ilk bağlantıyı kurabilirsiniz:

```bash
ssh -i /izinli/private/key automation@192.0.2.10
```

`accept_new` politikası TOFU davranışıdır: ilk anahtarı doğrulamadan kabul
eder, sonraki değişikliği reddeder. Demo kolaylığı için kullanılabilse de ilk
bağlantıda MITM riskini ortadan kaldırmaz. Host key doğrulamasını tamamen
kapatan bir ürün ayarı yoktur.

## 7. Inventory erişilebilirlik testi

Inventory detayında **Erişilebilirlik testi** bölümü bulunur.

1. Ping önizlemesini oluşturun.
2. Hedef host listesini ve sayısını inceleyin.
3. Tek kullanımlık onayı verin.
4. Sonucu bekleyin.

Bu gerçek bir Ansible execution'dır. Hedeflere SSH bağlantısı kurulur ve
Ansible hedefte geçici modül dosyaları oluşturabilir.

Sonuçlar host bazında ayrılır:

- **Erişilebilir:** Ansible ping başarılı.
- **Erişilemiyor:** SSH/ağ bağlantısı kurulamadı.
- **Başarısız:** Bağlantı kurulsa da modül çalışması hata verdi.
- **Sonuç alınamadı:** Beklenen host için doğrulanabilir sonuç yok.

Kök neden yalnız bu sınıftan kesin olarak çıkarılmaz. Örneğin
`unreachable`, kapalı host, ağ rotası, firewall, yanlış kullanıcı, host key veya
SSH daemon sorunundan kaynaklanabilir.

Ping geçmişi inventory detayında kalıcıdır, fakat gerçek zamanlı monitoring
değildir. Periyodik ölçüm, alarm veya uptime yüzdesi üretmez.

## 8. Playbook çalıştırma

Project detayında bir inventory ve keşfedilmiş playbook seçin.

### 8.1 Kip seçimi

**Check (Ansible `--check`)** varsayılandır:

- Runner argümanlarına `--check` eklenir.
- Modüller mümkün olduğunda yapılacak değişikliği simüle eder.
- Her modül veya playbook check mode'u eksiksiz desteklemek zorunda değildir.
- Check mode, hiçbir yan etki olmayacağının mutlak garantisi değildir.

**Normal**:

- Runner argümanlarına `--check` eklenmez.
- Playbook dosya, paket, servis, kullanıcı, SSH veya firewall üzerinde gerçek
  değişiklik yapabilir.
- Bağlantı kesilebilir ve otomatik rollback olmayabilir.

### 8.2 Plan

**Planı oluştur** adımında şunları kontrol edin:

- Project ve inventory adı
- Playbook relative path'i
- Check/Normal kip
- Hedef host sayısı ve adları
- Host key ve become hakkında gösterilen bilgiler

Preview hiçbir hedefe bağlanmaz. Ardından **Onaya hazırla** işlemi planı ve
çalışma bağlamını dondurur. Hazırlanan token kısa ömürlü ve tek kullanımlıktır.

### 8.3 Açık onay

Onay kutusu işaretlenmeden Job başlatılamaz. Normal mode'da risk metni ve
düğme, gerçek değişiklik uygulanacağını ayrıca belirtir.

Mode, inventory veya playbook değiştirildiğinde eski preview ve onay atılır;
yeni plan oluşturulmalıdır. Check planı launch isteğinde Normal Job'a
dönüştürülemez.

## 9. Job yaşam döngüsü

```text
pending → running → successful
                  └→ failed
```

- **Pending:** Job veritabanına yazıldı, worker bekleniyor.
- **Running:** Worker Job'ı lease ile sahiplendi ve runner çalışıyor.
- **Successful:** Güvenilir terminal sonuç ve başarılı recap üretildi.
- **Failed:** Runner, çıktı doğrulaması veya playbook sonucu başarısız oldu.

Playbook `assert` task'larıyla bir uyumsuzluğu raporlayıp `rc=2` döndürebilir.
Bu durumda execution mekanizması çalışmış olsa bile playbook sonucu
başarısızdır. UI, mümkün olduğunda “çalıştırma tamamlandı” ile “playbook
başarısız sonuç bildirdi” ayrımını gösterir.

## 10. Sonuç ekranı

### Host özeti

Her host için Ansible recap alanları gösterilir:

- `ok`
- `changed`
- `failures`
- `unreachable`
- `skipped`
- `rescued`
- `ignored`

Audit playbook'larında bağımsız kontrollerin devam edebilmesi için bazı
başarısız assert'ler `ignore_errors` ile devam ettirilebilir. Final özet task'ı
Job sonucunu dürüstçe başarısız yapar.

### Event listesi

UI yalnız normalize edilmiş alanları gösterir:

- event türü
- host
- task
- changed
- failed

Başarısız ve erişilemeyen event'ler görünür metinle ve satır vurgusuyla
ayrılır. UI bir kök neden uydurmaz.

### Ham Ansible çıktısı

Kapalı bir `<details>` bölümündedir ve yalnız sınırlandırılmış runner event
`stdout` görüntüsüdür. Şunları unutmayın:

- sanitize/redact edilmiş değildir,
- credential, host bilgisi, controller yolu veya playbook metni içerebilir,
- en fazla 128 KiB tutulur ve kırpılmış olabilir,
- paylaşmadan veya ekran görüntüsü almadan önce operatör tarafından
  incelenmelidir.

## 11. Çalıştırma geçmişi

**Çalıştırmalar** sayfası playbook Job'larını en yeniden eskiye gösterir.

- Durum ve kip filtreleri backend sorgusuna gönderilir; tarayıcı yalnız mevcut
  sayfayı yerel olarak elemez.
- Her sayfa en fazla 25 kayıttır.
- Sonraki/Önceki düğmeleri keyset cursor ile ilerler.
- Project ve inventory adları birincil, sayısal ID'ler ikincil referanstır.

Backend yeniden başlatıldığında aynı veritabanı ve `app-data` kullanılıyorsa
kalıcı terminal Job geçmişi görünmeye devam eder. Farklı çalışma dizini,
farklı `ANSIBLEOPS_APP_DATA_DIR`, yeni SQLite dosyası veya silinmiş artifact
dizini geçmişi eksik gösterebilir.

## 12. Önerilen audit/remediation akışı

SSH veya UFW için güvenli demo/operasyon sırası:

```text
1. Audit (Check)
2. Uygunsuz alanları incele
3. Hardening (Check)
4. Hardening (Normal)
5. Hardening'i tekrar çalıştır: changed=0 idempotency
6. Audit'i tekrar çalıştır: bağımsız doğrulama
```

Sample project'lerin exact politikaları ve rollback sınırları kendi README
dosyalarında açıklanır:

- [Ubuntu SSH Audit](../sample-projects/ubuntu-ssh-audit/README.md)
- [Ubuntu SSH Hardening](../sample-projects/ubuntu-ssh-hardening/README.md)
- [Ubuntu UFW Audit](../sample-projects/ubuntu-ufw-audit/README.md)
- [Ubuntu UFW Hardening](../sample-projects/ubuntu-ufw-hardening/README.md)

## 13. Yaygın sorunlar

### Job pending durumda kalıyor

- `ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true` ayarını kontrol edin.
- Backend'i ayar değişiminden sonra yeniden başlatın.
- Backend logunda recovery/worker başlangıç uyarısı olup olmadığına bakın.

### Path dialogu boş veya 403

- Yolun doğru allowlist altında olduğunu doğrulayın.
- Project için project allowlist, bağımsız inventory için inventory allowlist
  kullanılır.
- Symlink ve özel dosyalar bilinçli olarak gösterilmez.
- Backend proses kullanıcısının dizini listeleme izni olmalıdır.

### Inventory okunamıyor

Controller'da şunu çalıştırın:

```bash
ansible-inventory -i /controller/path/to/hosts.yml --list
```

YAML/INI biçimini, dosya izinlerini ve bağlı project'in aktif olduğunu kontrol
edin. UI ham parser çıktısını göstermeyebilir; controller logları incelenir.

### Ping unreachable

Controller'dan aynı kullanıcı ve anahtarla doğrudan SSH deneyin:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 \
  -i /izinli/private/key automation@192.0.2.10 \
  'python3 --version'
```

Host key, route, port, firewall, kullanıcı, key izni ve hedef Python kurulumu
ayrı ayrı kontrol edilmelidir.

### Become parola istiyor

MVP become parolası göndermez. Hedef kullanıcı için gerekli dar kapsamlı
`NOPASSWD` sudo politikası önceden kurulmalıdır. Sample hardening rolleri
non-interactive sudo kullanır ve parola istenirse kalıcı değişiklikten önce
durmayı hedefler.

### Job sonucu okunamıyor

`runner_output_invalid` veya `result_*` kodları, çıktı/artifact'ın güvenilir
sonuç belgesine dönüştürülemediğini belirtir; tek başına kök nedeni söylemez.
Controller backend logunu ve ilgili `app-data/jobs` artifact'ını, secret
sızdırmadan inceleyin.

## 14. Güvenli kapatma ve yedekleme

1. Yeni Job başlatmayın.
2. Çalışan Job'ın terminal duruma gelmesini bekleyin.
3. Frontend terminalini `Ctrl+C` ile durdurun.
4. Backend/worker terminalini `Ctrl+C` ile durdurun.
5. `app-data` ve gerekiyorsa harici allowlist project/inventory dizinlerini
   erişimi kısıtlı bir yedek hedefe kopyalayın.

SQLite dosyasını çalışan worker sırasında tek başına kopyalayıp artifact'ları
atlamak tutarsız yedek oluşturabilir. Veritabanı, Job artifact'ları ve ilgili
Ansible içeriği aynı yedekleme anına ait olmalıdır.

## 15. Üretim öncesi uyarı

Bu teslim tek operatörlü yerel MVP'dir. Ağdan başka kullanıcılara açmadan önce
en az authentication/RBAC, TLS veya güvenilir reverse proxy, işletim sistemi
servisleri, yedekleme politikası, log rotasyonu, secret yönetimi ve kontrollü
worker ölçekleme tasarlanmalıdır.
