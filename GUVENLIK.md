# DORAnsible Güvenlik Modeli

Bu belge teslim edilen MVP'nin güncel güvenlik sınırlarını ve açıkça geleceğe
ayrılmış entegrasyon koşullarını birlikte açıklar. Kaynak yorumlarında kullanılan
`ADR-xxx` izleme kimlikleri için [mimari karar dizinine](docs/KARAR_DIZINI.md)
bakın. Çalışan bileşenlerin genel görünümü [MIMARI.md](MIMARI.md) içindedir.

## 1. Tehdit modeli

DORAnsible yüksek etkili bir yönetim aracıdır. Güvenilir operatörün seçtiği
playbook, hedeflerde SSH ile komut çalıştırabilir; Normal mode dosya, paket,
servis, firewall, kullanıcı ve sistem yapılandırmasını değiştirebilir. Become
kullanan playbook'lar hedefte daha yüksek yetkiyle çalışabilir.

MVP'nin kullanıcı modeli:

- tek güvenilir ve profesyonel Ansible operatörü,
- tek Linux controller,
- operatörün kendi güvenilir project/inventory içeriği,
- internete doğrudan açılmayan yerel web uygulaması.

Ürün, güvenilmeyen kullanıcıların keyfî playbook yüklediği malicious veya
multi-tenant bir sandbox değildir. Playbook'un hedefte yaptığı, normal Ansible
CLI kullanımında olduğu gibi operatörün sorumluluğundadır. Platformun güvenlik
sorumluluğu kendi eklediği risklerdir: seçilen içerik ile çalışan içeriğin
ayrılması, token tekrar kullanımı, path kaçışı, secret sızıntısı, kontrolsüz
subprocess, yetkisiz ikinci launch ve yanlış sonuç iddiası.

Authentication/RBAC, şifreli credential deposu, AI provider, scheduler ve
multi-tenant izolasyon bugün yoktur. Bu varsayımlardan biri değişirse mevcut
tehdit modeli yeniden değerlendirilmelidir.

## 2. Temel güvenlik sınırı

Mevcut execution zinciri:

```text
Güvenilir operatör seçimi
  → salt-okunur plan önizlemesi
  → frozen workspace + manifest + fingerprint
  → kısa ömürlü tek kullanımlık token
  → mode'a özgü açık insan onayı
  → atomik Job rezervasyonu
  → ayrı runner child process
  → doğrulanmış ve bounded sonuç
```

Preview hedefe bağlanmaz. Launch token, actor, project, inventory, playbook,
mode, host-key policy, fingerprint ve manifest bağlarını tekrar doğrular.
Launch route'u runner çalıştırmaz; yalnız pending Job üretir. Worker varsayılan
kapalıdır ve kontrollü kurulumda açıkça etkinleştirilir.

Gelecekte AI eklenirse AI yalnız öneri/taslak kaynağı olabilir. İnsan incelemesi
ve yukarıdaki mevcut onay zinciri atlanamaz; AI kendi çıktısını otomatik
çalıştıramaz.

## 3. Secret ve hassas veri yönetimi

Bugünkü MVP'nin desteklediği credential biçimi, controller dosya sistemindeki
SSH private-key dosyasına yapılan referanstır. Key:

- ayrı `ANSIBLEOPS_SSH_KEY_ROOT_ALLOWLIST` altında olmalıdır,
- symlink olmayan düzenli dosya olarak yeniden doğrulanır,
- API response, Job özeti, ping geçmişi ve sanitize event listesinde gösterilmez,
- Git repository'sine veya `.env` dosyasına yazılmaz,
- hedefe gönderilen inventory snapshot'ında yalnız çalışma için gerekli path
  olarak bulunur.

MVP become parolası, Vault parolası, SSH parolası, LLM API key'i veya uygulama
yönetimli şifreli credential kasası sunmaz. Böyle bir alan eklenmesi yeni veri
modeli, erişim politikası ve tehdit değerlendirmesi gerektirir.

