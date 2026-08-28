# 009 — Ping ve Bağlantı Testi

DORAnsible ping'i ICMP `ping` komutu değildir. Ansible, hedefe SSH ile bağlanır,
Python tabanlı `ansible.builtin.ping` modülünü çalıştırır ve doğrulanmış sonuç
üretir.

## 1. Ping önizlemesi

1. Üst menüden **Inventory'ler** bölümüne gidin.
2. Hazırladığınız inventory'yi açın.
3. **Erişilebilirlik testi** bölümüne ilerleyin.
4. Önizleme oluşturun.
5. Hedef host adı ve sayısını kontrol edin.

Önizleme hiçbir SSH bağlantısı kurmaz. Yalnız çalıştırılacak kapsamı gösterir.

## 2. Tek kullanımlık onay

Önizleme doğruysa onaylayıp ping'i başlatın. Onay kısa ömürlü ve tek
kullanımlıktır. Sayfayı çok bekletir, inventory'yi değiştirir veya aynı onayı
yeniden kullanırsanız yeni önizleme oluşturmanız gerekir.

## 3. Sonuçların anlamı

| UI sonucu | Anlamı |
|---|---|
| Erişilebilir | SSH ve Ansible ping başarılı |
| Erişilemiyor | SSH, ağ, port, host key veya kullanıcı aşamasında sorun olabilir |
| Başarısız | Bağlantı kurulmuş olsa da modül çalışması hata verdi |
| Sonuç alınamadı | Beklenen host için güvenilir terminal sonucu üretilemedi |

`unreachable` yalnız “makine kapalı” demek değildir. Yanlış IP, firewall, yanlış
SSH kullanıcısı, key izni veya host key uyuşmazlığı da aynı sınıfa düşebilir.

## 4. Ping geçmişi

Inventory detayındaki geçmiş, kullanıcı tarafından başlatılmış ölçümleri en
yeniden eskiye gösterir. Bu alan sürekli monitoring, alarm veya uptime yüzdesi
değildir.

## 5. Başarısızsa controller'da elle test

Önce kılavuzun 007. bölümündeki tam SSH komutunu tekrar çalıştırın. Sonra backend
sanal ortamında Ansible'ı doğrudan deneyebilirsiniz:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
ansible all \
  -i ../app-data/projects/ubuntu-ssh-audit/inventory/hosts.yml \
  -m ansible.builtin.ping
```

Bu doğrudan komut DORAnsible'ın tüm kontrollü SSH argümanlarını birebir taklit
etmez; yalnız inventory/Ansible teşhisine yardımcı olur.

## 6. Bölüm sonu kontrolü

- [ ] Önizleme doğru host'u gösteriyor.
- [ ] Onay sonrası gerçek ping tamamlanıyor.
- [ ] Host `Erişilebilir` görünüyor.
- [ ] Ping geçmişinde yeni zaman damgalı kayıt var.
