"""Job ORM modeli ve veritabanı güvenlik kısıtları (T-204B1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    and_,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models.execution_mode import ExecutionMode, execution_mode_enum


class JobType(StrEnum):
    PING = "ping"
    PLAYBOOK = "playbook"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    CANCELED = "canceled"


def _enum(enum_class: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        length=16,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Job(Base):
    """Kalıcı execution yaşam döngüsü kaydı."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status <> 'running' OR started_at IS NOT NULL",
            name="ck_jobs_running_has_started_at",
        ),
        # Etkin bir PLAYBOOK Job'ı, yetkilendirildiği planı **taşımak
        # zorundadır** (R1-V3A). Kural yalnız servis katmanında dursaydı,
        # doğrudan yazılan bir satır onaysız ama çalıştırılabilir görünen bir iş
        # üretirdi; veritabanı böyle bir satırı hiç kabul etmemelidir.
        #
        # Terminal durumlar (successful/failed/canceled) kapsam dışıdır: geçmiş
        # kayıtlar ve migration'ın kapattığı legacy satırlar burada kalır.
        CheckConstraint(
            "job_type <> 'playbook' OR status NOT IN ('pending', 'running') OR ("
            "execution_plan_id IS NOT NULL "
            "AND project_id IS NOT NULL "
            "AND playbook_path IS NOT NULL "
            "AND limit_pattern IS NULL)",
            # İsimlendirme kuralı `ck_jobs_` ön ekini kendisi ekler.
            name="active_playbook_is_authorized",
        ),
        # Ping'in kendi onay akışı vardır ve plan kaydı üretmez; bir ping
        # satırının plana bağlanması, o planın Job'ının başka bir yerde
        # oluştuğu anlamına gelirdi.
        CheckConstraint(
            "job_type <> 'ping' OR execution_plan_id IS NULL",
            name="ping_has_no_execution_plan",
        ),
        # --- Worker ownership / lease (R1-V3C1A) --------------------------
        #
        # `running` bir PLAYBOOK Job'ı **sahipsiz olamaz**: onu hangi worker'ın
        # aldığı, en son ne zaman yaşadığını bildirdiği ve kirasının ne zaman
        # dolduğu satırın kendisinden okunabilmelidir. Alanlar boş kalabilseydi,
        # çökmüş bir worker'ın bıraktığı `running` satır ile canlı bir
        # execution'ı ayırt etmenin veritabanı içinde hiçbir yolu olmazdı.
        CheckConstraint(
            "job_type <> 'playbook' OR status <> 'running' OR ("
            "worker_id IS NOT NULL "
            "AND heartbeat_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="running_playbook_has_lease",
        ),
        # Ters yön de bağlanır. `pending` bir Job henüz alınmamıştır, terminal
        # bir Job artık kimseye ait değildir; ikisinde de duran bir sahiplik
        # alanı, sahibi olmayan bir kirayı canlıymış gibi gösterirdi.
        CheckConstraint(
            "job_type <> 'playbook' OR status = 'running' OR ("
            "worker_id IS NULL "
            "AND heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL)",
            name="idle_playbook_has_no_lease",
        ),
        # Ping'in worker'ı ve kirası yoktur; akışı T-204B1'de kendi
        # `job_stale_seconds` eşiğiyle uzlaştırılır. Bu alanları taşıyan bir
        # ping satırı, iki farklı sahiplik modelinin karışması demektir.
        CheckConstraint(
            "job_type <> 'ping' OR ("
            "worker_id IS NULL "
            "AND heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ping_has_no_lease",
        ),
        # Kira, tazelendiği andan **sonra** dolmalıdır. Alanların yalnızca dolu
        # olması yetmez: `lease_expires_at <= heartbeat_at` taşıyan bir satır,
        # yazıldığı anda süresi geçmiş bir kira demektir. Böyle bir satır canlı
        # bir execution'ı terk edilmiş gösterir ve işin ikinci bir worker
        # tarafından devralınmasına yol açardı — concurrency=1 sınırının
        # veritabanında durmasının anlamı da kalmazdı.
        CheckConstraint(
            "job_type <> 'playbook' OR status <> 'running' OR lease_expires_at > heartbeat_at",
            name="running_playbook_lease_outlives_heartbeat",
        ),
        # --- Execution mode (R1-V3H1A) ------------------------------------
        #
        # Mode'un karşılığı yalnız `ansible-runner` playbook argv'sindedir.
        # Ping o yoldan geçmez: `build_ping_command` sabit bir ad-hoc argv
        # (`ansible all -i <snapshot> -m ping --forks N -T T`) kurar ve
        # `--check` diye bir bayrak hiç söz konusu değildir. Dolayısıyla
        # `mode = 'normal'` taşıyan bir ping satırı hiçbir argv farkına
        # karşılık gelmez. Kural yalnız servis katmanında dursaydı, doğrudan
        # yazılan böyle bir satır mode'a bakan her sorgu tarafından yanlış
        # sınıflandırılırdı.
        #
        # PLAYBOOK satırları her iki kipi de taşıyabilir; plan ile Job kipinin
        # **eşitliği** cross-table bir trigger'la değil, H1B'de plan kaydından
        # kopyalanarak sağlanır.
        CheckConstraint(
            "job_type <> 'ping' OR mode = 'check'",
            name="ping_is_check_only",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[JobType] = mapped_column(_enum(JobType, "job_type"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus, "job_status"), nullable=False)
    # Job'un execution mode'u. Kaynağı Job'un kendisi değil, onu yetkilendiren
    # plan kaydıdır (R1-V3H1B); tip tanımı da plan ile **ortaktır**
    # (:class:`~app.models.execution_mode.ExecutionMode`).
    #
    # Varsayılan ``check``'tir: mode'u belirtmeyen mevcut constructor'lar ve
    # migration'ın doldurduğu legacy satırlar ``--check`` taşıyan argv'de kalır
    # ve sessizce ``normal``'a yükselmez. PING satırları için
    # ``ck_jobs_ping_is_check_only`` bunu tek seçenek hâline getirir.
    mode: Mapped[ExecutionMode] = mapped_column(
        execution_mode_enum(),
        nullable=False,
        default=ExecutionMode.CHECK,
        server_default=ExecutionMode.CHECK.value,
    )
    inventory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    playbook_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Job'u yetkilendiren, claim edilmiş execution planı (R1-V3A).
    #
    # Sütun nullable'dır çünkü ping işleri plan kaydı üretmez; **etkin**
    # (pending/running) PLAYBOOK işleri için zorunluluk
    # ``ck_jobs_active_playbook_is_authorized`` ile veritabanında uygulanır.
    # `unique`'tir: tek kullanımlık bir onay biletinden ikinci bir Job doğamaz.
    # Tek kullanım garantisi böylece iki bağımsız yerde durur — plan satırının
    # atomik ``prepared → claimed`` geçişi ve buradaki unique kısıt.
    # ``RESTRICT``, bir Job'un yetki kaynağının silinmesini engeller.
    execution_plan_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("execution_plans.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    limit_pattern: Mapped[str | None] = mapped_column(String(256), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Terminal bir Job'ın **makine tarafından okunabilir** sebebi. Serbest metin
    # değildir ve path, token, digest veya environment içeriği taşımaz
    # (public Job hata kodu sözleşmesi).
    #
    # `JobStatus`'a `timeout` üyesi bilinçli olarak **eklenmez**: timeout ayrı
    # bir durum değil, başarısızlığın bir sebebidir. İleride
    # `status = failed` + `error_code = runner_timeout` olarak temsil edilir.
    # Yeni bir enum üyesi, mevcut bütün terminal-durum sorgularını ve
    # `finish_job` çağrılarını sessizce eksik bırakırdı.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Normalize sonucun sınıra takılıp kırpıldığını **kaydın kendisi** söyler.
    # Nullable olsaydı "kırpılmadı" ile "bilinmiyor" aynı değere düşerdi ve
    # eksik bir sonuç tam sonuç gibi okunabilirdi.
    result_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # --- Worker ownership / lease ----------------------------------------
    #
    # Üçü birlikte "bu satırı şu an kim çalıştırıyor ve ne zamana kadar"
    # sorusunu yanıtlar; yukarıdaki CHECK'ler üçünün birlikte dolu veya birlikte
    # boş olmasını zorunlu kılar. Bu turda yazan bir worker **yoktur**; alanlar
    # yalnızca şema ve invariant olarak kurulur.
    worker_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        index=True,
    )

    @validates("id")
    def _canonical_uuid4(self, _key: str, value: str) -> str:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("Job id canonical UUID4 olmalıdır.")
        return value


_active_ping = and_(
    Job.job_type == JobType.PING,
    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
)
Index(
    "uq_jobs_active_ping_inventory",
    Job.inventory_id,
    unique=True,
    sqlite_where=_active_ping,
    postgresql_where=_active_ping,
)

# Global aktif PLAYBOOK concurrency'si **kesin olarak 1**'dir ve doğruluk
# kaynağı bu model ve veritabanı invariant'ıdır.
#
# Index'lenen sütunun `job_type` olması bir tuhaflık değil, kuralın kendisidir:
# predicate zaten `job_type = 'playbook'` diyor, yani index'e giren bütün
# satırlarda anahtar **aynı** değeri taşır. Dolayısıyla ikinci bir aktif
# PLAYBOOK satırı — inventory'si, project'i veya durumu ne olursa olsun —
# yinelenen anahtar olur ve veritabanı tarafından reddedilir. `pending` ve
# `running` aynı predicate içindedir: biri sıradayken diğerinin başlaması da
# engellenir.
#
# Sınırın in-process bir sayaç, kilit veya semafor olmaması bilinçlidir: iki
# backend süreci veya restart edilmiş tek bir süreç, süreç içi bir sayacı
# paylaşmaz ve ikinci bir aktif Job üretebilirdi.
_active_playbook = and_(
    Job.job_type == JobType.PLAYBOOK,
    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
)
Index(
    "uq_jobs_active_playbook_global",
    Job.job_type,
    unique=True,
    sqlite_where=_active_playbook,
    postgresql_where=_active_playbook,
)
