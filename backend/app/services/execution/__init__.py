"""Execution planı ve playbook çalıştırma servisleri.

R1-V3C1C2B2B'ye kadar bu paket hiçbir şey çalıştırmıyordu. Artık **tek bir yol**
gerçekten çalıştırır: :func:`execute_next_playbook_job`; çağrı başına **en fazla
bir** Job işler.

R1-V3C2C'den beri o yolu tekrarlayan bir arka plan döngüsü de vardır
(:class:`PlaybookWorker`) ve uygulama açılışı iki toparlama primitive'ini
gerçekten çağırır. Sözleşmenin değişmeyen kısmı şudur: worker **varsayılan
olarak kapalıdır** (``playbook_worker_enabled=False``) ve kapalıyken ne bir
thread ne de tek bir executor çağrısı doğar; eşzamanlılık açıkken de **birdir**
(periyodik janitor kendi thread'inde çalışır ama Job acquire etmez). Kullanıcı
iptali yoktur: worker'ın kapanış gözlemcisi yalnız sürecin kendi kapanışına
bağlıdır. R1-V3D1'den beri bir Job'ı **kuyruğa** koyan public bir endpoint
vardır; onu **çalıştıran** hâlâ yalnız worker'dır ve UI yoktur.

- **R1-V1** — okunabilir, çalıştırılamaz plan önizlemesi.
- **R1-V2** — dondurulmuş execution workspace'i ve tek kullanımlık, TTL'li
  plan token'ı.
- **R1-V3A** — token claim'i ile ``pending`` PLAYBOOK Job rezervasyonunun tek
  transaction'da bağlanması (:mod:`app.services.execution.authorize`).
- **R1-V3C1A** — runner temeli: child process environment'ının allowlist ile
  sıfırdan kurulması (:mod:`app.services.execution.runner_env`, ADR-021
  Kapı A1).
- **R1-V3C1B** — ayrı `ansible-runner` CLI süreci
  (:mod:`app.services.execution.runner_process`) ve onun JSON çıktısının
  güvenli şemaya dönüşümü (:mod:`app.services.execution.normalize`).

- **R1-V3C1C1A** — ``pending`` PLAYBOOK Job'ının tek worker tarafından atomik
  alınması ve lease heartbeat'i (:mod:`app.services.execution.job_state`).
- **R1-V3C1C1B** — aynı durum makinesinin terminal geçişi
  (:func:`finish_playbook_job`) ve aktif Job'a bağlı planın expired sweep'ten
  korunması (:func:`sweep_expired_plans`).
- **R1-V3C1C2A** — acquire edilmiş Job bağlamından, yalnız dondurulmuş
  workspace kullanılarak doğrulanmış ve değişmez execution girdisinin
  üretilmesi (:mod:`app.services.execution.executor`).
- **R1-V3C1C2B1** — uzun süren bir child process çalışırken Job kirasının ayrı,
  kısa ömürlü session'larla yenilenmesi ve kira kaybında süreç sonlandırmanın
  **talep edilmesi** (:mod:`app.services.execution.lease`).
- **R1-V3C1C2B2A** — bir denemenin bıraktığı ``<execution-runs>/<job-uuid>/``
  ağacının descriptor-relative, symlink izlemeyen ve sınırlı biçimde
  kaldırılması (:func:`remove_execution_run_directory`).
- **R1-V3C1C2B2B** — yukarıdaki bütün parçaları sabit bir sırayla birbirine
  bağlayan, **tek atımlık** internal executor
  (:func:`execute_next_playbook_job`).
- **R1-V3C2A** — çökmüş bir worker'ın bıraktığı, kirası **gerçekten dolmuş**
  ``running`` PLAYBOOK satırlarının terminal ``failed`` yapılması
  (:func:`reconcile_stale_playbook_jobs`). Yalnız veritabanı primitive'idir:
  dosya sistemine dokunmaz, satırı yeniden çalıştırmaz ve onu çağıran bir
  açılış yolu yoktur.
- **R1-V3C2B** — crash'ten artakalan ``<execution-runs>/<job-uuid>/`` ağaçlarının
  sınırlı ve fail-closed toplanması (:mod:`app.services.execution.reconcile`).
  Silme yalnız mevcut :func:`remove_execution_run_directory` primitive'iyle
  yapılır; janitor Job durumuna, artifact'lere ve dondurulmuş workspace'lere
  dokunmaz.
- **R1-V3C2C** — varsayılan kapalı, eşzamanlılığı bir olan arka plan worker'ı ve
  onu uygulama açılışına bağlayan lifespan (:mod:`app.services.execution.worker`
  ve :mod:`app.main`). Açılış sırası sabittir: önce C2A'nın veritabanı
  toparlaması, session kapandıktan sonra C2B'nin dosya sistemi janitor'ı ve
  **yalnız ikisi de başarılıysa** worker. C2B tüm ``running`` PLAYBOOK satırlarını
  kirasına bakmadan koruduğu için sıra güvenlik gereğidir; arızada fail-closed
  davranılır ve worker başlatılmaz. Worker iki thread sahiplenir — Job'ları
  teker teker çalıştıran execution döngüsü ve yalnız periyodik crash run
  temizliğini yapan janitor — ve kapanış ikisinin de gerçekten bittiğini
  **kanıtlamadan** tamamlanmış sayılmaz.
- **R1-V3D1** — dar launch façade'ı (:mod:`app.services.execution.launch`) ve onu
  çağıran ilk public HTTP yolu (``POST /api/projects/{project_id}/executions``).
  Façade yeni bir yetkilendirme mantığı kurmaz; yalnız girdi özetini istemciden
  **almaz**, sunucuda kurar ve veritabanı arızasını public-safe tek bir 503'e
  daraltır. Route da kendi claim/transaction mantığını yazmaz: aktörü,
  workspace kökünü ve host key politikasını sunucu ayarından geçirir, cevabı
  claim edilen plandan üretir ve runner/worker/artifact katmanlarına hiç
  dokunmaz. ``201`` "Job kalıcı olarak oluşturuldu" demektir, "execution
  başladı" değil.
- **R1-V3D2A1** — yetkilendirilmiş PLAYBOOK Job'larının bounded, salt-okunur ve
  aktöre bağlı sorgusu (:mod:`app.services.execution.read`). Yalnız domain
  servisidir: HTTP route, artifact okuma ve UI **yoktur**; ``GET /api/jobs``
  hâlâ eklenmemiştir. Görünürlük üç koşulun birlikte sağlanmasıdır — satır
  PLAYBOOK'tur, bir onay biletinden doğmuştur (``execution_plan_id`` doludur) ve
  aktörle tam eşleşir. Aktörün kendisi cevaba çıkmaz; plan/workspace kimliği,
  digest, artifact yolu ve kira alanları özette hiç bulunmaz. ``artifact_path``
  yalnız bir ``bool``'a (``has_recorded_result``) dönüşür ve dosyanın gerçekten
  okunabilir olduğunu **iddia etmez**.
- **R1-V3D2A2A** — yayımlanmış normalize sonuç belgesinin katı, saf doğrulaması
  (:mod:`app.services.execution.result`). Yalnız **decode edilmiş** bir nesne
  alır: dosya açmaz, JSON metni okumaz, veritabanına ve runner'a dokunmaz,
  girdiyi değiştirmez. ``result.json``'ı okuyan bir yol hâlâ yoktur. Yazan taraf
  güvenilir sayılmaz — her seviyede alan kümesi tam eşitlikle ölçülür, tipler
  (``bool``-as-``int`` dahil) ayrı ayrı doğrulanır ve alanları tek tek geçerli
  ama birlikte imkânsız bir belge semantik invariant'larda düşer. Bütün belge
  kaynaklı ihlaller tek bir :class:`JobResultUnavailableError`'a (503) çıkar ve
  offending değeri, alan adını, Job kimliğini veya parser hata metnini
  taşımazlar; çağıranın kendi parametre hataları ise ayrı kalır (``ValueError``).
- **R1-V3D2A2B2** — D2A1'in yetkilendirilmiş Job özetini, D2A2B1'in private
  descriptor-relative okuyucusunu ve D2A2A'nın katı parser'ını tek bir
  salt-okunur domain servisinde birleştirir
  (:func:`~app.services.execution.result_service.get_playbook_job_result`).
  Sıra sabittir: önce çağıran parametreleri, sonra yetkilendirme, sonra —
  yalnız Job terminal ve ``has_recorded_result`` ise — dosya okuma ve
  ayrıştırma, son olarak ayrıştırılmış sonuç ile DB özetinin ``job_id``,
  ``outcome``, ``return_code``, ``error_code`` ve ``result_truncated`` alanlarında
  **exact** karşılaştırılması. Alt katmanların hiçbirinin sözleşmesi
  gevşetilmez; bu turda da HTTP route ve UI yoktur.
- **R1-V3J3A** — sonuç belgesi artık Ansible'ın **display çıktısını** da taşır
  (``ansible_output``/``ansible_output_truncated``). Kaynak dardır: yalnız her
  runner event'inin **üst düzey** ``stdout`` alanı, sırasıyla ve
  :data:`~app.services.execution.normalize.MAX_ANSIBLE_OUTPUT_BYTES` (128 KiB)
  UTF-8 byte sınırıyla. Metin **sansürlenmez** ve "secret-free" sayılmaz: ürün
  modeli tek, güvenilir, profesyonel bir operatördür ve CLI'da göreceğini UI'da
  da görebilir. Yazan taraf artık ``schema_version=2`` üretir; sürüm 1 belgeleri
  diskte kalır, okunmaya devam eder ve output alanları o sürümde
  ``None``/``False``'tur. Fail-closed yollarda (timeout, geçersiz JSON, sınır
  aşımı) ham metin **kurtarılmaz**.

Alt katmanların hiçbiri **kendi başına** bir yol açmaz ve executor onları
birbirine bağlarken sözleşmelerini değiştirmez: süreç katmanı Job durumuna,
session'a ve kalıcı sonuca dokunmaz; girdi hazırlığı veritabanına, run
directory'ye ve artifact'e dokunmaz; temizlik primitive'i süreç başlatmaz; lease
gözlemcisi Job'ı bitirmez ve hata kodu üretmez; durum makinesi süreç, dosya
sistemi ve artifact katmanlarını hiç görmez.

Planı tüketen tek yol :func:`claim_and_reserve_playbook_job`'dır; onu tek çağıran
:func:`launch_prepared_playbook_job` façade'ıdır ve façade'ı tek çağıran da
``POST /api/projects/{project_id}/executions`` route'udur. Zincir tek yönlüdür:
o istek Job'ı ``pending`` olarak kalıcı yapar ve orada durur. Kuyruktaki Job'ı
çalıştırabilecek tek şey :func:`execute_next_playbook_job` çağrısıdır ve onu
tetikleyen tek şey de açıkça açılmış :class:`PlaybookWorker`'dır. Dolayısıyla
HTTP yüzeyinden **kuyruğa girilebilir**, ama bir çalıştırmayı doğrudan başlatan,
worker'ı uyandıran veya durumunu sorgulayan bir yol **yoktur**.
"""