Public hata cevapları controller path'i, DSN, token, private-key içeriği,
subprocess exception'ı veya ham inventory hostvar'ı taşımaz. `.env.example`
yalnız örnek değerler içerir.

`ansible_output` bu bölümdeki sanitize event yüzeyinin istisnasıdır: bounded
display metnidir fakat redakte veya secret-free değildir; ayrıntı 21. bölümdedir.

## 4. Path güvenliği

Project, standalone inventory ve SSH key için birbirinden bağımsız allowlist
kökleri vardır. Genel kontrol sırası:

```text
ham kullanıcı path'i
  → absolute/canonical biçim doğrulaması
  → izinli root altında kalma kontrolü
  → symlink/kaçış kontrolü
  → varlık ve dosya türü kontrolü
  → domain binding
```

Allowlist kontrolü varlık sorgusundan önce yapılır. Böylece allowlist dışındaki
mevcut ve olmayan path'ler farklı cevaplarla controller dosya sistemi oracle'ı
oluşturmaz.

Engellenen örnekler:

```text
../../etc/passwd
/root/.ssh/id_rsa
project dışına çözülen symlink
izinli kökle yalnız string prefix'i ortak olan kardeş dizin
canonical olmayan Job/workspace kimliği
```

Runner workspace, result reader ve cleanup hassas işlemleri descriptor-relative,
`O_NOFOLLOW` ve tür/dev/ino yeniden doğrulaması kullanır. Cleanup yalnız yönetilen
kökün doğrudan Job çocuğunu, bounded taramadan sonra siler; serbest `rmtree`
kullanılmaz.

## 5. Command injection ve subprocess sınırı

Kullanıcı değeri shell komut metnine birleştirilmez. Ansible inventory, ping ve
runner komutları argv listesiyle, `shell=False` davranışı altında çalışır.

```text
Yanlış:  os.system(f"ansible-playbook {playbook} -i {inventory}")
Doğru:   doğrulanmış executable + sabit argv parçaları + doğrulanmış path
```

Genel amaçlı terminal veya serbest argv/extra-vars/tags/limit yüzeyi yoktur.
Runner, API prosesinin environment'ını doğrudan miras almaz; dar bir allowlist
ile yeni environment kurulur ve ayrı process/session'da çalışır. Timeout veya
çıktı sınırında yalnız yönetilen süreç grubu sonlandırılır.

## 6. Gelecekteki AI entegrasyonu

Bu bölüm mevcut özellik değil, gelecekte AI eklenecekse korunacak sınırdır.
Bugünkü repository'de provider çağrısı, prompt endpoint'i veya AI remediation
yoktur; `backend/app/services/ai` boş bir domain yer tutucusudur.

Gelecekte:

- project dosyaları talimat değil veri kabul edilir,
- secret retrieval AI aracına açılmaz,
- yalnız gerekli bounded içerik modele gönderilir,
- model çıktısı kesin şema ve deterministik araçlarla doğrulanır,
- modelin “validation başarılı” iddiasına güvenilmez,
- AI çıktısı ayrı staging alanında tutulur,
- AI doğrudan launch endpoint'ini veya Normal execution'ı tetiklemez.

## 7. Execution approval

Bugünkü public plan en az şunları gösterir:

- project ve inventory,
- playbook relative path'i,
- Check veya Normal mode,
- host sayısı ve bounded host listesi,
- SSH host-key policy,
- become/limit/tags için platformun sabit değerleri,
- execution'ın henüz başlamadığı bilgisi.

Hazırlama frozen workspace ve kısa ömürlü token üretir. Token tek kullanımlık,
actor-bound ve Job'a özgüdür. Yanlış mode veya binding token'ı tüketmeden generic
invalid cevabı üretir; doğru değerle kontrollü retry mümkündür. Bir token ikinci
Job üretemez.

Diff, syntax-check, ansible-lint ve risk skoru bugün yoktur ve Normal mode'un
authorization önkoşulu değildir. Bunlar ileride operatöre ek görünürlük sağlayan
ayrı bir doğrulama katmanı olarak eklenebilir.

