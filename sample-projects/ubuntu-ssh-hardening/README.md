# Ubuntu SSH Hardening

Bu örnek project, komşu [`ubuntu-ssh-audit`](../ubuntu-ssh-audit/README.md)
project'inin tespit ettiği SSH baseline'ını **uygulayan** (remediation)
bir role içerir. Audit **salt-okunurdur**; bu project ise gerçek
değişiklik yapar: tek, DORAnsible'a ait bir `sshd_config.d` drop-in
dosyası yazar ve SSH servisini kontrollü biçimde reload eder.

**Önce audit'i, sonra bunu okuyun.** Bu README, audit'in README'sindeki
"Mevcut ürün gerçeği" ve "Fail-closed sözleşmesi" bölümleriyle aynı
varsayımları paylaşır ve onlarla ÇELİŞMEZ; burada tekrar edilmeyen
ayrıntılar için audit README'sine bakın.

## Amaç ve kapsam

- Ubuntu 22.04 LTS ve 24.04 LTS.
- Yalnız **tek** bir dosyayı yönetir:
  `/etc/ssh/sshd_config.d/00-doransible-ssh-hardening.conf`. Ana
  `sshd_config` dosyası ve başka hiçbir drop-in **hiçbir zaman** okunmaz
  (dışında salt-okunur `sshd -t`/`sshd -T` çağrılarının doğal olarak tüm
  aktif yapılandırmayı görmesi) veya değiştirilmez.
- Uyguladığı baseline, audit'in varsayılanlarıyla **birebir aynıdır** ve
  bu dilimde **özelleştirilemez** (bkz. "Profil kilidi" bölümü). Şablonun
  **yazdığı** güvenli varsayılan değerler:

  ```text
  PermitRootLogin no
  PasswordAuthentication no
  PubkeyAuthentication yes
  PermitEmptyPasswords no
  KbdInteractiveAuthentication no
  X11Forwarding no
  AllowAgentForwarding no
  AllowTcpForwarding no
  MaxAuthTries 6
  LoginGraceTime 60
  ```

  **LIVE-AUDIT-FIX2:** DOĞRULAMA (pre/post-reload compliance) tarafında bu
  10 alan artık TEK bir semantikle değerlendirilmiyor -- ubuntu-ssh-audit
  ile birebir hizalı biçimde ikiye ayrılmıştır (bkz. "Profil kilidi"
  bölümü):
  - İlk 8'i (boolean/enum) yukarıdaki değerlere **tam (exact) eşitlikle**.
  - `MaxAuthTries`, **1..6** arası pozitif tam sayıysa; `LoginGraceTime`,
    ayrıştırılabilir ve saniye karşılığı **0 < x <= 60** ise **de**
    COMPLIANT sayılır -- yani hedefte sözlüksel olarak önde sıralanan
    başka bir drop-in bu ikisini yazılan varsayılandan DAHA SIKI bir
    değere (ör. `MaxAuthTries 3`) çekiyorsa, bu artık candidate'i
    yanlışlıkla reddetmez.

- Host'lar **serial: 1** (birer birer) işlenir; bir host'taki reload/
  lockout riski diğer host'ları etkilemez.
- `shell` kullanılmaz; tüm komutlar FQCN modüller (`ansible.builtin.*`)
  ve sabit `argv` listeleriyle çalıştırılır.

### Yönetilen / yönetilmeyen alanlar

**Yönetilen (bu dilimde):** yukarıdaki 10 scalar directive (yazma
tarafında hâlâ 10 -- şablon her zaman bu 10 satırı üretir; doğrulama
tarafında 8'i exact, 2'si (MaxAuthTries/LoginGraceTime) bounded-numeric
semantikle değerlendirilir, bkz. "Profil kilidi").

**Yönetilmeyen (bu dilimde BİLEREK dışarıda bırakıldı):**

- `AllowUsers` / `AllowGroups` / `DenyUsers` / `DenyGroups`
- `Ciphers` / `MACs` / `KexAlgorithms`
- Password, Vault veya become-password credential desteği (audit'teki
  aynı sınır: yalnızca passwordless/NOPASSWD sudo ve private-key dosya
  yolu desteklenir)

Bu alanlar şablonda (`roles/ssh_hardening/templates/managed-drop-in.conf.j2`)
hiçbir zaman yazılmaz; drop-in dosyasında bulunmazlarsa OpenSSH kendi
normal Include zincirindeki (varsa) başka bir tanımı veya kendi
varsayılanını kullanmaya devam eder.

## Profil kilidi (path/baseline override koruması)