from app.services.execution.authorize import (
    AuthorizedPlaybookJob,
    claim_and_reserve_playbook_job,
)
from app.services.execution.executor import (
    ExecutionAttempt,
    ExecutionOutcome,
    PreparedExecutionInputs,
    execute_next_playbook_job,
    prepare_execution_inputs,
)
from app.services.execution.job_state import (
    ERROR_EXECUTION_BINDING_INVALID,
    ERROR_INTERRUPTED_BY_RESTART,
    FINISH_ERROR_CODES,
    MAX_LEASE_SECONDS,
    AcquiredPlaybookJob,
    AcquireOutcome,
    AcquireResult,
    acquire_pending_playbook_job,
    finish_playbook_job,
    heartbeat_playbook_job,
    reconcile_stale_playbook_jobs,
)
from app.services.execution.launch import (
    ExecutionLaunchUnavailableError,
    launch_prepared_playbook_job,
)
from app.services.execution.lease import (
    LEASE_OBSERVER_JOIN_SECONDS,
    PlaybookLeaseObserver,
)
from app.services.execution.normalize import (
    LEGACY_SCHEMA_VERSION,
    MAX_ANSIBLE_OUTPUT_BYTES,
    SCHEMA_VERSION,
    HostRecap,
    NormalizedEvent,
    NormalizedRun,
    normalize_runner_output,
)
from app.services.execution.plan import (
    MAX_PREVIEW_HOSTS,
    NOT_EXECUTABLE_REASON,
    ExecutionPlan,
    ExecutionPlanInventory,
    ExecutionPlanPlaybook,
    ExecutionPlanProject,
    InventoryNotLinkedToProjectError,
    PlaybookNotDiscoveredError,
    build_execution_plan,
)
from app.services.execution.prepare import PreparedExecutionPlan, prepare_execution_plan
from app.services.execution.read import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    PUBLIC_ERROR_CODES,
    UNKNOWN_FAILURE,
    JobNotFoundError,
    PlaybookJobCursor,
    PlaybookJobPage,
    PlaybookJobSummary,
    get_playbook_job,
    list_playbook_jobs,
)
from app.services.execution.reconcile import (
    ExecutionRunSweepResult,
    sweep_stale_execution_runs,
)
from app.services.execution.result import (
    MAX_ALLOWED_EVENTS,
    MAX_ALLOWED_RESULT_BYTES,
    MIN_ALLOWED_RESULT_BYTES,
    RESULT_ERROR_CODES,
    RESULT_EVENT_TYPES,
    RESULT_FIELDS_V1,
    RESULT_FIELDS_V2,
    RESULT_OUTCOMES,
    SUPPORTED_SCHEMA_VERSIONS,
    JobResultUnavailableError,
    PlaybookHostRecap,
    PlaybookJobResult,
    PlaybookResultEvent,
    parse_playbook_result,
)
from app.services.execution.result_service import get_playbook_job_result
from app.services.execution.runner_env import (
    INHERITED_ENV_NAMES,
    MAX_CLEANUP_DEPTH,
    MAX_CLEANUP_ENTRIES,
    MAX_RUN_ROOT_ENTRIES,
    RunDirectoryEntry,
    RunDirectoryIdentity,
    RunnerEnvironment,
    RunnerEnvironmentError,
    RunRootListing,
    build_runner_environment,
    list_execution_run_directories,
    remove_execution_run_directory,
)
from app.services.execution.runner_process import (
    RAW_DIRNAME,
    RunnerProcessError,
    RunnerProcessLimits,
    RunnerProcessResult,
    build_runner_arguments,
    run_playbook_process,
)
from app.services.execution.store import (
    CleanupResult,
    ExecutionPlanInvalidError,
    PreparedPlan,
    input_fingerprint,
    reconcile_execution_plans,
    reconcile_quietly,
    sweep_expired_plans,
    token_digest,
)
from app.services.execution.worker import (
    MAX_FAILURE_BACKOFF_SECONDS,
    SHUTDOWN_OBSERVER_JOIN_SECONDS,
    SHUTDOWN_WATCH_TICK_SECONDS,
    WORKER_JOIN_SECONDS,
    PlaybookWorker,
    ShutdownProcessObserver,
)
from app.services.execution.workspace import (
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_ENTRIES,
    FrozenWorkspace,
    WorkspaceIntegrityError,
    WorkspaceUnavailableError,
    WorkspaceUnsafeError,
    freeze_workspace,
    verify_frozen_workspace,
)

