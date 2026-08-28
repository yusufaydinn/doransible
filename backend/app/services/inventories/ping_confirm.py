"""Onaylanmış ping'in yürütülmesi — T-204B2.

Bu modül preview claim'ini (T-204A) gerçek execution'a (T-204B1 primitive'leri)
bağlayan tek orkestrasyondur. Sözleşmenin özü şudur: **çalıştırılacak iş,
kullanıcının onayladığı plandır.**

Akış::

    preview claim (atomik, tek kullanımlık)
    → meta/snapshot bütünlüğü + private key yeniden doğrulaması
    → inventory kaydı yalnız FK/project metadata'sı için okunur
    → execution workspace + kontrollü known_hosts
    → stale kurtarma + aktif Job ön kontrolü
    → canonical UUID4 üretimi
    → T1: pending Job (flush) → artifact dizini → commit
    → T2: koşullu pending → running → commit
    → (açık transaction yokken) ansible all -i <snapshot> -m ping
    → güvenli parser
    → atomik result.json
    → T3: koşullu running → terminal → commit
    → execution workspace temizliği + preview record discard

Bilinçli kararlar:

- **Özgün inventory dosyası yeniden açılmaz.** Hedef kümesi ve bağlantı
  alanları yalnızca claim edilen dondurulmuş snapshot'tan gelir; onay ile
  çalıştırma arasında dosya değişse, silinse veya izinleri kapansa bile
  çalıştırılan iş onaylanan iştir (ADR-018 Karar 2).
- **Snapshot temp alanı ve known_hosts, Job rezervasyonundan önce hazırlanır.**
  Bu iki adımın arızası altyapı arızasıdır ve geride pending bir Job veya boş
  bir artifact dizini bırakmamalıdır.
- **Alt süreç çalışırken açık veritabanı transaction'ı yoktur.** Ping timeout'u
  kadar süren bir SQLite yazma kilidi, uygulamanın geri kalanını bloklardı.
- **Ham stdout/stderr hiçbir yere yazılmaz.** Ne response, ne artifact, ne log.
  Yalnız parser'ın normalize ettiği, redaction ve uzunluk sınırından geçmiş
  host mesajları taşınır.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Job, JobStatus
from app.services.ansible.inventory_snapshot import (
    revalidate_snapshot_private_keys,
    snapshot_connection_values,
    snapshot_host_names,
)
from app.services.ansible.ping_execution import (
    PingHostResult,
    PingInvalidOutputError,
    parse_ping_output,
    run_ping_process,
)
from app.services.ansible.process import ProcessLaunchError, ProcessOutcome
from app.services.ansible.ssh import (
    SSHPolicyUnavailableError,
    build_ssh_arguments,
    prepare_known_hosts,
)
from app.services.inventories.service import get_inventory
from app.services.jobs.artifacts import (
    JobArtifactPreservedError,
    JobArtifactStore,
    JobArtifactUnavailableError,
)
from app.services.jobs.preview import PreviewNotFoundError, PreviewRecord, PreviewStore
from app.services.jobs.service import (
    ActivePingJobConflictError,
    active_ping_query,
    finish_job,
    mark_running,
    recover_stale_ping,
    reserve_pending_ping,
)
from app.services.security.redaction import REDACTED

# Execution workspace'ine yazılan dondurulmuş snapshot. Ad sabittir ve
# istemciden gelen hiçbir parça taşımaz.
EXECUTION_SNAPSHOT_FILENAME = "inventory-targets.yml"

# Result artifact'inin şema sürümü. Alan eklendiğinde/çıkarıldığında artar.
RESULT_SCHEMA_VERSION = 1

PING_JOB_TYPE = "ping"
PING_OPERATION = "ansible.builtin.ping"

# Host durumları. `no_result`, beklenen bir host için hiç sonuç bloğu
# görülmediğini gösterir; sessizce "başarılı" sayılmaz.
REACHABLE = "reachable"
UNREACHABLE = "unreachable"
FAILED = "failed"
NO_RESULT = "no_result"

# Mesaj taşıyan durumlar. Reachable ve no_result için mesaj normalde `null`dur:
# "pong" bilgi taşımaz, no_result'ın da anlatacak bir çıktısı yoktur.
_MESSAGE_STATUSES = frozenset({UNREACHABLE, FAILED})


class PingJobArtifactUnavailableError(AppError):
    """Job rezervasyonu, artifact dizini veya başlangıç commit'i başarısız.

    Execution **başlamamıştır**. Kullanıcıya dosya sistemi ayrıntısı, path veya
    exception metni gösterilmez.
    """

    status_code = 500
    code = "ping_artifact_unavailable"


class PingArtifactWriteFailedError(AppError):
    """Sonuç yazılamadı veya Job terminal duruma alınamadı.

    ``details`` yalnızca ``job_id`` taşır: operatör kaydı bu kimlikle
    inceleyebilir. Yayımlanmış bir ``result.json`` bu yolda **silinmez**.
    """

    status_code = 500
    code = "ping_artifact_write_failed"


class PingKnownHostsUnavailableError(AppError):
    """Kontrollü known_hosts dosyası hazırlanamadı; execution yapılmaz."""

    status_code = 500
    code = "ping_known_hosts_unavailable"


class PingSnapshotUnavailableError(AppError):
    """Execution snapshot'ı için özel geçici alan hazırlanamadı veya silinemedi."""

    status_code = 500
    code = "ping_snapshot_unavailable"


