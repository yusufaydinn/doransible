"""Runner child process environment'ı (R1-V3C1A, ADR-021 Kapı A1).

`ansible-runner`, **kendisini başlatan sürecin environment'ını miras alır** ve
``env/envvars`` bu environment'ın yerine geçmez, üzerine ekler. Dolayısıyla tek
güvenilir sınır, çocuğa verilen environment'ı **sıfırdan kurmaktır**: bu modül
o sözlüğü üretir.

Sözleşme:

- Parent environment **kopyalanmaz**. ``os.environ.copy()`` ve sözlük
  genişletmesiyle toplu aktarım bu modülde bilinçli olarak yoktur; parent'tan
  yalnızca :data:`INHERITED_ENV_NAMES` içindeki dar küme, adı adına okunur.
- ``extra``/override parametresi **yoktur**. Çağıranın environment'a serbestçe
  anahtar ekleyebilmesi, allowlist'i tek satırlık bir çağrıyla delerdi; Kapı A1
  ancak anahtar kümesi **tam eşitlikle** bağlandığında ölçülebilir.
- Raw token, master key, veritabanı DSN'i ve backend ayarları buraya hiç
  girmez. Modül veritabanına ve session'a **dokunmaz** (ADR-022 Karar 6).
- Üretilen absolute path'ler yalnız child environment'ına konur; bu modül API
  modeli veya kullanıcıya dönecek bir değer üretmez.
- SSH argümanları serbest metinden **üretilmez**: modül, T-204B1'in güvenilir
  :func:`build_ssh_arguments` primitive'ini doğrudan çağırır ve sonucu
  :func:`render_ansible_ssh_args` ile kayıpsız string'e çevirir. Çağıran ham
  bir ``ANSIBLE_SSH_ARGS`` metni geçiremez.

**Çalışma alanı sınırı (R1-V3C1AF).** Modül serbest bir absolute path altında
dizin ağacı **açmaz**. Çağıran bir ``run_dir`` seçemez; yalnızca execution run
kökünü ve Job kimliğini verir ve dizin adı buradan türetilir::

    <execution-run-root>/<canonical UUID4 job_id>

Kökün adı sabit :data:`app.core.config.EXECUTION_RUN_DIRNAME` olmalı, kök
**önceden var**, normal bir dizin ve 0700 olmalıdır; modül onu oluşturmaz ve
``parents=True`` kullanmaz. Bunun sebebi somut: önceki biçim ``run_dir`` olarak
verilen herhangi bir yolu ``mkdir(parents=True)`` ile açıp path tabanlı
``chmod`` uyguluyordu — yani ``/tmp``, uygulama kökü veya yerine symlink konmuş
bir girdi de hedef olabilirdi.

Bütün kök ve Job dizini işlemleri **descriptor-relative**'dir (``dir_fd`` +
``O_DIRECTORY`` + ``O_NOFOLLOW``) ve açılan her descriptor ``fstat`` ile
yeniden doğrulanır: path metnini çözüp ardından normal ``open`` yapmak güvenlik
kanıtı sayılmaz, çünkü çözme ile kullanma arasındaki pencerede girdi
değiştirilebilir.

**Mevcut bir Job dizini yeniden kullanılmaz.** Aynı kimlikte bir girdi varsa
hazırlık fail-closed düşer; önceki bir çalıştırmadan kalan raw artifact, geçici
dosya veya kısmi çıktı yeni bir execution'ın sonucuna karışamaz.

**Hazırlık failure-atomic'tir (R1-V3C1C2-AUDIT-FIX1).** Job dizini
oluşturulduktan sonra düşen her yol — alt dizinlerin kurulamaması, project
``ansible.cfg``'sinin reddedilmesi, tanınmayan SSH politikası ya da beklenmeyen
bir istisna — dizini geride **bırakmaz**: aynı modülün
:func:`remove_execution_run_directory` primitive'i, aynı sınırlar içinden
çağrılır. Bunun sebebi somut: geride kalan bir alan, aynı Job kimliğiyle
yapılacak bir sonraki hazırlığı "aynı kimlikte girdi var" diye düşürürdü ve
kalıntıyı toplayan bir janitor bu dilimde henüz yoktur. Temizlik de
başarısızsa hazırlık başarılı sayılmaz; sonuç sabit ve sızdırmayan bir hatadır.

**Çalışma alanının kaldırılması (R1-V3C1C2B2A).** Bir deneme bittikten sonra
alanı geri veren tek primitive :func:`remove_execution_run_directory`'dir ve o
da aynı sınırın içinden çalışır: hedef, kökün **doğrudan** ``<job_id>`` çocuğu
olmak zorundadır, silme descriptor-relative ilerler, symlink hiçbir adımda
izlenmez ve yürüyüş derinlik ile girdi sayısı bakımından **sınırlıdır**.
``shutil.rmtree``, glob ve çözülmüş path üzerinden serbest özyineleme bilinçli
olarak yoktur: hepsi, hedefin altına konmuş bir bağlantı üzerinden silmenin
ağacın dışına taşmasına izin verebilecek biçimlerdir. Temizlik **hiçbir izni
değiştirmez**; okunamayan veya beklenmeyen bir girdi fail-closed hatadır.

**Kökün listelenmesi (R1-V3C2B).** :func:`list_execution_run_directories` aynı
kök sözleşmesinin üzerinde duran, **yalnız okuyan** dar bir primitive'dir:
girdi adlarını bir ``scandir`` *iterator*'ından sınırlı biçimde tüketir, girdi
başına tek bir ``follow_symlinks=False`` ``stat`` yapar, hiçbir girdiyi açmaz,
hiçbir alt ağaca inmez ve hiçbir şey silmez. Sınır (:data:`MAX_RUN_ROOT_ENTRIES`)
tüketimin kendisindedir: kök hiçbir zaman tümüyle belleğe alınmaz ve sınır
aşıldığında tek bir ``stat`` bile yapılmamış olur. Fonksiyon bir "hangisi
silinsin" kararı da vermez; yaş, Job durumu ve silme kararı çağıranın işidir.

**Silmenin nesneye bağlanması (R1-V3C2B-AUDIT-FIX1).** Listeleme her adayın
:class:`RunDirectoryIdentity`'sini de üretir ve
:func:`remove_execution_run_directory` bunu ``expected_identity`` olarak kabul
eder. Ad ile nesne aynı şey değildir: bir listeleme ile silme arasında dizin
kaldırılıp aynı canonical adla **yeni ve gerçek** bir dizin oluşturulabilir.
Kimlik uyuşmadığında silme hiç başlamaz.

**Bu modül hiçbir şey çalıştırmaz.** Alt süreç açmaz, `ansible-runner`
çağırmaz; yalnızca bir sözlük ve onun yaslandığı 0700 dizinleri üretir. Gerçek
süreç dilimi R1-V3C1B'dedir.

**Project ``ansible.cfg`` desteklenir (ADR-022 Karar 3).** Dondurulmuş project
kökünde normal bir ``ansible.cfg`` varsa ``ANSIBLE_CONFIG`` onu gösterir; yoksa
çalışma alanında kontrollü ve **boş** bir config üretilir. "Project config her
durumda kapalı" yaklaşımı kullanılmaz: role, collection ve plugin yollarını
project'in kendi config'i tarif eder ve onu görmezden gelmek, operatörün kendi
Ansible project'ini çalıştırılamaz hâle getirirdi.

Config seçimi yalnız **dondurulmuş** project köküne bakar. Özgün ağaç claim'den
sonra hiç açılmaz: onaylanan içerik dondurulmuş kopyadır ve yalnız o kopya
manifest ile doğrulanmıştır.

**Fact-cache backend'i için `memory` istenir (R1-V3D0A, ADR-021 Kapı D).**
Project'in kendi ``fact_caching``/``fact_caching_connection`` ayarları
okunmaya devam eder — config kategorik olarak kapatılmaz — ama bu modülün
ürettiği environment ``ANSIBLE_CACHE_PLUGIN=memory`` **ister** ve bir bağlantı
değeri hiç vermez; ``ANSIBLE_CACHE_PLUGIN_CONNECTION`` de
:data:`INHERITED_ENV_NAMES` dışında olduğu için parent'tan miras alınmaz.

Bu, **uçtan uca bir runner garantisi değildir** — yalnız bu modülün ürettiği
başlangıç/child environment sözlüğünü ölçer ve environment'a uyan tüketiciler
için bir defense-in-depth/politika niyetidir. Production zincirinde
kullanılan ``ansible-runner`` 2.4.3 CLI'si
(``ansible_runner/config/_base.py:74,330-333``) ``fact_cache_type``'ı CLI'de
değiştirilemeyen sabit ``'jsonfile'`` varsayımıyla çalıştırır ve bu modülün
kurduğu environment'ı **kurulduktan sonra koşulsuz olarak ezer**: kendi
``ANSIBLE_CACHE_PLUGIN=jsonfile`` + ``ANSIBLE_CACHE_PLUGIN_CONNECTION=
<raw>/<ident>/fact_cache`` değerini yazar. Doğrudan mutation ölçümüyle
doğrulandı: ``ANSIBLE_CACHE_PLUGIN=memory`` satırı kaldırıldığında da sonuç
aynıdır — project'in kendi sentinel ``fact_caching_connection`` yolu ne
satır varken ne yokken kullanılır; onun yerine ansible-runner'ın kendi
controlled raw ``jsonfile`` yolu kullanılır. Yani "bu modülün memory isteği
project'in sentinel yolunu engelliyor" nedenselliği **yanlıştır**: engelleyen
ansible-runner CLI'nin kendi override'ıdır, bu modülün isteği değil. Çalışma
sırasında disk üzerinde kısa süreli düz metin bir ``jsonfile`` fact-cache
oluşur; terminal yollarda ``raw`` dizini temizliğiyle silinir, crash
senaryoları mevcut bounded janitor/TTL sözleşmesine tabidir. Bu artık risk
trusted-operator MVP güvenlik modelinde kabul edilmiştir; bu modül production
backend'inin gerçekten ``memory`` olduğunu kanıtlamaz.

**Platform sınırı.** Descriptor-relative primitive'ler yalnız POSIX'te vardır ve
Ansible zaten Windows'u control node olarak desteklemez (ADR-017). Zayıf bir
fallback'e düşülmez; Windows'a özgü environment değişkenleri de bilinçli olarak
aktarılmaz — aktarılsalardı desteklenmeyen bir platformun desteklendiği izlenimi
doğardı.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from app.core.config import EXECUTION_RUN_DIRNAME
from app.core.errors import AppError
from app.services.ansible.ssh import build_ssh_arguments, render_ansible_ssh_args

# Parent'tan **adı adına** okunan tek değişkenler. Üçü de platformun kendi
# çalışması için gereklidir: `PATH` olmadan yorumlayıcı ve `ssh` bulunamaz,
# `LANG`/`LC_ALL` olmadan Ansible'ın çıktı kodlaması makineye göre değişir.
# Liste **büyümemelidir**: her yeni ad, ölçülmüş Kapı A1 yüzeyini genişletir.
INHERITED_ENV_NAMES = ("PATH", "LANG", "LC_ALL")

# Çalışma alanının sabit iç düzeni. Adlar sabittir: çalışma anında üretilen bir
# ad, temizliğin neyi silebileceğini belirsiz kılardı.
HOME_DIRNAME = "home"
TEMP_DIRNAME = "tmp"
ANSIBLE_HOME_DIRNAME = "ansible-home"
ANSIBLE_LOCAL_TEMP_DIRNAME = "ansible-tmp"
SSH_CONTROL_DIRNAME = "ssh-control"
ANSIBLE_CONFIG_FILENAME = "ansible.cfg"

MANAGED_DIRNAMES = (
    HOME_DIRNAME,
    TEMP_DIRNAME,
    ANSIBLE_HOME_DIRNAME,
    ANSIBLE_LOCAL_TEMP_DIRNAME,
    SSH_CONTROL_DIRNAME,
)

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

# Job ağacı kaldırılırken uygulanan yapısal sınırlar. Sınırsız bir yürüyüş,
# derin veya çok girdili bir ağaçla temizliğin kendisini bir yük hâline
# getirirdi; sınır aşıldığında **hiçbir şey silinmez** (aşağıdaki iki geçiş).
#
# Değerler ölçüme değil, çalışma alanının bilinen tavanına dayanır: raw ağacı
# R1-V3C1B'de 200_000 girdi ve 16 derinlikle sınırlıdır, üstüne sabit alt
# dizinler ve `ansible.cfg` biner. Sınırlar bu tavanın hemen üstünde durur;
# meşru bir çalışma alanı bunlara değmez.
MAX_CLEANUP_ENTRIES = 250_000
MAX_CLEANUP_DEPTH = 24

# Kökün **tek geçişte** kabul edeceği azami doğrudan girdi sayısı
# (:func:`list_execution_run_directories`). Aktif PLAYBOOK concurrency'si 1
# olduğu için sağlıklı bir kurulumda kökte bir avuç girdi bulunur; binlercesi
# ya yanlış bir kökün ya da fark edilmemiş bir crash döngüsünün işaretidir.
# Böyle bir tabloda toplu silmeye girişmek yanlış olurdu: sınır aşıldığında
# listeleme fail-closed düşer ve çağıran hiçbir adaya dokunamaz.
MAX_RUN_ROOT_ENTRIES = 1_000

# Job dizini adı yalnız uygulamanın ürettiği canonical UUID4 olabilir
# (`Job.id` ile aynı biçim). Serbest bir ad, kök altında keyfi bir girdinin
# hedeflenmesine izin verirdi.
_JOB_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class RunnerEnvironmentError(AppError):
    """Runner environment'ı güvenli biçimde kurulamadı.

    Altyapı hatasıdır ve fail-closed'dır: environment yarım kurulup çalıştırmaya
    devam edilmez. ``details["reason"]`` yalnız makine tarafından okunabilir bir
    sebep taşır; path, config içeriği veya environment değeri **yazılmaz**.
    """

    status_code = 500
    code = "runner_environment_unavailable"


@dataclass(frozen=True)
class RunnerEnvironment:
    """Child process'e verilecek environment ve onun yaslandığı alan.

    ``environment`` doğrudan ``subprocess`` veya `ansible-runner` çağrısına
    verilebilecek **tam** sözlüktür; üstüne parent environment eklenmez.
    """

    environment: dict[str, str]
    run_dir: Path
    ansible_config: Path
    # Config, project'in kendi dosyası mı yoksa bu modülün ürettiği boş dosya
    # mı. Yalnız gözlemlenebilirlik içindir; bir karar girdisi değildir.
    uses_project_config: bool


@dataclass(frozen=True, slots=True)
class RunDirectoryIdentity:
    """Bir dosya sistemi **nesnesinin** kimliği; adı değil kendisi.

    ``(device, inode)`` tek başına **yetmez**: silinen bir dizinin inode'u aynı
    dosya sisteminde hemen yeniden kullanılabilir ve ölçüldü — aynı adla
    oluşturulan yeni bir dizin, ext4 üzerinde eskisinin inode'unu birebir
    devraldı. Bu yüzden kimliğe üçüncü bir alan, ``st_ctime_ns`` girer.

    ``st_ctime_ns`` bir **metadata değişim damgasıdır**: oluşturulma anı değildir
    (POSIX'te böyle bir alan yoktur) ve çakışmayacağı garanti edilen bir
    generation number da değildir. Yaptığı iş dardır ve buraya kadardır: inode
    yeniden kullanıldığında iki nesnenin ayırt edilme olasılığını belirgin
    biçimde artırır. Saat çözünürlüğü, saat kayması ya da elle kurcalanmış bir
    damga bu ayrımı zayıflatabilir; sözleşme bunu bir kesinlik iddiasına
    dayandırmaz.

    Kalan güvenceyi **yön** verir: karşılaştırma yalnız "beklenen kimlikle
    birebir aynı" durumunda silmeye izin verir. Damga bir biçimde eşleşmezse
    üretilen sonuç silme değil, **silmeme**dir; çağrı fail-closed düşer, dizin
    yerinde kalır ve bir sonraki tur yeniden dener. Yani hatanın maliyeti,
    yanlışlıkla silinen bir çalışma alanı değil, bir tur daha duran bir
    kalıntıdır.
    """

    device: int
    inode: int
    #: ``st_ctime_ns`` — nesnenin metadata değişim anı; oluşturulma anı veya
    #: çakışmaz bir generation number değildir.
    changed_ns: int


def _identity_of(status: os.stat_result) -> RunDirectoryIdentity:
    """``lstat`` sonucundan nesne kimliğini üretir."""
    return RunDirectoryIdentity(
        device=status.st_dev, inode=status.st_ino, changed_ns=status.st_ctime_ns
    )


@dataclass(frozen=True, slots=True)
class RunDirectoryEntry:
    """Kökün doğrudan çocuğu olan bir Job çalışma dizininin dar görüntüsü.

    Nesne bir **path taşımaz**: hedefi yeniden çözecek olan taraf
    :func:`remove_execution_run_directory`'dir ve o da yolu kökten türetir.
    Buradan bir path taşımak, silme hedefinin çağıran tarafından seçilebildiği
    bir yol açardı.

    Taşıdığı tek "hak" :attr:`identity`'dir ve o da bir hedef değil, bir
    **koşuldur**: silme anında aynı adın altında duran nesne bu kimlikte
    değilse, silme yapılmaz.
    """

    job_id: str
    #: Dizinin descriptor-relative ``lstat``'ından okunan epoch saniyesi.
    modified_at: float
    #: Listelenen **nesnenin** kimliği. Ad değil nesne bağlanır; ad tek başına
    #: bir kimlik değildir, çünkü aradan kaldırılıp aynı adla yeni bir dizin
    #: oluşturulabilir.
    identity: RunDirectoryIdentity


@dataclass(frozen=True, slots=True)
class RunRootListing:
    """Kökün sınırlı, symlink izlemeyen ve tek geçişte alınmış görüntüsü."""

    #: Canonical UUID4 adlı ve gerçekten dizin olan doğrudan çocuklar.
    candidates: tuple[RunDirectoryEntry, ...]
    #: Aday olmayan her doğrudan girdinin **sayısı**; adları taşınmaz.
    unexpected: int


def build_runner_environment(
    *,
    execution_run_root: Path,
    job_id: str,
    frozen_project_root: Path,
    ssh_policy: str,
    known_hosts: Path,
) -> RunnerEnvironment:
    """Bir Job için temiz environment ve **yeni** bir çalışma alanı üretir.

    Çalışma dizini çağıran tarafından seçilemez; ``execution_run_root/job_id``
    olarak türetilir ve descriptor-relative açılır (modül docstring'i).

    Args:
        execution_run_root: ``app-data/execution-runs`` kökü. Önceden var olan,
            normal ve 0700 bir dizin olmalıdır; bu fonksiyon onu **oluşturmaz**.
        job_id: Job'un canonical UUID4 kimliği. Çalışma dizininin adı budur.
        frozen_project_root: **Dondurulmuş** project ağacının kökü. Özgün
            project dizini burada kabul edilmez ve hiç açılmaz.
        ssh_policy: ``strict`` veya ``accept_new``. Bilinmeyen bir politika
            :func:`build_ssh_arguments` tarafından reddedilir.
        known_hosts: Hazırlanmış known-hosts dosyasının yolu.

    Returns:
        Child'a verilecek :class:`RunnerEnvironment`.

    Raises:
        RunnerEnvironmentError: Kök relative ise, adı beklenen kök adı değilse,
            yoksa/normal dizin değilse/0700 değilse; ``job_id`` canonical UUID4
            değilse; aynı kimlikte bir girdi **zaten varsa**; project
            ``ansible.cfg``'si normal bir dosya değilse (symlink, FIFO ...); ya
            da bir arızadan sonra yarım kalan Job dizini **kaldırılamadıysa**
            (``run_dir_not_cleaned``).
        ValueError: SSH politikası tanınmıyorsa ve yarım kalan Job dizini
            başarıyla kaldırılabildiyse.
    """
    _require_run_root_shape(execution_run_root)
    if not _JOB_ID_PATTERN.fullmatch(job_id):
        raise _unavailable("job_id_not_canonical")
    _require_absolute(frozen_project_root, "frozen_project_root")

    run_dir = execution_run_root / job_id

    # Dizin bu çağrıda **gerçekten** oluşturulduysa `True`. `run_dir_already_exists`
    # yolunda `False` kalır: bizim açmadığımız bir girdiyi temizlik hedefi yapmak,
    # fail-closed reddi sessiz bir silmeye çevirirdi.
    created = False
    try:
        with _open_run_root(execution_run_root) as root_fd:
            _create_job_directory(root_fd, job_id)
            created = True
            with _open_job_directory(root_fd, job_id) as job_fd:
                _prepare_managed_directories(job_fd)
                config_path, uses_project_config = _select_ansible_config(
                    job_fd=job_fd, run_dir=run_dir, frozen_project_root=frozen_project_root
                )

        ssh_arguments = build_ssh_arguments(
            policy=ssh_policy, known_hosts=known_hosts, work_dir=run_dir
        )

        # Parent'tan gelen dar küme. `os.environ.copy()` veya sözlük genişletmesi
        # yerine **ad ad** okunur; var olmayan bir ad sessizce atlanır.
        environment = {name: os.environ[name] for name in INHERITED_ENV_NAMES if name in os.environ}

        environment.update(
            {
                # Parent'ın `HOME`'u aktarılmaz. Ansible'ın `remote_tmp`
                # varsayılanı `~/.ansible/tmp`'dir ve `~` passwd kaydından da
                # çözülebildiği için `ANSIBLE_LOCAL_TEMP` tek başına yetmez
                # (Kapı A ölçümü): ev dizini de kontrollü alana bağlanır.
                "HOME": str(run_dir / HOME_DIRNAME),
                "TMPDIR": str(run_dir / TEMP_DIRNAME),
                "ANSIBLE_HOME": str(run_dir / ANSIBLE_HOME_DIRNAME),
                "ANSIBLE_LOCAL_TEMP": str(run_dir / ANSIBLE_LOCAL_TEMP_DIRNAME),
                # SSH control socket'i kullanıcının `~/.ansible/cp` dizinine
                # değil bu çalışma alanına yazılır; artakalan bir socket başka
                # bir Job'ın bağlantısını yeniden kullanamaz.
                "ANSIBLE_SSH_CONTROL_PATH_DIR": str(run_dir / SSH_CONTROL_DIRNAME),
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_FORCE_COLOR": "0",
                # Retry dosyaları çalışma alanının dışına yazılan kalıcı yan
                # etkilerdir ve ürün verisi değildir.
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                # Project ansible.cfg `fact_caching=jsonfile` isteyebilir (ADR-022
                # Karar 3 config'i kapatmaz); bu modül backend seçimini `memory`
                # olarak **ister** (ADR-021 Kapı D, defense-in-depth/politika
                # niyeti). Env var config dosyasındaki `fact_caching`'in
                # önündedir, bu yüzden dosya hâlâ okunur. Bir bağlantı anahtarı
                # bilinçli olarak **verilmez**: `ANSIBLE_CACHE_PLUGIN_CONNECTION`
                # de `INHERITED_ENV_NAMES` dışında olduğu için parent'tan miras
                # alınmaz. Bu istek **uçtan uca bir garanti değildir**:
                # production zincirindeki `ansible-runner` 2.4.3 CLI'si bu
                # environment'ı kurulduktan sonra kendi raw `jsonfile`
                # fact-cache'iyle koşulsuz ezer (modül docstring'i; Kapı D hâlâ
                # OPEN).
                "ANSIBLE_CACHE_PLUGIN": "memory",
                "PYTHONIOENCODING": "utf-8",
                "ANSIBLE_SSH_ARGS": render_ansible_ssh_args(ssh_arguments),
                "ANSIBLE_CONFIG": str(config_path),
            }
        )

        return RunnerEnvironment(
            environment=environment,
            run_dir=run_dir,
            ansible_config=config_path,
            uses_project_config=uses_project_config,
        )
    except Exception as error:
        # Temizlik başarılıysa dış sözleşme **değişmez**: çağıranın gördüğü hata
        # asıl reddin kendisidir. Başarısızsa hata sabit bir sebeple değiştirilir
        # — geride kalan alanı asıl reddin arkasında gizlemek, bir sonraki
        # hazırlığı düşürecek kalıntıyı görünmez kılardı.
        if created and not _discard_partial_run_directory(execution_run_root, job_id):
            raise _unavailable("run_dir_not_cleaned") from error
        raise
    except BaseException:
        # `KeyboardInterrupt`/`SystemExit`: alan yine geri verilir ama istisnanın
        # kendisi hiçbir koşulda başka bir hataya çevrilmez.
        if created:
            _discard_partial_run_directory(execution_run_root, job_id)
        raise


def remove_execution_run_directory(
    execution_run_root: Path,
    job_id: str,
    *,
    missing_ok: bool = True,
    expected_identity: RunDirectoryIdentity | None = None,
) -> bool:
    """Tek bir Job'un çalışma alanını kökün altından güvenle kaldırır.

    Hedef çağıran tarafından **seçilemez**: yalnız ``execution_run_root``'un
    doğrudan ``<job_id>`` çocuğudur ve kök :func:`build_runner_environment` ile
    aynı biçim kontrollerinden geçer. Kökün kendisine dokunulmaz.

    Silme iki geçişte yapılır. Önce ağaç **sınırlar içinde mi** diye taranır,
    sonra silinir: sınır ihlali silme başladıktan sonra fark edilseydi, çağrı
    yarısı silinmiş bir ağaç bırakırdı. İki geçiş de descriptor-relative
    ilerler (``dir_fd`` + ``O_DIRECTORY`` + ``O_NOFOLLOW``) ve inilen her dizin
    ``fstat`` ile dev/ino üzerinden yeniden doğrulanır.

    Symlink hiçbir adımda **izlenmez**: ağacın içindeki bir bağlantının kendisi
    ``unlink`` edilir, gösterdiği hedef açılmaz ve silinmez. FIFO, socket ve
    diğer özel girdiler de aynı biçimde yalnız girdi olarak kaldırılır.

    Fonksiyon **hiçbir iznini değiştirmez**; okunamayan veya silinemeyen bir
    girdi sessizce atlanmaz, fail-closed hataya dönüşür.

    Çalışan bir sürecin çalışma alanı üzerinde çağrılmamalıdır: çağıran, süreç
    tamamen reap edildikten sonra çağırmakla yükümlüdür.

    **Nesne kimliği (opsiyonel).** ``expected_identity`` verilirse silme, adın
    altında **tam olarak o nesnenin** durmasına bağlanır. Bunu isteyen taraf,
    hedefini daha önce görmüş olan çağırandır: bir listeleme ile silme arasında
    dizin kaldırılıp aynı canonical adla **yeni ve gerçek** bir dizin
    oluşturulabilir ve ad tek başına bu iki nesneyi ayırt edemez. Kimlik
    uyuşmazsa çağrı fail-closed düşer; replacement nesne **açılmaz**, içine
    inilmez ve hiçbir girdisi silinmez. Değer verilmediğinde davranış
    değişmez — hedefini aynı çağrıda kendisi oluşturan executor yolu bu yüzden
    olduğu gibi kalır.

    Args:
        execution_run_root: ``app-data/execution-runs`` kökü. Absolute, doğru
            adlı, önceden var olan, symlink olmayan ve 0700 bir dizin olmalıdır.
        job_id: Kaldırılacak Job'un canonical UUID4 kimliği.
        missing_ok: Job dizini yoksa ``True`` iken güvenli no-op, ``False`` iken
            açık hata.
        expected_identity: Beklenen :class:`RunDirectoryIdentity` ya da ``None``.
            ``None`` iken kimlik kontrolü yapılmaz.

    Returns:
        Dizin bu çağrıda kaldırıldıysa ``True``; ``missing_ok`` altında zaten
        yoksa ``False``.

    Raises:
        RunnerEnvironmentError: Kök relative ise, adı beklenen kök adı değilse,
            yoksa/normal dizin değilse/0700 değilse; ``job_id`` canonical UUID4
            değilse; ``<job_id>`` girdisi normal bir dizin değilse (symlink,
            dosya, FIFO ...); ``expected_identity`` verilip de uyuşmuyorsa
            (``run_dir_identity_changed``); ağaç derinlik veya girdi sınırını
            aşıyorsa; bir girdi kaldırılamıyorsa; ya da ``missing_ok`` ``False``
            iken dizin yoksa.
    """
    _require_run_root_shape(execution_run_root)
    if not _JOB_ID_PATTERN.fullmatch(job_id):
        raise _unavailable("job_id_not_canonical")

    with _open_run_root(execution_run_root) as root_fd:
        try:
            named = os.stat(job_id, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            if missing_ok:
                return False
            raise _unavailable("run_dir_missing") from exc
        except OSError as exc:
            raise _unavailable("run_dir_unavailable") from exc

        # Symlink, normal dosya veya özel girdi: hedefi **izlenmez** ve girdinin
        # kendisi de silinmez. Beklenen nesne bir dizindir; başka bir şeyin
        # oraya konmuş olması temizliğin değil, incelemenin konusudur.
        if not stat.S_ISDIR(named.st_mode):
            raise _unavailable("run_dir_not_a_directory")

        # Kimlik kontrolü **açmadan önce** durur: uyuşmayan bir nesne bu
        # noktadan sonra hiç açılmaz, taranmaz ve boşaltılmaz. Aşağıdaki
        # `_open_tree_directory` yalnız `named` ile kendi arasındaki pencereyi
        # kapatır; listeleme ile bu çağrı arasındaki daha geniş pencereyi
        # kapatan tek şey buradaki karşılaştırmadır.
        if expected_identity is not None and _identity_of(named) != expected_identity:
            raise _unavailable("run_dir_identity_changed")

        with _open_tree_directory(root_fd, job_id, named) as job_fd:
            _scan_tree(job_fd, depth=0, budget=_CleanupBudget())
            _empty_tree(job_fd, depth=0, budget=_CleanupBudget())

        try:
            os.rmdir(job_id, dir_fd=root_fd)
        except OSError as exc:
            raise _unavailable("run_dir_not_removed") from exc

    return True


def list_execution_run_directories(execution_run_root: Path) -> RunRootListing:
    """Kökün doğrudan çocuklarını **yalnız listeler**; hiçbir şey silmez.

    Primitive bilinçli olarak dardır: kökü :func:`remove_execution_run_directory`
    ile **aynı** biçim kontrollerinden geçirip descriptor-relative açar, girdi
    adlarını sınırlı bir ``os.scandir`` *iterator*'ından tüketir (kök hiçbir
    zaman tümüyle materialize edilmez) ve ancak tüketim bittikten sonra her adayı
    ``follow_symlinks=False`` ile bir kez ``stat`` eder. Hiçbir girdi
    **açılmaz**, hiçbir alt ağaca inilmez, hiçbir symlink izlenmez: bir
    bağlantının veya FIFO'nun hedefi bu fonksiyonda hiç dokunulmamış kalır.

    Sınır **tüketim sırasında** uygulanır (:data:`MAX_RUN_ROOT_ENTRIES`,
    :func:`_bounded_entry_names`): iterator'dan en fazla sınır ``+ 1`` ad
    okunur ve hiçbir girdi ``stat`` edilmeden fail-closed düşülür. Beklenmedik
    büyüklükteki bir kök tek tek incelenip "hangisini silelim" diye karara
    bağlanmaz; çağıran hiçbir aday görmediği için hiçbir şeye dokunamaz.

    Her aday, adının yanında listelenen **nesnenin**
    :class:`RunDirectoryIdentity`'sini de taşır. Ad tek başına bir kimlik
    değildir: aradan dizin kaldırılıp aynı adla yenisi oluşturulabilir ve bu iki
    nesne yalnız kimlikle ayırt edilir.

    Sınıflandırma iki kümelidir ve **ad ile tür birlikte** aranır: aday olmak
    için girdi hem canonical UUID4 adlı olmalı hem de gerçek bir dizin olmalıdır.
    Geri kalan her şey — canonical olmayan ad, canonical adlı bir symlink,
    normal dosya, FIFO, socket — yalnız *sayılır*: adı bile çağırana
    verilmez, çünkü bu primitive'in ürettiği tek şey sonraki adımın **silme
    hedefidir** ve beklenmeyen bir girdi hiçbir zaman hedef olmamalıdır.

    Args:
        execution_run_root: ``app-data/execution-runs`` kökü. Absolute, doğru
            adlı, önceden var olan, symlink olmayan ve 0700 bir dizin olmalıdır.

    Returns:
        Kökün o andaki :class:`RunRootListing` görüntüsü. Aday sırası
        deterministiktir (ada göre): sürücünün dizin sırasına bağlı bir sıra,
        aynı kökte iki çağrının farklı davranmasına yol açardı.

    Raises:
        RunnerEnvironmentError: Kök relative ise, adı beklenen kök adı değilse,
            yoksa/normal dizin değilse/0700 değilse; kök okunamıyorsa; ya da
            doğrudan girdi sayısı :data:`MAX_RUN_ROOT_ENTRIES`'i aşıyorsa.
    """
    _require_run_root_shape(execution_run_root)

    candidates: list[RunDirectoryEntry] = []
    unexpected = 0
    with _open_run_root(execution_run_root) as root_fd:
        for name in _bounded_entry_names(root_fd):
            try:
                status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                # Tolere edilen tek yarış: aradan gerçekten kaybolan bir girdi.
                # Böyle bir girdi ne aday ne de beklenmeyen sayılır; artık yok.
                continue
            except OSError as exc:
                raise _unavailable("execution_run_root_unavailable") from exc
            if _JOB_ID_PATTERN.fullmatch(name) is None or not stat.S_ISDIR(status.st_mode):
                unexpected += 1
                continue
            candidates.append(
                RunDirectoryEntry(
                    job_id=name,
                    modified_at=status.st_mtime,
                    identity=_identity_of(status),
                )
            )

    return RunRootListing(candidates=tuple(candidates), unexpected=unexpected)


def _bounded_entry_names(root_fd: int) -> list[str]:
    """Kökün doğrudan girdi adlarını sınırı **aşmadan** toplar.

    Sınır bir son-kontrol değil, tüketimin kendisidir: iterator'dan en fazla
    :data:`MAX_RUN_ROOT_ENTRIES` ``+ 1`` girdi okunur ve fazladan okunan o tek
    ad yalnız sınırın aşıldığını **kanıtlamak** içindir. Kökü önce tümüyle
    listeleyip sonra uzunluğuna bakmak, "sınırlı" olduğu iddia edilen bir
    yürüyüşün sınırsız yapılması olurdu: milyonlarca girdili bir kök, sınır
    kontrolü hiç çalışmadan belleğe alınırdı.

    Sınır aşıldığında **hiçbir girdi ``stat`` edilmemiştir**: sınıflandırma
    çağıranda, bu fonksiyon döndükten sonra başlar. Iterator her iki yolda da
    context manager tarafından kapatılır; açık kalan bir dizin akışı, kök
    descriptor'ı kapansa bile süreçte bir kaynak sızıntısı bırakırdı.

    ``os.scandir`` burada bir *iterator* olarak kullanılır: ``list(os.scandir())``
    ya da ``os.listdir`` aynı materialize etme sorununu geri getirirdi.
    Girdilerin yalnız **adı** okunur; ``DirEntry`` üzerinden tür sorulmaz, çünkü
    o yol symlink'i izleyen bir varsayılan taşır ve önbelleğe alınmış bir
    sonuç döndürebilir.
    """
    names: list[str] = []
    try:
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if len(names) >= MAX_RUN_ROOT_ENTRIES:
                    raise _unavailable("execution_run_root_too_many_entries")
                names.append(entry.name)
    except OSError as exc:
        raise _unavailable("execution_run_root_unavailable") from exc
    # Sıra deterministiktir: sürücünün dizin sırasına bağlı bir sıra, aynı kökte
    # iki çağrının farklı davranmasına yol açardı.
    return sorted(names)


def _discard_partial_run_directory(execution_run_root: Path, job_id: str) -> bool:
    """Yarım kalmış bir Job dizinini **aynı** primitive ile geri vermeyi dener.

    Ayrı bir silme yolu bilinçli olarak yoktur: hazırlığın arıza dalı da
    :func:`remove_execution_run_directory`'den geçer, yani descriptor-relative
    ilerler, symlink izlemez ve derinlik/girdi sınırlarına tabidir. İkinci bir
    "hızlı temizlik" biçimi, tam da denetlenen sınırın yanından dolaşan yol
    olurdu.

    Temizliğin **hiçbir** sıradan hatası dışarı sızmaz ve ``False``'a dönüşür.
    Dar bir exception tuple'ı bilinçli olarak kullanılmaz: sözleşme dışı bir
    ``RuntimeError`` yakalanmasaydı, hazırlığın asıl reddinin yerine geçer ve
    çağıran arızayı yanlış katmana yüklerdi. ``KeyboardInterrupt``/``SystemExit``
    ise yakalanmaz — süreci durdurulamaz hâle getirmemek için ``Exception``
    sınırında durulur.

    Returns:
        Dizin bu çağrıdan sonra gerçekten yoksa ``True``. ``False`` dönmek
        hazırlığın başarılı sayılamayacağı anlamına gelir; kararı çağıran verir.
    """
    try:
        remove_execution_run_directory(execution_run_root, job_id, missing_ok=True)
    except Exception:
        return False
    return True


# --- Çalışma alanı ----------------------------------------------------------


def _require_run_root_shape(execution_run_root: Path) -> None:
    """Kökün **biçimini** açmadan önce doğrular.

    Ad kontrolü bilinçlidir: kök adı sabit olmasaydı, çağıran herhangi bir
    dizini "execution run kökü" diye geçirip altına Job dizinleri açtırabilirdi.
    """
    _require_absolute(execution_run_root, "execution_run_root")
    if execution_run_root.name != EXECUTION_RUN_DIRNAME:
        raise _unavailable("execution_run_root_unexpected_name")


@contextlib.contextmanager
def _open_run_root(execution_run_root: Path) -> Iterator[int]:
    """Execution run kökünü güvenli biçimde açar ve niteliklerini doğrular.

    Kök **oluşturulmaz**: var olması `ensure_app_data_dirs`'in işidir. Kökün
    kendisi symlink ise ``O_NOFOLLOW`` açmayı ``ELOOP`` ile düşürür ve işlem
    fail-closed sonlanır.
    """
    try:
        root_fd = os.open(execution_run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _unavailable("execution_run_root_unavailable") from exc
    try:
        status = os.fstat(root_fd)
        if not stat.S_ISDIR(status.st_mode):
            raise _unavailable("execution_run_root_not_a_directory")
        # İzin **düzeltilmez**, doğrulanır: kökü burada 0700'e çekmek, yanlış
        # kurulmuş bir kurulumu sessizce kabul etmek olurdu.
        if stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
            raise _unavailable("execution_run_root_not_private")
        yield root_fd
    finally:
        os.close(root_fd)


def _create_job_directory(root_fd: int, job_id: str) -> None:
    """Job dizinini kök descriptor'ına göre **yeni** olarak oluşturur.

    ``exist_ok`` yoktur: aynı kimlikte duran bir girdi — dizin, symlink, FIFO,
    ne olursa olsun — hata üretir. Yeniden kullanmak, önceki bir çalıştırmadan
    kalan raw artifact ve geçici dosyaların yeni execution'ın sonucuna
    karışmasına izin verirdi.
    """
    try:
        os.mkdir(job_id, DIRECTORY_MODE, dir_fd=root_fd)
    except FileExistsError as exc:
        raise _unavailable("run_dir_already_exists") from exc
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc


@contextlib.contextmanager
def _open_job_directory(root_fd: int, job_id: str) -> Iterator[int]:
    """Yeni açılmış Job dizinini doğrulayarak açar.

    ``mkdir`` ile ``open`` arasında girdinin değiş-tokuş edilmediği, açılan
    descriptor ile isimdeki girdinin aynı nesne olduğu karşılaştırılarak
    kanıtlanır.
    """
    try:
        job_fd = os.open(job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc
    try:
        opened = os.fstat(job_fd)
        named = os.stat(job_id, dir_fd=root_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise _unavailable("run_dir_unavailable")
        # `mkdir` mode'u umask ile maskelenir; izin açıkça sabitlenir ve
        # ardından doğrulanır.
        os.fchmod(job_fd, DIRECTORY_MODE)
        _require_private_directory(job_fd)
        yield job_fd
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc
    finally:
        os.close(job_fd)


def _prepare_managed_directories(job_fd: int) -> None:
    """Sabit alt dizinleri Job descriptor'ına göre 0700 oluşturur ve doğrular."""
    for name in MANAGED_DIRNAMES:
        try:
            os.mkdir(name, DIRECTORY_MODE, dir_fd=job_fd)
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=job_fd)
        except OSError as exc:
            raise _unavailable("run_dir_unavailable") from exc
        try:
            os.fchmod(child_fd, DIRECTORY_MODE)
            _require_private_directory(child_fd)
        except OSError as exc:
            raise _unavailable("run_dir_unavailable") from exc
        finally:
            os.close(child_fd)


