"""Doğrulanmış execution girdisi ve tek atımlık playbook executor'ı.

R1-V3C1C2A (:func:`prepare_execution_inputs`) ve R1-V3C1C2B2B
(:func:`execute_next_playbook_job`).

Modülün iki yarısı vardır ve ikisi de **kendiliğinden bir execution yolu
açmaz**: burada polling döngüsü, arka plan worker'ı, startup recovery, janitor,
public endpoint, UI ve iptal yoktur. :func:`execute_next_playbook_job` yalnız
açıkça çağrıldığında **en fazla bir** Job işler.

R1-V3C2C'den beri onu tekrarlayan bir çağıran vardır
(:mod:`app.services.execution.worker`) ama sözleşme değişmemiştir: worker
varsayılan olarak **kapalıdır** ve bu fonksiyonu senkron, teker teker çağırır.

R1-V3D1'den beri public bir route da vardır
(``POST /api/projects/{project_id}/executions``); sınır yine de aynı yerdedir.
O route yalnız ``pending`` bir Job **rezerve eder**: bu modülü ne doğrudan
çağırır, ne worker'ı uyandırır, ne de onun açık olup olmadığına bakar. Bu
fonksiyonu çağıran tek yol açıkça açılmış worker'dır; worker kapalıyken istek
yine ``201`` alır ve Job kuyrukta bekler.

Alt yarı — girdi hazırlığı (R1-V3C1C2A) — hiçbir şey çalıştırmaz: veritabanına
dokunmaz, Job durumu değiştirmez, alt süreç açmaz, run directory veya
``known_hosts`` üretmez, artifact yazmaz. Yaptığı tek iş, elinde
:class:`AcquiredPlaybookJob` bulunan çağırana **yalnız dondurulmuş
workspace'ten türetilmiş, doğrulanmış ve değişmez** bir girdi vermektir.

Sıra bilinçlidir ve tersine çevrilemez::

    manifest yeniden doğrulaması (diskteki gerçek baytlar)
    → path türetme (kök + opaque workspace_id + sabit adlar)
    → dondurulmuş snapshot'ın okunması
    → host listesinin yeniden doğrulanması
    → private key yollarının **execution anındaki** allowlist'e karşı yeniden
      doğrulanması
    → redaction için bağlantı değerlerinin çıkarılması

Bütünlük doğrulaması **başta** durur: bozulmuş bir workspace'ten host adı veya
key yolu okuyup sonra manifesti kontrol etmek, reddedilecek içeriğe önce
davranış bağlamak olurdu.

**Özgün project ağacı ve özgün inventory dosyası hiçbir aşamada açılmaz.**
Onaylanan içerik dondurulmuş kopyadır; hazırlamadan sonra özgün dosyaların
değişmesi, silinmesi veya symlink'e dönüşmesi buradaki sonucu etkilemez.

**Çağıran path veremez.** Fonksiyon project veya inventory yolu parametresi
almaz; ikisi de aynı workspace dizininden sabit adlarla türetilir. Serbest bir
path parametresi, bir planın project'ini başka bir planın inventory'siyle
çalıştırmanın (veya workspace dışına çıkmanın) tek satırlık yolu olurdu.

**TOCTOU sınırı.** Manifest doğrulaması bir *içerik* doğrulamasıdır ve
descriptor-relative yapılır; ancak doğrulama bittiğinde descriptor'lar kapanır
ve Runner argv'deki yolları daha sonra kendisi yeniden açar. Dolayısıyla
hostile bir concurrent writer'a karşı "TOCTOU tamamen yoktur" garantisi
**verilmez**. Sözleşme, uygulamanın sahip olduğu, güvenilir ve dondurulmuş
workspace'tir (runner_process ile aynı sözleşme). Bu yüzden executor bu
fonksiyonu süreci başlatmadan **hemen önce** çağırır: doğrulama ile çalıştırma
arasındaki pencere ne kadar dar olursa o kadar iyidir, ama sıfır olduğu iddia
edilmez.

Üst yarı — tek atımlık executor (R1-V3C1C2B2B) — hazır primitive'leri sabit bir
sırayla birbirine bağlar ve **kendisi hiçbir güvenlik kararını yeniden
yorumlamaz**: argv üretmez, environment'a anahtar eklemez, path çözmez, hata
kodu icat etmez::

    acquire (kısa ömürlü Session)
    → execution girdisi (yalnız dondurulmuş workspace)
    → known-hosts + runner environment (run directory burada doğar)
    → artifact dizininin rezervasyonu (henüz DB'ye yazılmaz)
    → lease gözlemcisi + gerçek `ansible-runner` child process'i
    → kira sonucu (kaybedilmiş kira kısmi çıktıyı yayımlatmaz)
    → normalize
    → run directory'nin kaldırılması
    → artifact'ın atomik yayımı
    → terminal DB geçişi (yeni ve kısa ömürlü Session)

**Session sözleşmesi.** Fonksiyon hazır bir :class:`~sqlalchemy.orm.Session`
kabul etmez, bir *factory* alır. Sebep somut: child process saatlerce
çalışabilir ve o süre boyunca açık tutulan tek bir session, bütün çalıştırma
boyunca bir bağlantıyı (SQLite'ta bir yazma kilidini) elde tutardı. Bu yüzden
acquire kendi session'ını açıp **kapatır**, her heartbeat kendi session'ını
açıp kapatır ve terminal geçiş **yeni** bir session'da yapılır. Child
çalışırken bu süreçte açık hiçbir transaction bulunmaz.

**Sıra tersine çevrilemez.** Kira kaybedilmişse normalize edilmiş çıktı
yayımlanmaz: kirasını kaybetmiş bir worker'ın sonucu, satırı devralmış olabilecek
başka bir worker'ın sonucunun üstüne yazılırdı. Run directory artifact
yayımlanmadan **önce** kaldırılır ve kaldırılamıyorsa başarı ilan edilmez:
geride kalan bir çalışma alanı, sonraki bir çalıştırmanın "aynı kimlikte girdi
var" diye düşmesi demektir ve bunu başarı sayarak gizlemek, hatayı ilk
görülebildiği yerden kaldırırdı. Yayımlanmış bir ``result.json`` ise sonraki
hiçbir arızada silinmez — görünür bir sonucu geri almak, kullanıcının gördüğü
kaydı yok etmek olurdu.

**Artifact rezervasyonu da failure-atomic'tir (R1-V3C1C2-AUDIT-FIX1).**
Rezervasyondan sonra ``result.json`` yayımlanmadan biten **her** yol — sözleşme
içi arızalar, normalize sırasında yükselen beklenmeyen bir istisna, hatta
``KeyboardInterrupt`` — dizini en iyi çabayla geri verir. Sınır tek bir cümlede
durur: *yayımlanmamış* olan toplanır, *yayımlanmış* olan korunur; ikincisini
deponun kendisi reddederek garanti eder.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import JobStatus
from app.services.ansible.inventory_snapshot import (
    InventoryUnsafeError,
    revalidate_snapshot_private_keys,
    snapshot_connection_values,
    snapshot_host_names,
)
from app.services.ansible.process import (
    BoundedProcessObserver,
    CompositeProcessObserver,
)
from app.services.ansible.ssh import SSHPolicyUnavailableError, prepare_known_hosts
from app.services.execution.job_state import (
    AcquiredPlaybookJob,
    AcquireOutcome,
    acquire_pending_playbook_job,
    finish_playbook_job,
)
from app.services.execution.lease import PlaybookLeaseObserver
from app.services.execution.normalize import OUTCOME_SUCCESSFUL, normalize_runner_output
from app.services.execution.runner_env import (
    RunnerEnvironment,
    RunnerEnvironmentError,
    build_runner_environment,
    remove_execution_run_directory,
)
from app.services.execution.runner_process import (
    RunnerProcessError,
    RunnerProcessLimits,
    run_playbook_process,
)
from app.services.execution.workspace import (
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    read_frozen_inventory,
    verify_frozen_workspace,
    workspace_inventory_path,
    workspace_project_root,
)
from app.services.jobs.artifacts import JobArtifactStore, JobArtifactUnavailableError

# Absolute yolda kabul edilmeyen parçalar. `..` ile kurulmuş bir alias, aynı
# dizini iki farklı workspace gibi gösterebilir; runner_process aynı kontrolü
# kendi tarafında da uygular ve türetilen yolların oradan geçmesi gerekir.
_REJECTED_SEGMENTS = frozenset({"", ".", ".."})

#: Her çağrıda **yeni** bir session üreten çağrılabilir (lease ile aynı tür).
SessionFactory = Callable[[], Session]

# Executor'ın yazabileceği hata kodları. Hepsi `job_state.FINISH_ERROR_CODES`
# üyesidir; bu modül **yeni kod icat etmez**. Normalize edilmiş bir sonucun
# kendi kodu (`runner_timeout`, `runner_output_invalid`, `result_limit_exceeded`,
# `runner_no_hosts`, `runner_failed`) buradan geçmez, olduğu gibi taşınır.
ERROR_WORKSPACE_UNAVAILABLE = "workspace_unavailable"
ERROR_WORKSPACE_INTEGRITY_FAILED = "workspace_integrity_failed"
ERROR_RUNNER_START_FAILED = "runner_start_failed"
ERROR_RUNNER_FAILED = "runner_failed"

# `RunnerProcessError` sebeplerinden **dondurulmuş içeriğin düzenine** ait
# olanlar: playbook dondurulmuş ağaçta yok ya da güvenli bir yol değil, project
# ile inventory aynı workspace'e bağlı değil. Sebep workspace olduğu için kod da
# `workspace_integrity_failed`'dır.
#
# Geri kalan bütün sebepler — başlatma arızası, run/raw dizininin kurulamaması,
# kimlik tutarsızlığı — `runner_start_failed`'dır. Bilinmeyen bir sebep de oraya
# düşer: çalıştırmanın başladığına dair bir kanıt yokken sonucu workspace'e
# yüklemek, yanlış yeri işaret eden bir teşhis olurdu.
_WORKSPACE_LAYOUT_REASONS = frozenset(
    {
        "frozen_workspace_binding_invalid",
        "playbook_path_empty",
        "playbook_path_absolute",
        "playbook_path_unsafe_segment",
        "playbook_not_regular_file",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class PreparedExecutionInputs:
    """Bir çalıştırmanın doğrulanmış, değişmez girdisi.

    Bu nesne **internal bir process context'idir**: yalnız süreç boyunca
    bellekte yaşar. Loglanmaz, veritabanına yazılmaz, artifact'e serialize
    edilmez ve API cevabına dönüşmez. ``connection_values`` adres, port,
    kullanıcı adı, private key yolu ve interpreter gibi değerleri taşır —
    redaction'ın maskeleyeceği değerlerin kendisidir; kalıcılaştırılması, planın
    bilinçli olarak dışarı vermediği hostvar değerlerini kalıcı hâle getirirdi.

    ``__repr__`` bu yüzden alanları **basmaz**: ``logger.info("%s", inputs)``
    gibi tek bir kazayla bağlantı değerlerinin log'a düşmesi, sözleşmenin
    dokümantasyonla değil davranışla korunmasını gerektirir.
    """

    #: Bu girdinin ait olduğu, acquire edilmiş Job.
    job: AcquiredPlaybookJob
    #: ``<workspace>/project`` — dondurulmuş project kökü.
    frozen_project_root: Path
    #: Aynı workspace'in ``inventory/hosts.yml`` dosyası.
    frozen_inventory_path: Path
    #: Dondurulmuş snapshot metni (uygulamanın kendi ürettiği JSON).
    inventory_snapshot: str
    #: Snapshot'tan yeniden doğrulanmış, ada göre sıralı host adları.
    inventory_hosts: tuple[str, ...]
    #: Redaction'da maskelenecek bağlantı değerleri; uzun değerler önce.
    connection_values: tuple[str, ...]

    def __repr__(self) -> str:
        """Alan taşımayan sabit gösterim.

        Job kimliği bile yazılmaz: sabit bir metin, "hangi alan güvenli"
        tartışmasını tümüyle ortadan kaldırır.
        """
        return f"<{type(self).__name__}>"


def prepare_execution_inputs(
    job: AcquiredPlaybookJob,
    *,
    workspace_root: Path,
    key_roots: Sequence[Path],
) -> PreparedExecutionInputs:
    """Acquire edilmiş Job için doğrulanmış execution girdisini üretir.

    Fonksiyon yalnız **okur ve doğrular**: veritabanına dokunmaz, Job durumunu
    değiştirmez, alt süreç başlatmaz, dizin veya dosya oluşturmaz.

    Args:
        job: :func:`~app.services.execution.job_state.acquire_pending_playbook_job`
            tarafından üretilmiş, bu worker'a geçmiş Job bağlamı. Dondurulmuş
            içeriğe erişim ``job.workspace_id`` üzerinden çözülür ve beklenen
            digest ``job.manifest_digest``'tir.
        workspace_root: ``app-data/execution-plans`` kökünün absolute yolu.
            Kök çalışma anındaki ayarlardan gelir; Job'a yazılmış değildir.
        key_roots: **Execution anında** etkin olan SSH private-key root
            allowlist'i. Preview anındaki doğrulama burada kalıcı garanti
            sayılmaz: allowlist daralmışsa veya anahtar taşınmışsa çalıştırma
            reddedilmelidir.

    Returns:
        Yalnız bellekte kullanılacak, değişmez :class:`PreparedExecutionInputs`.

    Raises:
        WorkspaceUnavailableError: Kök absolute değilse veya alias parça
            içeriyorsa, workspace yoksa, adı geçersizse ya da güvenli biçimde
            açılamıyorsa.
        WorkspaceIntegrityError: Dondurulmuş içerik, izinler veya manifest
            dosyası onaylandığı andaki hâlinden farklıysa.
        InventoryUnsafeError: Dondurulmuş snapshot beklenen dar yapıda değilse,
            boşsa, bir host adı gösterim sözleşmesini karşılamıyorsa ya da bir
            private key yolu execution anındaki allowlist'in dışındaysa.
    """
    root = _require_safe_root(workspace_root)

    # 1. Bütünlük **önce**: digest diskteki gerçek baytlardan yeniden
    #    hesaplanır. Bu adım geçmeden aşağıdaki hiçbir içerik okunmaz.
    verify_frozen_workspace(root, job.workspace_id, expected_digest=job.manifest_digest)

    # 2. Yollar yalnız kök + opaque workspace_id + sabit adlardan türetilir.
    frozen_project_root = workspace_project_root(root, job.workspace_id)
    frozen_inventory_path = workspace_inventory_path(root, job.workspace_id)

    # 3-4. Snapshot dondurulmuş kopyadan okunur; yapısı ve host adları yeniden
    #      doğrulanır. Boş veya malformed snapshot fail-closed reddedilir.
    inventory_snapshot = read_frozen_inventory(root, job.workspace_id)
    inventory_hosts = snapshot_host_names(inventory_snapshot)

    # 5. Key yolları execution anındaki allowlist'e karşı yeniden doğrulanır.
    revalidate_snapshot_private_keys(inventory_snapshot, key_roots=key_roots)

    # 6. Redaction girdisi: bu değerler yalnız bellekte taşınır.
    connection_values = snapshot_connection_values(inventory_snapshot)

    return PreparedExecutionInputs(
        job=job,
        frozen_project_root=frozen_project_root,
        frozen_inventory_path=frozen_inventory_path,
        inventory_snapshot=inventory_snapshot,
        inventory_hosts=inventory_hosts,
        connection_values=connection_values,
    )


def _require_safe_root(workspace_root: Path) -> Path:
    """Workspace kökünün absolute ve alias'sız olduğunu doğrular.

    Relative bir kök sürecin çalışma dizinine göre çözülürdü; ``.``/``..``
    taşıyan bir kök ise aynı dizini iki farklı metinle temsil ederdi ve
    türetilen yollar runner_process'in düzen kontrolünden geçemezdi.

    Kökün kendisi hata mesajına **yazılmaz**.
    """
    if not workspace_root.is_absolute():
        raise WorkspaceUnavailableError("Execution workspace kökü geçersiz.")
    if any(part in _REJECTED_SEGMENTS for part in workspace_root.parts):
        raise WorkspaceUnavailableError("Execution workspace kökü geçersiz.")
    return workspace_root


# --- Tek atımlık executor (R1-V3C1C2B2B) -------------------------------------


class ExecutionOutcome(StrEnum):
    """Bir :func:`execute_next_playbook_job` çağrısının dört olası sonucu."""

    #: Alınacak ``pending`` PLAYBOOK Job'ı yok ya da yarışı başka worker kazandı.
    #: Bu yolda süreç, dosya sistemi ve artifact **hiç** ele alınmaz.
    IDLE = "idle"
    #: Aday bulundu ama plan bağı geçersizdi; acquire onu terminal `failed`
    #: yaptı. Child yine başlatılmaz.
    BINDING_INVALID = "binding_invalid"
    #: Job bu worker tarafından terminal duruma yazıldı.
    FINISHED = "finished"
    #: Sonuç hazırdı ama terminal geçiş hiçbir satırı etkilemedi: satır artık bu
    #: worker'ın değil. Sonuç **yeniden yazılmaya çalışılmaz**.
    OWNERSHIP_LOST = "ownership_lost"


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """Bir çalıştırma denemesinin değişmez, dışarı verilebilir özeti.

    Nesne bilinçli olarak **taşımadıkları** ile tanımlanır: plan token'ı, token
    veya manifest digest'i, workspace/run/artifact yolu, raw stdout/stderr,
    inventory host'ları ve bağlantı değerleri burada bulunmaz. Taşıdığı dört
    alanın hepsi zaten Job satırında duran ya da sabit sözlükten gelen
    değerlerdir, bu yüzden ``repr`` de güvenlidir ve alanları basar: gizlenecek
    bir şey yoktur, çünkü hiç alınmamıştır.
    """

    outcome: ExecutionOutcome
    #: Yalnız bir Job gerçekten ele alındıysa doludur.
    job_id: str | None = None
    #: Yalnız :attr:`ExecutionOutcome.FINISHED` sonucunda doludur.
    status: JobStatus | None = None
    #: Yalnız ``failed`` bir sonuçta doludur; sabit sözlükten gelir.
    error_code: str | None = None

    def __post_init__(self) -> None:
        """Alan kombinasyonlarını sonucun kendisiyle tutarlı tutar.

        Doğrulama bilinçlidir: "başarılı ama hata kodu taşıyan" veya "idle ama
        Job kimliği taşıyan" bir sonuç, okuyan tarafın hangi alana bakacağını
        belirsizleştirirdi.
        """
        handled = self.outcome in (ExecutionOutcome.FINISHED, ExecutionOutcome.OWNERSHIP_LOST)
        if (self.job_id is not None) is not handled:
            raise ValueError("Job kimliği yalnız ele alınmış bir denemede bulunur.")
        if (self.status is not None) is not (self.outcome is ExecutionOutcome.FINISHED):
            raise ValueError("Terminal durum yalnız bitirilmiş bir denemede bulunur.")
        if self.error_code is not None and self.status is not JobStatus.FAILED:
            raise ValueError("Hata kodu yalnız başarısız bir sonuçta bulunur.")


_IDLE_ATTEMPT = ExecutionAttempt(ExecutionOutcome.IDLE)
_BINDING_INVALID_ATTEMPT = ExecutionAttempt(ExecutionOutcome.BINDING_INVALID)


@dataclass(frozen=True, slots=True)
class _Decision:
    """Terminal geçişe verilecek, dosya sistemi işi **bitmiş** sonuç.

    Bu nesne üretildiğinde run directory kaldırılmış, artifact ya yayımlanmış
    ya da hiç yayımlanmamıştır. Böylece terminal ``UPDATE`` tek bir yerde ve tek
    bir biçimde yapılır; "hangi yolda hangi alanları yazıyorduk" sorusu ortadan
    kalkar.
    """

    status: JobStatus
    return_code: int | None
    error_code: str | None
    artifact_path: str | None
    result_truncated: bool


def execute_next_playbook_job(
    *,
    session_factory: SessionFactory,
    settings: Settings,
    worker_id: str,
    lifecycle_observer: BoundedProcessObserver | None = None,
) -> ExecutionAttempt:
    """En fazla **bir** ``pending`` PLAYBOOK Job'ını alıp gerçekten çalıştırır.

    Fonksiyon bir döngü **değildir**: tek bir denemede en çok bir Job işler ve
    döner. Onu tekrarlayan bir worker, zamanlayıcı veya endpoint bu dilimde
    yoktur.

    Sıra ve gerekçeleri modül docstring'indedir. Kısaca: acquire kendi kısa
    ömürlü session'ında yapılır ve kapatılır; child çalışırken bu süreçte açık
    hiçbir transaction bulunmaz; kira kaybedilmişse kısmi çıktı yayımlanmaz;
    run directory artifact yayımlanmadan önce kaldırılır; terminal geçiş yeni ve
    kısa ömürlü bir session'da yapılır.

    Args:
        session_factory: Her çağrıda **yeni** bir
            :class:`~sqlalchemy.orm.Session` üreten çağrılabilir. Hazır bir
            session bilinçli olarak kabul edilmez: saatlerce sürebilen bir
            çalıştırma boyunca açık tutulan session, bütün çalıştırma boyunca
            bir bağlantıyı (SQLite'ta bir yazma kilidini) elde tutardı.
        settings: Doğrulanmış ayarlar. Bütün kökler, komut, sınırlar ve kira
            süreleri buradan gelir; fonksiyon hiçbirini yeniden yorumlamaz.
        worker_id: Bu sürecin ömrü boyunca sabit kalan canonical UUID4 worker
            kimliği. Sahiplik kanıtı budur.
        lifecycle_observer: Child process çalışırken **pipe dışında** bir sınırı
            uygulayan isteğe bağlı gözlemci (R1-V3C2C'de worker'ın kapanış
            gözlemcisi). Tür bilinçli olarak generic
            :class:`~app.services.ansible.process.BoundedProcessObserver`'dır:
            executor gözlemcinin *neyi* izlediğini bilmez ve bilmemelidir.
            Verilirse Job kirasını yenileyen gözlemciyle **birlikte** çalışır —
            biri diğerinin yerine geçmez — ve süreç katmanının kendi raw bütçe
            gözlemcisi de zincirde kalır. Varsayılan ``None`` mevcut tek atımlık
            çağrıların yolunu birebir korur.

    Returns:
        Denemenin :class:`ExecutionAttempt` özeti.

    Raises:
        ValueError: ``worker_id`` canonical UUID4 değilse veya kira/heartbeat
            ayarları geçersizse. Bu yolda veritabanına dokunulmaz.
        SQLAlchemyError: Acquire, heartbeat dışı bir veritabanı işlemi veya
            terminal geçiş arıza verirse. Hata **yutulmaz**: bir disk veya
            bağlantı arızasını "iş yok" diye okumak, kuyruğun sessizce durmasına
            yol açardı. Yayımlanmış bir artifact bu yolda da korunur ve run
            directory kaldırılmış olur.
    """
    with contextlib.closing(session_factory()) as session:
        acquired = acquire_pending_playbook_job(
            session,
            worker_id=worker_id,
            lease_seconds=settings.playbook_worker_lease_seconds,
        )

    if acquired.outcome is AcquireOutcome.IDLE:
        return _IDLE_ATTEMPT
    if acquired.outcome is AcquireOutcome.BINDING_INVALID:
        return _BINDING_INVALID_ATTEMPT

    job = acquired.context
    if job is None:  # pragma: no cover - `AcquireResult` invariantı garanti eder
        raise ValueError("Acquire edilmiş sonuç bağlam taşımalıdır.")

    try:
        decision = _produce_decision(
            job,
            settings=settings,
            session_factory=session_factory,
            lifecycle_observer=lifecycle_observer,
        )
    except BaseException:
        # Beklenmeyen arıza Job'ı sessizce `running` bırakamaz: kirası dolana
        # kadar kuyruk tıkalı kalırdı. Terminalize etme **en iyi çaba**dır ve
        # kendi hatası asıl hatayı gölgelemez; asıl hata olduğu gibi yükselir.
        _terminalize_quietly(job, session_factory=session_factory)
        raise

    return _commit_decision(decision, job, session_factory=session_factory)


def _produce_decision(
    job: AcquiredPlaybookJob,
    *,
    settings: Settings,
    session_factory: SessionFactory,
    lifecycle_observer: BoundedProcessObserver | None,
) -> _Decision:
    """Çalıştırmayı yürütür ve terminal geçişe hazır sonucu üretir.

    Veritabanına **dokunmaz** (heartbeat'ler gözlemcinin kendi session'larında
    döner): bu fonksiyonun ürünü bir karardır, kaydın kendisi değil.
    """
    try:
        inputs = prepare_execution_inputs(
            job,
            workspace_root=settings.resolve_execution_plan_dir(),
            key_roots=settings.resolve_ssh_key_root_allowlist(),
        )
    except WorkspaceUnavailableError:
        return _failure(ERROR_WORKSPACE_UNAVAILABLE)
    except (WorkspaceIntegrityError, InventoryUnsafeError):
        # Dondurulmuş içerik değişmiş ya da bir private key execution anındaki
        # allowlist'in dışına çıkmış. İkisi de "onaylanan içerik artık bu değil"
        # demektir ve child **hiç** başlamaz.
        return _failure(ERROR_WORKSPACE_INTEGRITY_FAILED)

    try:
        environment = build_runner_environment(
            execution_run_root=settings.resolve_execution_run_dir(),
            job_id=job.job_id,
            frozen_project_root=inputs.frozen_project_root,
            ssh_policy=settings.ssh_host_key_policy,
            known_hosts=prepare_known_hosts(
                settings.app_data_dir, settings.resolve_ssh_known_hosts_path()
            ),
        )
    except (SSHPolicyUnavailableError, RunnerEnvironmentError, ValueError):
        # Child'ın yaslanacağı alan kurulamadı; hiçbir şey başlamadı.
        return _failure(ERROR_RUNNER_START_FAILED)

    # Bu noktadan sonra diskte **bu Job'a ait bir çalışma alanı vardır** ve
    # hangi yoldan çıkılırsa çıkılsın kaldırılmalıdır.
    run_directory = _RunDirectory(settings.resolve_execution_run_dir(), job.job_id)
    store = JobArtifactStore(settings.app_data_dir)
    try:
        try:
            store.create(job.job_id)
        except JobArtifactUnavailableError:
            # Sonucun konacağı yer hazırlanamıyorsa child hiç başlatılmaz:
            # yayımlanamayacak bir çıktı üretmek boşuna bir çalıştırma olurdu.
            # Rezervasyon **yarım** kalmış olabilir (dizin açıldıktan sonra
            # düşen bir `create`), bu yüzden temizlik burada da denenir.
            _discard_artifact_directory(store, job.job_id)
            return _failure(ERROR_RUNNER_FAILED)

        return _run_child(
            job,
            inputs=inputs,
            environment=environment,
            settings=settings,
            session_factory=session_factory,
            run_directory=run_directory,
            store=store,
            lifecycle_observer=lifecycle_observer,
        )
    except BaseException:
        # Beklenmeyen bir arıza (`create`'in sözleşme dışı bir hatası, normalize
        # sırasında yükselen bir `RuntimeError`, `KeyboardInterrupt` ...)
        # rezerve edilmiş ama **yayımlanmamış** bir artifact dizini bırakırdı.
        # Depo yayımlanmış bir `result.json`'u zaten silmeyi reddeder, bu yüzden
        # görünür bir sonuç bu yolda da korunur. İstisna olduğu gibi yükselir ve
        # `execute_next_playbook_job` Job'ı terminal yapar.
        _discard_artifact_directory(store, job.job_id)
        raise
    finally:
        # Başarı, hata, timeout ve beklenmeyen exception dâhil **her** yolda en
        # az bir kez denenir. Mutlu yolda çoktan kaldırılmıştır ve bu çağrı
        # no-op'tur; ikincil bir temizlik hatası asıl hatayı gölgelemez.
        run_directory.remove()


def _run_child(
    job: AcquiredPlaybookJob,
    *,
    inputs: PreparedExecutionInputs,
    environment: RunnerEnvironment,
    settings: Settings,
    session_factory: SessionFactory,
    run_directory: _RunDirectory,
    store: JobArtifactStore,
    lifecycle_observer: BoundedProcessObserver | None,
) -> _Decision:
    """Gerçek child process'i çalıştırır, sonucu güvenli hâle getirir.

    Kira gözlemcisi süreçle **birlikte** yaşar: onu başlatan ve süreç sonlandığı
    anda durduran taraf süreç katmanıdır. Buradaki ``stop`` yalnız hiç
    başlatılamamış bir çalıştırmayı da kapatan idempotent bir güvencedir.

    Dışarıdan bir yaşam döngüsü gözlemcisi geldiğinde kira gözlemcisi **yerini
    ona bırakmaz**: ikisi bir :class:`CompositeProcessObserver`'da birlikte
    çalışır. Süreç katmanı da kendi raw bütçe gözlemcisini bu zincire ekler,
    yani üç gözlemcinin hiçbiri diğerini devre dışı bırakmaz. Kira sonucu yine
    kira gözlemcisinin **kendi** bayraklarından okunur; bileşik gözlemcinin
    varlığı o okumayı değiştirmez.
    """
    lease = PlaybookLeaseObserver(
        session_factory=session_factory,
        job_id=job.job_id,
        worker_id=job.worker_id,
        heartbeat_seconds=settings.playbook_worker_heartbeat_seconds,
        lease_seconds=settings.playbook_worker_lease_seconds,
    )
    observer: BoundedProcessObserver = (
        lease if lifecycle_observer is None else CompositeProcessObserver(lease, lifecycle_observer)
    )
    try:
        process = run_playbook_process(
            command=settings.ansible_runner_command,
            runner_environment=environment,
            job_id=job.job_id,
            frozen_project_root=inputs.frozen_project_root,
            frozen_inventory_path=inputs.frozen_inventory_path,
            playbook_path=job.playbook_path,
            # Kip acquire'ın doğrulayıp bağladığı `AcquiredPlaybookJob.mode`'dan
            # gelir; ayardan, request'ten veya bir sabitten yeniden üretilmez
            # (R1-V3H1B2B).
            mode=job.mode,
            limits=RunnerProcessLimits.from_settings(settings),
            observer=observer,
        )
    except RunnerProcessError as error:
        _discard_artifact_directory(store, job.job_id)
        return _failure(_runner_error_code(error))
    finally:
        # Bileşik gözlemci hiç başlatılmamışsa bu çağrı no-op'tur; başlatılmışsa
        # süreç katmanı onu zaten durdurmuştur ve ikinci ``stop`` idempotenttir.
        observer.stop()

    # Kira **önce** sorulur. Kaybedilmiş bir kirada elde kalan çıktı kısmidir:
    # süreç supervisor tarafından kesilmiştir ve satır bu arada başka bir
    # worker'a geçmiş olabilir. Böyle bir çıktıyı normalize edip yayımlamak,
    # devralan worker'ın sonucunun üstüne yazmanın yolu olurdu.
    lease_sound = not (lease.lease_lost or lease.heartbeat_failed)

    normalized = (
        normalize_runner_output(
            job_id=job.job_id,
            stdout_text=process.stdout_text,
            return_code=process.return_code,
            timed_out=process.timed_out,
            oversized_stream=process.oversized_stream,
            raw_limit_exceeded=process.raw_limit_exceeded,
            known_hosts=inputs.inventory_hosts,
            connection_values=inputs.connection_values,
            max_events=settings.playbook_runner_max_events,
            max_result_bytes=settings.playbook_runner_max_result_bytes,
        )
        if lease_sound
        else None
    )

    # Çalışma alanı artifact yayımlanmadan ve Job terminal yapılmadan önce
    # kaldırılır. Süreç bu noktada tümüyle reap edilmiştir.
    removed = run_directory.remove()

    if normalized is None or not removed:
        _discard_artifact_directory(store, job.job_id)
        return _failure(ERROR_RUNNER_FAILED)

    try:
        artifact_path = store.write_result(job.job_id, normalized.to_document())
    except JobArtifactUnavailableError:
        _discard_artifact_directory(store, job.job_id)
        return _failure(ERROR_RUNNER_FAILED)

    if normalized.outcome == OUTCOME_SUCCESSFUL:
        return _Decision(
            status=JobStatus.SUCCESSFUL,
            return_code=normalized.return_code,
            error_code=None,
            artifact_path=artifact_path,
            result_truncated=False,
        )
    return _Decision(
        status=JobStatus.FAILED,
        return_code=normalized.return_code,
        error_code=normalized.error_code,
        artifact_path=artifact_path,
        result_truncated=normalized.result_truncated,
    )


def _commit_decision(
    decision: _Decision, job: AcquiredPlaybookJob, *, session_factory: SessionFactory
) -> ExecutionAttempt:
    """Sonucu **yeni** ve kısa ömürlü bir session'da terminal duruma yazar."""
    with contextlib.closing(session_factory()) as session:
        written = finish_playbook_job(
            session,
            job_id=job.job_id,
            worker_id=job.worker_id,
            status=decision.status,
            return_code=decision.return_code,
            error_code=decision.error_code,
            artifact_path=decision.artifact_path,
            result_truncated=decision.result_truncated,
        )

    if not written:
        # Satır artık bu worker'ın değil. Sonucu yeniden yazmaya çalışmak,
        # devralan tarafın kaydını ezmek olurdu; yayımlanmış artifact ise
        # silinmez.
        return ExecutionAttempt(ExecutionOutcome.OWNERSHIP_LOST, job_id=job.job_id)

    return ExecutionAttempt(
        ExecutionOutcome.FINISHED,
        job_id=job.job_id,
        status=decision.status,
        error_code=decision.error_code,
    )


class _RunDirectory:
    """Job run directory'sinin **tek kez** kaldırılmasını üstlenen sarmalayıcı.

    Kaldırma iki yerden istenir: mutlu yolda açıkça, arıza yollarında bir
    ``finally``'den. İkisinin de aynı sonucu vermesi ve ikinci çağrının
    kaldırılmış bir dizini yeniden aramaması için sonuç burada tutulur.
    """

    def __init__(self, execution_run_root: Path, job_id: str) -> None:
        self._root = execution_run_root
        self._job_id = job_id
        self.removed = False

    def remove(self) -> bool:
        """Çalışma alanını kaldırır; sıradan **hiçbir** hatayı dışarı vermez.

        Yutulan küme dar bir tuple değil, ``Exception``'ın tamamıdır: bu çağrı
        bir ``finally``'den de gelir ve orada yükselen sözleşme dışı bir hata,
        yayılmakta olan asıl istisnanın **yerine geçerdi**.
        ``KeyboardInterrupt``/``SystemExit`` bilinçli olarak yakalanmaz.

        Returns:
            Dizin bu çağrıdan sonra gerçekten yoksa ``True``. ``False``,
            başarının ilan edilemeyeceği anlamına gelir: kalıntının toplanması
            bir sonraki dilimdeki janitor'ın işidir.
        """
        if self.removed:
            return True
        try:
            remove_execution_run_directory(self._root, self._job_id)
        except Exception:
            return False
        self.removed = True
        return True


def _runner_error_code(error: RunnerProcessError) -> str:
    """Süreç katmanının sabit sebebini Job hata koduna çevirir."""
    details = error.details if isinstance(error.details, dict) else {}
    reason = details.get("reason")
    if reason in _WORKSPACE_LAYOUT_REASONS:
        return ERROR_WORKSPACE_INTEGRITY_FAILED
    return ERROR_RUNNER_START_FAILED


def _failure(error_code: str) -> _Decision:
    """Artifact taşımayan başarısız sonuç."""
    return _Decision(
        status=JobStatus.FAILED,
        return_code=None,
        error_code=error_code,
        artifact_path=None,
        result_truncated=False,
    )


def _discard_artifact_directory(store: JobArtifactStore, job_id: str) -> None:
    """Yayımlanmamış artifact dizinini en iyi çabayla kaldırır.

    Depo **yayımlanmış** bir ``result.json``'u zaten silmeyi reddeder; bu çağrı
    yalnız rezerve edilmiş boş dizini ve yarım kalmış geçici dosyayı toplar.

    Arızası yutulur ve yutulan küme dar bir tuple değil, ``Exception``'ın
    tamamıdır: bu fonksiyon asıl istisna yükselirken çağrılır ve deponun
    sözleşme dışı bir hatası — ``RuntimeError`` gibi — asıl arızanın yerine
    geçseydi teşhis temizlik katmanını işaret ederdi. ``KeyboardInterrupt`` ve
    ``SystemExit`` bilinçli olarak **bastırılmaz**: ikinci bir kesmeyi yutmak
    süreci durdurulamaz hâle getirirdi.
    """
    with contextlib.suppress(Exception):
        store.cleanup(job_id, missing_ok=True)


def _terminalize_quietly(job: AcquiredPlaybookJob, *, session_factory: SessionFactory) -> None:
    """Beklenmeyen bir arızadan sonra Job'ı en iyi çabayla `failed` yapar.

    ``Exception`` bastırılır ama ``BaseException`` bastırılmaz: bu çağrı zaten
    bir ``KeyboardInterrupt`` yolunda da çalışır ve ikinci bir kesmeyi yutmak,
    süreci durdurulamaz hâle getirirdi.
    """
    with contextlib.suppress(Exception):
        _commit_decision(_failure(ERROR_RUNNER_FAILED), job, session_factory=session_factory)
