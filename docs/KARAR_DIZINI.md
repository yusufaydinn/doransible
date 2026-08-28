# DORAnsible mimari karar dizini

Kaynak kod ve testlerdeki `ADR-xxx` ifadeleri, kararların kararlı izleme
kimlikleridir. Bu teslim repository'sinde güncel ve uygulanabilir sözleşmeler
[MIMARI.md](../MIMARI.md) ile [GUVENLIK.md](../GUVENLIK.md) içinde birlikte
belgelenir. Aşağıdaki dizin, yorumlarda görülen kimliklerin hangi konuya ait
olduğunu gösterir.

| Kimlik | Karar konusu | Güncel bağlayıcı kaynak |
|---|---|---|
| ADR-001 | Playbook execution backend'i olarak Ansible Runner | `MIMARI.md` execution ve worker bölümleri |
| ADR-004 | SQLite ile başlangıç ve SQLAlchemy üzerinden taşınabilir veri erişimi | `MIMARI.md` veri modeli bölümü |
| ADR-009 | Genel amaçlı terminal sunulmaması | `GUVENLIK.md` subprocess ve komut yüzeyi bölümleri |
| ADR-011 | Trusted-operator MVP'de tek kullanıcı modeli | `GUVENLIK.md` tehdit modeli ve kimlik sınırı |
| ADR-015 | Project ve inventory path politikalarının ayrılması | `MIMARI.md` inventory; `GUVENLIK.md` path sınırları |
| ADR-017 | Inventory'nin ayrı `ansible-inventory` sürecinde parse edilmesi | `MIMARI.md` inventory parse akışı |
| ADR-018 | Ping planı, dondurulmuş snapshot ve SSH hedef doğrulaması | `MIMARI.md` ping akışı; `GUVENLIK.md` SSH sınırı |
| ADR-019 | Onaylı ping yürütme ve Job altyapısı | `MIMARI.md` ping ve Job bölümleri |
| ADR-021 | Runner ölçüm kapıları ve execution çekirdeğinin sınırları | `MIMARI.md` execution; `GUVENLIK.md` runner sınırları |
| ADR-022 | Trusted-operator execution tehdit modeli | `GUVENLIK.md` tehdit modeli |
| ADR-023 | Trusted-operator MVP artık risk kabulü | `GUVENLIK.md` artık risk ve yeniden açılma koşulları |
| ADR-024 | Check/Normal için CLI-equivalent execution kararı | `GUVENLIK.md` normal-mode execution sınırı |
| ADR-025 | Sınırlandırılmış Ansible display output | `GUVENLIK.md` kullanıcıya gösterilen çıktı sınırı |

Bir yorum ile güncel davranış arasında fark görülürse yorum tek başına ürün
sözleşmesi sayılmaz. Uygulama kodu, otomatik testler ve yukarıdaki iki güncel
belge birlikte değerlendirilmelidir.
