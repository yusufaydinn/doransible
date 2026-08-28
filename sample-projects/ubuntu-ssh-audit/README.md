# Ubuntu SSH Audit

Salt-okunur örnek project. Ubuntu üzerinde çalışan `sshd`'nin
**global/default** yapılandırmasını (`sshd -T`, bağlantı bağlamı
olmadan) okur ve güvenlik açısından ilgili ayarları raporlar. Playbook
hiçbir dosya içeriğini, servis durumunu veya paketi değiştirmez —
ayrıntı ve sınırlar için aşağıdaki "Değişiklik kapsamı" ve "Sınırlar"
bölümlerine bakın. **Bu bir tam host güvenlik değerlendirmesi değildir**:
kapsamı ve `Match` blok sınırı için "Sınırlar" bölümüne bakın.

## Amaç

`ssh_hardening` gibi düzeltme (remediation) role'lerinden **önce**
çalıştırılacak bir tanı adımı: mevcut SSH yapılandırmasının neresinin
uygun, neresinin uygunsuz olduğunu değişiklik yapmadan görmek. Remediation
ve firewall bu örnek project'in kapsamı dışındadır.

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
  `ansible.builtin.command` (yalnız `sshd -T` okumak için,
  `changed_when: false`), `ansible.builtin.assert` ve `debug`. `shell`
  veya serbest komut üretimi kullanılmaz.
- `command` argümanları sabit bir listedir (`argv`); kullanıcı girdisinden
  veya inventory içeriğinden birleştirilmez.
- Playbook hiçbir **configuration** değişikliği yapmaz: dosya yazmaz,
  paket kurmaz/kaldırmaz, servis restart/reload etmez.
- Bu bir "hosta hiçbir iz bırakmama" garantisi **değildir**. `sshd -T`
  çağrısı `become` ile root olarak çalıştığı için hedefte SSH ve sudo
  auth logları (`/var/log/auth.log` vb.) normal şekilde satır üretir.
  Ayrıca Ansible, her task için kendi bağlantı/modül çalıştırma
  mekanizmasının parçası olarak hedefte geçici çalışma dosyaları
  oluşturup temizler (community.general değil, `ansible.builtin.command`
  dahil tüm modüller için standart Ansible davranışı); bu playbook'a özgü
  değildir ve önlenemez.
- Playbook Check veya Normal kipte çalıştırılabilir. `sshd -T` task'ı
  `check_mode: false` taşır; böylece salt-okunur komut Check kipinde de
  atlanmadan çalışır. Başka hiçbir task bu istisnaya ihtiyaç duymaz.
- Cipher/MAC/KEX listeleri yalnızca **hesaplanır ve bir `debug` task'ının
  `msg`'sine yazılır**, hiçbir "olması gereken liste" ile zorunlu kılınmaz.
  Sanitize edilmiş event özeti task payload'ını yayımlamaz; bounded ham
  Ansible görüntü çıktısı ise sanitize/redakte edilmiş kabul edilmez — bkz.
  "UI görünürlüğü" bölümü.

## Sudo gereksinimi

`sshd -T`'nin host key dosyalarını ve tüm yapılandırmayı eksiksiz
çözebilmesi için root yetkisi gerekir. Bu yüzden yalnızca o tek task
`become: true` taşır (playbook'un geneli `become: false`'dur).

DORAnsible şu an bir become-parolası credential'ı **saklamaz**: uygulamada
şifrelenmiş credential deposu veya onboarding sihirbazı henüz yoktur (bkz.
aşağıdaki "Mevcut ürün gerçeği"). Bu yüzden hedef kullanıcının **parolasız
sudo** (`NOPASSWD`) çalıştırabilmesi gerekir; interaktif parola sorulursa
`become` başarısız olur ve `sshd -T` task'ı hata verir.

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

## DORAnsible'a nasıl kaydedilir

1. **Project dizinini erişilebilir bir köke taşıyın**, iki seçenekten
   biriyle:
   - Bu dizini `app-data/projects/ubuntu-ssh-audit` altına kopyalayın
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
   dosyasının kendisinde secret tutulmaz.
4. **Playbook'u çalıştırın**: `ubuntu-ssh-audit.yml`.

## Profil bazlı beklentiler

Aşağıdaki kontroller ortam profiline göre değişebilir; bu yüzden sabit
"doğru" değer yerine `roles/ssh_audit/defaults/main.yml` içindeki
değişkenlerle ifade edilir ve inventory/group_vars üzerinden override
edilebilir:

- `ssh_audit_kbd_interactive_authentication_allowed` — PAM tabanlı MFA
  kullanan ortamlar `true` yapabilir.
- `ssh_audit_tcp_forwarding_allowed_values` — `AllowTcpForwarding`
  OpenSSH'ta boolean değildir: `yes`/`all`/`no`/`local`/`remote`
  değerlerini alabilir. Varsayılan yalnızca `["no"]`'dur; `local` ve
  `remote` varsayılan "kapalı" profilde de compliant SAYILMAZ. Bastion/
  jump-host rolündeki host grupları ihtiyaç duydukları değeri (örn.
  `"local"`) listeye açıkça eklemelidir.
- `ssh_audit_expected_allow_users` / `_allow_groups` / `_deny_users` /
  `_deny_groups` — boş bırakılırsa yalnız bilgi amaçlı raporlanır; bir
  liste verilirse etkin değerle karşılaştırılır. Kullanıcı/grup erişim
  kısıtları isteğe bağlıdır, zorunlu değildir.

## Fail-closed sözleşmesi

sshd -T alanları iki farklı biçimde gelir ve fail-closed kuralları buna
göre ayrılır.

**Scalar alanlar** (`PermitRootLogin`, `PasswordAuthentication`,
`PubkeyAuthentication`, `PermitEmptyPasswords`,
`KbdInteractiveAuthentication`, `X11Forwarding`, `AllowAgentForwarding`,
`AllowTcpForwarding`, `MaxAuthTries`, `LoginGraceTime`) OpenSSH'ta her
zaman **tam olarak bir satır** üretir. Bu alanlar için:

- Alan `sshd -T` çıktısında hiç yoksa (eksik) veya birden fazla kez
  görülüyorsa (duplicate) → **NON-COMPLIANT**.
- `yes`/`no` beklenen bir alanda değer boş veya `yes`/`no` dışında bir
  şeyse → **NON-COMPLIANT**. Boş değer, "false bekleniyordu" durumunda
  bile sessizce compliant sayılmaz.
- `MaxAuthTries` eksik, sayısal değilse veya `0` ise → **NON-COMPLIANT**.
- `LoginGraceTime` `0` ise (sınırsız bekleme) → **NON-COMPLIANT**.
  Saniye (`120`) ve tek birimli sshd_config biçimleri (`10m`, `1h`, `3w`
  — s/m/h/d/w) doğru saniyeye çevrilir; bileşik (`1h30m`) veya tanınmayan
  bir biçim ayrıştırılamaz kabul edilir ve **NON-COMPLIANT**'tır.

**Liste alanları** (`AllowUsers`, `AllowGroups`, `DenyUsers`,
`DenyGroups`) OpenSSH'ta scalar değildir: her eleman kendi satırında
yazılır (`dump_cfg_strarray`), bu yüzden **0..N satır** üretebilirler.
Bu dört alan için "tam olarak bir kez" kuralı **uygulanmaz**:

- **0 satır geçerli bir durumdur** ("liste boş") ve fail-closed hata
  değildir; varsayılan (boş) profilde yalnızca raporlanır.
- Birden fazla satır duplicate sayılmaz; tüm satırlardaki değerler
  toplanır (union), keyword'ün kaç kez göründüğü değil.
- Keyword'ün bulunup değerinin bulunmadığı bir satır (sshd -T'nin
  normalde asla üretmediği bozuk bir biçim) → **NON-COMPLIANT**
  (fail-closed); bu, "0 satır" durumuyla karıştırılmaz.
- Beklenen profil listesi (`ssh_audit_expected_allow_users` vb.) boşsa
  toplanan liste yalnız raporlanır ve compliant sayılır. Doluysa
  toplanan değerlerle sort edilmiş, deterministik biçimde karşılaştırılır.

## Kontrol listesi

- Root SSH girişi (`PermitRootLogin`)
- Parola ile kimlik doğrulama (`PasswordAuthentication`)
- Public-key kimlik doğrulama (`PubkeyAuthentication`)
- Boş parola ile giriş (`PermitEmptyPasswords`)
- Keyboard-interactive kimlik doğrulama (`KbdInteractiveAuthentication`)
- X11 forwarding, agent forwarding, TCP forwarding (`AllowTcpForwarding`
  ayrı bir allowed-values profiliyle değerlendirilir, bkz. yukarısı)
- `MaxAuthTries`, `LoginGraceTime`
- Kullanıcı/grup erişim kısıtları (`AllowUsers`/`AllowGroups`/`DenyUsers`/`DenyGroups`)
- Cipher/MAC/KEX listeleri (yalnız raporlama)

