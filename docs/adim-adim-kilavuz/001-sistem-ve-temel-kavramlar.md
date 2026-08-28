# 001 — Sistem Resmi ve Temel Kavramlar

## 1. DORAnsible ne yapar?

DORAnsible, Ansible project ve inventory dosyalarını web arayüzünde seçmenizi,
hedef bağlantısını test etmenizi, çalıştırma planını inceleyip onaylamanızı ve
sonucu geçmiş kaydı olarak görmenizi sağlar.

Uygulama bugün yapay zekâ çağrısı yapmaz. AI ile playbook önerme, açıklama veya
sonuç analizi gelecekte eklenebilecek genişleme alanlarıdır.

## 2. Parçalar nasıl konuşur?

```text
Siz ve tarayıcı
      |
      | http://127.0.0.1:5173
      v
React frontend
      |
      | HTTP/JSON
      v
FastAPI backend + SQLite + worker
      |
      | ansible-runner ve SSH
      v
Yönetilen Ubuntu hedef(ler)
```

Frontend SSH anahtarını kullanmaz. SSH bağlantısı controller üzerindeki backend
ve worker tarafından kurulur.

## 3. Üç makine rolü

| Rol | Açıklama |
|---|---|
| Controller | DORAnsible backend'i, worker, Ansible ve dosyaların bulunduğu Linux makine |
| Tarayıcı | Web arayüzünü açtığınız cihaz; controller ile aynı olabilir |
| Yönetilen host | Ansible'ın SSH ile bağlanıp task çalıştırdığı hedef makine |

Ana kurulum modelinde controller ve tarayıcı aynı Ubuntu bilgisayardır. Hedef
ayrı bir Ubuntu sanal makinesidir.

## 4. Ansible kavramları

| Kavram | Açıklama |
|---|---|
| Project | Playbook, role ve ilgili dosyaların bulunduğu klasör |
| Inventory | Hangi hedeflere, hangi adres/kullanıcı/key ile bağlanılacağını tanımlar |
| Playbook | Yapılacak işleri sıralayan YAML dosyası |
| Play | Bir host grubuna uygulanacak task kümesi |
| Task | Paket kur, dosyayı kontrol et, servisi başlat gibi tek işlem |
| Role | Yeniden kullanılabilir task, template ve varsayılanlar paketi |
| Module | Ansible'ın hedefte belirli işi yapan bileşeni |
| Job | DORAnsible'da onaylanmış bir playbook çalıştırma kaydı |

## 5. Check ve Normal arasındaki fark

**Check**, runner'a `--check` ekler. Ansible modülleri mümkün olduğunda yapacağı
değişikliği simüle eder. Buna rağmen her playbook ve modül check kipini eksiksiz
desteklemek zorunda değildir; check “hiç yan etki olmaz” garantisi değildir.

**Normal**, `--check` eklemez. Playbook'un normal Ansible davranışı uygulanır ve
hedef gerçekten değişebilir. Otomatik rollback garantisi yoktur.

## 6. Uygulamanın bilinçli sınırları

- Tek güvenilir operatör içindir; login ve RBAC yoktur.
- Tek controller ve tek worker kullanır.
- Scheduler, alarm ve sürekli monitoring yoktur.
- Become parolası, Vault ve uygulama-yönetimli credential deposu yoktur.
- Private key controller dosya sisteminde tutulur.
- Ham Ansible görüntü çıktısı sanitize edilmiş veya secret-free değildir.

Bu sınırlar kurulum hatası değildir; teslim edilen MVP'nin ürün sözleşmesidir.