## 8. Credential erişim ilkesi

Controller service identity, yalnız yapılandırılmış key allowlist'ini ve gerekli
runtime köklerini okuyabilmelidir. Backend root olarak çalıştırılmamalıdır.
Private key içeriği UI'ya verilmez; kullanıcı yalnız inventory'deki referansı
yönetir.

İkinci operatör, RBAC, credential oluşturma/değiştirme UI'ı, password/Vault veya
haricî secret manager eklenmesi bu tek-operatör politikasını genişletir ve yeni
authorization incelemesi gerektirir.

## 9. Log redaction ve sonuç yüzeyleri

Redaction yardımcıları şunları maskeler:

- private-key blokları,
- Bearer/token ve password biçimleri,
- Vault görünümlü içerik,
- bilinen hassas path/değer biçimleri.

Secret'ı önce loglayıp sonra yalnız regex'e güvenmek doğru değildir. Production
log mesajları mümkün olduğunca sabit tutulur; exception, DSN, workspace path'i
ve subprocess ham hatası log metnine eklenmez.

Inventory hostvar görünümü secret görünümlü anahtarları `***` yapar. Ping
geçmişi host mesajını taşımaz. Yapılandırılmış playbook event yüzeyi yalnız
allowlist alanlarını içerir; `event_data.res`, task args, environment ve argv
public response'a çıkmaz.

`ansible_output` redaction uygulanmayan, açık uyarılı ayrı display yüzeyidir;
log veya güvenli audit kaydı sayılmaz.

## 10. Network

Varsayılan geliştirme kurulumu:

- backend `127.0.0.1:8000`,
- frontend `127.0.0.1:5173`,
- CORS yalnız yapılandırılmış localhost origin'leri,
- frontend'den controller/host SSH bağlantısı yoktur.

Login/RBAC olmayan MVP `0.0.0.0` ile doğrudan internete açılmamalıdır. Başka
cihaz erişimi gerekiyorsa TLS, güvenilir reverse proxy, firewall ve kimlik modeli
birlikte tasarlanmalıdır; yalnız bind adresini değiştirmek yeterli değildir.

Uygulamada kullanıcı tanımlı AI provider/base URL veya genel HTTP fetch yüzeyi
yoktur. Böyle bir özellik eklenirse SSRF, metadata adresleri, DNS rebinding ve
timeout/response limitleri ayrıca ele alınmalıdır.

## 11. Dosya izinleri

Uygulama `app-data` ve yönettiği alt dizinleri POSIX üzerinde 0700 oluşturur.
Job result ve runner config gibi yönetilen dosyalar 0600'dür. SSH private key
dosyası operatör tarafından 0600 tutulmalıdır.

```text
app-data/             0700
app-data/jobs/        0700
app-data/secrets/     0700
execution plan/run    0700
result/config files   0600
private key           0600
```

Uygulama ayrı, yetkisiz bir OS service identity ile çalıştırılmalıdır. Hedefte
become gerekiyorsa dar kapsamlı Ansible/sudo politikası önceden hazırlanır;
uygulama become parolası göndermez.

## 12. Dependency ve supply chain

- Python doğrulanmış sürümleri `backend/requirements.lock.txt` içindedir.
- Frontend tam dependency ağacı `frontend/package-lock.json` ile kilitlidir.
- Kurulumda backend lock dosyası ve frontend için `npm ci` kullanılır.
- Backend/frontend dependency audit ayrı script ile fail-closed değerlendirilir.
- Yeni package/collection kullanıcı onayı olmadan otomatik indirilmez.
- Sample project ve test fixture'larında gerçek secret bulunmaz.
- TLS doğrulaması `--trusted-host` veya benzeri kalıcı atlamayla kapatılmaz.

## 13. Güvenli varsayılanlar