class AnsibleUnavailableError(AppError):
    """`ansible` ad-hoc komutu başlatılamadı veya beklenmedik biçimde çöktü."""

    status_code = 503
    code = "ansible_unavailable"


class PingTimeoutError(AppError):
    """Ping süreci verilen süre içinde tamamlanmadı ve sonlandırıldı."""

    status_code = 504
    code = "ping_timeout"


class PingOutputTooLargeError(AppError):
    """Ping süreci kabul edilen sınırdan çok çıktı üretti.

    ``details`` yalnızca güvenli ``stream`` alanını taşır (``stdout`` |
    ``stderr``); çıktının kendisi hiçbir yere yazılmaz.
    """

    status_code = 502
    code = "ping_output_too_large"


@dataclass(frozen=True)
class PingHostOutcome:
    """Tek bir host için normalize edilmiş sonuç."""

    name: str
    status: str
    message: str | None


@dataclass(frozen=True)
class PingSummary:
    """Host durumlarının sayımı. ``total`` beklenen host sayısıdır."""

    total: int
    reachable: int
    unreachable: int
    failed: int
    no_result: int


@dataclass(frozen=True)
class PingRun:
    """Tamamlanmış bir ping execution'ının güvenli gösterimi.

    Token, snapshot içeriği, artifact path'i, controller dosya sistemi
    ayrıntısı ve ham çıktı bilinçli olarak **taşınmaz**.
    """

    job_id: str
    job_type: str
    status: str
    inventory_id: int
    project_id: int | None
    limit: str | None
    return_code: int | None
    started_at: datetime
    finished_at: datetime
    summary: PingSummary
    hosts: tuple[PingHostOutcome, ...]


