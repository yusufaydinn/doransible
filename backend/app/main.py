"""FastAPI uygulama giriş noktası.

Uygulama modül seviyesinde değil, factory ile oluşturulur::

    uvicorn app.main:create_app --factory

Böylece import etmek yan etki üretmez ve test'ler farklı ayarlarla kendi
uygulama örneğini kurabilir. Bu dosyada modül seviyesinde engine, thread veya
worker **yoktur**; hepsi lifespan'in ömrüne bağlıdır.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.api.router import api_router, root_router
from app.core.config import Settings, ensure_app_data_dirs, get_settings
from app.core.errors import register_exception_handlers
from app.db.session import create_db_engine
from app.services.execution import (
    PlaybookWorker,
    reconcile_quietly,
    reconcile_stale_playbook_jobs,
    sweep_stale_execution_runs,
)

_logger = logging.getLogger(__name__)

# Log metinleri **sabittir**: arızanın kendi metni işletim sisteminin hata
# dizesini, DSN'i veya bir workspace yolunu taşır ve log, bunların en kolay
# okunabildiği yerdir.
_LOG_RECOVERY_FAILED = "playbook execution recovery failed; worker not started"
_LOG_ENGINE_UNAVAILABLE = "playbook execution recovery skipped; database engine unavailable"
_LOG_WORKER_START_FAILED = "playbook worker could not be started; execution stays closed"
_LOG_SHUTDOWN_INCOMPLETE = "playbook worker still running; shutdown did not complete"

# Kapanış tamamlanamadığında yükseltilen **sabit** metin: path, DSN, Job kimliği
# ve exception içeriği taşımaz.
SHUTDOWN_INCOMPLETE_MESSAGE = "Playbook worker durmadı; kapanış tamamlanamadı."


def create_app(settings: Settings | None = None) -> FastAPI:
    """Uygulamayı oluşturur ve yapılandırır.

    Test'ler kendi ``Settings`` örneğini geçirerek izole bir ``app-data``
    dizini kullanabilir.
    """
    active_settings = settings or get_settings()
    ensure_app_data_dirs(active_settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Açılışta state'i uzlaştırır, kapanışta arka plan işini gerçekten kapatır.

        Açılış iki bağımsız toparlamadan oluşur:

        1. *Execution planı* (R1-V2). Crash pencereleri ancak burada kapanır:
           yayımlanmış ama kaydı yazılamamış workspace'ler, kaydı olup dizini
           kaybolmuş planlar ve yarım kalmış staging dizinleri toplanır. İşlem
           best-effort'tur; hatası uygulamayı açılmaktan alıkoymaz, çünkü
           güvenlik kararları buna bağlı değildir — kayıp workspace claim anında
           yeniden fail-closed kontrol edilir.
        2. *Playbook çalıştırma* (R1-V3C2A + R1-V3C2B + R1-V3C2C). Bu ikincisi
           **fail-closed**'dır ve sırası zorunludur; gerekçesi
           :func:`_start_playbook_runtime`'dadır.

        Kapanış :meth:`_PlaybookRuntime.shutdown`'a bırakılır ve o, arka plan
        işinin gerçekten bittiğini kanıtlayamazsa **başarıyla dönmez**.
        """
        _reconcile_execution_plans(active_settings)
        runtime = _start_playbook_runtime(active_settings)
        try:
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(
        title=active_settings.app_name,
        version=__version__,
        description="Ansible project, inventory ve job yönetimi için API.",
        lifespan=lifespan,
    )

    # GUVENLIK.md bölüm 10: CORS allowlist ile sınırlıdır, wildcard kullanılmaz.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    register_exception_handlers(app)
    app.include_router(root_router)
    app.include_router(api_router)
    return app


def _reconcile_execution_plans(settings: Settings) -> None:
    """Açılış reconciliation'ını kendi kısa ömürlü session'ında çalıştırır."""
    try:
        engine = create_db_engine(settings)
    except SQLAlchemyError:  # pragma: no cover - yapılandırma arızası
        return
    try:
        with Session(engine) as session:
            reconcile_quietly(
                session,
                workspace_root=settings.resolve_execution_plan_dir(),
                staging_stale_seconds=settings.execution_plan_staging_stale_seconds,
            )
    finally:
        engine.dispose()


