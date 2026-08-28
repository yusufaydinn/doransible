# 003 — Projeyi GitHub'dan İndirme

## 1. Çalışma klasörünü oluşturun

```bash
mkdir -p "$HOME/Projeler"
cd "$HOME/Projeler"
```

## 2. Repository'yi klonlayın

GitHub erişiminiz hazırsa:

```bash
git clone https://github.com/yusufaydinn/doransible.git DORAnsible
cd DORAnsible
```

Repository özel olduğu için GitHub hesabınızın projeye erişimi olmalıdır.
HTTPS kimlik doğrulamasında GitHub personal access token veya yapılandırılmış
credential helper kullanılabilir. Token'ı komutun içine veya belgeye yazmayın.
SSH erişimi yapılandırıldıysa repository'nin SSH clone adresi de kullanılabilir.

## 3. Doğru klasörde olduğunuzu doğrulayın

```bash
pwd
ls
git status --short --branch
```

`ls` çıktısında en az şunları görmelisiniz:

```text
backend
frontend
sample-projects
docs
scripts
README.md
```

`git status` çıktısında beklenmeyen `M`, `D` veya `??` satırı olmamalıdır.

## 4. Kurulacak sürümü sabitleyin

Size belirli bir release/tag/commit verildiyse onu kullanın. Aksi durumda
GitHub repository'sinin varsayılan `main` dalını güncelleyin:

```bash
git switch main
git pull --ff-only
```

Bu iki komuttan sonra `git status --short --branch` çıktısı `main...origin/main`
göstermeli ve altında değişiklik satırı bulunmamalıdır.

## 5. Arşiv (ZIP) ile aldıysanız

Git kullanmadan ZIP indirildiyse arşivi bir çalışma klasörüne çıkartın ve
çıkarılan repository köküne geçin. Güncelleme ve sürüm kontrolü daha zor olduğu
için mümkünse Git clone tercih edilir.

## 6. Bölüm sonu kontrolü

- [ ] Aktif çalışma dizini repository kökü.
- [ ] `backend/` ve `frontend/` klasörleri görünüyor.
- [ ] Kullanılacak branch doğrulandı.
- [ ] Working tree temiz.