def confirm_ping(
    session: Session,
    inventory_id: int,
    *,
    preview_token: str,
    store: PreviewStore,
    artifacts: JobArtifactStore,
    key_roots: Sequence[Path],
    command: Sequence[str],
    app_data_dir: Path,
    known_hosts_path: Path | None,
    host_key_policy: str,
    forks: int,
    connect_timeout: int,
    timeout_seconds: float,
    max_output_bytes: int,
    job_stale_seconds: float,
    requested_by: str,
) -> PingRun:
    """Onaylanmış planı çalıştırır ve normalize sonucu döndürür.

    Token **önce** claim edilir: sonraki her arıza (aktif Job çakışması dâhil)
    token'ı tüketilmiş bırakır. Aksi hâlde başarısız bir istekten sonra aynı
    token yeniden denenebilir ve tek-kullanım garantisi yalnızca "mutlu yolda"
    geçerli olurdu.

    Args:
        session: Aktif veritabanı session'ı.
        inventory_id: İstek URL'sinden gelen inventory kimliği.
        preview_token: Preview cevabında bir kez dönen onay token'ı.
        store: Preview state deposu.
        artifacts: Job artifact deposu.
        key_roots: Private key dosyaları için izin verilen kökler.
        command: `ansible` ad-hoc komutu (argüman listesi).
        app_data_dir: Uygulamanın çalışma verisi kökü.
        known_hosts_path: Yapılandırılmış known_hosts yolu; ``None`` ise
            varsayılan ``app-data/ssh/known_hosts``.
        host_key_policy: ``strict`` veya ``accept_new``.
        forks: Ansible fork sınırı.
        connect_timeout: SSH connect timeout'u (saniye).
        timeout_seconds: Ping sürecinin toplam timeout'u.
        max_output_bytes: stdout üst sınırı.
        job_stale_seconds: Terk edilmiş Job kurtarma eşiği.
        requested_by: Geçerli aktör etiketi.

    Returns:
        Job kimliği, durumu, özeti ve host sonuçlarını taşıyan :class:`PingRun`.

    Raises:
        PreviewNotFoundError: Token bilinmiyor, süresi geçmiş, eşleşmiyor veya
            kullanılmış.
        PreviewStoreUnavailableError: Preview deposu arızalı.
        InventoryUnsafeError: Snapshot yapısı veya private key yolu artık
            güvenli değil.
        ActivePingJobConflictError: Bu inventory için taze bir aktif ping var.
        PingJobArtifactUnavailableError: Job/artifact rezervasyonu başarısız.
        PingSnapshotUnavailableError: Execution workspace'i hazırlanamadı.
        PingKnownHostsUnavailableError: known_hosts hazırlanamadı.
        AnsibleUnavailableError: Süreç başlatılamadı.
        PingTimeoutError: Süre aşıldı.
        PingOutputTooLargeError: Çıktı sınırı aşıldı.
        PingInvalidOutputError: Çıktı beklenen biçimde değil.
        PingArtifactWriteFailedError: Sonuç yazılamadı veya Job terminal duruma
            alınamadı.
    """
    record = store.claim(preview_token, inventory_id=inventory_id, requested_by=requested_by)
    try:
        run = _run_claimed_ping(
            session,
            inventory_id,
            record=record,
            artifacts=artifacts,
            key_roots=key_roots,
            command=command,
            app_data_dir=app_data_dir,
            known_hosts_path=known_hosts_path,
            host_key_policy=host_key_policy,
            forks=forks,
            connect_timeout=connect_timeout,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            job_stale_seconds=job_stale_seconds,
            requested_by=requested_by,
        )
    except BaseException:
        # Asıl hata maskelenmez: claim edilmiş state temizlenemezse claim-stale
        # süpürücüsü onu daha sonra toplar.
        with contextlib.suppress(AppError, OSError):
            store.discard(record)
        raise
    # Başarı yolunda temizlik arızası yutulmaz (ADR-018 Karar 9).
    store.discard(record)
    return run


