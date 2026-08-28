# DORAnsible dokümantasyon indeksi

Bu dizin, teslim edilen ürün için kanonik dokümantasyon giriş noktasıdır.
Belgeler okuyucu türüne göre ayrılmıştır.

## Kullanıcı ve işletici

| Belge | İçerik |
|---|---|
| [Adım Adım Kurulum ve Kullanım Kılavuzu](adim-adim-kilavuz/README.md) | Kurulum, yapılandırma, hedef hazırlığı, UI kullanımı, yedekleme ve sorun giderme için numaralı uygulama bölümleri |
| [Tek Dosyalık Birleşik Kılavuz](adim-adim-kilavuz/DORANSIBLE_TAM_KURULUM_VE_KULLANIM_KILAVUZU.md) | Numaralı kurulum ve kullanım rehberlerinin tek dosyada birleştirilmiş sürümü |
| [Kullanıcı Rehberi](KULLANICI_REHBERI.md) | Ekranlar, project/inventory kaydı, ping, Check/Normal çalışma, Job sonuçları, örnek audit/remediation akışı ve hata çözümü |
| [Kurulum ve Yapılandırma](gelistirme-ortami.md) | Controller kurulumu, ortam değişkenleri, servisleri başlatma, yedekleme ve doğrulama |
| [Örnek Project'ler](../sample-projects/README.md) | Ubuntu SSH ve UFW audit/remediation içerikleri ve güvenlik sınırları |

## Geliştirici

| Belge | İçerik |
|---|---|
| [Geliştirici Rehberi](GELISTIRICI_REHBERI.md) | Kod yapısı, katmanlar, veri modeli, API, execution akışı, test yaklaşımı ve katkı adımları |
| [Mimari](../MIMARI.md) | Ayrıntılı bileşenler, invariants ve tarihsel mimari genişlemeler |
| [Güvenlik](../GUVENLIK.md) | Path, secret, subprocess, runner, artifact ve hardening güvenlik kararları |

## Belge bakım kuralı

Davranış değiştiren bir özellik teslim edilirken:

1. Kurulum veya temel kullanıcı akışı değişiyorsa `adim-adim-kilavuz/`
   içindeki ilgili numaralı bölüm ve tek dosyalık birleşik kılavuz birlikte
   güncellenir.
2. Kullanıcının gördüğü akış değişiyorsa `KULLANICI_REHBERI.md` güncellenir.
3. API, veri modeli, environment veya execution invariant'ı değişiyorsa
   `GELISTIRICI_REHBERI.md` ve gerekiyorsa `MIMARI.md` güncellenir.
4. Güvenlik sınırı değişiyorsa `GUVENLIK.md` ve ilgili sample-project README'si
   güncellenir.
5. Yeni environment değişkeni `backend/.env.example` ile
   `gelistirme-ortami.md` içinde aynı teslimde belgelenir.
6. Eski bir davranış değiştiğinde aynı teslimde artık geçerli olmayan ifadeler
   güncellenir veya kaldırılır.