Bu dilimde güvenlik profili **özelleştirilebilir değildir**. Yönetilen
path, desteklenen Ubuntu sürüm listesi, baseline değerleri, compliance
alan listeleri ve reconnect zaman aşımı ayarları `defaults/main.yml`'deki
değişkenlerden gelir; bu değişkenler group_vars, host_vars veya (bu
backend'de bugün mümkün olmasa da) extra_vars ile TEORİK olarak override
edilebileceği için, role'ün İLK adımı (`profile_lock_check.yml`) yedi ayrı
kontrolle bunları doğrular:

- Yönetilen path'in tam olarak
  `/etc/ssh/sshd_config.d/00-doransible-ssh-hardening.conf` olması,
- Desteklenen Ubuntu sürüm listesinin tam olarak `["22.04", "24.04"]`
  olması,
- 8 exact + 2 numeric (MaxAuthTries=6, LoginGraceTime=60) **yazma**
  varsayılanının tamamının audit varsayılanıyla birebir eşleşmesi,
- **LIVE-AUDIT-FIX2:** `ssh_hardening_max_auth_tries_max` (=6) ve
  `ssh_hardening_login_grace_time_max_seconds` (=60) -- DOĞRULAMA
  tarafının bounded-numeric üst sınırları, yukarıdaki yazma
  varsayılanlarından AYRI kilitlenir,
- `ssh_hardening_baseline_fields_exact` (8 exact alan listesi) 8 alanının
  da eksiksiz, doğru sırada ve doğru beklenen değerlerle bulunması,
- **LIVE-AUDIT-FIX2:** `ssh_hardening_baseline_fields_numeric` (2
  bounded-numeric alan listesi) 2 alanının da eksiksiz, doğru sırada ve
  doğru üst sınırlarla (maxauthtries max=6, logingracetime
  max_seconds=60) bulunması,
- Reconnect `timeout`/`sleep` ayarlarının tam olarak `30`/`2` olması
  ("güvenli, pozitif, sıkı üst sınırlı" gereksinimi, bir aralık kontrolü
  yerine bilinen-güvenli sabit değerlere PIN edilerek karşılanır --
  diğer baseline değerleriyle aynı desen).

**FIX1.1 -- gölgeleme koruması:** bu yedi kontrolün karşılaştırdığı
"beklenen sabit değerler" `profile_lock_check.yml` içinde bir
`vars:` bloğunda ADLANDIRILMIŞ DEĞİŞKENLER olarak TUTULMAZ; doğrudan
`that:`/`fail_msg` Jinja ifadelerinin İÇİNE gömülü LİTERAL değerlerdir.
Gerekçe: `vars:` altında tanımlanan bir isim yine de bir Ansible değişken
adıdır ve Ansible'ın değişken önceliği sırasında `set_fact`/registered
vars, task vars'tan DAHA YÜKSEK önceliğe sahiptir -- yani play içinde
daha önce çalışan bir `set_fact` (veya, bu backend'de bugün mümkün
olmasa da, bir extra-var) AYNI ADI kullanarak "kilit" değerini
gölgeleyebilirdi. Literal gömülü değerlerin gölgelenecek bir adı yoktur.
`tests/check_profile_lock.yml`'deki `*_combined_shadow_attempt_*`
testleri, hem GERÇEK değişkeni HEM DE eski "kilit" adını taklit eden bir
extra-var'ı BİRLİKTE vererek bunu offline kanıtlar.

Bu yedi kontrolden **herhangi biri** sapma bulursa role, `apply.yml`'e hiç
ulaşmadan, **hiçbir dosya yazmadan** fail-closed durur. Bu kontrol
olmasaydı şöyle bir açık kalırdı: `ssh_hardening_baseline_fields_exact`/
`ssh_hardening_baseline_fields_numeric`'in `expected`/`max`/`max_seconds`
alanları, YAZILAN içeriği üreten AYNI değişkenlerden türetilir -- biri o
değişkeni override ederse hem yazılan içerik hem onu doğrulayan beklenti
birlikte kayar ve normal compliance kontrolü bunu **yakalayamaz** (ikisi
de aynı bozuk kaynaktan beslenir). Profil kilidi, bu ikisinden BAĞIMSIZ
bir referans noktasıdır.

**LIVE-AUDIT-FIX2 -- neden bounded-numeric ayrımı gerekti:** canlı bir
normal-mode koşusunda hedefte sözlüksel olarak daha erken sıralanan başka
bir drop-in (`/etc/ssh/sshd_config.d/00-ansible-hardening.conf`)
`MaxAuthTries 3` uyguluyordu -- audit açısından DAHA SIKI ve tamamen
uyumlu bir değer. Eski (tek tip, tam exact) compliance kontrolü bunu
`6` beklediği için yanlışlıkla NON-COMPLIANT saydı, candidate'i geri aldı
ve reload'u engelledi. Düzeltme, MaxAuthTries/LoginGraceTime için
ubuntu-ssh-audit'in (`roles/ssh_audit/tasks/checks.yml`) kullandığı
BİREBİR AYNI "bounded" (üst-sınırlı) semantiği benimser: bu iki alan için
DAHA SIKI bir değer (daha düşük MaxAuthTries, daha kısa LoginGraceTime)
güvenlik açısından EN AZ yazılan varsayılan kadar güvenlidir. Diğer 8
boolean/enum alan için "daha sıkı" diye bir kavram YOKTUR (ikili
durumlar arasında sıkılık sıralaması yapılamaz) -- bu yüzden onlar exact
eşitlikte kalır.

## Drop-in precedence kararı (gerekçeli)

Bu bölüm, "hangi dosya adı kazanır" sorusunu **kör bir varsayımla değil**,
Ubuntu'nun gerçek paketlenmiş `sshd_config`'i ve `sshd_config(5)` man
sayfası incelenerek yanıtlar.

1. Ubuntu'nun openssh-server paketinin gönderdiği varsayılan
   `/etc/ssh/sshd_config` dosyası, `Include /etc/ssh/sshd_config.d/*.conf`
   satırını dosyanın **EN ÜSTÜNDE**, kendi (yorum satırı olmayan)
   ayarlarından (`KbdInteractiveAuthentication no`, `X11Forwarding yes`,
   `UsePAM yes` vb.) **ÖNCE** taşır.
2. `Include`'daki glob joker karakterleri **sözlüksel (lexical) sırada**
   genişletilir (`sshd_config(5)`: "each pathname may contain wildcards
   that will be expanded and processed in lexical order").
3. `sshd_config(5)`: "for each keyword, the first obtained value will be
   used" -- yani OpenSSH bir keyword için satır satır okuduğu dosyalar
   arasında **İLK GÖRDÜĞÜ** değeri kullanır, en son değeri değil.

Bu üç gerçeğin birleşimi:

- Bu drop-in'deki her değer, ana `sshd_config`'in kendi (yorum satırı
  olmayan) varsayılanlarına **otomatik olarak** kazanır, çünkü `Include`
  onlardan önce işlenir -- dosya adımızdan bağımsız, her zaman doğru.
- `sshd_config.d/` içindeki dosyalar **arasında** ise sözlüksel olarak
  **ERKEN** sıralanan dosya kazanır. Bu, birçok başka `*.d/` sisteminin
  (ör. `logrotate.d`, çoğu "son yükleyen kazanır" konvansiyonu)
  alıştığı davranışın **TERSİDİR**.
- Bu yüzden dosya adı bilinçli olarak **`00-doransible-ssh-hardening.conf`**
  seçildi: Ubuntu cloud image'larında yaygın biçimde görülen
  `50-cloud-init.conf` gibi paket/araç drop-in'lerine karşı sözlüksel
  olarak önde durup kazanmasını garanti eder.
- **Dürüst sınır:** bu dosyadan sözlüksel olarak DAHA ERKEN sıralanan bir
  drop-in (ör. elle eklenmiş `"00-"`dan önce gelen bir ad, veya
  `00-doransible-ssh-hardening.conf`'tan önce sıralanan başka bir araç)
  yine de bu değerleri geçersiz kılabilir. DORAnsible bunu ne engeller ne
  de tespit eder -- bu, OpenSSH'ın `Include` davranışının doğal bir
  sonucudur. Böyle bir override'ın **NASIL** yakalandığı önemlidir: aday
  drop-in disk üzerine yazıldıktan hemen sonra, **reload'dan önce**
  yapılan `sshd -T` okuması zaten TÜM `Include` zincirini (dolayısıyla
  varsa daha erken sıralanan başka bir dosyayı da) görür -- çünkü
  `sshd -t`/`sshd -T` çalışan bir daemon'ın belleğini değil, DİSKTEKİ
  dosyaları okur. Bu yüzden precedence sorunu, servis hiç reload
  edilmeden, `apply.yml`'in **pre-reload** effective-baseline kontrolünde
  (bkz. "Güvenli uygulama sırası" adım 3, "effective-baseline" alt-adımı)
  yakalanır ve aday geri alınır.
  Reload SONRASI aynı kontrolün tekrarı (`post_verify.yml`), reload
  anındaki/systemd tarafındaki olası bir tutarsızlığa karşı ikinci,
  bağımsız bir son-kontroldür -- precedence'ın birincil yakalama noktası
  DEĞİLDİR.
- Ansible'ın otomatik backup dosya adı (`<dosya>.<pid>.<zaman damgası>~`)
  **`.conf` ile bitmez**; bu yüzden `Include .../*.conf` glob'una hiçbir
  zaman dahil olmaz ve sshd tarafından yanlışlıkla canlı yapılandırma
  olarak okunmaz.

## Güvenli uygulama sırası

`roles/ssh_hardening/tasks/main.yml`in **gerçek** kontrol sırası (bu
bölüm ve `main.yml`'in başındaki yorum birebir aynı tutulur):

0. **Profil kilidi** (`profile_lock_check.yml`): bkz. yukarıdaki "Profil
   kilidi" bölümü. Sapma varsa hiçbir sonraki adıma geçilmez.
1. **OS desteği** (`os_check.yml`): Ubuntu 22.04/24.04 dışı bir sürüm
   `UNSUPPORTED` ile durur.
2. **Sistem ön-koşulları** (`system_checks.yml`): passwordless sudo/root
   (**gerçek UID=0**, `become` ile `id -u` çalıştırılarak -- R1-V3H4-
   SIMPLIFY, bkz. "Otomatik SSH/sudo önkoşulu"), `sshd` binary'sinin
   varlığı, DEĞİŞİKLİKTEN ÖNCE mevcut yapılandırmanın zaten `sshd -t`'den
   geçtiği doğrulanır. Bu adım artık apply.yml'e ulaşmadan önceki TEK
   erişim/yetki önkoşuludur; başarısızsa hiçbir sonraki adıma geçilmez.
3. **Failure-atomic apply** (`apply.yml`): aşağıdaki alt-adımların
   TAMAMI, bir doğrulamanın `nonzero rc` dönmesi Ansible'ın kendi
   task-failure mekanizmasıyla play'i KESMEDEN (her komut
   `failed_when: false` taşır; sonuç yakalanıp normalize edilir ve
   rollback kararı BİZİM `when:` koşullarımızla verilir -- rollback
   task'larına HER ZAMAN ulaşılır) çalışır:
   - **Yerleştirme**: mevcut yönetilen drop-in varsa,
     `ansible.builtin.template`'in `backup: true` seçeneği onu üzerine
     yazmadan ÖNCE restorable bir kopya olarak saklar; yeni drop-in
     `root:root` sahipliği ve `0644` izinle (yalnız root yazabilir)
     yerleştirilir.
   - **Syntax doğrulaması**: `sshd -t` ile TAM aktif yapılandırma
     (yalnız bizim dosyamız değil, tüm `Include` zinciri) doğrulanır.
   - **Effective-read** (yalnız syntax geçerliyse): `sshd -T` ile aday'ın
     effective config'i disk üzerinden OKUNUR. Bu OKUMA komutunun
     kendisi de ayrı ve ÖNCELİKLİ bir başarısızlık sınıfıdır --
     "okuma başarısız oldu" (stdout güvenilmez, hiçbir alan
     değerlendirilmedi) ile "okuma başarılı ama değerler yanlış" farklı
     şeylerdir ve mesajda ayrı raporlanır.
   - **Effective-baseline** (yalnız okuma da başarılıysa): okunan
     effective değerler baseline'a karşı **tam eşitlikle** doğrulanır
     (bkz. "Drop-in precedence kararı" -- precedence sorunları BURADA
     yakalanır).
   - **Rollback** (yukarıdaki üç alt-adımdan HERHANGİ BİRİ
     başarısızsa): önceki dosya varsa geri yüklenir, yoksa yeni dosya
     kaldırılır; ardından aktif dosya yapısı **TEKRAR** `sshd -t` ile
     doğrulanır (rollback'in kendisi de doğrulanır, yine
     `failed_when: false` ile). "Rollback yapıldı" iddiası ancak bu
     ikinci doğrulama da geçerse kurulur; geçmezse mesaj bunu KRİTİK
     olarak ayrı raporlar ve otomatik onarım denemez. Her koşulda
     `ansible.builtin.fail` ile durulur -- **servis HİÇ reload edilmez**.
   - **Reload** (yalnız ÜÇ doğrulama da başarılıysa VE içerik gerçekten
     değiştiyse): doğru SSH servisi (`ssh.service` -- Ubuntu'nun
     openssh-server paketinin gerçek systemd birim adı;
     `Alias=sshd.service` ile eşleşir) reload edilir. `ssh.service`'in
     kendi `ExecReload`'ı zaten `sshd -t` + `kill -HUP` çalıştırır --
     bizim ön-doğrulamalarımız buna **ek** bir savunma katmanıdır,
     yerine geçmez.
4. **Reload sonrası doğrulama** (`post_verify.yml`, main.yml tarafından
   `include_tasks` ile **dinamik** olarak ve YALNIZ `not ansible_check_mode
   and not ssh_hardening_candidate_failed` iken dahil edilir -- bkz. aşağıdaki
   "check mode'da reset_connection çalışmaması nasıl sağlanıyor" notu):
   `meta: reset_connection` ile Ansible'ın ControlPersist ile çoğullanmış ESKİ
   bağlantısı kasıtlı olarak kapatılır, ardından
   `ansible.builtin.wait_for_connection` ile **GERÇEKTEN YENİ** bir SSH
   bağlantısının kurulabildiği **sınırlı** bir sürede
   (`ssh_hardening_reconnect_timeout_seconds`, sabit 30sn --
   `profile_lock_check.yml` tarafından kilitlenir) doğrulanır -- sonsuza
   dek asılı kalınmaz. Bu ikili adım (reset + wait), role'ün kendi
   içine gömülü somut bir **"lockout olmadı" duman testidir**: reload
   sonrasında bu hedefe gerçekten yeni bir SSH oturumu (şu anki
   PasswordAuthentication/PubkeyAuthentication ayarlarıyla) açılabiliyor
   mu sorusunu doğrudan cevaplar. Bağlantı kurulunca `sshd -T` ile disk
   üzerindeki config YENİDEN OKUNUP (bkz. "sshd -T ne yapar" notu)
   baseline'a karşı **aynı** mantıkla (adım 3'teki ile birebir aynı
   `compliance_assert.yml`) tekrar doğrulanır. Bu adım başarısız olursa
   role burada otomatik ikinci bir yazma/reload denemez (trusted-operator
   güvenlik modelinde otomatik remediation kapsam dışıdır) -- yalnız açık bir hata ile
   durur; inceleme operatöre aittir.

**check mode'da `reset_connection` çalışmaması NASIL sağlanıyor (dürüst
düzeltme):** `ansible.builtin.meta: reset_connection` bir `when:` koşulunu
**kendi üzerinde desteklemez** -- Ansible bunu, `when:` iliştirilmiş olsa
bile, `[WARNING]: reset_connection task does not support when conditional`
uyarısıyla birlikte **koşulsuz** çalıştırır. Bu, canlı bir UI check-mode
koşusunda gerçekten **gözlemlenmiş** bir davranıştır: `post_verify.yml`
önceki sürümde `main.yml` tarafından `import_tasks` (STATIC) ile dahil
ediliyordu ve hem dış `import_tasks`'ın hem `meta` task'ının kendi üzerinde
`when: not ansible_check_mode` vardı -- ama STATIC import tüm task'ları
(meta dahil) her zaman play'in task listesine ekler; meta task'ın kendi
when-desteksizliği yüzünden bu koşul onu check mode'da çalışmaktan
ALIKOYAMADI. Düzeltme: `main.yml`, `post_verify.yml`'i artık
`include_tasks` (**DYNAMIC**) ile dahil eder. `include_tasks`'taki `when:`
çalışma anında değerlendirilir; YANLIŞSA `post_verify.yml`'in içeriği
(`meta: reset_connection` dahil) play'in task listesine **hiç eklenmez** --
meta task'ın kendi when-desteksizliği hiç devreye giremez, çünkü task zaten
hiç var olmaz. Bu yüzden `post_verify.yml` içindeki task'ların artık kendi
üzerlerinde ayrıca `not ansible_check_mode` koşulu **yoktur** (gereksiz VE
aynı uyarıyı tekrar tetiklerdi) -- tek gate, main.yml'deki `include_tasks`
seviyesindedir. Aynı gate, aday başarısız olduğunda da (`ssh_hardening_
candidate_failed`) zinciri açmaz. Bu davranış `tests/check_post_verify_gate.yml`
ile offline kanıtlanmıştır (bkz. "Offline testler").

**`sshd -T` ne yapar (dürüst düzeltme):** hem pre-reload hem post-reload
adımlarındaki `sshd -T` çağrısı, ÇALIŞAN sshd daemon'ının BELLEĞİNİ
sorgulamaz. Her çağrıda `/usr/sbin/sshd` kendi başına YENİ bir süreç
olarak başlar ve disk üzerindeki `sshd_config` + `Include` zincirini o an
baştan PARSE eder -- `sshd -t` ile aynı mekanizma. İki çağrı arasındaki
fark İÇERİK değil, NE ZAMAN ve NASIL koştuklarıdır: post-reload çağrısı
reload'dan SONRA ve -- daha önemlisi -- yukarıdaki reset_connection +
wait_for_connection ile kurulan GERÇEKTEN YENİ bir SSH oturumu üzerinden
çalışır.

## Otomatik SSH/sudo önkoşulu (R1-V3H4-SIMPLIFY)

Bu role `PasswordAuthentication`'ı kapatır. Eğer hedefte **çalışan bir
key tabanlı giriş yoksa** ve bu değişiklik uygulanırsa, host'a SSH ile
erişim tamamen kaybedilebilir (out-of-band/console erişimi hariç).

**Eskiden** (BULGU3) bu risk, operatörün AYRI bir terminalde elle
doğrulayıp project'in `group_vars/all.yml` dosyasında elle
`true`'ya çektiği bir manuel mandalla (`ssh_hardening_confirm_key_based_
access`) yönetiliyordu. Bu mandal ve `group_vars/all.yml` dosyası
**tamamen kaldırıldı** (R1-V3H4-SIMPLIFY) -- artık elle düzenlenecek bir
dosya, yeniden kaydedilecek bir project veya çalıştırma sonrası "tekrar
false'a çekmeyi unutma" sorumluluğu **yoktur**. Bunun yerine güvenlik
zinciri **tamamen otomatiktir** ve İKİ gerçek (simüle edilmeyen) adıma
dayanır:

1. **Gathering Facts** (`ubuntu-ssh-hardening.yml`: `gather_facts: true`,
   play seviyesinde, bu role'ün İLK task'ından ÖNCE çalışır): Ansible,
   hedefe DORAnsible'ın mevcut credential sınırı içinde (inventory'deki
   `ansible_ssh_private_key_file`/`ansible_user`) GERÇEKTEN bir SSH
   bağlantısı kurar. Bağlantı kurulamazsa play bu role'e hiç girmeden
   host için başarısız olur -- hiçbir dosya yazılmaz.
2. **Passwordless sudo/root önkoşulu** (`system_checks.yml`, role'ün
   İKİNCİ adımı -- bkz. "Güvenli uygulama sırası"): `become: true` ile
   `id -u` çalıştırılır ve çıktının **gerçekten `0`** (gerçek UID=0)
   olduğu, ortak `uid_gate_assert.yml` task dosyası ile assert edilir
   (bkz. "R1-V3H4-SIMPLIFY-AUDIT-FIX1 -- tek kaynaklı assert" altta). Bu,
   operatörün elle işaretlediği bir BEYAN değil, **GERÇEK bir yetki
   denemesidir**.
   - Bu adım **başarısızsa** Ansible'ın standart task-failure
     davranışıyla play burada durur; `apply.yml`'deki **HİÇBİR** task
     (dosya yazma, reload) çalışmaz.
   - Bu adım **check mode'da DA** çalışır (`check_mode: false`, salt-
     okunur olduğu için -- bkz. "Check ve normal mode farkı"): operatör
     önizlemede bile ön-koşulun sağlanıp sağlanmadığını görür.

**R1-V3H4-SIMPLIFY-AUDIT-FIX1 -- non-interactive sözleşmesi AÇIKÇA
pinlenir:** sudo denemesinin **neden** non-interaktif kaldığı, "DORAnsible
become-parolası saklamıyor" GÖZLEMİNE değil, `ubuntu-ssh-hardening.yml`
play seviyesinde AÇIKÇA tanımlanan üç literal değere dayanır:

```yaml
become_method: sudo
become_user: root
become_flags: "-H -S -n"
```

- **Doğru sözleşme (FIX1.1 ile netleştirilmiştir):** play seviyesindeki
  `become: false` DEĞİŞMEDİ -- bu yüzden task'lar OTOMATİK olarak
  privilege escalation KAZANMAZ. Ancak role genelinde AÇIKÇA
  `become: true` taşıyan **her** task (bugün `system_checks.yml`,
  `apply.yml` VE `post_verify.yml`'de birden fazla task var -- bu SABİT
  bir sayı DEĞİLDİR, role geliştikçe yeni privileged task'lar
  eklenebilir/mevcutlar kaldırılabilir) yukarıdaki üç değeri miras alır.
  `become: true` TAŞIMAYAN task'lar (ör. `profile_lock_check.yml`,
  `os_check.yml` gibi salt-okunur/local kontroller) etkilenmez.
- `-n` (non-interactive): sudo'ya "parola gerekiyorsa PROMPT AÇMA,
  doğrudan hata ile çık" talimatını verir -- bu task'ın asla bir TTY'ye
  asılı KALMAYACAĞININ gerçek mekanizmasıdır. "DORAnsible become-
  parolası saklamaz/göndermez" gözlemi (bu projede hiçbir yerde
  `ansible_become_pass`/`ansible_sudo_pass` kullanılmaz) buna **ek** bir
  savunma katmanıdır, YERİNE geçmez.
- `-H`/`-S`: hedef kullanıcının (root) HOME'unu ayarlar / parola
  gerekirse stdin'den okumayı dener -- ama `-n` ile birlikte, parola
  GEREKİYORSA sudo yine de prompt açmadan başarısız olur.

**R1-V3H4-SIMPLIFY-AUDIT-FIX1 -- tek kaynaklı assert:** UID=0 kontrolünün
`that:`/`fail_msg`/`success_msg` ifadeleri artık YALNIZ
`roles/ssh_hardening/tasks/uid_gate_assert.yml` dosyasında tanımlıdır.
Hem `system_checks.yml` (gerçek `id -u` sonrası) hem offline test
harness'i (`tests/check_system_checks_gate.yml`, test-double sonrası) bu
dosyayı `import_tasks` ile İTHAL EDER -- assert iki yerde AYRI AYRI
YAZILMAZ. **FIX1.1:** bu paylaşım artık HERMETİK bir yapı testiyle
(`uid_gate_assert_import_resolves_to_canonical_file_in_both_callers`)
kanıtlanır -- her iki dosya YAML olarak parse edilir, `import_tasks`
hedefleri kendi dosya konumlarına göre resolve edilir ve ikisinin de
GERÇEKTEN aynı `uid_gate_assert.yml` dosyasına işaret ettiği doğrulanır;
test suite hiçbir tracked kaynak dosyaya YAZMAZ (bkz. "Offline testler").

**Dürüst sınır:** DORAnsible hedefe sudo yetkisi **vermez** -- yalnız
çalıştırmadan ÖNCE var olup olmadığını doğrular. Hedef kullanıcının
**ÖNCEDEN** (bu playbook'tan bağımsız olarak) NOPASSWD sudo yetkisine
sahip olması gerekir; yoksa role hiçbir kalıcı değişiklik yapmadan
`system_checks.yml`'de durur. Private key'in hedefte parolasız erişim
sağladığından emin olmak da operatörün sorumluluğundadır (bkz. "Sudo ve
key gereksinimleri") -- ama bunu ayrıca bir mandalla DORAnsible'a "beyan
etmesi" gerekmez; role bunu her çalıştırmada kendisi dener ve kanıtlar.

Tek kullanıcı onayı artık DORAnsible'ın **mevcut normal-mode risk
onayıdır** (check/diff sonrası UI'daki "Run" butonu) -- bu, HER
ÇALIŞTIRMANIN kendi run confirmation'ıdır, platformun genel onay
akışıdır ve bu role'e özgü ayrı bir onay YOKTUR.

## Check ve normal mode farkı

| | check mode | normal mode |
|---|---|---|
| Passwordless sudo/root önkoşulu (`system_checks.yml`, gerçek UID=0) | **çalışır** (salt-okunur, `check_mode: false`) | çalışır, başarısızsa zincir burada durur |
| Drop-in dosyası yazılır mı | **hayır** (`template` modülü hedef dosyayı yazmaz) | önkoşul geçtiyse ve içerik farklıysa evet |
| `sshd -t`/`sshd -T` / rollback | çalışmaz (komut task'ları check mode'da atlanır) | çalışır |
| Servis reload | **asla** | yalnız her iki doğrulama da başarılı VE içerik değiştiyse |
| Reconnect + post-reload `sshd -T` | çalışmaz | reload denemesi başarılıysa çalışır |

check mode'da `apply.yml` içindeki tüm komut tabanlı task'lar (`sshd -t`,
`sshd -T`, rollback, reload) kendi üzerlerindeki `when: not
ansible_check_mode` ile varsayılan Ansible davranışıyla **otomatik
atlanır** (hiçbiri `check_mode: false` taşımaz). `post_verify.yml`
(`reset_connection`, `wait_for_connection`, post-reload `sshd -T`) için
aynı garanti FARKLI bir mekanizmayla sağlanır: bu dosyanın TAMAMI
main.yml'de `include_tasks` (DYNAMIC) ile ve yalnız `not ansible_check_mode
and not ssh_hardening_candidate_failed` iken dahil edilir, dolayısıyla
check mode'da dosyanın içeriği play'e hiç eklenmez (bkz. "check mode'da
reset_connection çalışmaması nasıl sağlanıyor" notu -- `meta:
reset_connection` kendi üzerinde bir `when:` koşulunu desteklemediği için
task-seviyesi bir `when:` ile GÜVENİLİR biçimde engellenemezdi). Yalnız
`profile_lock_check.yml`, `os_check.yml` ve `system_checks.yml`'deki
salt-okunur kontroller (audit role'ündeki AYNI gerekçeyle) check mode'da
da çalışır, böylece operatör önizlemede ön-koşulların sağlanıp
sağlanmadığını da görür.

**Check mode'un UI'da GERÇEKTE gösterdiği şey** (dürüst düzeltme): audit
README'sindeki "UI görünürlüğü" bölümüyle aynı sınır burada da geçerlidir
-- DORAnsible'ın sanitize edilmiş Job UI'ı her event için yalnızca
**task adı**, **host** ve **ok/changed/failed** bilgisini gösterir
(`backend/app/services/execution/normalize.py`); modülün tam sonucu
(`res`, dolayısıyla `template` modülünün ürettiği içerik **diff'i**)
hiçbir koşulda dışarı taşınmaz. Bu yüzden UI'da check mode'da
görebileceğiniz şey, "Yönetilen drop-in'i yerleştir" task'ının
**changed=true/false** durumudur (yani "bu dosya değişecek mi") -- bu
dosyanın **içeriğinin ne olacağını** (satır satır diff) GÖRMEZSİNİZ. Tam
diff'i görmek için playbook'u doğrudan `ansible-playbook --check --diff`
ile çalıştırmanız veya offline test harness'ini (`tests/render_template.yml`)
kullanmanız gerekir.

## Idempotency

`apply.yml`, `ansible.builtin.template`'in standart içerik-diff
davranışına dayanır: ikinci bir normal-mode çalıştırmasında drop-in
içeriği zaten beklenen değerlerle aynıysa `template` task'ı
`changed=false` raporlar, backup oluşturulmaz ve reload task'ı
(`ssh_hardening_apply_changed` koşuluna bağlı olduğu için) çalışmaz.
`sshd -t`/`sshd -T` doğrulamaları her çalıştırmada (idempotent bir
no-op'ta bile) tekrar çalışır -- bunlar `changed_when: false` taşıdığı
için idempotency sonucunu etkilemez, yalnızca sürekli bir sağlık
kontrolü sağlar. Bu davranış `tests/check_apply_decisions.yml`'in
`apply_decision_idempotent_second_run_no_reload` senaryosuyla offline
kanıtlanmıştır (bkz. "Offline testler").

## Backup / rollback sınırları (dürüst)

- Rollback yalnızca **adım 3'ün İÇİNDE** (dosya yazıldı, syntax VEYA
  effective-read VEYA effective-baseline doğrulamasından biri başarısız
  oldu, servis HENÜZ reload edilmedi) otomatiktir ve bu pencerede
  güvenilirdir. Rollback'in kendisi de `sshd -t` ile TEKRAR doğrulanır;
  "rollback yapıldı" iddiası ancak bu ikinci doğrulama da geçerse kurulur
  (bkz. "Güvenli uygulama sırası" adım 3, "Rollback" alt-adımı). Bu
  ikinci doğrulama da başarısız olursa (rollback sonrası aktif
  yapılandırma HÂLÂ geçersizse -- son derece nadir, örneğin bir yarış
  koşulu), mesaj bunu KRİTİK olarak ayrı raporlar ve otomatik onarım
  denemez; hedefin elle incelenmesi gerekir.
- **Reload SONRASI** bir sorun (adım 4'ün `sshd -T` doğrulaması
  başarısız olursa) role BURADA otomatik olarak ikinci bir yazma/reload
  denemez. Servis zaten reload edilmiş olabileceğinden, "eski dosyayı
  geri yaz + tekrar reload et" döngüsü YENİ bir riskli operasyondur ve
  bilinçli olarak uygulanmadı (trusted-operator güvenlik modeli, "otomatik
  remediation" kapsam dışı; ayrıca GUVENLIK.md bölüm 20.3: sistem
  otomatik rollback yapmaz).
- **Bağlantı TAMAMEN kesilirse** (ör. host `AllowTcpForwarding`/ağ
  değişikliği, sistem çökmesi veya beklenmeyen bir nedenle erişilemez
  hale gelirse), rollback **garanti edilemez**. Bu role'ün kendi
  rollback mantığı SSH bağlantısının kendisine bağımlıdır; bağlantı
  yoksa hiçbir uzak eylem (ne rollback ne reload) çalıştırılamaz.
  `wait_for_connection`'ın sınırlı zaman aşımı da bu garantiyi
  GENİŞLETMEZ -- yalnızca "sonsuza dek asılı kalma" riskini önler.
  GUVENLIK.md bölüm 20.3'teki genel platform sözleşmesi burada da
  geçerlidir: "Sistem otomatik rollback yapmaz ve 'hedef kesin
  değişmedi' garantisi vermez."
- Backup dosyaları (`00-doransible-ssh-hardening.conf.<pid>.<zaman>~`)
  her gerçek değişiklikte disk üzerinde BİRİKİR; bu role onları
  otomatik temizlemez (MVP dışı kapsam genişletmesi). Operatör isterse
  bunları elle inceleyip silebilir.
- Bu rollback mekanizması yalnızca DORAnsible'ın kendi yazdığı TEK
  drop-in dosyası içindir; ana `sshd_config` veya başka bir drop-in'de
  (bizim tarafımızdan yönetilmeyen) bir sorun varsa bu role onu ne tespit
  eder ne de düzeltir.

## Sudo ve key gereksinimleri

Audit role'üyle birebir aynı sınır: DORAnsible bugün bir become-parolası
credential'ı **saklamaz**; hedef kullanıcının **parolasız sudo**
(`NOPASSWD`) çalıştırabilmesi gerekir. Bu, yalnız bir gözlem değil,
`ubuntu-ssh-hardening.yml`'de AÇIKÇA pinlenmiş `become_flags: "-H -S -n"`
(`-n` = non-interactive) ile ZORUNLU kılınır -- bkz. "Otomatik SSH/sudo
önkoşulu" bölümündeki "non-interactive sözleşmesi AÇIKÇA pinlenir" notu.
Private key dosyasının kendisi, `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST` ile
izin verilen bir kök altında (varsayılan `app-data/secrets`) durur ve
inventory'de yalnızca dosya yoluna referans verilir (bkz.
`inventory/hosts.yml`).

## DORAnsible'a nasıl kaydedilir

Audit'teki aynı adımlar (bkz. `../ubuntu-ssh-audit/README.md` →
"DORAnsible'a nasıl kaydedilir"), bu project'e uyarlanmış:

1. **Project dizinini erişilebilir bir köke taşıyın**:
   `app-data/projects/ubuntu-ssh-hardening` altına kopyalayın (varsayılan
   allowlist'e uyar) veya `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST`'e bu
   dizinin mutlak yolunu ekleyip backend'i yeniden başlatın.
2. **Project ekle**: kopyaladığınız/allowlist'e eklediğiniz kökü
   DORAnsible'da kaydedin.
3. **Inventory ekle**: `inventory/hosts.yml`'i kaydedin, kendi host'unuzu
   tanımlayın, private key kullanıyorsanız allowlist'e uygun bir yola
   koyup `ansible_ssh_private_key_file` ile referans verin. Hedef
   kullanıcının **önceden** NOPASSWD sudo yetkisine sahip olduğundan emin
   olun (bkz. "Otomatik SSH/sudo önkoşulu").
4. **Önce check mode ile önizleyin.** UI, "Yönetilen drop-in'i yerleştir"
   task'ının changed=true/false durumunu gösterir (bu dosyanın
   değişip değişmeyeceği); içerik diff'ini GÖRMEZ (bkz. "Check ve normal
   mode farkı"). Passwordless sudo/root önkoşulu check mode'da da
   çalışır, böylece ön-koşulun sağlanıp sağlanmadığını önizlemede de
   görürsünüz.
5. **Normal mode'u çalıştırın.** Manuel bir onay adımı veya elle
   düzenlenecek bir dosya YOKTUR (bkz. "Otomatik SSH/sudo önkoşulu") --
   role, hedefe erişimi ve yetkiyi kendisi doğrular; başarısızsa hiçbir
   dosya yazmadan durur.
6. **Sonucu audit ile doğrulayın.** Bu remediation'ın gerçek bir hedefte
   kapsamlı biçimde doğrulanması (`sample-projects/ubuntu-ssh-audit`
   playbook'unun aynı host'ta yeniden çalıştırılıp bulguların kapandığının
   kanıtlanması) bu dilimin KAPSAMI DIŞINDADIR -- bkz. "Sınırlar".

## Offline testler

`tests/run_offline_tests.sh`, gerçek bir SSH hostu, bağlantı veya sudo
gerektirmeden şunları doğrular:

- Projedeki tüm `.yml`/`.yaml` dosyalarının YAML olarak parse edildiği
  VE bulunan dosya sayısının sıfır olmadığı (find ifadesinin gerçekten
  eşleştiği).
- `ubuntu-ssh-hardening.yml`'in `ansible-playbook --syntax-check`'ten
  geçtiği.
- `profile_lock_check.yml`: varsayılan değerlerle geçtiği; yönetilen
  path override edilince, desteklenen sürüm listesi eksiltilince veya
  fazladan bir sürüm eklenince, herhangi bir baseline değeri override
  edilince, `ssh_hardening_baseline_fields_exact` (8 alan) eksiltilince
  veya bir `expected` değeri doğrudan değiştirilince (aynı 8 anahtar/sıra
  korunsa bile) fail-closed durduğu. LIVE-AUDIT-FIX2: AYRICA
  `ssh_hardening_baseline_fields_numeric` (2 alan -- maxauthtries/
  logingracetime) eksiltilince veya bir `max`/`max_seconds` sınırı
  doğrudan değiştirilince, VE `ssh_hardening_max_auth_tries_max`/
  `ssh_hardening_login_grace_time_max_seconds` bounded-policy üst
  sınırlarının KENDİSİ override edilince VE reconnect timeout/sleep
  override edilince fail-closed durduğu. FIX1.1: ayrıca GERÇEK değişkeni
  ve eski "kilit" adını (ör. `ssh_hardening_locked_baseline`,
  `ssh_hardening_locked_numeric_fields`) TAKLİT eden bir extra-var'ı
  BİRLİKTE vererek, kilit referanslarının artık gölgelenebilir
  adlandırılmış değişkenler OLMADIĞININ (literal gömülü değerler
  olduğunun) hem exact hem numeric liste için kanıtlandığı.
- `os_check.yml`: desteklenen (22.04, 24.04) ve desteklenmeyen
  (20.04, Debian) sürümlerin doğru işlendiği.
- `tests/check_system_checks_gate.yml` (R1-V3H4-SIMPLIFY, davranışsal
  kanıt): yalnızca `become` gerektiren gerçek `id -u` çağrısını (offline
  sandbox'ta gerçek root olmadan çalıştırılamaz -- bkz. "Sınırlar")
  `fake_uid_check_stdout` ile sürülen bir test-double komutla değiştirir;
  assert'in KENDİSİ ise (R1-V3H4-SIMPLIFY-AUDIT-FIX1) artık burada
  TEKRAR YAZILMAZ, `uid_gate_assert.yml` doğrudan import edilir.
  Kanıtlanan: UID **0 değilken** assert fail-closed durur VE sonraki
  "apply/reload marker" task'ı çıktıda hiç GÖRÜNMEZ; UID **0** iken
  assert geçer VE marker task'ı ÇALIŞIR. Gerçek `become`+`id -u`
  çağrısının kendisi (gerçek bir hedef ve root gerektirdiği için) bu
  round'un kapsamı DIŞINDADIR -- bkz. "Sınırlar".
- **R1-V3H4-SIMPLIFY-AUDIT-FIX1 (şema/yapı testleri):**
  `playbook_become_contract_pinned_exact`: `ubuntu-ssh-hardening.yml`'in
  YAML yapısı doğrudan parse edilerek (gerçek bir become denemesi
  ÇALIŞTIRMADAN) `become_method`/`become_user`/`become_flags`'in tam
  olarak `sudo`/`root`/`"-H -S -n"` olduğu VE play seviyesindeki
  `become`'un `false` KALDIĞI doğrulanır.
  `main_yml_system_checks_import_precedes_apply_import`: `main.yml`'in
  YAML yapısı üzerinden `system_checks.yml`'in import edildiği task
  index'inin `apply.yml`'inkinden KÜÇÜK (yani ÖNCE) olduğu doğrulanır.
- **R1-V3H4-SIMPLIFY-AUDIT-FIX1.1 (hermetik paylaşım kanıtı --
  KAYNAK DOSYA YAZMAZ):**
  `uid_gate_assert_import_resolves_to_canonical_file_in_both_callers`:
  önceki round'da bu paylaşımı kanıtlamak için `uid_gate_assert.yml`'i
  `cp`/`sed`/`trap` ile GEÇİCİ olarak bozan bir mutasyon bloğu vardı --
  bu KALDIRILDI (test suite artık hiçbir tracked kaynak dosyaya
  YAZMAZ). Yerine gelen test tamamen HERMETİKTİR: `system_checks.yml`
  VE `check_system_checks_gate.yml` YAML olarak parse edilir, ikisinin
  de `uid_gate_assert.yml` import'unu taşıyan task'ı bulunur, bu
  import'un HEDEF YOLU kendi dosyasının bulunduğu dizine göre resolve
  edilir (`system_checks.yml` için `uid_gate_assert.yml`,
  `check_system_checks_gate.yml` için `../roles/ssh_hardening/tasks/
  uid_gate_assert.yml`) ve ikisinin de GERÇEKTEN aynı, tek
  `roles/ssh_hardening/tasks/uid_gate_assert.yml` dosyasına (aynı
  `os.path.realpath`) işaret ettiği doğrulanır. Mutasyon doğrulaması
  istenirse yalnız elle, geçici olarak yapılıp geri alınabilir --
  kalıcı suite'e kaynak değiştiren kod GİRMEZ.
- `managed-drop-in.conf.j2`: varsayılan değerlerle render edilince tam
  olarak beklenen 10 satırı ürettiği, YÖNETİLMEYEN hiçbir directive'in
  (`AllowUsers` vb.) bir satır olarak görünmediği. Bir de -e ile bir
  baseline değişkenini override eden ayrı bir test VAR, ama bu test
  YALNIZ Jinja2 ikame mekanizmasını (şablon dosyası doğru değişken
  ikamesi yapıyor mu) render_template.yml adlı İZOLE bir harness'te
  ölçer -- role'ün gerçek giriş noktasını (`main.yml`) hiç çağırmaz,
  `profile_lock_check.yml` orada hiç çalışmaz. Testin adı ve yorumu bunu
  açıkça belirtir; "override edilir ve normal mode'da kabul edilir"
  anlamına GELMEZ -- gerçek role çağrısında AYNI override
  `profile_lock_baseline_value_override_fails_closed` testinin
  kanıtladığı gibi reddedilir.
- `compliance_assert.yml` (pre-reload VE post-reload'da ORTAK kullanılan
  mantık): tam uyumlu, tek alan sapmış (tampered), eksik alan ve
  duplicate alan senaryolarının doğru compliant/NON-COMPLIANT sonucu
  ürettiği; birden fazla uygunsuzlukta TÜM 10 alanın (ilk hatada
  durmadan) değerlendirildiği. LIVE-AUDIT-FIX2: MaxAuthTries/
  LoginGraceTime bounded-numeric politikası ayrıca kendi başına --
  "canlı hedef senaryosu" (maxauthtries=3, logingracetime=60 -> COMPLIANT,
  daha sıkı değer kabul edilir), maxauthtries=6 (write-default) COMPLIANT,
  maxauthtries 0/7/bozuk/eksik/duplicate NON-COMPLIANT, logingracetime=30
  COMPLIANT, logingracetime 0/61/bozuk/eksik/duplicate NON-COMPLIANT --
  fixture'larla (`tests/fixtures/post_sshd_t_maxauthtries_*.txt`,
  `post_sshd_t_logingracetime_*.txt`, `post_sshd_t_live_target_scenario.txt`)
  doğrulanır.
- `apply_decisions_*.yml` (BULGU1/BULGU2/FIX1.1 failure-atomic karar
  mantığı, gerçek modül çağrıları SİMÜLE edilerek): aday syntax hatası +
  backup varken restore/reload=0; aday syntax hatası + backup yokken
  remove/reload=0; pre-reload effective OKUMA komutunun KENDİSİ
  başarısız olduğunda (effective-read, "değerler yanlış"tan AYRI bir
  sınıf) rollback/reload=0; pre-reload effective baseline uyuşmazlığında
  rollback/reload=0; tam başarıda tek reload; idempotent ikinci
  çalıştırmada reload YOK; rollback-sonrası doğrulama başarılı/başarısız
  olduğunda "rollback yapıldı" iddiasının doğru kurulup kurulmadığı.
- `tests/check_post_verify_gate.yml` (LIVE-AUDIT-FIX1: main.yml'in gerçek
  `include_tasks` gate'ini BİREBİR aynı `when:` ifadesiyle çoğaltan bir
  harness -- davranışsal kanıt, `--check` bayrağı GERÇEKTEN geçilerek
  elde edilir, `ansible_check_mode` sahte bir değişkenle taklit
  EDİLMEZ): check mode'da VE aday-başarısız yolunda çıktıda ne
  `reset_connection` task adının ne de "does not support when
  conditional" uyarısının hiç GÖRÜNMEDİĞİ (dosya hiç dahil edilmediği
  için); normal-mode başarılı aday yolunda ise `include_tasks`'ın
  gerçekten devreye girdiği ve "Bağlantıyı sıfırla" task'ının çıktıda
  BAŞLADIĞI (bu ortamda gerçek sudo/hedef olmadığı için zincir daha
  sonra `sshd -T` adımında sudo hatasıyla durur -- bu beklenen ve bu
  testin kapsamı DIŞINDadır; ölçülen tek şey INCLUDE'un tetiklendiğidir).
- (Varsa) `ansible-lint`; bu geliştirme ortamında kurulu değildir, bu
  yüzden test script'i onu **atlar** (fail etmez).

Çalıştırmak için:

```
./tests/run_offline_tests.sh
```

## Sınırlar (dürüst)

- **Bu dilimde gerçek bir hedefte hiç çalıştırılmadı.** `system_checks.yml`
  (sudo/root, sshd binary, ön-koşul `sshd -t`) ve `apply.yml`/
  `post_verify.yml`'nin gerçek write → syntax-verify → pre-reload-verify
  → rollback-veya-reload → reconnect → wait_for_connection → post-verify
  zincirinin MODÜL ÇAĞRILARI (template/command/copy/file/
  systemd_service/wait_for_connection) gerçek bir Ubuntu hedef ve become
  gerektirir; bu round bunu KAPSAMAZ. Bunun yerine failure-atomic KARAR
  MANTIĞI (`apply_decisions_*.yml`), gerçek modüllerin üreteceği
  sonuçları simüle eden sahte register değişkenleriyle offline
  doğrulanmıştır (bkz. "Offline testler") -- bu, gerçek modül
  çağrılarının kendisini (ör. `template`'in gerçekten doğru dosyayı
  yazması, `systemd_service`'in gerçekten `ssh.service`'i reload etmesi)
  KANITLAMAZ; bunlar iyi bilinen, standart Ansible primitifleridir.
- İdempotency, `ansible.builtin.template`'in standart, iyi bilinen
  içerik-diff davranışına DAYANDIRILDI ve bu davranışın KARAR
  MANTIĞI offline kanıtlandı (yukarıya bakın), ama gerçek bir hedefte
  tekrar çalıştırılarak UÇTAN UCA AYRICA kanıtlanmadı.
- Yalnızca global/default sshd baseline'ını yazar ve doğrular; audit'teki
  gibi `Match` blok sınırlaması burada da geçerlidir -- bu role
  `sshd_config`'teki olası `Match User`/`Match Group`/`Match
  Address` bloklarını okumaz, yazmaz veya bunlarla etkileşmez.
- AllowUsers/AllowGroups/DenyUsers/DenyGroups, Ciphers/MACs/KexAlgorithms
  bu dilimde yönetilmez (bkz. "Yönetilen/yönetilmeyen alanlar").
- Password/Vault/become-password credential desteği eklenmedi.
- Bu role'ün kendi rollback'i yalnızca kendi yazdığı TEK dosya için ve
  yalnızca reload'dan ÖNCEKİ pencerede güvenilirdir (bkz.
  "Backup/rollback sınırları").
- **Dürüst sınır (R1-V3H4-SIMPLIFY):** DORAnsible sudo yetkisi VERMEZ.
  Hedef kullanıcı daha önceden NOPASSWD sudo yetkisine sahip olmalıdır;
  yoksa role `system_checks.yml`'de, hiçbir kalıcı değişiklik yapmadan
  durur. Bu önkoşulun GERÇEK `become`+`id -u` çağrısı, offline test
  suite'inde (gerçek root/hedef olmadığı için) doğrudan çalıştırılamaz --
  yalnız DECISION mantığı (`tests/check_system_checks_gate.yml`) offline
  kanıtlanmıştır (bkz. "Offline testler").
- Gerçek IP, kullanıcı adı, private key veya başka bir secret içermez.