def _run_claimed_ping(
    session: Session,
    inventory_id: int,
    *,
    record: PreviewRecord,
    artifacts: JobArtifactStore,
    key_roots: Sequence[Path],
    command: Sequence[str],
    app_data_dir: Path,
    known_hosts_path: Path | None,
    host_key_policy: str,
    forks: int,
    connect_timeout: int,
    timeout_seconds: float,
    max_output_bytes: int,
    job_stale_seconds: float,
    requested_by: str,
) -> PingRun:
    """Claim edilmiş bir plan üzerinde bütün execution akışını yürütür."""
    snapshot_text = record.snapshot_text
    hosts = snapshot_host_names(snapshot_text)
    _verify_plan_consistency(
        record.meta,
        hosts,
        inventory_id=inventory_id,
        host_key_policy=host_key_policy,
    )
    # Anahtar yolu controller üzerinde dosya okutur; preview'daki doğrulama
    # kalıcı garanti değildir. Dosya silinmiş, symlink ile değiştirilmiş veya
    # allowlist dışına çıkmışsa burada fail-closed durulur.
    revalidate_snapshot_private_keys(snapshot_text, key_roots=key_roots)

    # Kayıt yalnızca FK ve project metadata'sı için okunur; dosyası açılmaz.
    inventory = get_inventory(session, inventory_id)
    project_id = inventory.project_id
    limit = record.meta.get("limit")

    work_dir = _create_workspace()
    try:
        snapshot_path = _write_execution_snapshot(work_dir, snapshot_text)
        known_hosts = _prepare_known_hosts(app_data_dir, known_hosts_path)
        ssh_arguments = build_ssh_arguments(
            policy=host_key_policy, known_hosts=known_hosts, work_dir=work_dir
        )
        _release_stale_or_conflict(
            session, inventory_id, stale_seconds=job_stale_seconds, artifacts=artifacts
        )
        job_id = str(uuid.uuid4())
        started_at = _reserve_and_start(
            session,
            job_id=job_id,
            inventory_id=inventory_id,
            project_id=project_id,
            limit=limit,
            requested_by=requested_by,
            artifacts=artifacts,
        )
        run = _execute_and_finalize(
            session,
            job_id=job_id,
            inventory_id=inventory_id,
            project_id=project_id,
            limit=limit,
            hosts=hosts,
            connection_values=snapshot_connection_values(snapshot_text),
            started_at=started_at,
            artifacts=artifacts,
            command=command,
            snapshot_path=snapshot_path,
            work_dir=work_dir,
            ssh_arguments=ssh_arguments,
            forks=forks,
            connect_timeout=connect_timeout,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    except BaseException:
        _remove_workspace(work_dir, suppress=True)
        raise
    _remove_workspace(work_dir, suppress=False)
    return run


# --- Plan bütünlüğü -----------------------------------------------------------


def _verify_plan_consistency(
    meta: dict[str, Any],
    hosts: Sequence[str],
    *,
    inventory_id: int,
    host_key_policy: str,
) -> None:
    """Claim edilen planın hâlâ **onaylanan** plan olduğunu doğrular.

    ``inventory_id``, ``host_count`` ve ``operation`` için bu bir savunma
    katmanıdır; depo onları claim sırasında zaten doğrular.

    ``host_key_policy`` ise yeni ve **zorunlu** bir bağdır. Politika plana
    yazılır ve kullanıcıya gösterilir (ADR-018 Karar 1); ancak execution'da
    kullanılan değer güncel Settings'ten gelir. İkisi ayrışabilirdi: `strict`
    ile onaylanmış bir plan, ayar arada `accept_new` yapıldıysa kullanıcının
    görmediği daha gevşek bir host-key politikasıyla çalışırdı — TOFU penceresi
    açılır ve ilk bağlantı MITM'e açık hâle gelirdi.

    Eski politika **kullanılmaz**: onaylanmış plana bakıp güncel yönetici
    ayarını geçersiz kılmak, bu kez yapılandırmayı sessizce delerdi. Uyuşmazlık
    yeni bir preview gerektirir.

    Kontrol, workspace/known_hosts/Job/artifact üretilmeden önce çalışır; token
    ise claim edildiği için tüketilmiş kalır.
    """
    if (
        meta.get("inventory_id") != inventory_id
        or meta.get("host_count") != len(hosts)
        or meta.get("operation") != PING_OPERATION
        or meta.get("host_key_policy") != host_key_policy
    ):
        raise PreviewNotFoundError(
            "Ping önizlemesi bu istekle eşleşmiyor. Planı yeniden oluşturun.",
            details={"reason": "mismatch"},
        )


# --- Execution workspace ------------------------------------------------------


def _create_workspace() -> Path:
    """Yalnız bu execution'a ait, 0700 izinli ve tahmin edilemez bir dizin açar."""
    try:
        work_dir = Path(tempfile.mkdtemp(prefix="ansibleops-ping-exec-"))
    except OSError as exc:
        raise PingSnapshotUnavailableError("Ping çalışma alanı hazırlanamadı.") from exc
    if os.name == "posix":
        try:
            os.chmod(work_dir, 0o700)
        except OSError as exc:  # pragma: no cover - platform davranışı
            _remove_workspace(work_dir, suppress=True)
            raise PingSnapshotUnavailableError("Ping çalışma alanı hazırlanamadı.") from exc
    return work_dir


def _write_execution_snapshot(work_dir: Path, snapshot_text: str) -> Path:
    """Claim edilen snapshot'ı yeni ve özel bir dosyaya yazar.

    ``O_EXCL`` var olan bir dosyanın üzerine yazılmasını, ``O_NOFOLLOW`` aynı
    ada konmuş bir symlink'in izlenmesini engeller. İçerik ``fsync`` edilir:
    alt süreç dosyayı okurken yarım bir yazım görmez.
    """
    path = work_dir / EXECUTION_SNAPSHOT_FILENAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(snapshot_text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PingSnapshotUnavailableError("Ping çalışma snapshot'ı yazılamadı.") from exc
    return path


def _remove_workspace(work_dir: Path, *, suppress: bool) -> None:
    """Execution workspace'ini siler.

    Dizin uygulamanın kendi ürettiği, 0700 izinli ve tahmin edilemez adlı bir
    geçici dizindir. Silme arızası yalnızca başka bir hata yükselmiyorken
    raporlanır; aksi hâlde asıl hatayı maskelerdi.
    """
    try:
        shutil.rmtree(work_dir)
    except OSError as exc:
        if suppress:
            return
        raise PingSnapshotUnavailableError("Ping çalışma alanı temizlenemedi.") from exc


def _prepare_known_hosts(app_data_dir: Path, configured: Path | None) -> Path:
    """Kontrollü known_hosts dosyasını hazırlar ve arızayı ping koduna çevirir."""
    try:
        return prepare_known_hosts(app_data_dir, configured)
    except SSHPolicyUnavailableError as exc:
        raise PingKnownHostsUnavailableError(
            "SSH known_hosts dosyası hazırlanamadı; ping çalıştırılmadı."
        ) from exc


# --- Job rezervasyonu ---------------------------------------------------------


def _release_stale_or_conflict(
    session: Session,
    inventory_id: int,
    *,
    stale_seconds: float,
    artifacts: JobArtifactStore,
) -> None:
    """Terk edilmiş ping'leri kurtarır, taze olanı çatışma olarak bildirir.

    Bu ön sorgu **yarış garantisi değildir**; garanti partial unique index'tir.
    Amacı, kullanıcıya anlaşılır bir 409 vermek ve çökmüş bir sürecin bıraktığı
    kaydı kilitlenmiş hâlde bırakmamaktır.

    Stale kararı ile geçiş tek koşullu UPDATE içindedir. ``rowcount=0`` "kayıt
    taze" anlamına gelmez; yalnızca "bu koşulla yazamadım" demektir, bu yüzden
    durum yeniden sorgulanır.
    """
    try:
        job_ids = [
            job.id for job in session.execute(active_ping_query(inventory_id)).scalars().all()
        ]
        for job_id in job_ids:
            if recover_stale_ping(session, job_id, stale_seconds=stale_seconds):
                session.commit()
                _release_recovered_artifact(artifacts, job_id)
                continue
            session.rollback()
            current = session.get(Job, job_id)
            if current is not None and current.status in {
                JobStatus.PENDING,
                JobStatus.RUNNING,
            }:
                raise ActivePingJobConflictError(
                    "Bu inventory için hâlâ çalışan bir ping işi var.",
                    details={"job_id": job_id},
                )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise PingJobArtifactUnavailableError("Ping işi hazırlanamadı.") from exc


def _release_recovered_artifact(artifacts: JobArtifactStore, job_id: str) -> None:
    """Kurtarılmış bir Job'un **yayımlanmamış** artifact dizinini temizler.

    Eksik dizin no-op'tur. Görünür ``result.json`` korunur: kurtarma, operatör
    incelemesi için duran bir sonucu silmez. Beklenmeyen içerik, symlink ve I/O
    arızası ise gizlenmez.
    """
    try:
        artifacts.cleanup(job_id, missing_ok=True)
    except JobArtifactPreservedError:
        return
    except JobArtifactUnavailableError as exc:
        raise PingJobArtifactUnavailableError(
            "Önceki ping işinin artifact dizini temizlenemedi."
        ) from exc


def _reserve_and_start(
    session: Session,
    *,
    job_id: str,
    inventory_id: int,
    project_id: int | None,
    limit: str | None,
    requested_by: str,
    artifacts: JobArtifactStore,
) -> datetime:
    """T1 ve T2'yi yürütür; ``started_at`` değerini döndürür.

    T1: pending satır flush edilir (asıl yarış garantisi burada, veritabanı
    partial unique index'indedir), **ancak flush başarılıysa** aynı UUID için
    artifact dizini açılır ve sonra commit edilir. Ters sıra, hiçbir zaman
    kullanılmayacak dizinler bırakırdı.

    T2: koşullu ``pending → running`` geçişi ayrı bir transaction'dır. Böylece
    alt süreç başlamadan önce commit edilmiş ve kapanmış bir transaction kalır.
    """
    _reserve_pending(
        session,
        job_id=job_id,
        inventory_id=inventory_id,
        project_id=project_id,
        limit=limit,
        requested_by=requested_by,
    )
    try:
        artifacts.create(job_id)
    except JobArtifactUnavailableError as exc:
        session.rollback()
        _discard_reserved_artifact(artifacts, job_id)
        raise PingJobArtifactUnavailableError(
            "Ping işi için artifact dizini oluşturulamadı."
        ) from exc
    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        _discard_reserved_artifact(artifacts, job_id)
        raise PingJobArtifactUnavailableError("Ping işi kaydedilemedi.") from exc

    started_at = datetime.now(UTC)
    try:
        started = mark_running(session, job_id, now=started_at)
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        # Pending Job ve boş dizin kalabilir; stale recovery onları toplar.
        raise PingJobArtifactUnavailableError("Ping işi başlatılamadı.") from exc
    if not started:
        raise PingJobArtifactUnavailableError("Ping işi başlatılamadı.")
    return started_at


def _reserve_pending(
    session: Session,
    *,
    job_id: str,
    inventory_id: int,
    project_id: int | None,
    limit: str | None,
    requested_by: str,
) -> None:
    """Pending rezervasyonunu yapar; çatışmayı güvenilir biçimde sınıflandırır."""
    try:
        reserve_pending_ping(
            session,
            job_id=job_id,
            inventory_id=inventory_id,
            project_id=project_id,
            limit_pattern=limit,
            requested_by=requested_by,
        )
    except ActivePingJobConflictError as exc:
        # `reserve_pending_ping` session'ı zaten rollback etti; artifact
        # oluşturulmadığı için temizlenecek bir dizin de yoktur.
        raise _active_conflict(session, inventory_id) from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise PingJobArtifactUnavailableError("Ping işi kaydedilemedi.") from exc


def _active_conflict(session: Session, inventory_id: int) -> AppError:
    """Unique index çatışmasını, aktif Job'u yeniden sorgulayarak raporlar.

    ``job_id`` yalnızca **güvenilir biçimde** okunabildiğinde verilir: çatışan
    kayıt bu arada terminal duruma geçmiş olabilir ve o zaman gösterilecek
    doğru bir kimlik yoktur.
    """
    try:
        existing = session.execute(active_ping_query(inventory_id)).scalars().first()
    except SQLAlchemyError:
        session.rollback()
        existing = None
    details = {"job_id": existing.id} if existing is not None else None
    return ActivePingJobConflictError(
        "Bu inventory için hâlâ çalışan bir ping işi var.", details=details
    )


def _discard_reserved_artifact(artifacts: JobArtifactStore, job_id: str) -> None:
    """Yalnız bu isteğe ait, yayımlanmamış UUID dizinini temizlemeyi dener.

    İstek zaten ``ping_artifact_unavailable`` ile sonuçlanıyor; temizlik
    arızasının o hatayı maskelemesine izin verilmez. Beklenmeyen içerik veya
    yayımlanmış sonuç zaten korunur.
    """
    with contextlib.suppress(JobArtifactUnavailableError, ValueError):
        artifacts.cleanup(job_id, missing_ok=True)


# --- Execution ----------------------------------------------------------------


def _execute_and_finalize(
    session: Session,
    *,
    job_id: str,
    inventory_id: int,
    project_id: int | None,
    limit: str | None,
    hosts: tuple[str, ...],
    connection_values: Sequence[str],
    started_at: datetime,
    artifacts: JobArtifactStore,
    command: Sequence[str],
    snapshot_path: Path,
    work_dir: Path,
    ssh_arguments: list[str],
    forks: int,
    connect_timeout: int,
    timeout_seconds: float,
    max_output_bytes: int,
) -> PingRun:
    """Sabit ping'i çalıştırır, sonucu yayımlar ve Job'u terminal yapar.

    Timeout, çıktı sınırı, süreç arızası ve geçersiz çıktı da terminal bir
    sonuçtur: bütün beklenen host'ları ``no_result`` gösteren güvenli bir
    artifact yayımlanır, Job ``failed`` yapılır ve ancak sonra ilgili hata
    yükseltilir. Aksi hâlde Job ``running`` asılı kalır ve inventory yalnızca
    stale eşiği dolduğunda tekrar ping'lenebilirdi.
    """
    outcome: ProcessOutcome | None = None
    results: tuple[PingHostResult, ...] | None = None
    failure: AppError | None = None
    try:
        outcome = run_ping_process(
            command=command,
            snapshot_path=snapshot_path,
            work_dir=work_dir,
            ssh_arguments=ssh_arguments,
            forks=forks,
            connect_timeout=connect_timeout,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
    except ProcessLaunchError:
        failure = AnsibleUnavailableError(
            "Ansible ad-hoc komutu çalıştırılamadı. `ansible-core` kurulu olmalıdır."
        )
    except Exception:
        # `OSError` ve beklenmeyen her normal istisna aynı sonucu doğurur:
        # Job'un `running` asılı kalmasına izin verilmez. Aksi hâlde tek bir
        # beklenmedik arıza, inventory'yi stale eşiği dolana kadar
        # ping'lenemez hâle getirirdi.
        #
        # `BaseException` bilinçli olarak yakalanmaz: `KeyboardInterrupt` ve
        # `SystemExit` süreç sonlandırma sinyalleridir, execution arızası
        # değil; onları "ansible kullanılamıyor" diye raporlamak yanıltıcı
        # olurdu.
        #
        # Exception metni, traceback, path ve argv dışarı **verilmez**.
        failure = AnsibleUnavailableError("Ping çalıştırılamadı.")
    else:
        failure = _process_failure(outcome)
        if failure is None:
            results, failure = _parse_results(outcome, hosts)

    finished_at = datetime.now(UTC)
    return_code = outcome.return_code if outcome is not None else None
    if failure is not None:
        run = _build_run(
            job_id=job_id,
            inventory_id=inventory_id,
            project_id=project_id,
            limit=limit,
            hosts=tuple(
                PingHostOutcome(name=host, status=NO_RESULT, message=None) for host in hosts
            ),
            return_code=return_code,
            status=JobStatus.FAILED,
            started_at=started_at,
            finished_at=finished_at,
        )
        _publish(session, run, artifacts=artifacts)
        raise failure

    assert results is not None  # `failure is None` ⇒ parser sonuç üretti.
    outcomes = tuple(
        PingHostOutcome(
            name=item.host,
            status=item.status,
            message=_safe_message(item, connection_values),
        )
        for item in sorted(results, key=lambda item: item.host)
    )
    reachable = all(item.status == REACHABLE for item in outcomes)
    status = JobStatus.SUCCESSFUL if return_code == 0 and reachable else JobStatus.FAILED
    run = _build_run(
        job_id=job_id,
        inventory_id=inventory_id,
        project_id=project_id,
        limit=limit,
        hosts=outcomes,
        return_code=return_code,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    _publish(session, run, artifacts=artifacts)
    return run


def _process_failure(outcome: ProcessOutcome) -> AppError | None:
    """Süreç sınırlarının aşılmasını hata sınıfına çevirir."""
    if outcome.oversized_stream is not None:
        return PingOutputTooLargeError(
            "Ping kabul edilen sınırdan çok çıktı üretti; işlem durduruldu.",
            details={"stream": outcome.oversized_stream},
        )
    if outcome.timed_out:
        return PingTimeoutError("Ping zaman aşımına uğradı ve durduruldu.")
    return None


def _parse_results(
    outcome: ProcessOutcome, hosts: Sequence[str]
) -> tuple[tuple[PingHostResult, ...] | None, AppError | None]:
    """Çıktıyı güvenli parser'dan geçirir ve boş-ama-başarısız hâli ayırır."""
    try:
        results = parse_ping_output(outcome.stdout_text, hosts)
    except PingInvalidOutputError as exc:
        return None, exc
    if outcome.return_code != 0 and all(item.status == NO_RESULT for item in results):
        # Sıfırdan farklı çıkış kodu **ve** hiç host bloğu yok: bu, geçerli bir
        # unreachable sonucu değil, ayrıştırılamayan bir çalıştırmadır.
        return None, PingInvalidOutputError("Ping çıktısı beklenen biçimde değil.")
    return results, None


def _safe_message(result: PingHostResult, connection_values: Sequence[str]) -> str | None:
    """Mesajı yalnız anlam taşıyan durumlar için, maskeleyerek döndürür.

    Ölçülen sızıntı: kapalı bir porta yapılan gerçek ping'de Ansible'ın mesajı
    ``connect to host 127.0.0.1 port 1: Connection refused`` biçimindedir; yani
    onay planının bilinçli olarak dışarı vermediği ``ansible_host`` ve
    ``ansible_port`` değerlerini API cevabına ve artifact'e geri taşırdı
    (MIMARI.md bölüm 7, GUVENLIK.md bölüm 3).

    Bu yüzden snapshot'taki bağlantı değerleri mesaj metninde maskelenir.
    Maskeleme bilinçli olarak agresiftir: fazladan maskelenen bir sayı zararsız,
    sızan bir adres değildir. Host **adı** maskelenmez; o zaten plandadır.
    """
    if result.status not in _MESSAGE_STATUSES:
        return None
    return _mask_connection_values(result.message, connection_values) or None


def _mask_connection_values(text: str, connection_values: Sequence[str]) -> str:
    """Snapshot'tan gelen bağlantı değerlerini metinde maskeler."""
    masked = text
    for value in connection_values:
        if value.isdigit():
            # Sayısal değer (port) yalnız tam eşleşmede maskelenir; aksi hâlde
            # başka sayıların içini parçalardı.
            masked = re.sub(rf"(?<![\w.]){re.escape(value)}(?![\w.])", REDACTED, masked)
        else:
            masked = masked.replace(value, REDACTED)
    return masked


def _build_run(
    *,
    job_id: str,
    inventory_id: int,
    project_id: int | None,
    limit: str | None,
    hosts: tuple[PingHostOutcome, ...],
    return_code: int | None,
    status: JobStatus,
    started_at: datetime,
    finished_at: datetime,
) -> PingRun:
    """Host sonuçlarından deterministik sıralı bir :class:`PingRun` kurar."""
    ordered = tuple(sorted(hosts, key=lambda item: item.name))
    counts = {REACHABLE: 0, UNREACHABLE: 0, FAILED: 0, NO_RESULT: 0}
    for item in ordered:
        counts[item.status] += 1
    return PingRun(
        job_id=job_id,
        job_type=PING_JOB_TYPE,
        status=status.value,
        inventory_id=inventory_id,
        project_id=project_id,
        limit=limit,
        return_code=return_code,
        started_at=started_at,
        finished_at=finished_at,
        summary=PingSummary(
            total=len(ordered),
            reachable=counts[REACHABLE],
            unreachable=counts[UNREACHABLE],
            failed=counts[FAILED],
            no_result=counts[NO_RESULT],
        ),
        hosts=ordered,
    )


# --- Yayımlama ve T3 ----------------------------------------------------------


def _publish(session: Session, run: PingRun, *, artifacts: JobArtifactStore) -> None:
    """Sonucu atomik yayımlar ve Job'u terminal duruma alır.

    Sonuç yazılamazsa Job yine de terminal ``failed`` yapılmaya çalışılır ve
    ``artifact_path`` boş bırakılır: yayımlanmamış bir dosyaya işaret eden kayıt
    bırakmak, olmayan bir kanıta güven verirdi.
    """
    try:
        artifact_path = artifacts.write_result(run.job_id, _artifact_document(run))
    except JobArtifactUnavailableError as exc:
        _finalize_quietly(session, run)
        raise PingArtifactWriteFailedError(
            "Ping sonucu kaydedilemedi.", details={"job_id": run.job_id}
        ) from exc

    try:
        finished = finish_job(
            session,
            run.job_id,
            status=JobStatus(run.status),
            return_code=run.return_code,
            artifact_path=artifact_path,
            now=run.finished_at,
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        # Yayımlanmış `result.json` **silinmez**: görünür sonuç, terminal DB
        # commit'i başarısız olsa bile operatör incelemesi için korunur.
        raise PingArtifactWriteFailedError(
            "Ping işi terminal duruma alınamadı.", details={"job_id": run.job_id}
        ) from exc
    if not finished:
        raise PingArtifactWriteFailedError(
            "Ping işi terminal duruma alınamadı.", details={"job_id": run.job_id}
        )


def _finalize_quietly(session: Session, run: PingRun) -> None:
    """Sonuç yazılamadığında Job'u terminal ``failed`` yapmayı dener."""
    try:
        finish_job(
            session,
            run.job_id,
            status=JobStatus.FAILED,
            return_code=run.return_code,
            artifact_path=None,
            now=run.finished_at,
        )
        session.commit()
    except SQLAlchemyError:
        session.rollback()


def _artifact_document(run: PingRun) -> dict[str, Any]:
    """Artifact'in düz ve güvenli JSON gösterimi.

    stdout/stderr, hostvar, token, snapshot içeriği, private key veya inventory
    yolu, argv, environment ve controller dosya sistemi ayrıntısı bilinçli
    olarak **yoktur**.
    """
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job_id": run.job_id,
        "job_type": run.job_type,
        "status": run.status,
        "inventory_id": run.inventory_id,
        "project_id": run.project_id,
        "limit": run.limit,
        "return_code": run.return_code,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "summary": {
            "total": run.summary.total,
            "reachable": run.summary.reachable,
            "unreachable": run.summary.unreachable,
            "failed": run.summary.failed,
            "no_result": run.summary.no_result,
        },
        "hosts": [
            {"name": item.name, "status": item.status, "message": item.message}
            for item in run.hosts
        ],
    }