@dataclass(slots=True)
class _PlaybookRuntime:
    """Lifespan'in sahiplendiği engine ve (varsa) arka plan worker'ı.

    İkisi de **lifespan'e aittir**: modül seviyesinde tutulan bir engine ya da
    worker, import etmeyi yan etkili yapar ve iki test uygulamasının aynı
    thread'i paylaşmasına yol açardı.
    """

    engine: Engine | None
    worker: PlaybookWorker | None

    def shutdown(self) -> None:
        """Önce worker'ı gerçekten durdurur, **sonra** engine'i bırakır.

        Sıra tersine çevrilemez: engine önce dispose edilseydi, hâlâ çalışan bir
        child'ın kirasını yenileyen heartbeat ve onu terminal yazacak son
        session, altından çekilmiş bir connection pool'a giderdi. Worker'ın
        :meth:`~app.services.execution.worker.PlaybookWorker.stop`'u child
        sonlandırılıp reap edilene ve iki thread de bitene kadar geri dönmez.

        ``stop`` ``False`` döndüğünde iki şey birden yapılır ve ikisi de
        gereklidir:

        - *Engine bırakılmaz.* Bir kapanışta havuzu kapatmamak en kötü ihtimalle
          süreç sonuna kadar duran bir kaynaktır; hâlâ çalışan bir execution'ın
          bağlantısını altından çekmek ise yarım kalmış bir Job'ı kaydedilemez
          hâle getirirdi.
        - *Kapanış başarıyla dönmez.* Sessizce dönmek, FastAPI lifespan'inin
          shutdown'ı tamamlanmış göstermesi olurdu: worker ve muhtemelen bir
          child hâlâ canlıyken süreç kapanmış sayılırdı. Arıza görünür olmalı,
          bu yüzden **sabit** metinli bir :class:`RuntimeError` yükselir.

        Worker sonradan yeniden durdurulabilir: canlı thread'in referansı
        ``stop``'ta korunur, dolayısıyla ikinci bir ``stop`` (veya ikinci bir
        :meth:`shutdown`) aynı thread'i yeniden bekler.

        Raises:
            RuntimeError: Worker join bütçesinde durmadıysa. Mesaj sabittir;
                path, DSN, Job kimliği veya exception içeriği taşımaz.
        """
        if self.worker is not None and not self.worker.stop():
            _logger.warning(_LOG_SHUTDOWN_INCOMPLETE)
            raise RuntimeError(SHUTDOWN_INCOMPLETE_MESSAGE)
        if self.engine is not None:
            self.engine.dispose()


