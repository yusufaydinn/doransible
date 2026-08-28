# 007 — Hedef Makine ve SSH Hazırlığı

Bu bölüm DORAnsible'ın bağlanacağı ayrı Ubuntu test makinesini hazırlar. İlk
denemede snapshot alınabilen bir sanal makine kullanın.

## 1. Hedefte yapılacaklar

Hedef Ubuntu'nun terminalinde:

```bash
sudo apt update
sudo apt install -y openssh-server python3 sudo
sudo systemctl enable --now ssh
sudo systemctl status ssh --no-pager
```

Hedefin IP adresini bulun:

```bash
ip -br address
```

`lo` dışındaki satırda `192.168...`, `10...` veya laboratuvar ağınıza ait adresi
not edin. Bu belgede bu değer `<HEDEF_IP>` olarak anılır.

## 2. Otomasyon kullanıcısı oluşturun

Hedefte:

```bash
sudo adduser automation
sudo usermod -aG sudo automation
```

İlk komut sizden kullanıcı parolası ister. Bu parola yalnız ilk SSH public key
kurulumunda kullanılabilir; DORAnsible parola saklamaz ve göndermez.

### Laboratuvar için passwordless sudo

Audit/hardening örnekleri interaktif become parolası kullanamaz. Disposable
laboratuvar hedefinde:

```bash
sudo visudo -f /etc/sudoers.d/doransible-automation
```

Şu satırı yazın:

```text
automation ALL=(ALL) NOPASSWD: ALL
```

Kaydedip çıktıktan sonra:

```bash
sudo chmod 440 /etc/sudoers.d/doransible-automation
sudo visudo -cf /etc/sudoers.d/doransible-automation
```

Bu geniş yetki yalnız izole demo/laboratuvar içindir. Gerçek ortamda kurumun
yetki politikası ve daha dar sudo tasarımı kullanılmalıdır.

## 3. Controller'da SSH anahtarı üretin

Controller'da yeni terminal açın:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p app-data/secrets
chmod 700 app-data/secrets
ssh-keygen -t ed25519 -f app-data/secrets/doransible_demo -C doransible-demo
chmod 600 app-data/secrets/doransible_demo
chmod 644 app-data/secrets/doransible_demo.pub
```

Demo anahtarı için parola sorulduğunda boş bırakmak otomatik çalışmayı
kolaylaştırır fakat disk güvenliğinin önemini artırır. Gerçek ortamda anahtar
yönetimi kurum politikasına göre tasarlanmalıdır.

Private key içeriğini ekrana, inventory'ye, Git'e veya mesaja yapıştırmayın.

## 4. Public key'i hedefe kurun

Controller'da `<HEDEF_IP>` yerine gerçek adresi yazın:

```bash
ssh-copy-id -i app-data/secrets/doransible_demo.pub automation@<HEDEF_IP>
```

İlk bağlantıda host key sorusu gelebilir; parmak izini hedef konsolu veya güvenilir
ayrı kanalla doğrulamadan `yes` yazmayın. Ardından `automation` parolasını girin.

## 5. DORAnsible known_hosts dosyasını hazırlayın

DORAnsible normal kullanıcının `~/.ssh/known_hosts` dosyasını kullanmaz; kendi
kontrollü `app-data/ssh/known_hosts` dosyasını kullanır.

Önce hedefin parmak izini güvenilir ayrı kanaldan öğrenin. Sonra controller'da:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p app-data/ssh
chmod 700 app-data/ssh
ssh-keyscan -p 22 <HEDEF_IP> > /tmp/doransible-host-key
ssh-keygen -lf /tmp/doransible-host-key
```

Gösterilen fingerprint güvenilir bilgiyle eşleşiyorsa:

```bash
cp /tmp/doransible-host-key app-data/ssh/known_hosts
chmod 600 app-data/ssh/known_hosts
```

`ssh-keyscan` tek başına kimlik doğrulamaz; karşılaştırma yapılmadan kopyalamak
MITM riskini çözmez.

## 6. DORAnsible ile aynı SSH koşullarını test edin

Controller'da, repository kökünde:

```bash
ssh -F /dev/null \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile="$PWD/app-data/ssh/known_hosts" \
  -o StrictHostKeyChecking=yes \
  -i "$PWD/app-data/secrets/doransible_demo" \
  automation@<HEDEF_IP> \
  'python3 --version && sudo -n true && echo DORANSIBLE_SSH_OK'
```

Son satır `DORANSIBLE_SSH_OK` olmalıdır. Parola sorulmamalıdır.

## 7. Bölüm sonu kontrolü

- [ ] Hedef IP biliniyor.
- [ ] SSH servisi çalışıyor.
- [ ] `automation` kullanıcısı public key ile bağlanabiliyor.
- [ ] Demo gerekiyorsa `sudo -n true` başarılı.
- [ ] Private key `app-data/secrets` altında ve modu 600.
- [ ] DORAnsible known_hosts dosyası hazır ve doğrulanmış.
