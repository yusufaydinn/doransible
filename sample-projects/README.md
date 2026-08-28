# sample-projects

Uygulamanın demo ve test amaçlı kullandığı Ansible project'leri bu dizinde tutulur.

## ubuntu-ssh-audit

Salt-okunur SSH audit örnek project'i. Detaylar için
[`ubuntu-ssh-audit/README.md`](ubuntu-ssh-audit/README.md).

## ubuntu-ssh-hardening

`ubuntu-ssh-audit`'in tespit ettiği SSH baseline'ını uygulayan (gerçek
değişiklik yapan) remediation project'i. Yalnız DORAnsible'a ait tek bir
`sshd_config.d` drop-in dosyasını yönetir; otomatik SSH/passwordless-sudo
önkoşulu, backup/rollback ve check/normal mode farkları için
[`ubuntu-ssh-hardening/README.md`](ubuntu-ssh-hardening/README.md)
dosyasına bakın. Role kontrollü Ubuntu laboratuvarında audit → ilk Normal
uygulama → idempotent ikinci uygulama → son audit zinciriyle de doğrulanmıştır;
bu kanıt bütün dağıtımlar için güvenlik garantisi değildir.

## ubuntu-ufw-audit

Salt-okunur UFW (Uncomplicated Firewall) audit örnek project'i. UFW'nin
gerçekten active olup olmadığını (yalnız `ufw status verbose`'a dayanarak,
`ufw.service`'in systemd durumuna DEĞİL), firewalld çakışmasını (rc +
stdout birlikte, fail-closed karar matrisiyle), `/etc/default/ufw`
IPv6/default policy profilini ve inventory'deki gerçek `ansible_port` için
açık bir SSH allow kuralını değerlendirir. Kural ekleme/kaldırma yapmaz;
bu iddia kalıcı bir yapısal testle (`tests/assert_read_only_surface.py`)
korunur. Firewalld karar matrisi, desteklenen `ufw status verbose`
biçimleri ve Docker/ham nftables gibi UFW'yi bypass edebilecek trafik
yolları için [`ubuntu-ufw-audit/README.md`](ubuntu-ufw-audit/README.md)
dosyasına bakın.

## ubuntu-ufw-hardening

`ubuntu-ufw-audit`'in denetlediği UFW baseline'ını uygulayan (gerçek
değişiklik yapan) remediation project'i. Inventory'deki SSH portu için
UFW enable edilmeden ÖNCE bir TCP allow kuralı ekler, üç default policy'yi
(`DROP`/`ACCEPT`/`DROP`) ve logging'i (`low`) ayarlar, UFW'yi etkinleştirir;
enable sonrası hem mevcut bağlantı üzerinden config-doğruluğu hem
`reset_connection` + `wait_for_connection` ile GERÇEKTEN YENİ bir SSH
bağlantısının kurulabildiğini kanıtlar. Firewalld aktifse veya durumu
güvenle belirlenemiyorsa hiçbir değişiklik yapmadan fail-closed durur;
IPv6 alanı bu dilimde BİLEREK yönetilmez. Lockout/rollback sınırı,
check/normal mode farkı ve audit→remediation→audit sunum akışı için
[`ubuntu-ufw-hardening/README.md`](ubuntu-ufw-hardening/README.md)
dosyasına bakın. Role kontrollü Ubuntu laboratuvarında Check → ilk Normal →
idempotent ikinci Normal → bağımsız UFW audit zinciriyle doğrulanmıştır;
Docker/ham nftables ve bağlantı kaybı sınırları devam eder.

## Kurallar

- Bu dizindeki hiçbir dosya gerçek secret içermez (GUVENLIK.md bölüm 12).
- Inventory örneklerinde gerçek host adresi veya kullanıcı adı bulunmaz.
- Örnek project'ler `--syntax-check` kontrolünden geçmelidir.