def _start_playbook_runtime(settings: Settings) -> _PlaybookRuntime:
    """Playbook recovery'sini sırayla çalıştırır ve **ancak sonra** worker'ı açar.

    Sıra sabittir ve güvenlik gereği zorunludur::

        a. reconcile_stale_playbook_jobs   (kısa session/transaction)
        b. session tamamen kapanır
        c. sweep_stale_execution_runs      (crash run janitor'ı)
        d. yalnız (a) ve (c) başarılıysa **ve** worker açıksa: worker.start()

    **Neden bu sıra.** Janitor, ``running`` görünen her PLAYBOOK Job'ının
    çalışma dizinini kirasına bakmadan korur. Kirası dolmuş satırlar önce
    terminal yapılmazsa, çökmüş bir worker'ın bıraktığı dizin "aktif bir Job'a
    ait" sayılır ve hiçbir zaman toplanmaz. Ters sıra sessiz bir sızıntı
    üretirdi.

    **Neden fail-closed.** (a) veya (c) arıza verirse worker **başlatılmaz**.
    Bir arızayı "0 satır uzlaştırıldı" ya da "kök boş" diye okuyup ardından
    çalıştırmaya başlamak, gerçekte hâlâ ``running`` duran bir satırın üstüne
    ikinci bir çalıştırma açmanın yolu olurdu. Açılış toparlaması bu yolda da
    denenmiş olur; yapılmayan tek şey arka plan çalıştırmasıdır.

    **Neden tek karar anı.** (a) ve (c) aynı timezone-aware ``now`` değerini
    alır: iki ayrı ``now``, aralarında geçen sürede kirası dolan bir satırı
    birinin canlı diğerinin ölü saymasına yol açabilirdi.

    **Açılıştaki bu tur, kapalı kalmış bir servisin tek garantisidir.** Süreç
    ayakta değilken hiçbir periyodik tur çalışmaz; süresi geçmiş orphan'lar bu
    yüzden ilk açılışta **hemen** taranır. Çalışan bir servis için verilen
    sınır ayrıdır ve ayarların kendisinde zorunlu kılınır
    (:data:`~app.core.config.MAX_ORPHAN_RETENTION_SECONDS`).

    **Worker açılamazsa da fail-closed.** Worker açık olmasına rağmen ``start``
    arıza verirse (thread oluşturulamaması gibi) arıza sabit bir metinle
    bildirilir ve execution kapalı kalır; uygulama yine açılır. ``start`` kendi
    içinde atomiktir, dolayısıyla o yolda yarım başlamış bir thread kalmaz.
    Engine'in sahipliği lifespan'de kalır ve :meth:`_PlaybookRuntime.shutdown`
    onu — worker'ın durduğunu doğruladıktan sonra — bırakır; burada erken
    dispose etmek, engine'i sızdırmamak için verilen sözü iki ayrı yere
    dağıtmak olurdu.

    Worker kapalıyken (varsayılan) toparlama yine uygulanır ama ne bir thread ne
    de tek bir executor çağrısı doğar.
    """
    try:
        engine = create_db_engine(settings)
    except SQLAlchemyError:  # pragma: no cover - yapılandırma arızası
        _logger.warning(_LOG_ENGINE_UNAVAILABLE)
        return _PlaybookRuntime(engine=None, worker=None)

    session_factory = _session_factory(engine)
    try:
        moment = datetime.now(UTC)
        # (a) ve (b): session `with` bloğuyla, janitor başlamadan önce kapanır.
        with Session(engine) as session:
            reconcile_stale_playbook_jobs(session, now=moment)
        # (c): kendi kısa ömürlü session'ını açıp kapatır, sonra temizler.
        sweep_stale_execution_runs(
            session_factory,
            execution_run_root=settings.resolve_execution_run_dir(),
            stale_seconds=settings.execution_run_stale_seconds,
            now=moment,
        )
    except Exception:
        _logger.warning(_LOG_RECOVERY_FAILED)
        return _PlaybookRuntime(engine=engine, worker=None)

    if not settings.playbook_worker_enabled:
        return _PlaybookRuntime(engine=engine, worker=None)

    worker = PlaybookWorker(session_factory=session_factory, settings=settings)
    try:
        worker.start()
    except Exception:
        # Fail-closed: arka plan çalıştırması açılmaz ve arıza sabit bir metinle
        # bildirilir. Kısmen başlamış bir thread `start`'ın kendi geri alımında
        # durdurulmuştur; worker yine de runtime'a verilir, çünkü engine'i
        # bırakma hakkı **yalnız** thread'lerin gerçekten durduğu kanıtlandığında
        # doğar ve o kanıt tek bir yerde — `shutdown`'da — üretilir. Burada
        # dispose etmek, o kanıtı ikinci bir kez ve farklı bir yerde üretmek
        # olurdu.
        _logger.warning(_LOG_WORKER_START_FAILED)
    return _PlaybookRuntime(engine=engine, worker=worker)


def _session_factory(engine: Engine) -> Callable[[], Session]:
    """Her çağrıda **yeni** bir Session üreten fabrika.

    Hazır bir session bilinçli olarak paylaşılmaz: worker kendi thread'inde
    çalışır ve :class:`~sqlalchemy.orm.Session` thread-safe değildir.
    """

    def factory() -> Session:
        return Session(engine, expire_on_commit=False)

    return factory
