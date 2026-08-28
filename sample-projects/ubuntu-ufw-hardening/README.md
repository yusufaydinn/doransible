# Ubuntu UFW Hardening

Bu örnek project, komşu [`ubuntu-ufw-audit`](../ubuntu-ufw-audit/README.md)
project'inin denetlediği UFW (Uncomplicated Firewall) baseline'ını
**uygulayan** (remediation) bir role içerir. Audit **salt-okunurdur**; bu
project ise gerçek değişiklik yapar: inventory'deki SSH portu için bir TCP
allow kuralı ekler, üç default policy'yi ve logging seviyesini ayarlar,
UFW'yi etkinleştirir.

**Önce audit'i, sonra bunu okuyun.** Bu README, audit'in README'sindeki
"Firewalld karar matrisi", "Desteklenen `ufw status verbose` biçimleri" ve
"Sınırlar" (Docker/ham nftables UFW'yi bypass edebilir) bölümleriyle aynı
varsayımları paylaşır ve onlarla ÇELİŞMEZ; burada tekrar edilmeyen
ayrıntılar için audit README'sine bakın. **Mevcut audit project'i bu round
kapsamında değiştirilmemiştir.**

## Amaç ve kapsam

- Ubuntu 22.04 LTS ve 24.04 LTS.
- Uygulanan tam profil (ubuntu-ufw-audit'in varsayılan profiliyle
  IPv6 HARİÇ birebir aynı -- bkz. "Yönetilen/yönetilmeyen alanlar"):
  - Inventory'deki `ansible_port` (tanımlı değilse 22) için bir TCP allow
    kuralı -- **UFW enable edilmeden ÖNCE**.
  - `DEFAULT_INPUT_POLICY=DROP` (gelen trafik varsayılan reddedilir).
  - `DEFAULT_OUTPUT_POLICY=ACCEPT` (giden trafik varsayılan izinlidir).
  - `DEFAULT_FORWARD_POLICY=DROP` (routed/forward trafiği varsayılan
    reddedilir -- bu host bir router/NAT gateway olarak kullanılmıyorsa).
  - `Logging: on (low)`.
  - UFW etkinleştirilir (`ufw --force enable`).
- Host'lar **serial: 1** (birer birer) işlenir; **`serial: 1` TEK BAŞINA**
  bir host'taki hatanın kalan host'lara devam etmeyi durdurmasını GARANTİ
  ETMEZ (yalnız batch büyüklüğüdür) -- bu garanti **`serial: 1` +
  `any_errors_fatal: true`** BİRLEŞİMİNDEN gelir: herhangi bir host'ta
  unhandled bir hata (preflight/apply/reverify/reconnect'in herhangi bir
  adımında) oluşursa play TÜMÜYLE sonlanır, henüz sırası gelmemiş host'lar
  işlenmeden kalır. Otomatik rollback DEĞİLDİR -- bkz. "Lockout ve
  rollback sınırı".
- `shell`/`raw` hiç kullanılmaz; her komut FQCN modül
  (`ansible.builtin.command`) ve sabit bir `argv` listesidir. Tek
  değişken bileşeni, önceden 1..65535 aralığında doğrulanmış SSH portu
  ve sabit `ufw_hardening_logging_level` (="low") değeridir -- kullanıcıdan
  veya inventory'den doğrulanmadan hiçbir değer argv'ye ulaşmaz. Bu iddia
  kalıcı bir yapısal testle (`tests/assert_command_surface_and_order.py`)
  korunur: her `ansible.builtin.command` task'ının argv'si sabit bir
  allowlist'e karşı EXACT eşleşmelidir; allowlist dışı bir argv veya
  `shell`/`raw` kullanımı testi kırar.

### Yönetilen / yönetilmeyen alanlar

**Yönetilen:** SSH portu için TCP allow kuralı, `DEFAULT_INPUT_POLICY`,
`DEFAULT_OUTPUT_POLICY`, `DEFAULT_FORWARD_POLICY`, logging seviyesi, UFW
etkinliği.

**Yönetilmeyen (bu dilimde BİLEREK dışarıda bırakıldı):**

- **IPv6 (`/etc/default/ufw` içindeki `IPV6` alanı).** ubuntu-ufw-audit
  bunu denetler (varsayılan beklenti: `yes`) ama BU role dokunmaz --
  UFW'nin CLI'ı IPv6 desteğini açıp kapatmak için bir alt komut sunmaz
  (yalnızca `/etc/default/ufw` dosyasını doğrudan düzenleyip ardından
  UFW'yi disable/enable döngüsünden geçirerek değiştirilebilir); bu,
  sistem varsayılan dosyasını doğrudan yazan, karşılığında CLI-düzeyi
  idempotent bir doğrulaması olmayan EK bir risk yüzeyi olurdu. Hedefte
  IPv6 zaten paket varsayılanıyla (`yes`) geliyorsa bu bir sorun
  yaratmaz; gelmiyorsa bu role SONRASINDA `ubuntu-ufw-audit` çalıştırıldığında
  IPv6 alanı hâlâ NON-COMPLIANT raporlanabilir -- bu dürüstçe bilinen bir
  sınırdır (bkz. "Sınırlar").
- Uygulama profili tabanlı kurallar, port aralıkları, ek `ufw allow`
  kuralları (ör. HTTP/HTTPS) -- bu dilimde yalnız SSH portu yönetilir.
- `ufw route`/gerçek IP forwarding etkinleştirme -- yalnız
  `DEFAULT_FORWARD_POLICY` değeri yazılır; sistemin gerçekten paket
  yönlendirmesi yapıp yapmadığı (`net.ipv4.ip_forward`) bu role'ün
  kapsamı dışındadır.
- Password/Vault/become-password credential desteği (audit'teki aynı
  sınır: yalnızca passwordless/NOPASSWD sudo ve private-key dosya yolu
  desteklenir).

## Güvenli uygulama sırası

`roles/ufw_hardening/tasks/main.yml`in **gerçek** kontrol sırası:

0. **Profil kilidi** (`profile_lock_check.yml`): desteklenen sürüm
   listesi, üç default policy, logging seviyesi ve reconnect timeout/
   sleep ayarları sabit değerlerden sapmışsa hiçbir UFW yazma komutu
   çalıştırılmadan fail-closed durur (bkz. "Profil kilidi").
1. **OS desteği** (`os_check.yml`): Ubuntu 22.04/24.04 dışı bir sürüm
   `UNSUPPORTED` ile durur.
2. **Sistem ön-koşulları** (`system_checks.yml`), sırayla:
   1. **Passwordless sudo/root önkoşulu** (`uid_gate_assert.yml`, İLK ve
      TEK privileged gate) -- gerçek `become` ile `id -u` çalıştırılıp
      çıktının **gerçekten `0`** olduğu doğrulanır. Başarısızsa
      apply.yml'deki HİÇBİR UFW yazma komutu çalışmaz (bkz. "Passwordless
      sudo gereksinimi").
   2. `ufw` binary'sinin varlığı/çalıştırılabilirliği.
   3. **firewalld fail-closed gate'i** (`firewalld_gate_assert.yml`) --
      firewalld aktifse VEYA durumu güvenle belirlenemiyorsa (rc/stdout
      matrisinin dışındaki HERHANGİ bir kombinasyon) hiçbir değişiklik
      yapmadan durur (bkz. "Firewalld karar matrisi").
   4. **SSH portu geçerliliği** (`ssh_port_gate_assert.yml`) --
      `ansible_port` (yoksa 22) 1..65535 aralığında değilse hiçbir UFW
      yazma komutu çalışmaz; geçerliyse `ufw_hardening_ssh_port_numeric`
      sabitlenir.
3. **Sıralı UFW yazma komutları** (`apply.yml`), her biri yalnız normal
   modda (`not ansible_check_mode`) ve `become: true` ile:
   1. **SSH portu için TCP ALLOW kuralı** (`ufw allow <port>/tcp`) --
      **HER ZAMAN sonraki tüm adımlardan, özellikle enable'dan, ÖNCE**.
   2. `ufw default deny incoming`
   3. `ufw default allow outgoing`
   4. `ufw default deny routed`
   5. `ufw logging low`
   6. `ufw --force enable` (`--force`: Ansible'ın TTY'si yoktur;
      olmadan interaktif "Proceed with operation (y|n)?" istemi sonsuza
      dek asılı kalırdı).
   7. **Enable-sonrası yeniden doğrulama** (`compliance_reverify.yml`,
      mevcut bağlantı üzerinden): active/üç policy/logging/SSH-allow
      kuralı tekrar okunup doğrulanır. Başarısızsa `ansible.builtin.fail`
      ile durulur -- UFW ZATEN etkinleştirilmiş olabilir, bu role burada
      otomatik ikinci bir düzeltme DENEMEZ (bkz. "Lockout ve rollback
      sınırı").
4. **Reload sonrası bağlantı/lockout doğrulaması** (`post_verify.yml`,
   yalnız normal modda): `meta: reset_connection` ile ESKİ bağlantı
   kapatılır, `wait_for_connection` ile GERÇEKTEN YENİ bir SSH
   bağlantısının sınırlı sürede (`ufw_hardening_reconnect_timeout_seconds`,
   sabit 30sn) kurulabildiği kanıtlanır. Bu adım, adım 3.7'nin mevcut
   bağlantı üzerinden yaptığı config-doğruluğu kontrolünden AYRI ve
   TAMAMLAYICI bir amaca hizmet eder -- bkz. "Neden iki ayrı doğrulama
   adımı var".

## Neden iki ayrı doğrulama adımı var

UFW/netfilter tipik olarak ESTABLISHED/RELATED bağlantılara (mevcut SSH
oturumunuz dahil) izin veren bir "before" kuralı taşır. Bu yüzden
`ufw --force enable` ANINDA hâlâ açık olan mevcut SSH oturumunuz, SSH
portu için yazılan allow kuralı yanlış/eksik olsa BİLE conntrack
ESTABLISHED istisnası sayesinde kesilmeyebilir -- yani "mevcut
bağlantım hâlâ çalışıyor" tek başına "YENİ bağlantılar da çalışıyor"
ANLAMINA GELMEZ. Bu yüzden:

1. **Config-doğruluğu kontrolü** (adım 3.7, `compliance_reverify.yml`):
   mevcut bağlantı üzerinden `ufw status verbose`/`/etc/default/ufw`
   okunur -- active/policy/logging/SSH-allow kuralının GERÇEKTEN yazıldığı
   doğrulanır.
2. **Bağlantı-kanıtı** (adım 4, `post_verify.yml`): `reset_connection` +
   `wait_for_connection` ile GERÇEKTEN YENİ bir el sıkışma zorlanır -- SSH
   portu allow kuralının fiilen YENİ bağlantılara izin verdiğini kanıtlayan
   somut bir "lockout olmadı" duman testidir.

## Profil kilidi

Bu dilimde güvenlik profili **özelleştirilebilir değildir**. Desteklenen
sürüm listesi, üç default policy, logging seviyesi ve reconnect timeout/
sleep ayarları `defaults/main.yml`'deki değişkenlerden gelir; bu
değişkenler group_vars, host_vars veya (bu backend'de bugün mümkün olmasa
da) extra_vars ile TEORİK olarak override edilebileceği için, role'ün
İLK adımı (`profile_lock_check.yml`) bunları, `ubuntu-ssh-hardening`'deki
AYNI FIX1.1 gerekçesiyle (gölgeleme koruması -- literal gömülü değerlerin
gölgelenecek bir adı yoktur), sabit LİTERAL değerlerle karşılaştırır.
Sapma varsa apply.yml'e hiç ulaşılmadan, HİÇBİR UFW YAZMA KOMUTU
ÇALIŞTIRILMADAN fail-closed durulur.

## Firewalld karar matrisi (hard gate)

`ubuntu-ufw-audit`'teki AYNI karar matrisi (`systemctl is-active
firewalld` rc + stdout BİRLİKTE değerlendirilir), ama orada salt
raporlanan bir bulguyken BURADA bir **HARD GATE**'tir:

| rc | stdout (trim edilmiş) | Sonuç |
|----|------------------------|-------|
| 0  | `active`               | **FAIL-CLOSED** (çakışma) |
| 3  | `inactive`             | Güvenli -- zincire devam edilir |
| 4  | `inactive` veya `unknown` | Güvenli -- zincire devam edilir |
| başka her rc/stdout kombinasyonu | — | **FAIL-CLOSED** |

firewalld aktifse veya durumu güvenle belirlenemiyorsa **hiçbir UFW yazma
komutu çalıştırılmaz**.

## Passwordless sudo gereksinimi

DORAnsible sudo yetkisi **VERMEZ** -- yalnız çalıştırmadan ÖNCE var olup
olmadığını doğrular. Hedef kullanıcının **ÖNCEDEN** NOPASSWD sudo yetkisine
sahip olması gerekir; yoksa role, `system_checks.yml`'in İLK privileged
adımında (`uid_gate_assert.yml`), hiçbir kalıcı değişiklik yapmadan durur.
`ubuntu-ssh-hardening.yml` ile BİREBİR aynı sözleşme: play seviyesinde
`become: false`, `become_method: sudo`, `become_user: root`,
`become_flags: "-H -S -n"` (`-n` = non-interactive: parola gerekiyorsa
PROMPT AÇMADAN doğrudan hata ile çık) AÇIKÇA pinlenir; yalnız AÇIKÇA
`become: true` taşıyan task'lar bu değerleri MİRAS alır.

## Check ve normal mode farkı

| | check mode | normal mode |
|---|---|---|
| Profil kilidi / OS / passwordless sudo / ufw binary / firewalld / SSH portu kontrolleri | **çalışır** (salt-okunur, `check_mode: false`) | çalışır, başarısızsa zincir burada durur |
| Ön-okumalar (`ufw show added`, `ufw status verbose`, `/etc/default/ufw`, `/etc/ufw/ufw.conf`) | **çalışır** (`check_mode: false`, önizleme için) | çalışır |
| "Planlanan değişiklik" önizleme mesajları (PLANNED/NO-CHANGE) | **basılır** (dürüst önizleme) | basılır (bilgi amaçlı) |
| SSH allow / default policy / logging / enable yazma komutları | **hiçbiri çalışmaz** | ön-koşullar geçtiyse VE kendi `would_*` kararı `true` ise çalışır (zaten uygun bir alan İÇİN komut ÇALIŞTIRILMAZ) |
| Enable-sonrası yeniden doğrulama | çalışmaz | enable denemesi sonrası HER ZAMAN çalışır (bir şey değişmiş olsun ya da olmasın) |
| Reset connection + wait_for_connection | **asla** | yalnız zincir buraya kadar başarıyla ulaştıysa VE en az bir `would_*` bayrağı `true` idiyse (`ufw_hardening_any_change`) |

Check mode'da `apply.yml` içindeki TÜM yazma/enable/reverify task'ları
kendi üzerlerindeki `when: not ansible_check_mode` ile atlanır. Bunun
yerine, PRE-okuma verilerinden türetilen altı "would_*" bayrağı (SSH allow
eklenecek mi, üç default policy değişecek mi, logging değişecek mi, enable
edilecek mi) her zaman değerlendirilir ve altı ayrı `debug` mesajıyla
**dürüstçe** raporlanır -- "PLANNED: ..." (değişecek) veya "NO-CHANGE: ...
zaten uygun" (değişmeyecek). Bu, DORAnsible'ın check mode'da TAM içerik
diff'i göstermediği (bkz. "UI görünürlüğü") bir ortamda "planlanan
değişiklikleri dürüstçe göstermeli" gereksinimini karşılar.

`post_verify.yml` (`reset_connection`, `wait_for_connection`) için aynı
garanti FARKLI bir mekanizmayla sağlanır: bu dosyanın TAMAMI main.yml'de
`include_tasks` (DYNAMIC) ile ve yalnız `not ansible_check_mode` iken
dahil edilir -- `ansible.builtin.meta: reset_connection` kendi üzerinde
bir `when:` koşulunu DESTEKLEMEZ (Ansible bunu `when:` iliştirilmiş olsa
bile her zaman koşulsuz çalıştırıp "does not support when conditional"
uyarısı basar; bu, `ubuntu-ssh-hardening`'in LIVE-AUDIT-FIX1 düzeltmesinde
canlı bir UI check-mode koşusunda GÖZLEMLENMİŞ bir davranıştır). Bu
role bu dersi BAŞTAN uygular: `post_verify.yml`'in tamamı, dosya hiç
dahil edilmediği için, check mode'da meta task'ın kendi when-desteksizliği
devreye GİREMEZ. `tests/run_offline_tests.sh`'deki
`post_verify_gate_check_mode_never_includes_reset_connection` testi bunu
gerçek `--check` bayrağıyla (taklit edilmiş `ansible_check_mode` DEĞİL)
davranışsal olarak kanıtlar.

## Idempotency

**DÜRÜST GERÇEK (BULGU1/AUDIT-FIX1):** UFW CLI komutları zararsız,
metin-tabanlı no-op'lar DEĞİLDİR -- gerçek sistem mutasyonu yaparlar:
`ufw default <policy> <yön>` kalıcı `/etc/default/ufw` dosyasını YAZAR ve
UFW aktifse firewall'ı **stop/start eder** (kısa bir kesinti/reload
penceresi anlamına gelebilir); `ufw logging <seviye>` `/etc/ufw/ufw.conf`'u
günceller; `ufw --force enable` **ZATEN aktifken bile** start_firewall
yolunu çalıştırır. `changed_when: false` bu mutasyonu/reload'u ENGELLEMEZ
-- yalnız Ansible'ın raporunu değiştirir. Bu yüzden idempotency ARTIK
`ufw`'nin kendi CLI-düzeyi davranışına DAYANDIRILMAZ.

Bunun yerine altı yazma komutunun HER BİRİ **kendi doğrulanmış `would_*`
kararına** bağlıdır (`apply_decisions.yml`, ön-okumalardan türetilir):

- `ufw allow <port>/tcp` -> yalnız `ufw_hardening_would_add_ssh_allow`
- `ufw default deny incoming` -> yalnız `ufw_hardening_would_set_incoming`
- `ufw default allow outgoing` -> yalnız `ufw_hardening_would_set_outgoing`
- `ufw default deny routed` -> yalnız `ufw_hardening_would_set_forward`
- `ufw logging low` -> yalnız `ufw_hardening_would_set_logging`
- `ufw --force enable` -> yalnız `ufw_hardening_would_enable`

Her komutun `when:` koşulu HEM `not ansible_check_mode` HEM kendi
`would_*` bayrağını taşır (`tests/assert_command_surface_and_order.py`
bunu yapısal olarak ölçer) -- ilgili alan ZATEN uyumluysa karşılık gelen
komut apply.yml'de **HİÇ ÇALIŞTIRILMAZ**, ilgisiz bir mutasyon/reload
tetiklenmez. `would_*` bayrakları üretilmeden ÖNCE apply_decisions.yml,
girdileri (rc + alan biçimi) FAIL-CLOSED doğrular -- bkz. "Fail-closed
ön-okuma sözleşmesi".

Altı bayrağın OR birleşimi `ufw_hardening_any_change`, `reset_connection`
+ `wait_for_connection`'ın (post_verify.yml) normal modda yalnız
GERÇEKTEN bir değişiklik uygulandıysa çalışmasını sağlar.

İkinci normal çalıştırmada (host zaten compliant), `tests/
check_apply_decisions.yml`'in kanıtladığı gibi altı "would_*" bayrağı da
`False`, `ufw_hardening_any_change` de `False` olur -- Job UI'ında altı
yazma task'ı `changed=false` DEĞİL, **`skipped`** raporlanır (hiçbiri
çalışmaz), enable-sonrası yeniden doğrulama yine de (salt-okunur olarak)
çalışıp COMPLIANT sonucunu doğrular, ve `reset_connection`/
`wait_for_connection` hiç TETİKLENMEZ.

## Fail-closed ön-okuma sözleşmesi

`apply.yml`'deki dört ön-okuma task'ı (`ufw show added`, `ufw status
verbose`, `/etc/default/ufw` slurp, `/etc/ufw/ufw.conf` slurp) hiçbiri
**`failed_when: false` TAŞIMAZ** -- rc≠0 veya dosya okunamazsa (slurp'un
varsayılan modül davranışı) bu task'lar burada BAŞARISIZ olur, play
ORADA durur, `apply_decisions.yml`'e (dolayısıyla HİÇBİR UFW yazma
komutuna) hiç ulaşılmaz.

Ayrıca `apply_decisions.yml`'in İLK task'ı, would_* bayrakları
üretilmeden ÖNCE bu okumaların ürettiği alanları AYRICA hard-gate ile
doğrular:

- `ufw show added` rc=0, `ufw status verbose` rc=0.
- `Status:` satırı **tam olarak bir kez** ve değeri `active`/`inactive`.
- `DEFAULT_INPUT_POLICY`/`DEFAULT_OUTPUT_POLICY`/`DEFAULT_FORWARD_POLICY`
  her biri **tam olarak bir kez** ve değeri `ACCEPT`/`DROP`/`REJECT`'ten
  biri.
- `LOGLEVEL` **tam olarak bir kez** ve değeri `off`/`low`/`medium`/
  `high`/`full`'dan biri.

Eksik, duplicate (aynı alanın 2+ kez görülmesi) veya tanınmayan bir değer
**ASLA sessizce "değişiklik gerekli" (`would_*=true`) sayılmaz** -- bu
durumda role burada HARD FAIL ile durur. Bu ikinci gate özellikle offline
test harness'i için gereklidir: `apply_decisions.yml` orada gerçek modül
çağrıları OLMADAN, sahte register değişkenleriyle doğrudan çalıştırılır;
o senaryoda rc/format doğrulaması TEK KAYNAK olarak bu gate'ten gelir.

## Lockout ve rollback sınırı (dürüst)

SSH allow kuralı her zaman UFW enable'dan ÖNCE uygulanır ve enable
sonrasında hem config-doğruluğu (mevcut bağlantı üzerinden) hem gerçek
bağlantı-kanıtı (`reset_connection` + `wait_for_connection`) ile
doğrulanır. **Buna rağmen bağlantı TAMAMEN kesilirse, uzaktan otomatik
rollback GARANTİ EDİLEMEZ** -- konsol/snapshot erişimi önerilir.

Ayrıntı:

- Bu role'ün **kendi** bir "geri al" mekanizması YOKTUR (ubuntu-ssh-
  hardening'in dosya-tabanlı backup/restore modelinin AKSİNE). DÜRÜST
  GERÇEK: UFW komutları `/etc/default/ufw`, `/etc/ufw/ufw.conf`, kural
  dosyalarını VE çalışan netfilter durumunu GERÇEKTEN DEĞİŞTİRİR (bkz.
  "Idempotency") -- bu değişikliklerin bir "önceki içerik" yedeği
  ALINMAZ; 1..6 arası adımlardan biri başarılı olup bir SONRAKİ
  başarısız olursa KISMİ UYGULAMA mümkündür ve otomatik geri alınmaz.
- Enable-sonrası yeniden doğrulama (`compliance_reverify.yml`) BAŞARISIZ
  olursa role burada otomatik ikinci bir düzeltme DENEMEZ (trusted-operator
  güvenlik modelinde otomatik remediation kapsam dışı; GUVENLIK.md bölüm 20.3:
  sistem otomatik rollback yapmaz) -- yalnız açık bir hata ile durur;
  UFW ZATEN etkinleştirilmiş olabilir, hedefin elle incelenmesi gerekir.
- `wait_for_connection`'ın sınırlı zaman aşımı (`GUVENLIK.md` bölüm 20.3'teki
  genel platform sözleşmesiyle TUTARLI) yalnızca "sonsuza dek asılı
  kalma" riskini önler -- bağlantının GERİ GELECEĞİNİ garanti ETMEZ.
- **Bağlantı tamamen kesilirse** (ör. beklenmeyen bir ağ/routing sorunu,
  host'un kendisinin erişilemez hale gelmesi), bu role'ün rollback
  mantığı SSH bağlantısının kendisine bağımlıdır; bağlantı yoksa hiçbir
  uzak eylem çalıştırılamaz. Bu durumda **konsol (ör. bulut sağlayıcı
  web konsolu) veya disk/VM snapshot erişimi** önerilir.
- Bu sınır, SSH allow kuralının enable'dan ÖNCE uygulanmasına VE
  reconnect doğrulamasına RAĞMEN geçerlidir -- iki güvence de riski
  AZALTIR, ORTADAN KALDIRMAZ (ör. host'ta bu playbook'tan bağımsız bir ağ
  yapılandırma sorunu, routing değişikliği veya beklenmeyen bir
  kesinti).

## Docker ve ham nftables UFW'yi bypass edebilir

`ubuntu-ufw-audit`'teki AYNI sınır burada da geçerlidir: UFW, Linux
netfilter/nftables üzerinde çalışan bir kural YÖNETİM katmanıdır, ağdaki
fiili trafiği değil KENDİ kural kümesini temsil eder.

- **Docker**, kendi iptables/nftables kurallarını doğrudan netfilter'a
  ekler; UFW'nin bilgisi/kontrolü dışında konteyner port yayınlamaları
  (`-p`) trafiği UFW kurallarını ATLAYARAK doğrudan yönlendirilebilir.
- **Ham (raw) `nftables`/`iptables` kuralları** (UFW'nin yönetmediği,
  elle veya başka bir araçla eklenmiş) UFW'nin kural kümesiyle etkileşime
  girip beklenmeyen izin/reddetme davranışına yol açabilir.
- **Sonuç:** bu role'ün "COMPLIANT (reverify)" sonucu ürettikten sonra
  bile, tam ağ güvenliği garantisi VERİLEMEZ -- yalnız UFW'nin kendi
  bildirdiği kural kümesi beklenen baseline ile uyumlu demektir.

## Audit → remediation → audit sunum akışı

Önerilen demo/sunum sırası:

1. **`ubuntu-ufw-audit`'i çalıştırın** (Check veya Normal, salt-okunurdur)
   -- hedefin MEVCUT durumunu (UFW muhtemelen inaktif veya eksik profil)
   dürüstçe raporlar.
2. **Bu project'i (`ubuntu-ufw-hardening`) önce Check mode ile
   önizleyin** -- "planlanan değişiklikler" (PLANNED/NO-CHANGE) altı
   mesajını gözden geçirin.
3. **Normal mode'u çalıştırın** -- SSH allow, üç default policy, logging,
   enable sırayla uygulanır; enable sonrası config-doğruluğu VE
   reconnect kanıtı otomatik doğrulanır.
4. **`ubuntu-ufw-audit`'i TEKRAR çalıştırın** -- IPv6 HARİÇ (bkz.
   "Yönetilen/yönetilmeyen alanlar") tüm kontrollerin artık COMPLIANT
   raporladığını bağımsız, salt-okunur bir kaynaktan doğrulayın. Bu
   ikinci audit koşusu bu dilimin KAPSAMI DIŞINDADIR (ayrıca doğrulanmadı)
   -- yalnızca önerilen sunum akışıdır.

## DORAnsible'a nasıl kaydedilir

Audit'teki aynı adımlar (bkz. `../ubuntu-ufw-audit/README.md` →
"DORAnsible'a nasıl kaydedilir"), bu project'e uyarlanmış:

1. **Project dizinini erişilebilir bir köke taşıyın**:
   `app-data/projects/ubuntu-ufw-hardening` altına kopyalayın (varsayılan
   allowlist'e uyar) veya `ANSIBLEOPS_PROJECT_ROOT_ALLOWLIST`'e bu
   dizinin mutlak yolunu ekleyip backend'i yeniden başlatın.
2. **Project ekle**: kopyaladığınız/allowlist'e eklediğiniz kökü
   DORAnsible'da kaydedin.
3. **Inventory ekle**: `inventory/hosts.yml`'i kaydedin, kendi host'unuzu
   tanımlayın, private key kullanıyorsanız allowlist'e uygun bir yola
   koyup `ansible_ssh_private_key_file` ile referans verin. Hedefte sshd
   standart olmayan bir portta dinliyorsa `ansible_port`'u burada
   tanımlayın -- role, SSH allow kuralını TAM OLARAK bu port için ekler.
   Hedef kullanıcının **önceden** NOPASSWD sudo yetkisine sahip
   olduğundan emin olun.
4. **Önce check mode ile önizleyin.** Altı "planlanan değişiklik"
   mesajını gözden geçirin; hiçbir kalıcı değişiklik yapılmaz.
5. **Normal mode'u çalıştırın.** Manuel bir onay adımı veya elle
   düzenlenecek bir dosya YOKTUR -- role, hedefe erişimi ve yetkiyi
   kendisi doğrular; başarısızsa hiçbir UFW yazma komutu çalıştırmadan
   durur.
6. **Sonucu audit ile doğrulayın** (bkz. "Audit → remediation → audit
   sunum akışı").

## Offline testler

`tests/run_offline_tests.sh`, gerçek bir SSH hostu, bağlantı veya sudo
gerektirmeden şunları doğrular:

- Projedeki tüm `.yml`/`.yaml` dosyalarının YAML olarak parse edildiği.
- `ubuntu-ufw-hardening.yml`'in `ansible-playbook --syntax-check`'ten
  geçtiği.
- `profile_lock_check.yml`: varsayılan değerlerle geçtiği; desteklenen
  sürüm listesi, üç default policy, logging seviyesi veya reconnect
  timeout/sleep override edildiğinde fail-closed durduğu.
- `os_check.yml`: desteklenen (22.04, 24.04) ve desteklenmeyen (20.04,
  Debian) sürümlerin doğru işlendiği.
- `firewalld_gate_assert.yml`: karar matrisinin TAMAMI (active, rc/stdout
  çelişkisi, tanınmayan stdout, rc=3/inactive güvenli, rc=4/inactive
  güvenli, rc=4/unknown güvenli).
- `ssh_port_gate_assert.yml`: port tanımsızken 22 varsayıldığı; geçerli
  özel port kabul edildiği; 0 ve 65536'nın (Ansible'ın `ansible_port`
  bağlantı anahtarı için `int()` dönüşümünü GEÇTİĞİ, role'ün KENDİ
  1..65535 aralık kontrolüne ulaştığı ve orada reddedildiği); sayısal
  olmayan bir `ansible_port`'un role'e hiç ulaşmadan Ansible'ın kendi
  bağlantı katmanında reddedildiği (DÜRÜST modelleme -- role'ün mesajı
  değil, Ansible'ın kendi ret mesajı aranır).
- `system_checks_gate` (davranışsal, R1-V3H4-SIMPLIFY ile aynı desen):
  UID=0 iken sonraki adımlara ulaşıldığı, UID≠0 iken hiçbir sonraki
  adıma ulaşılmadığı.
- `apply_decisions.yml` (ön-okuma karar mantığı, `would_*`): tam uyumlu
  bir hedefte tüm altı bayrağın `False` olduğu (ikinci normal çalıştırma
  idempotency kanıtı); tamamen yeni bir hedefte tüm altı bayrağın `True`
  olduğu; nonstandard port + eşleşen/eşleşmeyen `ufw show added`
  kaydının doğru değerlendirildiği.
- `compliance_reverify.yml` (enable-sonrası yeniden doğrulama): tam
  uyumlu geçer; inactive, logging kapalı, SSH allow kuralı yok, yanlış
  input/forward policy AYRI AYRI fail-closed durur; nonstandard port
  eşleşen/eşleşmeyen kural senaryoları; birden fazla uygunsuzlukta TÜM
  kontrollerin (ilk hatada durmadan) çalıştığı.
- `post_verify.yml` include-gate'i (davranışsal, gerçek `--check`
  bayrağıyla): check mode'da `reset_connection`'ın (ve "does not support
  when conditional" uyarısının) çıktıda HİÇ görünmediği; normal modda
  include'un gerçekten tetiklenip yerel bağlantı üzerinden başarıyla
  tamamlandığı.
- `assert_command_surface_and_order.py` (yapısal kilit): tüm
  `ansible.builtin.command` task'larının argv'sinin sabit bir allowlist'e
  EXACT eşleştiği, `shell`/`raw` hiç kullanılmadığı, SSH allow task'ının
  apply.yml'de enable task'ından ÖNCE geldiği; checker'ın KENDİ allowlist
  mantığının hermetik regresyon kanıtı (`--self-test`: izin verilen argv
  geçer, arbitrary unit/eksik-fazla argüman/farklı alt komut/bilinmeyen
  Jinja değişkeni/`shell`/`raw`/argv'siz `command` reddedilir).
- `assert_shared_gate_imports.py` (yapısal kilit): `uid_gate_assert.yml`,
  `firewalld_gate_assert.yml`, `ssh_port_gate_assert.yml` -- üçü de hem
  `system_checks.yml` hem kendi offline harness'i tarafından GERÇEKTEN
  AYNI dosyaya (`os.path.realpath` eşitliği) import edildiği; assert
  mantığı iki yerde AYRI AYRI YAZILMADIĞI.
- (Varsa) `ansible-lint`; bu geliştirme ortamında kurulu değildir, bu
  yüzden test script'i onu **dürüstçe atlar** (fail etmez, "SKIP" olarak
  raporlar).

Çalıştırmak için:

```
./tests/run_offline_tests.sh
```

## Sınırlar (dürüst)

- **Bu dilimde gerçek bir hedefte hiç çalıştırılmadı.**
  `system_checks.yml` (sudo/root, ufw binary, firewalld) ve
  `apply.yml`/`post_verify.yml`'nin gerçek write → enable → reverify →
  reconnect → wait_for_connection zincirinin MODÜL ÇAĞRILARI (`command`
  ile become) gerçek bir Ubuntu hedef VE become gerektirir; bu geliştirme
  ortamında passwordless sudo yoktur, bu round bunu KAPSAMAZ. Bunun
  yerine SAF karar mantığı (`apply_decisions.yml`, `compliance_
  reverify.yml`, üç `*_gate_assert.yml`) gerçek modüllerin ÜRETMİŞ
  OLACAĞI sonuçları simüle eden sahte register/fixture değişkenleriyle
  offline doğrulanmıştır -- bu, gerçek modül çağrılarının kendisini
  (`command`'ın gerçekten doğru `ufw` alt komutunu çalıştırması,
  `wait_for_connection`'ın gerçek bir SSH bağlantısını beklemesi)
  KANITLAMAZ; bunlar iyi bilinen, standart Ansible primitifleridir.
- İdempotency, her yazma komutunun KENDİ doğrulanmış `would_*` kararına
  DAYANDIRILIR (bkz. "Idempotency"/"Fail-closed ön-okuma sözleşmesi") ve
  bu karar mantığı offline kanıtlandı (yukarıya bakın), ama gerçek bir
  hedefte tekrar çalıştırılarak UÇTAN UCA AYRICA kanıtlanmadı.
- IPv6 (`IPV6=` alanı) bu role tarafından YÖNETİLMEZ (bkz.
  "Yönetilen/yönetilmeyen alanlar") -- bu role'den sonra çalıştırılan
  `ubuntu-ufw-audit`, hedefte IPv6 zaten `yes` değilse bu tek alan için
  hâlâ NON-COMPLIANT raporlayabilir.
- Bu role'ün **kendi** bir rollback mekanizması YOKTUR (bkz. "Lockout ve
  rollback sınırı") -- ubuntu-ssh-hardening'in dosya-backup/restore
  modelinin AKSİNE.
- Yalnızca SSH portu için TEK bir allow kuralı yönetir; başka servisler
  (HTTP/HTTPS, özel uygulama portları) için kural eklemez.
- `ufw route`/gerçek IP forwarding etkinleştirme bu dilimin kapsamı
  dışındadır -- yalnız `DEFAULT_FORWARD_POLICY` DEĞERİ yazılır.
- Docker/ham nftables gibi UFW'yi bypass edebilecek trafik yolları bu
  role'ün (ve audit'in) görüş alanı dışındadır (bkz. "Docker ve ham
  nftables UFW'yi bypass edebilir").
- Gerçek IP, kullanıcı adı, private key veya başka bir secret içermez.