- Playbook worker varsayılan kapalıdır.
- UI varsayılan olarak Check mode seçer; operatör Normal'i açıkça seçer.
- Check mode Normal için zorunlu önkoşul veya yan etkisizlik kanıtı değildir.
- SSH host-key policy varsayılan `strict`tir.
- `accept_new` yalnız bilinçli TOFU seçimidir; doğrulamayı kapatan seçenek yoktur.
- SSH agent, proxy command/jump, control socket ve parola auth miras alınmaz.
- Arbitrary extra vars, limit, tags, skip-tags, forks ve timeout request alanı yoktur.
- Otomatik reboot, rollback, remediation, commit veya push yoktur.
- AI özelliği ve otomatik AI execution yoktur.

## 14. Incident yaklaşımı

Şüpheli durumda:

1. Yeni launch başlatmayın ve worker'ı kontrollü kapatın.
2. Aktif Job'ın ve hedefteki kısmi değişiklik ihtimalinin durumunu değerlendirin.
3. `app-data/database`, Job result'ları ve ilgili project/inventory snapshot'ını
   değişmeden koruyun.
4. Sızıntı şüphesi varsa SSH key'i iptal edip yenileyin.
5. Controller loglarını ve hedef sistem loglarını bağımsız inceleyin.
6. Hedeflerde bağımsız audit çalıştırın.
7. Kök neden anlaşılmadan Job'ı başarılı veya güvenli kabul etmeyin.

## 15. Güvenlik testleri

Mevcut otomatik testler özellikle şu sınıfları kapsar:

- project/inventory/key path traversal ve symlink escape,
- allowlist dışı varlık oracle'ı,
- secret response/log redaction,
- request şemasında yasak alanlar,
- token TTL, tek kullanım, actor/mode/binding uyuşmazlığı,
- duplicate active Job ve atomik state geçişleri,
- transaction rollback ve retry,
- frozen workspace/manifest bütünlüğü,
- runner environment mirası, timeout ve süreç ağacı,
- artifact/result path, boyut, şema ve symlink sınırları,
- output/event alanı ve UI'da düz metin render,
- migration ve DB CHECK/FK/index invariant'ları.

Test suite production hostuna SSH ile bağlanmaz. Runner gate testleri yalnız
localhost ve `ansible_connection=local` kullanır. AI/SSRF/RBAC testleri, ilgili
özellikler eklenmeden “mevcut güvence” sayılmaz.

## 16. Ping process ve artifact sınırı

Ping Ansible/SSH süreci yeni POSIX session/process group içinde başlar. Timeout
veya stdout/stderr sınırında ağaç önce `SIGTERM`, grace sonrasında gerekirse
`SIGKILL` alır. Uygulamanın kendi process group'una sinyal gönderilmez.

Komut yüzeyi sabittir: `ansible all -i <snapshot> -m ping`. İstemci modül,
shell, host listesi, forks veya timeout göndermez. SSH `-F /dev/null`, kapalı
agent/proxy/control seçenekleri, public-key auth ve `strict`/`accept-new`
known_hosts politikasıyla çalışır.

Ping Job result'ı yalnız `app-data/jobs/<canonical-uuid>/result.json` altında,
0700/0600 izinlerle ve atomik yazılır. Aynı inventory için ikinci aktif ping'i
DB index engeller. Bu servis yalnız confirm çağrısıyla execution başlatır.

## 17. Ping confirm sınırı

Confirm şu sırayı uygular:

1. Preview token'ını ve sabit actor'ı doğrular.
2. Token'ı tek kullanımlık claim eder.
3. Dondurulmuş inventory snapshot'ını kullanır; özgün inventory'yi yeniden açmaz.
4. Private-key referansını execution anında allowlist'e karşı yeniden doğrular.
5. Ping Job'ını ve bounded sonucu yazar.

Token, host bilgisi veya hata ayrıntısı URL/query'ye konmaz. Başarısız claim
token oracle'ı üretmeyen generic hata döndürür. Kalıcı ping geçmişi yalnız
sanitize edilmiş durum ve zaman bilgisini sunar.

## 18. Ping arayüzü sınırı

UI preview ile gerçek ping'i ayrı eylem olarak gösterir. Preview hedefe
bağlanmadığını açıkça söyler; kullanıcı host sayısını/adlarını görüp onaylar.
Token component belleğinde tutulur, URL/cache/storage'a yazılmaz ve seçim
değişince temizlenir.

