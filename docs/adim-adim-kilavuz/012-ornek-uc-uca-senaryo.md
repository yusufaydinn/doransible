# 012 — Baştan Sona Örnek Senaryo

Bu senaryo kurulumun işlevsel olduğunu göstermeye yarar. Hedefin snapshot'ını
alın ve yalnız test makinesi kullanın.

## Senaryo A — SSH audit

1. Hedef Ubuntu VM'yi açın.
2. Backend ve frontend'i başlatın.
3. Inventory detayında ping önizlemesi oluşturun.
4. Hedef adı/IP beklentinize uyuyorsa ping'i onaylayın.
5. Host `Erişilebilir` olmalıdır.
6. `Ubuntu SSH Audit Demo` project detayını açın.
7. Kip **Check**, doğru inventory ve `ubuntu-ssh-audit.yml` seçin.
8. Planı oluşturun ve host listesini inceleyin.
9. Onaya hazırlayın, açık onayı verin ve Job'ı başlatın.
10. Job terminal duruma geldiğinde task ve recap'i okuyun.

Audit `failed` olabilir. Bu, SSH baseline'da uygunsuzluk bulunduğu anlamına
gelebilir. Task adları hangi kontrolün kaldığını gösterir.

## Senaryo B — SSH remediation (yalnız laboratuvar)

SSH hardening bağlantıyı kesebilecek gerçek değişiklikler yapar. Önce hedef VM
snapshot'ı ve konsol erişimi olmadan uygulamayın.

Runtime project'i kopyalayın:

```bash
cd "$HOME/Projeler/DORAnsible"
cp -a sample-projects/ubuntu-ssh-hardening app-data/projects/
```

Inventory dosyasını 008. bölümdeki gerçek hedef bilgileriyle düzenleyin. UI'da
ayrı Project ve bağlı Inventory olarak kaydedin.

Önerilen sıra:

```text
1. SSH Audit — Check
2. SSH Hardening — Check
3. Planlanan task ve hedefleri incele
4. SSH Hardening — Normal
5. SSH Hardening — Normal tekrar (idempotency kontrolü)
6. SSH Audit — Check tekrar (bağımsız doğrulama)
```

İkinci Normal çalıştırmada hedef zaten uygunsa `changed=0` beklenir. Bu sonuç
bile tüm dağıtımlar için genel güvenlik garantisi değildir; sample README'sindeki
profil ve `Match` blok sınırlarını okuyun.

## Senaryo C — UFW (ileri ve riskli laboratuvar)

UFW hardening yanlış SSH portu veya ağ topolojisinde lockout yaratabilir. Konsol
erişimi, VM snapshot'ı ve mevcut firewall bilgisiniz yoksa bu senaryoyu atlayın.

Kanonik ayrıntılar:

- [Ubuntu UFW Audit](../../sample-projects/ubuntu-ufw-audit/README.md)
- [Ubuntu UFW Hardening](../../sample-projects/ubuntu-ufw-hardening/README.md)

Önerilen sıra yine Audit Check → Hardening Check → Hardening Normal → ikinci
Normal → Audit Check'tir. `serial: 1`, otomatik rollback anlamına gelmez.

## Demo başarı ölçütü

- Ping sonucu kalıcı geçmişte görünüyor.
- Plan doğru mode/project/inventory/playbook/host bağını gösteriyor.
- Job `pending → running → terminal` ilerliyor.
- Result ekranı recap, event ve bounded görüntü çıktısı gösteriyor.
- Çalıştırma, filtreli Job geçmişinde bulunuyor.
