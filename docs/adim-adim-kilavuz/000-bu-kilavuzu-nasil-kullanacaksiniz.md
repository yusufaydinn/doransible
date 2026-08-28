# 000 — Kılavuzun Kapsamı ve Uygulama Akışı

Bu kılavuz DORAnsible'ın kurulması, yapılandırılması, başlatılması ve web
arayüzü üzerinden kullanılması için uygulanacak adımları içerir. İlk kurulumda
bölümleri numara sırasıyla tamamlayın; mevcut bir kurulumda doğrudan ihtiyaç
duyduğunuz bölüme geçebilirsiniz.

## 1. Desteklenen kurulum modeli

Ana kurulum yolu aşağıdaki ortamı esas alır:

- Ubuntu 24.04 LTS controller
- DORAnsible backend, worker ve frontend süreçleri aynı controller üzerinde
- Uygulamanın controller üzerindeki tarayıcıdan kullanılması
- SSH ile erişilebilen ayrı bir Ubuntu test/hedef makinesi
- Tek güvenilir operatör

Kurulumdan önce şunların hazır olması gerekir:

- controller üzerinde `sudo` yetkisi,
- GitHub repository erişimi,
- hedef makinenin IP adresi veya DNS adı,
- hedefte kullanılacak SSH kullanıcı hesabı,
- gerekirse hedefin konsoluna veya yönetim paneline erişim.

## 2. İzlenecek sıra

İlk kurulumdaki ana akış şöyledir:

1. Controller gereksinimlerini kurun.
2. Repository'yi indirin ve kullanılacak sürümü doğrulayın.
3. Backend Python ortamını ve frontend bağımlılıklarını kurun.
4. Uygulama veri dizinini ve environment ayarlarını hazırlayın.
5. Backend/worker ile frontend'i başlatın ve sağlık kontrollerini yapın.
6. Hedef makineyi SSH erişimine hazırlayın.
7. Web arayüzünde Project ve Inventory oluşturun.
8. Ping ile erişimi doğrulayın.
9. Playbook planını inceleyip Check veya Normal kipte çalıştırın.
10. Job durumunu, recap'i ve bounded Ansible çıktısını inceleyin.

## 3. Komutların çalıştırılacağı konum

Her bölüm komutun çalıştırılacağı konumu açıkça belirtir. Kılavuzda kullanılan
başlıca konumlar şunlardır:

| İfade | Konum |
|---|---|
| Repository kökü | `backend/`, `frontend/`, `docs/` ve `scripts/` klasörlerini içeren `DORAnsible/` dizini |
| Backend dizini | `DORAnsible/backend/` |
| Frontend dizini | `DORAnsible/frontend/` |
| Controller | DORAnsible'ın kurulu olduğu Ubuntu makine |
| Hedef | Ansible'ın SSH ile yöneteceği makine |

Bir komut “hedef makinede” olarak işaretlenmedikçe controller üzerinde
çalıştırılır. Repository köküne geçmek için kılavuzdaki örnek yol yerine kendi
kurulum yolunuzu kullanın.

## 4. Değiştirilecek örnek değerler

Komutlardaki aşağıdaki değerleri kendi ortamınıza göre değiştirin:

```text
<HEDEF_IP>       → hedef makinenin IP adresi veya DNS adı
<KULLANICI>      → hedefte kullanılacak SSH hesabı
<REPOSITORY_URL> → yetkili GitHub clone adresi
```

Köşeli yer tutucular gerçek komuta aynen yazılmaz. Gerçek parola, token, private
key içeriği veya başka bir secret belgeye, komut geçmişine ya da repository'ye
eklenmez.

## 5. Güvenli uygulama sınırı

DORAnsible gerçek hedeflere SSH ile bağlanıp Ansible playbook'u çalıştırır.
**Normal** kip dosya, paket, servis, SSH ve firewall ayarlarını gerçekten
değiştirebilir. Check kip de mutlak yan etkisizlik garantisi vermez.

İlk kurulum ve çalıştırmayı snapshot alınabilen veya yeniden oluşturulabilen
bir Ubuntu test makinesi üzerinde doğrulayın. Aşağıdaki durumlardan biri varsa
Normal kipte devam etmeyin:

- seçilen Project, Inventory veya playbook beklediğiniz kayıt değilse,
- execution plan içeriği beklediğiniz hedefi göstermiyorsa,
- SSH kullanıcı/key eşleşmesi doğrulanmadıysa,
- Check sonucu açıklanamayan failure veya unreachable içeriyorsa,
- hedefte geri dönüş yöntemi belirlenmediyse.

## 6. Bölüm sonu kontrolleri

Her bölümün sonundaki kontrol listesini tamamladıktan sonra sonraki bölüme
geçin. Bir adım başarısız olursa hata metniyle birlikte
[Sorun Giderme](014-sorun-giderme.md) bölümündeki ilgili başlığı uygulayın.