Bir kontrol uygunsuz olsa bile audit durmaz; tüm kontroller çalışır ve her
biri kendi task adıyla ayrı ayrı raporlanır. Play'in son task'ı
(`SSH Audit | Sonuç: uyumluluk özeti`) yalnızca **tüm** kontroller
geçtiyse başarılı olur; böylece job'ın terminal durumu (successful/failed)
host'un global/default baseline için genel compliant/non-compliant
durumunu dürüstçe yansıtır (bkz. "Sınırlar" — bu, `Match` bloklarını
kapsamaz).

## UI görünürlüğü

DORAnsible'ın sanitize edilmiş Job UI'ı bugün her event için yalnızca
**task adı**, **host** ve **ok/changed/failed** bilgisini gösterir
(`backend/app/services/execution/normalize.py` — `event_data.res`, yani
modülün tam sonucu, hiçbir koşulda dışarı taşınmaz). Bu yüzden:

- Hangi kontrolün geçtiği/kaldığı **task adından ve o task'ın
  ok/failed durumundan** anlaşılır.
- Bu playbook'taki `assert`/`debug` task'larının `fail_msg`/`success_msg`/
  `msg` içeriği (örn. "PermitRootLogin=yes, izin verilen: ['no']" gibi
  ayrıntılar, Cipher/MAC/KEX değerleri, toplanan AllowUsers/AllowGroups
  listeleri) bugün UI'da **görünmez**. Bu ayrıntıları görmek için
  playbook'u doğrudan çalıştırmanız gerekir (örn. `ansible-playbook -v`)
  veya offline test harness'ini (`tests/run_offline_tests.sh`) kullanmanız
  gerekir.

## Offline testler

`tests/run_offline_tests.sh`, `roles/ssh_audit/tasks/checks.yml`
mantığını gerçek bir SSH hostu, bağlantı veya sudo gerektirmeden sahte
(fixture) `sshd -T` çıktılarıyla çalıştırır (`tests/fixtures/*.txt`).
Kapsananlar: tam uyumlu çıktı, birden fazla uygunsuzlukta tüm 11
kontrolün (kullanıcı/grup kontrolü dahil) yine de çalışması, eksik/
tekrarlanan zorunlu scalar alan, `LoginGraceTime` için `0`/birimli/bozuk
değer, `AllowTcpForwarding` için `local`/`remote` (hem varsayılan
profilde reddedilmesi hem de bastion profili override'ıyla kabul
edilmesi), ve `AllowUsers`/`AllowGroups` için: 0 satırlı varsayılan boş
liste durumu, birden fazla satırın duplicate sayılmadan toplanıp profille
eşleştirilmesi, ve değersiz (bozuk) bir liste satırının fail-closed
olması. Çalıştırmak için:

```
./tests/run_offline_tests.sh
```

## Sınırlar

- **Yalnızca global/default sshd baseline'ı değerlendirir.** `sshd -T`
  bu playbook'ta bağlantı bağlamı veren `-C user=...,host=...,addr=...`
  olmadan çalıştırılır. OpenSSH `-C` verilmeden koşullu `Match` bloklarını
  (`Match User`, `Match Group`, `Match Address`, `Match LocalAddress`
  vb.) hiç uygulamaz/değerlendirmez — yalnız dosyadaki en üst (global)
  ayarları döndürür. Bu dilimde bir bağlantı-bağlamı sistemi veya
  `sshd_config` dosya taraması **yoktur**. Sonuç olarak: bir host'un
  belirli bir kullanıcı, grup veya adres için `Match` bloğuyla daha gevşek
  (veya daha sıkı) bir yapılandırması olabilir ve bu audit onu **görmez**.
  **COMPLIANT sonucu tam host güvenliği veya tüm bağlantı bağlamları için
  bir iddia değildir** — yalnızca global/default baseline için geçerlidir.
- MFA, bastion ve port-forwarding ihtiyaçlarını körlemesine "uygunsuz"
  saymaz; yukarıdaki profil değişkenleriyle açıkça yönetilir.
- Cipher/MAC/KEX içeriğini zorunlu kılmaz, yalnız hesaplar ve `debug`
  task'ının `msg`'sine yazar; bugünkü sanitize UI'da görünmez (bkz. "UI
  görünürlüğü").
- Remediation (düzeltme) yapmaz; bu örnek project yalnızca audit'tir.
- Firewall, hesap/kullanıcı hardening'i ve paket güncellemeleri bu dilimin
  kapsamı dışındadır.
- Gerçek IP, kullanıcı adı, private key veya başka bir secret içermez.
