# 010 — Playbook Planlama ve Çalıştırma

## 1. Project detayını açın

Üst menüden **Project'ler** seçin ve `Ubuntu SSH Audit Demo` project'ini açın.
Uygulama project içindeki güvenli `.yml`/`.yaml` playbook dosyalarını keşfeder.

## 2. Form seçimleri

Çalıştırma bölümünde:

1. Kip olarak ilk denemede **Check** seçin.
2. Inventory olarak `Ubuntu Demo Host` seçin.
3. Playbook olarak `ubuntu-ssh-audit.yml` seçin.
4. **Planı Oluştur** düğmesine basın.

## 3. Planı okuyun

Şunların tamamını kontrol edin:

- Project adı
- Inventory adı
- Playbook relative path'i
- Kip (`check` veya `normal`)
- Hedef host sayısı ve listesi
- SSH bağlantı ve host key politikası
- Üretilme zamanı

Plan ekranı hedefe bağlanmaz. Yanlış seçim varsa onaya geçmeden seçimi
değiştirin ve yeni plan üretin.

## 4. Planı dondurun

**Onaya Hazırla** düğmesine basın. Backend project ve inventory'nin dondurulmuş
çalışma kopyasını üretir. Bundan sonra ekrandaki içerik ile çalıştırılacak içerik
birbirine bağlanır.

Hazırlanan onay kısa ömürlüdür. Süresi dolarsa bu hata değildir; yeni plan
hazırlayın.

## 5. Açık onay ve launch

Onay metnini okuyun, kutuyu işaretleyin ve **Onayla ve Çalıştır** düğmesine
basın. `201` cevabının ürün anlamı “pending Job kalıcı olarak oluşturuldu”dur;
runner'ın o anda bittiği anlamına gelmez.

Uygulama Job detayına yönlendirir:

```text
pending → running → successful
                  └→ failed
```

Worker kapalıysa Job `pending` kalır. `.env` içindeki
`ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED=true` değerini ve backend'in bu ayardan sonra
yeniden başlatıldığını kontrol edin.

## 6. Check kipini doğru yorumlayın

Check kipinde runner argümanlarına `--check` eklenir. Bu güvenlik sandbox'ı veya
yan etkisizlik garantisi değildir. Örneğin bir task açıkça `check_mode: false`
taşıyabilir; Ansible modülünün check desteği sınırlı olabilir.

## 7. Normal kip

Normal kip gerçek Ansible çalıştırmasıdır:

- dosya yazabilir,
- paket kurup kaldırabilir,
- servis restart/reload edebilir,
- SSH veya firewall ayarını değiştirebilir,
- bağlantıyı kesebilir,
- kısmi değişiklik bırakabilir.

Yalnız playbook'u, hedef kapsamını ve geri dönüş planını bildiğinizde Normal
seçin. Normal plan ayrı kip olarak hazırlanır; Check için hazırlanmış token
Normal Job'a yükseltilemez.

## 8. Audit örneğinin sonucu

SSH audit playbook'u salt-okunur kontroller yapar fakat hedef yapılandırma
baseline'a uymuyorsa `assert` task'ları başarısız olabilir. Job'ın `failed`
olması bu durumda runner'ın bozuk olduğu değil, playbook'un uygunsuzluk
raporladığı anlamına gelebilir. Sonuç ekranındaki hata sınıfı ve task'ları
birlikte okuyun.
