# 002 — Ubuntu ve Gereksinim Kurulumu

## 1. Önerilen işletim sistemi

Bu rehberin ana kurulum yolu **Ubuntu 24.04 LTS** içindir. Temiz bir
bilgisayara Ubuntu kuracaksanız önce kişisel dosyalarınızı yedekleyin. Disk
silme/partition işlemleri geri döndürülemez olabilir.

Resmî kurulum anlatımı:

- <https://documentation.ubuntu.com/desktop/en/24.04/tutorial/install-ubuntu-desktop/>

Windows doğrudan Ansible controller olarak desteklenmez. Sunum/laboratuvar için
Ubuntu sanal makinesi kullanılabilir; üretim için WSL önerilmez.

## 2. Sistem bilgilerini kontrol edin

Controller üzerinde sırayla çalıştırın:

```bash
cat /etc/os-release
uname -m
```

İlk komutta `Ubuntu 24.04` benzeri bir değer, ikinci komutta çoğu bilgisayarda
`x86_64` görmeniz beklenir.

## 3. Sistem paketlerini kurun

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git openssh-client curl ca-certificates
```

Kontrol edin:

```bash
python3 --version
git --version
ssh -V
curl --version
```

Python en az `3.11` olmalıdır. Ubuntu 24.04 normalde Python 3.12 sağlar. Daha
düşük sürüm görüyorsanız burada durun; işletim sistemi veya Python kurulumu
güncellenmeden backend'i kurmayın.

## 4. Node.js ve npm kurun

Projenin asgari gereksinimi Node.js 20+'dır; ancak Node 20 artık kullanım ömrü
sonuna geldiği için yeni kurulumda güncel **LTS** sürüm kullanılmalıdır. Bu
belge hazırlanırken Node 24 LTS'tir. Güncel durumu resmî sayfadan kontrol edin:

- <https://nodejs.org/en/download>
- <https://nodejs.org/en/about/previous-releases>

Node.js sayfasında Linux + `nvm` yöntemini seçin ve sayfanın verdiği güncel nvm
kurulum komutunu uygulayın. 26 Ağustos 2026
tarihinde resmî indirme sayfasında gösterilen komut aşağıdadır; sayfadaki sürüm
daha yeniyse resmî sayfadaki komutu esas alın:

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

Beklenen: `node --version` çıktısı `v22` veya `v24` gibi desteklenen bir LTS
sürümü göstermelidir. Yeni kurulumda EOL olan Node 20'yi seçmeyin.

> Kurumsal ağda `curl`, `pip` veya `npm` sertifika hatası verirse TLS
> doğrulamasını kapatmayın. Kurum CA sertifikası sistem ve araç trust store'una
> eklenmelidir.

Resmî başvuru kaynakları:

- Ubuntu kurulumu: <https://documentation.ubuntu.com/desktop/en/24.04/tutorial/install-ubuntu-desktop/>
- Git Linux kurulumu: <https://git-scm.com/install/linux>
- Ansible control node gereksinimleri: <https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html>
- Windows control node sınırı: <https://docs.ansible.com/projects/ansible/latest/os_guide/intro_windows.html#using-windows-as-the-control-node>

## 5. Kaynak ve disk kontrolü

```bash
free -h
df -h .
```

Geliştirme kurulumu için pratik alt sınır olarak en az 4 GB RAM ve bağımlılıklar,
runtime çıktıları ve yedekler için en az 10 GB boş alan ayırın. Yönetilecek VM'ler
aynı bilgisayarda çalışacaksa daha fazla RAM gerekir.

## 6. Bölüm sonu kontrolü

- [ ] Ubuntu controller açılıyor.
- [ ] `python3 --version` en az 3.11.
- [ ] `node --version` desteklenen LTS sürüm.
- [ ] `npm --version` çıktı veriyor.
- [ ] `git --version` çıktı veriyor.
- [ ] `ssh -V` çıktı veriyor.