__all__ = [
    "AcquireOutcome",
    "AcquireResult",
    "AcquiredPlaybookJob",
    "AuthorizedPlaybookJob",
    "CleanupResult",
    "DEFAULT_PAGE_LIMIT",
    "ERROR_EXECUTION_BINDING_INVALID",
    "ERROR_INTERRUPTED_BY_RESTART",
    "ExecutionAttempt",
    "ExecutionLaunchUnavailableError",
    "ExecutionOutcome",
    "ExecutionPlan",
    "ExecutionPlanInvalidError",
    "ExecutionPlanInventory",
    "ExecutionPlanPlaybook",
    "ExecutionPlanProject",
    "ExecutionRunSweepResult",
    "FINISH_ERROR_CODES",
    "FrozenWorkspace",
    "HostRecap",
    "INHERITED_ENV_NAMES",
    "InventoryNotLinkedToProjectError",
    "JobNotFoundError",
    "JobResultUnavailableError",
    "LEASE_OBSERVER_JOIN_SECONDS",
    "LEGACY_SCHEMA_VERSION",
    "MAX_ALLOWED_EVENTS",
    "MAX_ALLOWED_RESULT_BYTES",
    "MAX_ANSIBLE_OUTPUT_BYTES",
    "MAX_CLEANUP_DEPTH",
    "MAX_CLEANUP_ENTRIES",
    "MAX_FAILURE_BACKOFF_SECONDS",
    "MAX_LEASE_SECONDS",
    "MAX_PAGE_LIMIT",
    "MAX_PREVIEW_HOSTS",
    "MAX_RUN_ROOT_ENTRIES",
    "MAX_WORKSPACE_BYTES",
    "MAX_WORKSPACE_ENTRIES",
    "MIN_ALLOWED_RESULT_BYTES",
    "NOT_EXECUTABLE_REASON",
    "NormalizedEvent",
    "NormalizedRun",
    "PUBLIC_ERROR_CODES",
    "PlaybookHostRecap",
    "PlaybookJobCursor",
    "PlaybookJobPage",
    "PlaybookJobResult",
    "PlaybookJobSummary",
    "PlaybookLeaseObserver",
    "PlaybookNotDiscoveredError",
    "PlaybookResultEvent",
    "PlaybookWorker",
    "PreparedExecutionInputs",
    "PreparedExecutionPlan",
    "PreparedPlan",
    "RAW_DIRNAME",
    "RESULT_ERROR_CODES",
    "RESULT_EVENT_TYPES",
    "RESULT_FIELDS_V1",
    "RESULT_FIELDS_V2",
    "RESULT_OUTCOMES",
    "RunDirectoryEntry",
    "RunDirectoryIdentity",
    "RunRootListing",
    "RunnerEnvironment",
    "RunnerEnvironmentError",
    "RunnerProcessError",
    "RunnerProcessLimits",
    "RunnerProcessResult",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SHUTDOWN_OBSERVER_JOIN_SECONDS",
    "SHUTDOWN_WATCH_TICK_SECONDS",
    "ShutdownProcessObserver",
    "UNKNOWN_FAILURE",
    "WORKER_JOIN_SECONDS",
    "WorkspaceIntegrityError",
    "WorkspaceUnavailableError",
    "WorkspaceUnsafeError",
    "acquire_pending_playbook_job",
    "build_execution_plan",
    "build_runner_arguments",
    "build_runner_environment",
    "claim_and_reserve_playbook_job",
    "execute_next_playbook_job",
    "finish_playbook_job",
    "freeze_workspace",
    "get_playbook_job",
    "get_playbook_job_result",
    "heartbeat_playbook_job",
    "input_fingerprint",
    "launch_prepared_playbook_job",
    "list_execution_run_directories",
    "list_playbook_jobs",
    "normalize_runner_output",
    "parse_playbook_result",
    "prepare_execution_inputs",
    "prepare_execution_plan",
    "reconcile_execution_plans",
    "reconcile_quietly",
    "reconcile_stale_playbook_jobs",
    "remove_execution_run_directory",
    "run_playbook_process",
    "sweep_expired_plans",
    "sweep_stale_execution_runs",
    "token_digest",
    "verify_frozen_workspace",
]
