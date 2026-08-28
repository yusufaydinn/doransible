# 011 — Sonuçları ve Geçmişi Okuma

## 1. Job özet alanları

Job detayında genellikle şunlar görünür:

- Job kimliği
- Durum ve çalışma kipi
- Project ve Inventory
- Playbook yolu
- Oluşturulma, başlama ve bitiş zamanı
- Return code ve güvenli hata sınıfı

`successful`, yalnız doğrulanmış terminal sonuç ve başarılı recap üretildiğini
gösterir. Bir işin kurum politikanıza uygun olduğunu tek başına kanıtlamaz.

## 2. Host recap sayaçları

| Sayaç | Anlamı |
|---|---|
| `ok` | Başarılı veya zaten uygun task'lar |
| `changed` | Ansible'ın değişiklik uyguladığını raporladığı task'lar |
| `failures` | Başarısız task sayısı |
| `unreachable` | Host'a erişilemeyen çalışma sayısı |
| `skipped` | Koşul nedeniyle atlanan task'lar |
| `rescued` | Rescue bloğunda ele alınan hatalar |
| `ignored` | Playbook tarafından devam edilmesine izin verilen hatalar |

Bir audit playbook'u bütün kontrolleri göstermek için bazı hataları geçici olarak
ignore edip final task'ta genel sonucu başarısız yapabilir. Bu yüzden yalnız tek
sayıya değil task listesine bakın.

## 3. Event listesi

Event satırlarında normalize edilmiş sınırlı bilgiler bulunur:

- event türü,
- host,
- task adı,
- changed/failed durumu.

UI ham `event_data.res`, task argümanları veya secret alanlarını yapılandırılmış
event tablosuna taşımaz. Hata metni eksik görünüyorsa bu bilinçli sanitize
sınırından kaynaklanabilir.

## 4. Ham Ansible görüntü çıktısı

Sonuç ekranındaki kapalı detay alanında bounded Ansible görüntü çıktısı olabilir.
Bu alan:

- yalnız top-level event `stdout` parçalarından oluşturulur,
- en fazla 128 KiB tutulur,
- kırpılmış olabilir,
- sanitize/redact edilmiş değildir,
- secret-free garantisi taşımaz.

Bu çıktıyı e-posta, mesaj, ticket veya ekran görüntüsüyle paylaşmadan önce
private key yolu, kullanıcı, IP, hostname, token, parola veya hassas playbook
verisi açısından elle inceleyin.

## 5. Hata sınıflarını yorumlama

| Kod/sınıf | Güvenli yorum |
|---|---|
| `playbook_failed` | Güvenilir sonuç bazı task'ların failed/unreachable olduğunu bildirdi; tek başına kök neden değildir |
| `runner_failed` | Genel/legacy toplayıcıdır; altyapı veya playbook kaynaklı olabilir |
| `runner_no_hosts` | Terminal sonuç hiçbir host işlendiğini göstermedi |
| `runner_timeout` | Süre sınırı aşıldı; hedefte kısmi değişiklik kalmış olabilir |
| `workspace_integrity_failed` | Dondurulmuş çalışma içeriği beklenen bütünlüğü taşımadı |
| `result_*` / `runner_output_invalid` | Artifact güvenilir sonuç belgesine dönüştürülemedi |

UI bir hata kodundan kesin SSH/ağ/playbook kök nedeni uydurmaz.

## 6. Çalıştırmalar listesi

Üst menüden **Çalıştırmalar** sayfası:

- kayıtları yeniden eskiye sıralar,
- durum ve kip filtresi uygular,
- her sayfada en fazla 25 kayıt gösterir,
- **Sonraki/Önceki** ile keyset cursor sayfalama yapar.

Filtre değişince ilk sayfaya dönülmesi normaldir. Backend ve aynı `app-data`
kullanıldığı sürece terminal Job geçmişi yeniden başlatma sonrasında kalır.
