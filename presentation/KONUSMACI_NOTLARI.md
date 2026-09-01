# DORAnsible — Konuşmacı Notları

Bu metin yaklaşık **15–18 dakikalık anlatım + 4–6 dakikalık canlı demo** için
hazırlanmıştır. Süre kısalırsa 4. ve 8. slaytlar daha hızlı geçilebilir.

## Slayt 1 — DORAnsible

**Hedef süre: 45 saniye**

> Merhaba. Bugün sizlere DORAnsible isimli projemi sunacağım. DORAnsible,
> project, inventory, playbook, execution ve sonuç inceleme süreçlerini tek bir
> web arayüzünde birleştiren bir Ansible yönetim ve çalıştırma platformudur.
> Buradaki amacım Ansible’ın yerine yeni bir otomasyon motoru yazmak değil.
> Ansible’ın mevcut gücünü koruyarak, profesyonel bir operatör için daha görünür,
> izlenebilir ve yönetilebilir bir operasyon akışı oluşturmaktır. Önce Ansible’ın
> temel kavramlarını kısaca anlatacağım, ardından DORAnsible’ın çalışan
> özelliklerini ve mimarisini göstereceğim. Son bölümde ise bu altyapının ileride
> hangi yapay zekâ entegrasyonlarına imkân sağlayabileceğini ele alacağım.

**Vurgu:** Kapakta AI’dan bahsetme. İlk olarak mevcut ve çalışan ürünü konumlandır.

---

## Slayt 2 — Ansible nedir?

**Hedef süre: 60 saniye**

> Ansible, merkezi bir controller üzerinden uzak sistemleri otomatikleştirmek
> için kullanılan bir araçtır. En önemli özelliklerinden biri agentless
> olmasıdır. Yani yönetilen Ubuntu makinelerine sürekli çalışan özel bir
> DORAnsible veya Ansible agent’ı kurmamız gerekmez. Controller genellikle SSH
> üzerinden hedefe bağlanır ve Ansible modüllerini çalıştırır. Paket kurma,
> servis yönetme, dosya yapılandırma, kullanıcı oluşturma ve güvenlik ayarları
> gibi operasyonlar YAML biçimindeki playbooklarla tanımlanır. Bu sayede aynı
> işlemler farklı makinelerde tekrarlanabilir ve tutarlı biçimde uygulanabilir.

**Muhtemel soru:** Ansible ping klasik ICMP ping midir?

**Kısa cevap:** Hayır. Ansible ping, bağlantının ve basit bir Ansible modülünün
hedefte çalışabildiğini sınar; yalnız ICMP erişimini ölçmez.

---

## Slayt 3 — Inventory, playbook, task ve module

**Hedef süre: 75 saniye**

> Ansible’daki temel kavramları basit sorularla ayırabiliriz. Inventory, “hangi
> makineler?” sorusunu cevaplar. Hostları ve web ya da database gibi grupları
> tanımlar. Playbook, uygulanacak otomasyon senaryosudur. Play ise playbook
> içinde belirli bir host grubuna uygulanacak bölümdür. Task tek bir işlem
> adımıdır. Module ise bu işlemi gerçekleştiren Ansible bileşenidir. Örnekte
> `web` grubunu hedefleyen bir play var. İçindeki task, Nginx paketinin kurulu
> olmasını istiyor ve bunu `ansible.builtin.apt` modülüyle gerçekleştiriyor.
> Role ise task, handler, variable ve template gibi parçaları tekrar
> kullanılabilir bir yapı altında toplamamızı sağlar.

**Akılda kalıcı özet:** Inventory “nerede?”, playbook “hangi senaryo?”, task
“hangi adım?”, module “nasıl?” sorularını cevaplar.

---

## Slayt 4 — Desired state ve idempotency

**Hedef süre: 60 saniye**

> Ansible’ın önemli yaklaşımı yalnızca komut çalıştırmak değil, hedef sistemin
> istenen durumunu tanımlamaktır. “Nginx’i şimdi kur” demek yerine “Nginx kurulu
> olmalı” deriz. “Servisi şimdi başlat” yerine “Servis running ve enabled olmalı”
> deriz. Hedef zaten istenen durumdaysa iyi yazılmış bir task yeniden gereksiz
> değişiklik yapmaz. Buna idempotency diyoruz. Sunumda remediation playbookunu
> ikinci kez çalıştırdığımda `changed=0` görmemiz, sistemin zaten istenen
> durumda olduğunu ve aynı otomasyonun tekrar değişiklik üretmediğini gösterir.
> Ancak Ansible kullanmak tek başına idempotency garantisi değildir; playbookun
> ve kullanılan modüllerin buna uygun tasarlanması gerekir.

---

## Slayt 5 — Problem ve çözüm

**Hedef süre: 90 saniye**

