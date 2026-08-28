# Ubuntu UFW Audit

Salt-okunur örnek project. Ubuntu üzerinde UFW (Uncomplicated Firewall)
durumunu, `/etc/default/ufw` profilini ve inventory'deki SSH portu için
açık bir allow kuralının varlığını okur, güvenlik açısından ilgili
sonuçları raporlar. Playbook hiçbir kural eklemez/kaldırmaz, `ufw enable`/
`disable` çalıştırmaz, servis restart/reload etmez, hiçbir dosya yazmaz —
ayrıntı için aşağıdaki "Değişiklik kapsamı" ve "Sınırlar" bölümlerine
bakın. **Bu bir tam ağ güvenliği garantisi değildir**: kapsamı ve UFW'yi
bypass edebilecek yollar için "Sınırlar" bölümüne bakın.

## Amaç

UFW tabanlı bir remediation/hardening adımından **önce veya sonra**
çalıştırılabilecek bir tanı adımı: mevcut firewall durumunun neresinin
uygun, neresinin uygunsuz olduğunu değişiklik yapmadan görmek. Kural
ekleme/kaldırma ve profil düzeltme bu örnek project'in kapsamı dışındadır.

## Desteklenen sürümler

- Ubuntu 22.04 LTS
- Ubuntu 24.04 LTS

Başka bir dağıtım veya sürüm tespit edilirse audit ilk task'ta durur ve
host'u açıkça **UNSUPPORTED** olarak raporlar; diğer kontroller o host için
çalıştırılmaz. Inventory'deki diğer host'lar bundan etkilenmez.

## Değişiklik kapsamı

- Kullanılan modüller: `ansible.builtin.setup` (playbook'taki
  `gather_facts: true` bunu örtük olarak çalıştırır; salt-okunur fact
  toplamadır, desteklenen sürüm kontrolü için kullanılır),
  `ansible.builtin.stat` (yalnız `/usr/sbin/ufw` var mı/çalıştırılabilir
  mi, root gerektirmez), `ansible.builtin.command` (yalnız
  `ufw status verbose` ve `systemctl is-active <unit>` okumak için,
  `changed_when: false`), `ansible.builtin.slurp` (yalnız
  `/etc/default/ufw` okumak için), `ansible.builtin.set_fact`,
  `ansible.builtin.assert` ve `debug`. `shell`, `raw` veya serbest komut
  üretimi hiç kullanılmaz.
- `command` argümanları sabit bir listedir (`argv`); kullanıcı girdisinden
  veya inventory içeriğinden birleştirilmez. Çalıştırılan TEK üç argv,
  EXACT (birebir, prefix değil) eşleşmelidir:
  - `["/usr/sbin/ufw", "status", "verbose"]`
  - `["systemctl", "is-active", "firewalld"]`
  - `["systemctl", "is-active", "ufw.service"]`
  Farklı bir unit adı, eksik/fazla argüman veya farklı bir alt komut bu
  üç argv'den hiçbirine birebir uymaz.
- Playbook hiçbir **configuration** değişikliği yapmaz: kural eklemez/
  kaldırmaz, `ufw enable`/`disable`/`reload`/`reset` çalıştırmaz, paket
  kurmaz/kaldırmaz, servis restart/reload etmez, dosya yazmaz.
