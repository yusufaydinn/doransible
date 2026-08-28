# 008 — İlk Project ve Inventory

Bu bölüm örnek SSH audit project'ini runtime alanına kopyalar ve hedef bilgilerini
girer. Repository'deki orijinal sample dosyasını değiştirmeyin.

## 1. Örnek project'i kopyalayın

Controller'da:

```bash
cd "$HOME/Projeler/DORAnsible"
mkdir -p app-data/projects
cp -a sample-projects/ubuntu-ssh-audit app-data/projects/
```

Tam yolları öğrenin:

```bash
realpath app-data/projects/ubuntu-ssh-audit
realpath app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml
realpath app-data/secrets/doransible_demo
```

Bu üç çıktıyı not edin.

## 2. Runtime inventory dosyasını düzenleyin

```bash
nano app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml
```

`hosts:` altındaki demo host'u gerçek bilgilerle şu biçime getirin:

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

IP ve private key yolunu kendi değerinizle değiştirin. Girintiler boşlukla
yapılmalıdır; Tab kullanmayın. Private key'in **içeriğini değil yalnız tam dosya
yolunu** yazın.

## 3. Inventory'yi komut satırında doğrulayın

Backend sanal ortamını etkinleştirin:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
ansible-inventory \
  -i ../app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml \
  --list
```

JSON çıktısı görmelisiniz. YAML hatası varsa UI'ya geçmeden dosyayı düzeltin.

## 4. UI'da Project kaydı oluşturun

1. `http://127.0.0.1:5173` adresini açın.
2. Üst menüden **Project'ler** seçin.
3. **Yeni project ekle** bağlantısına basın.
4. Ad: `Ubuntu SSH Audit Demo`.
5. Path alanına `realpath app-data/projects/ubuntu-ssh-audit` çıktısını yazın
   veya **Gözat…** ile klasörü seçin.
6. İsterseniz açıklama yazın.
7. **Project'i kaydet** düğmesine basın.

Project kaydı dosyayı kopyalamaz veya değiştirmez; controller'daki mevcut
klasöre güvenli referans oluşturur.

## 5. UI'da bağlı Inventory kaydı oluşturun

1. Üst menüden **Inventory'ler** seçin.
2. **Yeni inventory kaydet** bağlantısına basın.
3. Ad: `Ubuntu Demo Host`.
4. Biçim: `YAML`.
5. Project: az önce eklediğiniz `Ubuntu SSH Audit Demo`.
6. Path: project içindeki `inventory/hosts.yml` dosyasının tam yolu.
7. Kaydedin.

Inventory detayında `ubuntu-demo` host'u görünmelidir. Secret görünümlü
değişkenlerin maskeli gösterilmesi normaldir.

## 6. Path reddedilirse

Varsayılan project allowlist yalnız `app-data/projects` altıdır. Kopyalama
adımını atladıysanız `sample-projects/...` yolu 403 ile reddedilebilir. En kolay
çözüm sample project'i yukarıdaki gibi runtime alanına kopyalamaktır.

## 7. Bölüm sonu kontrolü

- [ ] Runtime project `app-data/projects` altında.
- [ ] Inventory gerçek IP, kullanıcı ve mutlak key yolu içeriyor.
- [ ] `ansible-inventory --list` başarılı.
- [ ] UI'da Project kaydı görünüyor.
- [ ] UI'da bağlı Inventory ve host görünüyor.