> Ansible güçlü bir automation motorudur ve profesyonel operatör terminalden
> kullanmaya devam edebilir. Burada sol tarafta Ansible’ın kusurlarını değil,
> CLI çevresindeki operasyon yönetimi ihtiyaçlarını görüyoruz. Project,
> inventory, playbook ve mode ayrı bağlamlarda takip edilebilir. Çalıştırmalar
> kendiliğinden yapılandırılmış ve kalıcı Job geçmişine dönüşmez. Host, ping ve
> execution geçmişi ortak bir ürün görünümünde olmayabilir. DORAnsible sağ
> tarafta bu ihtiyaçlara birebir karşılık verir. Project, inventory ve playbook
> tek bağlamda birleşir. Hedef, içerik ve mode çalıştırmadan önce plan üzerinde
> açıkça gösterilir. Her execution pending, running ve terminal durumları olan
> kalıcı bir Job olarak izlenir. Ping, recap, event ve geçmiş sonuçları aynı
> arayüzde sunulur. Yani DORAnsible, Ansible’ın yerine geçmez; execution’ı
> görünür ve izlenebilir bir ürün akışına dönüştürür.

**Dürüst sınır:** Check mode, değişiklik yapılmayacağının mutlak garantisi
değildir. Normal mode ise hedefte gerçek değişiklik uygular.

---

## Slayt 6 — Katmanlı sistem mimarisi

**Hedef süre: 90 saniye**

> Diyagram bugün çalışan sistemin bileşenlerini gösteriyor. Profesyonel operatör
> React ve TypeScript arayüzünü kullanır. Tarayıcı doğrudan SSH bağlantısı kurmaz ve Ansible
> çalıştırmaz. İstekler FastAPI backend’ine gider. Pydantic API sözleşmesini,
> servis katmanı ise plan ve authorization kurallarını uygular. Kalıcı durum
> SQLite üzerinde SQLAlchemy ile tutulur. Launch isteği pending Job oluşturur.
> Background worker bu Job’ı lease ve heartbeat ile sahiplenir. Frozen
> workspace ve manifest doğrulandıktan sonra ansible-runner child process’i
> başlatılır. Hedef Ubuntu makinelerine SSH bağlantısını Ansible kurar. Event ve
> sonuçlar artifact’a yazılır ve Job API üzerinden kullanıcıya sunulur. AI
> entegrasyonuna daha sonra, mevcut ürün ve canlı demo anlatıldıktan sonra
> geçeceğim.

---

## Slayt 7 — Canlı demo

**Hedef süre: 4–6 dakika**

> Şimdi bu akışı çalışan uygulama üzerinde göstereceğim. Önce kayıtlı project’i
> ve keşfedilmiş playbookları açacağım. Ardından inventory detayında hostları ve
> grupları göstereceğim. Ansible ping ile hedeflerde temel Ansible erişimini ve
> kalıcı ping geçmişini göstereceğim. Bir playbook seçip check ya da normal
> mode’la oluşturulan planı, hedefleri ve açık onay adımını göstereceğim. Job’ın
> pending durumdan terminal sonuca uzanan yaşam döngüsünü; recap, event ve sonuç
> görünümünü açacağım. Son olarak Job listesindeki filtre ve sayfalama üzerinden
> kalıcı geçmişi göstereceğim. Kullandığım örnek audit veya remediation olabilir;
> bunlar standart Ansible pratikleridir. DORAnsible’ın katkısı bu pratiği bulmak
> değil, execution bağlamını ve sonucunu ürün akışında birleştirmektir.

### Demo sırasında unutma

1. Her menüyü gezme; tek hikâye anlat.
2. `201` cevabını değil Job’ın ilerleyen durumunu göster.
3. Audit failure ile runner failure’ı birbirine karıştırma.
4. Normal mode’un gerçek değişiklik yaptığını açıkça söyle.
5. Zaman kalırsa ikinci çalıştırmada `changed=0` örneğini göster.
6. Yeni çalıştırma aksarsa önceden tamamlanmış Job kaydına geç.

---

## Slayt 8 — DORAnsible hangi AI entegrasyonlarına kapı açıyor?

**Hedef süre: 75 saniye**

> Buraya kadar gösterdiğim bölüm mevcut ve çalışan üründür. Şimdi bu execution
> omurgasının gelecekte sağlayabileceği AI entegrasyonlarına bakalım. Kullanıcı
> doğal dilde “Ubuntu web sunucularına Nginx kur, servisi etkinleştir ve UFW’de
> 80 ile 443 portlarına izin ver” diyebilir. AI yalnız YAML metni değil;
> playbook taslağı, varsayımlar, hedef grup, olası etkiler ve uyarılar içeren
> yapılandırılmış bir cevap üretebilir. Bu taslak YAML parse, syntax-check, lint
> ve diff gibi deterministic kontrollerden geçer. Ardından profesyonel operatör
> tarafından incelenip onaylanır. Yalnız bundan sonra mevcut plan-token-Job-
> worker execution zinciri kullanılır. AI doğrudan execution yetkisine sahip
> olmaz.

---

## Slayt 9 — AI kullanım alanları

