# 015 — Güvenlik Sınırları ve Sözlük

## 1. Kurulumdan sonra unutulmaması gerekenler

- DORAnsible tek güvenilir operatörlü yerel MVP'dir.
- Authentication/RBAC olmadığı için doğrudan internete açmayın.
- Yalnız güvendiğiniz playbook ve inventory'leri kaydedin.
- Private key içeriğini Git'e, inventory'ye veya frontend environment'a yazmayın.
- Normal kipte hedef değişikliği ürünün beklenen davranışıdır.
- Check kipini sandbox sanmayın.
- Timeout veya bağlantı kaybında hedefte kısmi değişiklik kalabilir.
- Ham Ansible görüntü çıktısını paylaşmadan önce elle inceleyin.
- Firewall/SSH remediation öncesinde konsol erişimi ve geri dönüş planı hazırlayın.

## 2. Git'e hiçbir zaman eklenmemesi gerekenler

```text
backend/.env
frontend/.env.local
app-data/
*.pem, *.key, id_rsa, id_ed25519
gerçek inventory secret'ları
VM diskleri ve gerçek Job artifact'ları
```

Repository `.gitignore` bunların çoğunu dışlar; yine de commit öncesi
`git status` operatör tarafından incelenmelidir.

## 3. Üretim öncesi gereken ek tasarım

Uygulamayı başka kullanıcıların erişimine açmadan önce en az:

- authentication ve authorization/RBAC,
- TLS veya güvenilir reverse proxy,
- firewall ve ağ segmentasyonu,
- ayrı non-root service identity,
- işletim sistemi servis tanımları,
- yedekleme ve geri yükleme testi,
- log rotasyonu ve gözlemleme,
- kurumsal secret/credential yönetimi,
- kullanıcı/actor isolation incelemesi

tasarlanmalıdır. Geliştirme sunucularını (`uvicorn --reload`, `vite`) üretim
servisi gibi kullanmayın.

## 4. Kısa sözlük

| Terim | Açıklama |
|---|---|
| API | Frontend ile backend arasındaki HTTP/JSON arayüzü |
| Artifact | Bir Job'a ait kontrollü sonuç/çalışma dosyaları |
| Controller | DORAnsible ve Ansible'ın çalıştığı Linux makine |
| CORS | Tarayıcı frontend'inin hangi API origin'ine erişebileceğini sınırlar |
| Cursor | Job listesinin sonraki/önceki sayfa konumunu belirleyen opaque değer |
| Environment | `.env` ile verilen çalışma ayarları |
| Frozen workspace | Onaylanan içeriğin çalıştırma için dondurulmuş kopyası |
| Host key | SSH sunucusunun kimliğini doğrulayan anahtar |
| Idempotency | Aynı playbook tekrarında gereksiz değişiklik oluşmaması özelliği |
| Inventory | Host/grup ve SSH bağlantı bilgisi tanımı |
| Job | Onaylanmış playbook çalıştırmasının kalıcı kaydı |
| Lease/heartbeat | Tek worker'ın running Job sahipliğini koruyan mekanizma |
| Migration | Veritabanı şemasını yeni sürüme taşıyan Alembic adımı |
| Normal mode | `--check` olmadan gerçek Ansible davranışı |
| Playbook | YAML biçiminde otomasyon adımları |
| Project | Playbook/role dosyalarının controller klasörü |
| Recap | Host başına ok/changed/failed/unreachable sayaçları |
| Role | Yeniden kullanılabilir Ansible task/template paketi |
| SSH | Controller'ın hedefe güvenli uzaktan bağlantı protokolü |
| Token | Hazırlanmış planı tek kullanımlık onaya bağlayan kısa ömürlü değer |
| Worker | Pending Job'ı sahiplenip runner'ı başlatan arka plan bileşeni |

## 5. Kanonik ileri belgeler

- [Kullanıcı Rehberi](../KULLANICI_REHBERI.md)
- [Kurulum ve Environment](../gelistirme-ortami.md)
- [Geliştirici Rehberi](../GELISTIRICI_REHBERI.md)
- [Mimari](../../MIMARI.md)
- [Güvenlik](../../GUVENLIK.md)
