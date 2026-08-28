# DORAnsible Adım Adım Kurulum ve Kullanım Kılavuzu

Bu klasör, DORAnsible'ın Ubuntu controller üzerinde kurulması,
yapılandırılması, başlatılması ve web arayüzü üzerinden kullanılması için
operasyonel adımları içerir. Her dosya belirli bir kurulum veya kullanım
aşamasını kapsar. İlk kurulumda dosyaları numara sırasıyla uygulayın.

## Hangi dosyadan başlamalıyım?

| Durumunuz | Başlangıç dosyası |
|---|---|
| Kuruluma başlıyorum | [000 — Kapsam ve uygulama akışı](000-bu-kilavuzu-nasil-kullanacaksiniz.md) |
| Ubuntu hazır, gereksinimler kurulu değil | [002 — Gereksinimleri kurma](002-ubuntu-ve-gereksinim-kurulumu.md) |
| Repository bilgisayarda hazır | [004 — Backend kurulumu](004-backend-kurulumu.md) |
| Uygulama açılıyor, hedef bağlamak istiyorum | [007 — Hedef ve SSH hazırlığı](007-hedef-makine-ve-ssh-hazirligi.md) |
| Project/inventory hazır, playbook çalıştıracağım | [009 — Ping](009-ping-ve-baglanti-testi.md) ve [010 — Playbook çalıştırma](010-playbook-calistirma.md) |
| Bir hata görüyorum | [014 — Sorun giderme](014-sorun-giderme.md) |

## Okuma sırası

1. [000 — Kılavuzun kapsamı ve uygulama akışı](000-bu-kilavuzu-nasil-kullanacaksiniz.md)
2. [001 — Sistem resmi ve temel kavramlar](001-sistem-ve-temel-kavramlar.md)
3. [002 — Ubuntu ve gereksinim kurulumu](002-ubuntu-ve-gereksinim-kurulumu.md)
4. [003 — Projeyi GitHub'dan indirme](003-projeyi-indirme.md)
5. [004 — Backend kurulumu](004-backend-kurulumu.md)
6. [005 — Frontend kurulumu](005-frontend-kurulumu.md)
7. [006 — İlk açılış ve sağlık kontrolü](006-ilk-acilis-ve-saglik-kontrolu.md)
8. [007 — Hedef makine ve SSH hazırlığı](007-hedef-makine-ve-ssh-hazirligi.md)
9. [008 — İlk Project ve Inventory](008-ilk-project-ve-inventory.md)
10. [009 — Ping ve bağlantı testi](009-ping-ve-baglanti-testi.md)
11. [010 — Playbook planlama ve çalıştırma](010-playbook-calistirma.md)
12. [011 — Sonuçları ve geçmişi okuma](011-sonuclar-ve-gecmis.md)
13. [012 — Baştan sona örnek senaryo](012-ornek-uc-uca-senaryo.md)
14. [013 — Kapatma, yedekleme ve güncelleme](013-kapatma-yedekleme-guncelleme.md)
15. [014 — Sorun giderme](014-sorun-giderme.md)
16. [015 — Güvenlik sınırları ve sözlük](015-guvenlik-sinirlari-ve-sozluk.md)

Tek dosya okumayı tercih ediyorsanız aynı içerik
[DORAnsible Tam Kurulum ve Kullanım Kılavuzu](DORANSIBLE_TAM_KURULUM_VE_KULLANIM_KILAVUZU.md)
dosyasında birleştirilmiştir.

## Bu kılavuzun desteklediği ana yol

- Controller işletim sistemi: **Ubuntu 24.04 LTS**
- Kullanım biçimi: aynı bilgisayarda backend ve frontend
- Tarayıcı: Firefox, Chromium veya güncel eşdeğeri
- Hedef: SSH erişimi olan Ubuntu 22.04/24.04 test makinesi
- Ürün modeli: tek güvenilir operatör

Ubuntu 22.04 controller teknik olarak kullanılabilir; ancak Python 3.11+
ayrıca kurulmalıdır. Ek Python repository yapılandırmasını ana kurulum yolundan
çıkarmak için Ubuntu 24.04 LTS esas alınmıştır. Windows doğrudan Ansible
controller değildir; ayrı bir Ubuntu makine veya VM kullanılmalıdır.

## İleri başvuru belgeleri

- [Kanonik kullanıcı rehberi](../KULLANICI_REHBERI.md)
- [Kurulum ve environment ayarları](../gelistirme-ortami.md)
- [Geliştirici rehberi](../GELISTIRICI_REHBERI.md)
- [Mimari](../../MIMARI.md)
- [Güvenlik modeli](../../GUVENLIK.md)
