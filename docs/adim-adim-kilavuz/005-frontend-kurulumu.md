# 005 — Frontend Kurulumu

Backend terminalini kapatmayın. Yeni bir terminal açın.

## 1. Frontend klasörüne girin

```bash
cd "$HOME/Projeler/DORAnsible/frontend"
```

Kontrol:

```bash
pwd
ls
```

`package.json` ve `package-lock.json` görünmelidir.

## 2. JavaScript bağımlılıklarını kurun

```bash
npm ci
```

`npm ci`, repository'deki lock dosyasına göre tekrarlanabilir kurulum yapar.
`node_modules/` oluşur; bu klasör Git'e eklenmez.

## 3. Frontend environment dosyasını oluşturun

```bash
cp .env.example .env.local
```

İçerik varsayılan yerel backend'i göstermelidir:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Frontend environment dosyasına secret, SSH key, API key veya parola yazmayın.
`VITE_` ile başlayan değerler tarayıcı bundle'ına girebilir ve gizli değildir.

## 4. Frontend'i başlatın

```bash
npm run dev
```

Terminalde aşağıdakine benzer bir adres görünür:

```text
Local: http://127.0.0.1:5173/
```

Tarayıcıyı açıp şu adresi ziyaret edin:

```text
http://127.0.0.1:5173
```

## 5. Beklenen ekran

Üst bölümde `DORAnsible` ve şu menüler görünmelidir:

- Genel bakış
- Project'ler
- Inventory'ler
- Çalıştırmalar

Sayfa açılıyor fakat veri yükleme hatası gösteriyorsa backend terminalinin
çalıştığını ve `http://127.0.0.1:8000/health` adresini kontrol edin.

## 6. Bölüm sonu kontrolü

- [ ] `npm ci` tamamlandı.
- [ ] `.env.local` backend adresini gösteriyor.
- [ ] `npm run dev` çalışıyor.
- [ ] Tarayıcıda DORAnsible ana sayfası açılıyor.
- [ ] Backend ve frontend iki ayrı terminalde açık.