Sonuç ekranı backend'in public host durumunu gösterir; stderr, private-key path'i,
hostvar veya raw Ansible çıktısı için ikinci bir istemci kanalı oluşturmaz.
Kalıcı geçmiş manuel ölçümlerdir, sürekli erişilebilirlik garantisi değildir.

## 19. Gelecekte onboarding ve monitoring

Bu bölüm uygulanmış özellik değildir. Bugünkü üründe host onboarding servisi,
credential bootstrap, scheduler, periyodik health check, alarm veya filo ekranı
yoktur.

İleride eklenirse:

- host kimliği güvenilir ayrı kanal/parmak iziyle doğrulanmalı,
- key bootstrap mevcut güvenilir kanal olmadan otomatik yapılmamalı,
- scheduler duplicate execution ve kaynak tüketimini atomik sınırlandırmalı,
- “host kapalı” ile “ölçüm alınamadı” ayrılmalı,
- stale veri zaman damgasıyla gösterilmeli,
- monitoring ayrı ve daha geniş bir SSH/subprocess motoru açmamalıdır.

## 20. Normal-mode execution sınırı

Normal mode trusted-operator MVP'de Ansible CLI'ına eşdeğer bir yetenektir.
Profesyonel operatörün kendi güvenilir playbook'unu çalıştırması, henüz olmayan
lint/risk hattına bağlanmaz. Platform playbook'un hedef etkisini sandbox'lamaz.

Korunan platform invariant'ları mode'dan bağımsızdır:

- frozen workspace ve manifest,
- plan/token/actor/mode/binding bağı,
- tek kullanımlık claim ve tek aktif playbook Job,
- allowlist environment ve SSH credential sınırı,
- timeout, bounded artifact ve güvenli state geçişleri,
- sanitize edilmiş yapılandırılmış public sonuç.

Check ile Normal arasındaki kontrollü runner farkı, Check için eklenen exact
`--cmdline=--check` argv elemanıdır. Check task'ın `check_mode: false` gibi kendi
davranışlarını ortadan kaldırmaz. Normal mode hedefte değişiklik yapabilir;
timeout, bağlantı kaybı veya task failure sonrasında kısmi değişiklik kalabilir.
Otomatik rollback garantisi yoktur.

Yeni credential türü, birden fazla güven sınırına sahip kullanıcı, internete
doğrudan açılma, concurrency artışı veya output yüzeyinin genişletilmesi bu
kararın yeniden değerlendirilmesini gerektirir.

## 21. Kullanıcıya gösterilen Ansible display çıktısı

Public Job result iki farklı veri yüzeyi taşır:

1. **Yapılandırılmış sonuç:** recap ve allowlist event alanları şema ile
   doğrulanır ve sanitize edilir.
2. **`ansible_output`:** runner event'lerinin yalnız top-level `stdout`
   alanlarından event sırasıyla üretilen görüntü metnidir.

`ansible_output`:

- yalnız terminal PLAYBOOK Job result'ında bulunur,
- UTF-8 sınırında en fazla 128 KiB'dir,
- kesildiyse `ansible_output_truncated=true` taşır,
- raw process stderr/stdout veya nested `event_data.res` değildir,
- actor-bound Job detail/result authorization'ından geçer,
- HTTP `Cache-Control: no-store` cevabında sunulur,
- UI'da varsayılan kapalı `<details>` içinde ve düz metin olarak render edilir.

Bu çıktı **sanitize, redakte veya secret-free değildir**. Kapalı `<details>`
güvenlik sınırı değil yalnız sunum tercihidir. Güvenilir playbook/role/plugin
çıktıya secret yazarsa kullanıcı bunu görebilir; `no_log` doğru kullanılmalıdır.
Output indirme/paylaşma/export, retention veya boyut artışı, multi-user erişim,
internet açılımı ya da “secret-free” garantisi talebi yeni tehdit değerlendirmesi
gerektirir.