- Bu iddia kalıcı, kalıcılaştırılmış bir yapısal testle korunur:
  `tests/assert_read_only_surface.py` üretim rolünün (`roles/ufw_audit/`)
  ve üst playbook'un YAML AST'ini PyYAML ile parse eder, her task'ın
  modülünü Ansible'ın kendi kanonik task/block/play keyword listeleriyle
  ayırt eder ve yalnızca yukarıdaki modül allowlist'ine + yukarıdaki üç
  EXACT `command` argv'sine izin verir — bunun dışındaki her modül veya
  argv (örn. gelecekte yanlışlıkla eklenmiş bir `ufw allow`, `lineinfile`,
  farklı bir systemd unit'i, fazla argüman veya Jinja/dinamik bir değer)
  testi kırar. Bu, kaynak metindeki README/yorum kelimelerine bakan
  kırılgan bir grep DEĞİLDİR; `tests/run_offline_tests.sh` içinde
  `read_only_surface_is_structurally_locked` adıyla her çalışmada otomatik
  doğrulanır. Checker'ın kendi argv/modül allowlist mantığı ayrıca
  `python3 tests/assert_read_only_surface.py --self-test` ile (ve
  `read_only_surface_argv_allowlist_is_hermetically_regression_tested`
  adıyla offline suite içinde) hermetik biçimde regresyona karşı
  korunur — bellek-içi sahte argv/task girdileriyle çalışır, tracked
  production dosyalarını değiştirmez.
- Bu bir "hosta hiçbir iz bırakmama" garantisi **değildir**.
  `ufw status verbose` ve `/etc/default/ufw` okuması `become` ile root
  olarak çalıştığı için hedefte sudo auth logları (`/var/log/auth.log` vb.)
  normal şekilde satır üretir. Ayrıca Ansible, her task için kendi
  bağlantı/modül çalıştırma mekanizmasının parçası olarak hedefte geçici
  çalışma dosyaları oluşturup temizler; bu playbook'a özgü değildir ve
  önlenemez.
- DORAnsible UI'da bir job başlatılırken **Check (Ansible `--check`)**
  kipi varsayılan olarak seçilidir; kullanıcı isterse **normal mod**'u
  açıkça seçebilir (bkz. "Check/normal mod ve `check_mode: false`
  gerekçesi"). Bu audit salt-okunur olduğu için **her iki kipte de**
  configuration değiştirmez — modül seviyesinde hiçbir yazma task'ı
  yoktur (bkz. yukarıdaki modül listesi ve
  `tests/assert_read_only_surface.py`). `ufw status verbose` ve
  `/etc/default/ufw` okuma task'ları istisnai olarak `check_mode: false`
  taşır; bunun gerekçesi ve normal kiple ilişkisi için aşağıdaki bölüme
  bakın.

## Sudo gereksinimi

`ufw status verbose` ve `/etc/default/ufw` okuması host key/kural
dosyalarını ve tüm yapılandırmayı eksiksiz çözebilmek için root yetkisi
gerektirir. Bu yüzden yalnızca bu iki task `become: true` taşır
(playbook'un geneli `become: false`'dur); firewalld/ufw.service systemd
durumu sorguları root gerektirmez.

DORAnsible şu an bir become-parolası credential'ı **saklamaz**: uygulamada
şifrelenmiş credential deposu veya onboarding sihirbazı henüz yoktur (bkz.
aşağıdaki "Mevcut ürün gerçeği"). Bu yüzden hedef kullanıcının **parolasız
sudo** (`NOPASSWD`) çalıştırabilmesi gerekir; interaktif parola sorulursa
`become` başarısız olur ve ilgili task hata verir. `become_flags: "-H -S -n"`
içindeki `-n` (non-interactive) bunu prompt açmadan doğrudan bir hataya
çevirir — sessizce takılıp kalmaz.

Bağlantı için de standart SSH kimlik doğrulaması (kullanıcı adı + private
key dosya yolu, `inventory/hosts.yml` üzerinden) gerekir; parola tabanlı
SSH kimlik doğrulaması bu örnek project'in inventory sözleşmesinde
desteklenmez.

## Check/normal mod ve `check_mode: false` gerekçesi

DORAnsible UI'da bir job başlatılırken **Check (Ansible `--check`)**
kipi **varsayılan olarak seçilidir**; kullanıcı isterse **Normal mode**'u
açıkça seçebilir. "Her job zaten check-mode çalışır" gibi kullanıcının
seçemediği tek-kip bir sözleşme **yoktur** — Normal mode kullanılabilir
bir seçenektir, yalnızca varsayılan değildir.

Bu audit için pratik fark yoktur, çünkü playbook zaten salt-okunurdur:

- **UFW audit her iki kipte de salt-okunurdur.** Normal modda da bu
  playbook hiçbir configuration değiştirmez — yukarıdaki "Değişiklik
  kapsamı" bölümündeki modül listesi ve `tests/assert_read_only_surface.py`
  'nin yapısal kilidi, hangi kip seçilirse seçilsin geçerlidir. Normal
  modda çalıştırmak, check mode'a göre ek bir risk **eklemez**.
- **Rutin audit ve sunum için varsayılan Check seçimi kullanılabilir**:
  Ansible'ın kendi `--check` bayrağı ek bir güvence katmanı olarak
  eklenir ve kullanıcıya "bu çalıştırma değişiklik yapmayacak" niyetini
  UI'da açıkça gösterir; bu audit için varsayılanı değiştirmeye gerek
  yoktur.
- `ufw status verbose` ve `/etc/default/ufw` okuma task'larındaki
  `check_mode: false` **aynı zorunluluk gerekçesine sahip değildir** —
  ikisi farklı modüllerdir ve Ansible'ın check-mode desteği modüle göre
  değişir (yerel `ansible-doc` çıktısı):
  - **`ansible.builtin.command`** (`ufw status verbose`) check-mode
    desteğini **`partial`** taşır: `creates`/`removes` parametreleri
    verilmeden Ansible bu modülü check mode'da normalde **skip eder**
    (olası bir yazma komutu olabileceği varsayımıyla). Salt-okunur bu
    komutun **check kipinde de gerçekten çalışıp** audit sonucunu üretmesi
    için `check_mode: false` **gereklidir** — bu istisna olmadan check
    modda task atlanır ve sonraki tüm firewalld/SSH kontrolleri boş veri
    üzerinde çalışır.
  - **`ansible.builtin.slurp`** (`/etc/default/ufw` okuma) check-mode
    desteğini **`full`** taşır ve zaten kendiliğinden salt-okunurdur;
    Ansible onu check modda da normal şekilde çalıştırır, atlamaz.
    Üzerindeki `check_mode: false` bu modülde yeni bir davranış
    **açmaz** — yalnızca "bu okuma her zaman gerçek dosya içeriğini
    yansıtır" niyetini kodda açıkça pinler (dokümantasyon amaçlı
    tutarlılık), `command`'daki gibi task'ın çalışıp çalışmayacağını
    belirleyen zorunlu bir ayar değildir.
  - Her iki task da **normal modda** zaten yazma yapmaz (salt-okunur
    modüllerdir); `check_mode: false` bu gerçeği değiştirmez, yalnızca
    `command`'ın check moddaki varsayılan skip davranışını audit için
    gerekli biçimde geçersiz kılar (ssh_audit/ssh_hardening role'lerindeki
    aynı istisna gerekçesiyle). Başka hiçbir task bu istisnaya ihtiyaç
    duymaz.

Kullanıcı DORAnsible UI'da job'ı başlatırken varsayılan Check seçimini
koruyabilir veya Normal mode'u açıkça seçebilir.

## Mevcut ürün gerçeği (önemli)

Bu bölüm, örnek project'i denerken karşılaşacağınız gerçek ürün davranışını ve
desteklenmeyen genişlemeleri ayırmak için vardır:

- **UI onboarding akışı henüz yok.** Public key kurulumu, host-key
  parmak izi doğrulaması gibi yönlendirmeli bir sihirbaz bu sürümde
  mevcut değildir. Bağlantı bilgisi (kullanıcı adı, private key yolu)
  bugün yalnızca inventory dosyasındaki `ansible_user` ve
  `ansible_ssh_private_key_file` gibi allowlist'ten geçmiş alanlarla
  verilir.
- **Şifreli/uygulama-yönetimli credential deposu yok.** Private key
  dosyasının kendisi diskte, `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST` ile
  izin verilen bir kök altında (varsayılan: `app-data/secrets`) durur;
  uygulama onu şifrelemez veya ayrı bir credential kaydı olarak saklamaz.
- **Become parolası desteği yok.** Yukarıdaki "Sudo gereksinimi"
  bölümüne bakın: yalnızca parolasız sudo desteklenir.
- **`sample-projects/` varsayılan olarak doğrudan kaydedilemez.** Project
  kaydı, `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` boşken yalnızca
  `app-data/projects` altını kabul eder; bunun dışındaki her yol
  `403 path_not_allowed` ile reddedilir. Bu dizini kaydetmeden önce
  aşağıdaki adımlardan birini uygulayın.

## DORAnsible'a nasıl kaydedilir ve çalıştırılır

1. **Project dizinini erişilebilir bir köke taşıyın**, iki seçenekten
   biriyle:
   - Bu dizini `app-data/projects/ubuntu-ufw-audit` altına kopyalayın
     (varsayılan allowlist'e uyar, ek yapılandırma gerekmez), **veya**
   - `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST` ortam değişkenine bu dizinin
     mutlak yolunu (üst dizinini) ekleyip backend'i yeniden başlatın.
2. **Project ekle**: kopyaladığınız/allowlist'e eklediğiniz project
   kökünü DORAnsible'da kaydedin.
3. **Inventory ekle**: `inventory/hosts.yml` dosyasını project'e bağlı
   inventory olarak kaydedin (project köküne bağlı inventory'ler ayrı bir
   allowlist kontrolüne tabi değildir), ardından kendi host'unuzu
   tanımlayın (bkz. `inventory/hosts.yml` içindeki yorum satırları).
   Private key kullanıyorsanız dosyayı `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST`
   ile izin verilen bir köke (varsayılan `app-data/secrets`) koyup
   `ansible_ssh_private_key_file` ile referans verin; bu inventory
   dosyasının kendisinde secret tutulmaz. Hedefte sshd standart olmayan
   bir portta dinliyorsa `ansible_port`'u burada tanımlayın — bkz. aşağıda
   "SSH allow kuralı ↔ inventory portu eşleşmesi".
4. **Playbook'u çalıştırın**: `ubuntu-ufw-audit.yml`. Job'ı başlatırken
   UI'da kip olarak **Check (Ansible `--check`)** varsayılan seçilidir;
   sunum ve rutin audit kullanımı için bu varsayılan korunabilir, isterseniz
   **normal mod**'u açıkça seçebilirsiniz (bkz. "Check/normal mod ve
   `check_mode: false` gerekçesi"); bu audit salt-okunur olduğu için her
   iki kip de configuration değiştirmez. Job
   UI'da hedef host, playbook, kullanılan inventory, seçilen kip ve dönen
   ok/failed durumu görüntülenir; task bazlı `fail_msg`/`success_msg`
   ayrıntıları için bkz. "UI görünürlüğü".

## UFW active kararı neden `ufw.service`'den değil `ufw status verbose`'dan gelir

`systemctl is-active ufw.service` yalnızca systemd unit'inin systemd
gözünden durumunu söyler; bu, UFW'nin **gerçekte etkin kural kümesi**
uyguluyor olduğu anlamına gelmez (ör. unit "active" görünürken UFW'nin
kendi iç durumu farklı raporlanabilir, ya da tam tersi). Bu yüzden tek
gerçek kaynak `ufw status verbose` çıktısındaki `Status:` satırıdır —
audit'in "aktif mi" kararı **YALNIZ** buna dayanır. `ufw.service`'in
systemd durumu ayrı bir task'ta okunur ve `debug` ile raporlanır ama
compliance kararına **hiçbir zaman** dahil edilmez; bu sözleşme
`roles/ufw_audit/tasks/checks.yml` içinde açıkça yorumlanmıştır ve
`ufw_service_active_but_real_status_inactive_is_non_compliant` offline
testiyle korunur (ufw.service "active" görünse bile `ufw status verbose`
"inactive" derse sonuç NON-COMPLIANT'tır).

## Firewalld karar matrisi

`systemctl is-active firewalld` sorgusunun sonucu **rc + normalize
edilmiş stdout birlikte** değerlendirilir; yalnız stdout'a bakıp rc'yi
yok saymak, komut hatasını yanlışlıkla compliant saymanın bir yoludur —
bu audit'te bu asla olmaz.

| rc | stdout (trim edilmiş) | Sonuç |
|----|------------------------|-------|
| 0  | `active`               | **NON-COMPLIANT** (çakışma) — stdout `active` ise rc ne olursa olsun aynı sonuç (savunma amaçlı: rc'ye tek başına güvenilmez) |
| 3  | `inactive`             | **COMPLIANT** (kurulu, çalışmıyor) |
| 4  | `inactive` veya `unknown` | **COMPLIANT** (unit hiç bulunamadı/kurulu değil — systemd sürümüne göre iki biçim de görülür) |
| *herhangi biri* | `active` | **NON-COMPLIANT** (çakışma) |
| başka her rc/stdout kombinasyonu (command hatası, izin/bus hatası, rc/stdout çelişkisi — örn. rc=0 + stdout=inactive —, boş veya tanınmayan stdout, `failed`/`activating`/`reloading` gibi geçiş durumları) | — | **NON-COMPLIANT/FAIL-CLOSED** ("durumu güvenle inactive/kurulu-değil olarak doğrulanamadı") |

Yukarıdaki matrisin tamamı `tests/run_offline_tests.sh` içinde
`firewalld_*` adlı ayrı testlerle (active, inactive, unit-not-found,
command error, rc/stdout çelişkisi, malformed çıktı) kanıtlanır.

## IPv6 ve default policy beklentileri

`/etc/default/ufw` içindeki dört alan (`IPV6`, `DEFAULT_INPUT_POLICY`,
`DEFAULT_OUTPUT_POLICY`, `DEFAULT_FORWARD_POLICY`) `roles/ufw_audit/defaults/main.yml`
içindeki değişkenlerle ifade edilir ve inventory/group_vars üzerinden
override edilebilir. Varsayılan beklenen profil:

- `ufw_audit_expected_ipv6: "yes"` — IPv6 trafiği de UFW kural kümesine
  tabi olmalı; `no` NON-COMPLIANT'tır.
- `ufw_audit_expected_default_input_policy: "DROP"` — gelen trafik
  varsayılan olarak reddedilmeli.
- `ufw_audit_expected_default_output_policy: "ACCEPT"` — giden trafik
  varsayılan olarak izinli (standart UFW/masaüstü-sunucu varsayımı).
- `ufw_audit_expected_default_forward_policy: "DROP"` — bu host bir
  router/NAT gateway olarak kullanılmıyorsa forward trafiği reddedilmeli.

Her dört alan için aynı fail-closed kural geçerlidir: alan dosyada hiç
yoksa, birden fazla kez görülüyorsa (duplicate) veya beklenmeyen/
ayrıştırılamaz bir değer taşıyorsa (örn. tırnaksız/bozuk biçim) kontrol
her zaman **NON-COMPLIANT**'tır — boş veya belirsiz bir değer hiçbir
zaman sessizce compliant kabul edilmez.

## SSH allow kuralı ↔ inventory portu eşleşmesi

Audit, SSH portu için açık bir TCP allow kuralı arar ve bu port **tam
olarak** inventory'deki `ansible_port` değeridir (tanımlı değilse
Ansible'ın gerçek SSH bağlantısı için kullandığı varsayılanla tutarlı
biçimde 22 varsayılır). Bu, audit'in kendi hedefine bağlandığı portun
gerçekten UFW tarafından açık bırakıldığını kanıtlar; sabit/hardcoded
22 kontrolü değildir.

`ansible_port` önce Ansible'ın **bağlantı katmanında** doğrulanır:
sayıya çevrilemeyen bir değer (`ansible_port=notanumber` gibi) audit'in
kendi task'larından biri bile çalışmadan, play'in en başında Ansible
tarafından fail-closed reddedilir — bu, role'ün kendi port kontrolüne
hiç ulaşmaz ve `tests/run_offline_tests.sh` içinde
`ansible_port_non_numeric_is_rejected_by_ansible_connection_layer_before_any_audit_task_runs`
testiyle dürüstçe (Ansible'ın kendi ret mesajı aranarak, role'ün
mesajıymış gibi gizlenmeden) kanıtlanır. Sayıya çevrilebilen ama
1..65535 aralığı dışında kalan değerler (`0`, `65536`, `70000` gibi) bu
katmanı geçer ve role'ün **kendi** `Inventory SSH portu geçerli mi
(1..65535)` kontrolüne ulaşır; bu kontrol de fail-closed NON-COMPLIANT
üretir (davranışsal olarak `ssh_port_zero_is_out_of_range_fails_closed` ve
`ssh_port_65536_is_out_of_range_fails_closed` testleriyle doğrulanır).

## Desteklenen `ufw status verbose` biçimleri

SSH allow kuralı kontrolü, yalnızca açıkça desteklenen biçimleri kabul
eder: `"<port>/tcp ALLOW IN <kaynak>"`, `"<port> ALLOW IN <kaynak>"`
(protokol verilmemişse hem tcp hem udp'yi kapsadığı UFW sözleşmesiyle
kabul edilir) ve bunların `"(v6)"` IPv6 varyantları. Yalnızca `ALLOW IN`
eylemi kabul edilir.

Aşağıdakiler **KASITLI olarak eşleşmez** ve compliant SAYILMAZ (belirsiz/
tanınmayan çıktı hiçbir zaman compliant kabul edilmez):

- **Uygulama profili tabanlı kurallar** (`"OpenSSH ALLOW IN Anywhere"`
  gibi) — hangi port(lar)ı kapsadığı bu çıktıdan doğrudan anlaşılamaz.
- **Port aralığı kuralları** (`"6000:6007/tcp ALLOW IN Anywhere"`) —
  belirli tek bir port için açık bir izin olduğunu kanıtlamaz.
- **`LIMIT IN`** (rate-limited) kuralları — bu, `ALLOW`'dan farklı bir
  eylemdir; kontrolü sağlamaz.
- **Yalnız UDP** kuralları — TCP gereksinimini karşılamaz.
- **`DENY`/`REJECT`** kuralları — açıkça izin değildir.

## Kontrol listesi

- UFW binary'sinin varlığı ve çalıştırılabilirliği (`/usr/sbin/ufw`)
- UFW gerçekten active mi (`ufw status verbose` → `Status:`, bkz. yukarısı)
- firewalld çakışması (bkz. "Firewalld karar matrisi")
- `/etc/default/ufw`: `IPV6`, `DEFAULT_INPUT_POLICY`,
  `DEFAULT_OUTPUT_POLICY`, `DEFAULT_FORWARD_POLICY` (bkz. yukarısı)
- Logging seviyesi (`Logging: on (<level>)`; `off` veya ayrıştırılamayan
  bir seviye her zaman NON-COMPLIANT)
- Inventory SSH portunun 1..65535 aralığında geçerli olması
- SSH portu için en az bir desteklenen biçimde TCP allow kuralı

Bir kontrol uygunsuz olsa bile audit durmaz; tüm kontroller çalışır ve her
biri kendi task adıyla ayrı ayrı raporlanır (yalnızca desteklenmeyen
Ubuntu sürümü tespiti durdurucudur, bkz. "Desteklenen sürümler"). Play'in
son task'ı (`UFW Audit | Sonuç: uyumluluk özeti`) yalnızca **tüm**
kontroller geçtiyse başarılı olur; böylece job'ın terminal durumu
(successful/failed) host'un UFW baseline'ı için genel compliant/
non-compliant durumunu dürüstçe yansıtır.

## UI görünürlüğü

DORAnsible'ın sanitize edilmiş Job UI'ı bugün her event için yalnızca
**task adı**, **host** ve **ok/changed/failed** bilgisini gösterir
(`backend/app/services/execution/normalize.py` — `event_data.res`, yani
modülün tam sonucu, hiçbir koşulda dışarı taşınmaz). Bu yüzden:

- Hangi kontrolün geçtiği/kaldığı **task adından ve o task'ın
  ok/failed durumundan** anlaşılır.
- Bu playbook'taki `assert`/`debug` task'larının `fail_msg`/`success_msg`/
  `msg` içeriği (örn. hangi allow kuralının bulunduğu, firewalld
  rc/stdout ayrıntısı, hangi `/etc/default/ufw` alanının hangi değeri
  taşıdığı) bugün UI'da **görünmez**. Bu ayrıntıları görmek için
  playbook'u doğrudan çalıştırmanız gerekir (örn. `ansible-playbook -v`)
  veya offline test harness'ini (`tests/run_offline_tests.sh`) kullanmanız
  gerekir.

## Offline testler

`tests/run_offline_tests.sh`, `roles/ufw_audit/tasks/checks.yml`
mantığını gerçek bir SSH hostu, bağlantı veya sudo gerektirmeden sahte
(fixture) `ufw status verbose` ve `/etc/default/ufw` içerikleriyle
çalıştırır (`tests/fixtures/*.txt`) ve ayrıca üretim rolünün salt-okunur
yüzeyini yapısal olarak doğrular (`tests/assert_read_only_surface.py`).
Kapsananlar: tam uyumlu çıktı; binary eksik/çalıştırılamaz durumlarının
AYRI AYRI (JSON tipli extra-vars ile gerçek boolean girdiyle, string
"false" DEĞİL) fail-closed kanıtlanması; `ufw status verbose` rc≠0;
`ufw.service` "active" görünse bile gerçek `Status:` "inactive" ise
NON-COMPLIANT olması; duplicate `Status:` satırı; firewalld karar
matrisinin tamamı (active, inactive, unit-not-found, command error,
rc/stdout çelişkisi, malformed çıktı); IPv6/default policy'lerin eksik/
duplicate/malformed/yanlış değer durumları; logging kapalı/malformed/
kabul edilen seviye; SSH allow kuralı eksik, uygulama-profili, yalnız-UDP,
yalnız-LIMIT, port-aralığı biçimlerinin hiçbirinin kabul edilmemesi;
nonstandard `ansible_port` ile eşleşen/eşleşmeyen kural; `ansible_port`
için erişilebilir sınır değerleri (`0`, `65536`, `70000`); `ansible_port`
sayısal olmayan bir değer verildiğinde Ansible'ın bağlantı katmanında
role'e hiç ulaşmadan fail-closed reddedilmesi; birden fazla uygunsuzlukta
tüm bağımsız kontrollerin yine de çalışması; salt-okunur yüzeyin yapısal
kilidi; ve o kilidin kendi EXACT argv/modül allowlist mantığının hermetik
regresyon kanıtı (`--self-test`: iki izin verilen systemctl argv'si geçer,
arbitrary unit/eksik-fazla argüman/Jinja-dinamik değer/farklı alt komut
reddedilir, `ufw allow` ve `lineinfile` reddedilir). Çalıştırmak için:

```
./tests/run_offline_tests.sh
```

## Sınırlar

- **UFW kural kümesini değerlendirir, ağdaki fiili trafiği DEĞİL.**
  UFW, Linux netfilter/nftables üzerinde çalışan bir kural yönetim
  katmanıdır. Aşağıdaki hiçbiri bu audit'in görüş alanında **değildir**
  ve UFW'yi tamamen **bypass edebilir**:
  - **Docker.** Docker, kendi iptables/nftables kurallarını doğrudan
    netfilter'a ekler; UFW'nin bilgisi/kontrolü dışında konteyner port
    yayınlamaları (`-p`) trafiği UFW kurallarını atlayarak doğrudan
    yönlendirilebilir (bu, yaygın bilinen bir Docker+UFW etkileşim
    sorunudur).
  - **Ham (raw) `nftables`/`iptables` kuralları.** UFW'nin yönetmediği,
    elle veya başka bir araçla eklenmiş ek kurallar UFW'nin kural
    kümesiyle etkileşime girip beklenmeyen izin/reddetme davranışına yol
    açabilir; bu audit yalnızca `ufw status verbose` çıktısını okur, ham
    netfilter kural tablosunu incelemez.
  - Diğer trafik yolları: host ağı modunda çalışan servisler, VPN/tünel
    arayüzleri, veya UFW etkinleştirilmeden önce/sonra elle eklenmiş
    kalıcı kurallar da bu audit'in kapsamı dışındadır.
  - **Sonuç: "COMPLIANT" sonucu tam ağ güvenliği garantisi DEĞİLDİR.**
    Yalnızca UFW'nin kendi bildirdiği kural kümesi ve `/etc/default/ufw`
    profili beklenen baseline ile uyumlu demektir; hostun fiilen aldığı/
    reddettiği trafiğin tam ve nihai açıklaması değildir.
- Uygulama profili tabanlı kurallar, port aralıkları, `LIMIT IN`
  kuralları desteklenmez (bkz. "Desteklenen `ufw status verbose`
  biçimleri") — bu biçimler SSH allow kontrolünü sağlamaz, sessizce
  yok sayılmaz ve NON-COMPLIANT üretir.
- Remediation (düzeltme) yapmaz; bu örnek project yalnızca audit'tir.
- SSH sertleştirmesi (bkz. `../ubuntu-ssh-audit/`), hesap/kullanıcı
  hardening'i ve paket güncellemeleri bu dilimin kapsamı dışındadır.
- Gerçek IP, kullanıcı adı, private key veya başka bir secret içermez.