def _require_private_directory(dir_fd: int) -> None:
    """Açık descriptor gerçekten 0700 bir dizin mi."""
    status = os.fstat(dir_fd)
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
        raise _unavailable("run_dir_not_private")


# --- Çalışma alanının kaldırılması -------------------------------------------


class _CleanupBudget:
    """Tek bir geçişte dokunulan girdi sayısını sınırlar.

    Bütçe iş yapılırken tüketilir: sayım önce bitirilip sonra karar verilseydi,
    sınırın kendisi zaten sınırsız bir yürüyüşün ardından uygulanırdı.
    """

    def __init__(self) -> None:
        self.remaining = MAX_CLEANUP_ENTRIES

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise _unavailable("run_dir_too_many_entries")


@contextlib.contextmanager
def _open_tree_directory(parent_fd: int, name: str, expected: os.stat_result) -> Iterator[int]:
    """Ağaçtaki bir dizini, ``lstat`` ile görülen nesnenin **aynısı** olarak açar.

    ``O_NOFOLLOW`` bağlantının izlenmesini, ``O_DIRECTORY`` dizin olmayan bir
    girdinin açılmasını engeller; ``O_NONBLOCK`` ise araya konmuş bir FIFO'nun
    açılışı bloklamasını. Açılan descriptor yine de dev/ino ile karşılaştırılır:
    ``lstat`` ile ``open`` arasındaki pencerede yapılan bir değiş-tokuş ancak
    böyle görülür.
    """
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc
    try:
        opened = os.fstat(child_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise _unavailable("run_dir_unavailable")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise _unavailable("run_dir_unavailable")
        yield child_fd
    finally:
        os.close(child_fd)


def _scan_tree(dir_fd: int, *, depth: int, budget: _CleanupBudget) -> None:
    """Silmeden **önce** ağacın sınırların içinde kaldığını kanıtlar.

    Job dizininin kendisi derinlik ``0``'dır; en fazla :data:`MAX_CLEANUP_DEPTH`
    iç içe dizin kabul edilir. Bu geçiş hiçbir şey silmez, bu yüzden sınır
    aşımında ağaç olduğu gibi kalır.
    """
    for name, status in _entries(dir_fd, depth=depth, budget=budget):
        if not stat.S_ISDIR(status.st_mode):
            # Symlink, FIFO, socket: içine **inilmez**, hedefi açılmaz.
            continue
        with _open_tree_directory(dir_fd, name, status) as child_fd:
            _scan_tree(child_fd, depth=depth + 1, budget=budget)


def _empty_tree(dir_fd: int, *, depth: int, budget: _CleanupBudget) -> None:
    """Dizinin içeriğini descriptor-relative boşaltır; dizinin kendisini bırakır.

    Sınır burada da uygulanır: tarama ile silme arasında büyütülen bir ağaç,
    silmeyi sınırsız hâle getirmemelidir.
    """
    for name, status in _entries(dir_fd, depth=depth, budget=budget):
        if stat.S_ISDIR(status.st_mode):
            with _open_tree_directory(dir_fd, name, status) as child_fd:
                _empty_tree(child_fd, depth=depth + 1, budget=budget)
            _remove(os.rmdir, name, dir_fd)
            continue
        # Symlink'in **kendisi** kaldırılır; gösterdiği hedef hiç açılmaz.
        _remove(os.unlink, name, dir_fd)


def _entries(
    dir_fd: int, *, depth: int, budget: _CleanupBudget
) -> Iterator[tuple[str, os.stat_result]]:
    """Bir dizinin girdilerini sınırlar içinde, symlink izlemeden gezer."""
    if depth > MAX_CLEANUP_DEPTH:
        raise _unavailable("run_dir_too_deep")
    try:
        names = os.listdir(dir_fd)
    except OSError as exc:
        raise _unavailable("run_dir_unavailable") from exc
    for name in names:
        budget.consume()
        try:
            status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            # Tolere edilen tek yarış: aradan gerçekten kaybolan bir girdi.
            continue
        except OSError as exc:
            raise _unavailable("run_dir_unavailable") from exc
        yield name, status


def _remove(operation: Callable[..., None], name: str, dir_fd: int) -> None:
    """Tek bir girdiyi kaldırır; kaldıramamak sessizce geçilmez."""
    try:
        operation(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _unavailable("run_dir_not_removed") from exc


# --- Ansible yapılandırması --------------------------------------------------


def _select_ansible_config(
    *, job_fd: int, run_dir: Path, frozen_project_root: Path
) -> tuple[Path, bool]:
    """``ANSIBLE_CONFIG``'in göstereceği dosyayı seçer.

    Project'in kendi ``ansible.cfg``'si **normal bir dosyaysa** o kullanılır
    (ADR-022 Karar 3). Symlink veya özel dosya fail-closed reddedilir: bir
    symlink, dondurulmuş ağacın dışındaki bir dosyayı Ansible'a okutabilirdi ve
    dondurulan içerik ile gerçekte okunan içerik ayrışırdı.
    """
    try:
        project_fd = os.open(frozen_project_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _unavailable("frozen_project_unreadable") from exc
    try:
        try:
            status = os.stat(ANSIBLE_CONFIG_FILENAME, dir_fd=project_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _write_empty_config(job_fd, run_dir), False
        except OSError as exc:
            raise _unavailable("frozen_project_unreadable") from exc

        if stat.S_ISLNK(status.st_mode):
            raise _unavailable("ansible_config_symlink")
        if not stat.S_ISREG(status.st_mode):
            raise _unavailable("ansible_config_not_regular")
    finally:
        os.close(project_fd)

    return frozen_project_root / ANSIBLE_CONFIG_FILENAME, True


def _write_empty_config(job_fd: int, run_dir: Path) -> Path:
    """Çalışma alanına 0600 izinli, **boş** bir ``ansible.cfg`` yazar.

    Project'in config'i yokken ``ANSIBLE_CONFIG`` boş bırakılmaz: Ansible o
    durumda cwd, ev dizini ve ``/etc/ansible/ansible.cfg`` üzerinden keşif
    yapar ve çalıştırma, dondurulmuş içeriğin dışındaki bir yapılandırmaya
    bağlanırdı.

    ``O_EXCL`` kullanılır: Job dizini bu çağrıda oluşturulduğu için dosya
    olamaz, varsa bir şey yanlıştır ve üzerine yazılmaz.
    """
    try:
        descriptor = os.open(
            ANSIBLE_CONFIG_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            FILE_MODE,
            dir_fd=job_fd,
        )
    except OSError as exc:
        raise _unavailable("ansible_config_unwritable") from exc

    try:
        with os.fdopen(descriptor, "wb") as handle:
            # Dosya açıldıktan **sonra** yeniden doğrulanır: `lstat` ile açma
            # arasında yapılan bir değiş-tokuş ancak burada yakalanır.
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _unavailable("ansible_config_not_regular")
            # `open` mode'u umask ile maskelenir; izin açıkça sabitlenir.
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _unavailable("ansible_config_unwritable") from exc
    return run_dir / ANSIBLE_CONFIG_FILENAME


# --- Ortak ------------------------------------------------------------------


def _require_absolute(path: Path, field: str) -> None:
    """Relative path sürecin çalışma dizinine göre çözülür; kabul edilmez."""
    if not path.is_absolute():
        raise _unavailable(f"{field}_not_absolute")


def _unavailable(reason: str) -> RunnerEnvironmentError:
    """Ortak, sızdırmayan altyapı hatası."""
    return RunnerEnvironmentError(
        "Runner çalışma ortamı hazırlanamadı.", details={"reason": reason}
    )
