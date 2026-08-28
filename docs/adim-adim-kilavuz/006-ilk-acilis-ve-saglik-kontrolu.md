# 006 — İlk Açılış ve Sağlık Kontrolü

## 1. Her normal kullanımda açılış sırası

Bilgisayarı yeniden başlattıktan sonra bağımlılıkları tekrar kurmazsınız. Yalnız
iki uygulama sürecini başlatırsınız.

Terminal 1 — backend:

```bash
cd "$HOME/Projeler/DORAnsible/backend"
source .venv/bin/activate
uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Terminal 2 — frontend:

```bash
cd "$HOME/Projeler/DORAnsible/frontend"
npm run dev
```

Tarayıcı:

```text
http://127.0.0.1:5173
```

## 2. Dört hızlı kontrol

### Backend sağlık cevabı

```bash
curl -fsS http://127.0.0.1:8000/health
```

### API dokümanı

Tarayıcıda `http://127.0.0.1:8000/docs` açılmalıdır.

### Frontend

Tarayıcıda `http://127.0.0.1:5173` açılmalıdır.

### Runtime klasörleri

Repository kökünde:

```bash
cd "$HOME/Projeler/DORAnsible"
find app-data -maxdepth 2 -type d | sort
```

`database`, `projects`, `inventories`, `jobs`, `secrets`, `ssh` ve execution
klasörlerini görmeniz normaldir.

## 3. Bu dosyalar nerede tutulur?

```text
app-data/
├── database/app.db       # SQLite ve kalıcı kayıtlar
├── projects/             # varsayılan izinli project kökü
├── inventories/          # bağımsız inventory kökü
├── secrets/              # private key dosyaları
├── ssh/known_hosts       # DORAnsible'a özel host key kayıtları
├── jobs/                 # Job sonuç/artifact dosyaları
├── execution-plans/      # kısa ömürlü dondurulmuş planlar
└── execution-runs/       # runner çalışma alanları
```

`app-data/` Git tarafından izlenmez. Bu klasörü silmek temizleme değil, yerel
veri ve geçmiş kaybıdır.

## 4. İlk açılışta henüz ne görünür?

Temiz veritabanında Project, Inventory ve Çalıştırmalar listeleri boştur. Bu
beklenen davranıştır. Uygulama bilgisayarınızdaki herhangi bir Ansible klasörünü
kendiliğinden taramaz; önce izinli bir project hazırlayıp UI'da kaydetmeniz
gerekir.

## 5. Başka cihazdan açma sınırı

Bu kılavuz backend ve frontend'i yalnız `127.0.0.1` üzerinde açar. Aynı ağdaki
başka bilgisayar bu adreslere erişemez. Servisi `0.0.0.0` ile internete/ağa
açmak basit bir adres değişikliği değildir: login/RBAC olmayan MVP için TLS,
reverse proxy, firewall ve erişim modeli ayrıca tasarlanmalıdır.