**Hedef süre: 100 saniye**

> AI entegrasyonlarını iki grupta ele alabiliriz. Playbook tarafında mevcut bir
> playbookun ne yaptığını, hangi hostları hedeflediğini, hangi modülleri
> kullandığını, hangi varsayımlara ve olası yan etkilere sahip olduğunu
> açıklayabilir. Doğal dilden yeni taslak oluşturabilir, audit ve remediation
> playbooklarını bağlı biçimde önerebilir, idempotent olmayan shell desenlerini
> fark edip uygun Ansible modülleri önerebilir ve kullanıcıya doğrudan dosyanın
> üzerine yazmak yerine diff gösterebilir. Operasyon tarafında sanitize edilmiş
> Job sonucunu sadeleştirebilir, failed ve unreachable sonuçları için kanıtın
> izin verdiği ölçüde olası nedenler ve sonraki adımlar sunabilir. Ping ve Job
> geçmişindeki eğilimleri özetleyebilir. Ancak AI source-of-truth değildir;
> playbook dosyası, Job kaydı ve normalize runner sonucu gerçek kaynak olarak
> kalır.

---

## Slayt 10 — Mevcut ürün ve AI use-case’leri

**Hedef süre: 90 saniye**

> Diyagramın sol tarafında mevcut ürünün kullanım alanları bulunuyor: project
> ve inventory yönetimi, playbook keşfi, ping geçmişi, check ve normal mode
> execution, Job sonucu ve geçmişi. Sağ tarafta planlanan AI kullanım alanları
> var. Playbook açıklama ve taslak üretimi mevcut project bağlamını kullanır.
> Sonuç açıklama mevcut sanitize Job sonucunu kullanır. Fleet eğilim analizi
> ping ve Job geçmişinden sınırlı bağlam alır. Modelin doğrudan veritabanına,
> private key dosyasına veya sınırsız controller filesystem’ine erişimi olmaz.
> Backend, modele yalnız görevi tamamlamak için gereken minimum bağlamı sağlar.

**Önemli:** “ChatGPT’ye bütün inventory’yi gönderiyoruz” deme. IP veya secret
gerekmiyorsa yalnız grup, platform ve host sayısı gibi özet bağlam kullanılabilir.

---

## Slayt 11 — Sonuç ve yol haritası

**Hedef süre: 60 saniye**

> Özetlemek gerekirse DORAnsible bugün project, inventory, playbook, plan,
> check ve normal mode, Job-worker lifecycle ve sonuç görünürlüğünü bir araya
> getiren gerçek bir Ansible execution MVP’sidir. Yakın dönemde validation,
> diff, canlı event ve diğer operasyonel iyileştirmeler eklenebilir. İlk AI
> dilimi playbook açıklama, yapılandırılmış taslak üretimi ve Job sonucu
> açıklama olabilir. Daha sonraki aşamalarda audit-remediation üretimi, fleet
> eğilim analizi ve test senaryosu üretimi geliştirilebilir. DORAnsible’ın
> bugünkü değeri Ansible operasyonlarını görünür ve izlenebilir hâle getirmesi;
> gelecekteki değeri ise doğal dil, playbook ve operasyon sonucu arasında insan
> denetimli bir AI katmanı kurmasıdır. Buradaki temel ilkemiz şudur: AI karar
> veren değil, profesyonel operatörü güçlendiren yardımcıdır.

---

# Sunum boyunca kullanılacak doğru ifadeler

| Kaçınılacak ifade | Kullanılacak ifade |
|---|---|
| “AI özelliğimiz playbook üretiyor.” | “Planlanan AI Builder playbook taslağı üretecek.” |
| “Check mode değişiklik yapmaz.” | “Check mode olası değişiklikleri değerlendirmeye yardım eder; yan etkisizlik garantisi değildir.” |
| “Normal mode güvenlidir.” | “Normal mode gerçek Ansible değişikliği uygular ve açık operatör onayı gerektirir.” |
| “Sistem rollback yapar.” | “Job durumu dürüst tutulur; genel amaçlı otomatik rollback garantisi yoktur.” |
| “Ham çıktı secret içermez.” | “Ham çıktı hassas veri içerebilir ve yalnız trusted operator için gösterilir.” |
| “AI sonucu bilir.” | “AI, deterministic Job sonucunu açıklayan yardımcı katmandır.” |
| “Bu AWX’in alternatifidir.” | “Bu, odaklı trusted-operator MVP’sidir; AWX’in tüm kapsamını yeniden üretme iddiası yoktur.” |

# Kısa kapanış

> DORAnsible, Ansible’ın yerine geçmez; Ansible operasyonlarını görünür,
> izlenebilir ve insan onaylı bir ürün akışına dönüştürür. Kurduğumuz execution
> omurgası da gelecekte AI’nın güvenilir bir karar mercii değil, profesyonel
> operatöre yardımcı olan bir katman olarak eklenmesini mümkün kılar.
