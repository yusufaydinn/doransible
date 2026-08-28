"""Project CRUD servisi (T-102).

İş mantığı bilinçli olarak route katmanının dışındadır (route/service katman ayrımı sözleşmesi).
Servis fonksiyonları session'ı ve izin verilen root listesini parametre olarak
alır; böylece HTTP katmanı olmadan da test edilebilirler.

Bu servis dosya sistemine **yazmaz**. Project dizini kullanıcıya aittir;
uygulama yalnızca kaydını tutar (MIMARI.md bölüm 5).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models import Project
from app.services.projects.discovery import (
    PlaybookScanResult,
    ScanLimits,
    ScanRootUnavailableError,
    discover_playbooks,
)
from app.services.security.paths import (
    PathIsNotADirectoryError,
    PathNotFoundError,
    ensure_existing_directory,
    ensure_within_allowed_roots,
    normalize_filesystem_path,
    path_comparison_key,
)


class ProjectAlreadyExistsError(AppError):
    """Aynı dizin için zaten bir project kaydı var."""

    status_code = 409
    code = "project_already_exists"


class ProjectInactiveError(AppError):
    """İşlem yalnızca aktif project kayıtlarında yapılabilir."""

    status_code = 409
    code = "project_inactive"


class ProjectPathUnavailableError(AppError):
    """Kayıtlı project dizini artık kullanılabilir durumda değil.

    ``details["reason"]`` makine tarafından okunabilir sebebi taşır:
    ``missing``, ``not_a_directory`` veya ``changed_during_scan``.
    """

    status_code = 409
    code = "project_path_unavailable"


def create_project(
    session: Session,
    *,
    name: str,
    path: str,
    description: str | None = None,
    allowed_roots: Sequence[Path],
) -> Project:
    """Yeni bir project kaydeder.

    Kontroller GUVENLIK.md bölüm 4 sırasıyla uygulanır:

    1. Path normalize edilir (``..``, symlink ve casing çözülür).
    2. İzin verilen root'ların altında mı diye bakılır.
    3. Mevcut bir dizin olduğu doğrulanır.
    4. Duplicate kaydı reddedilir.

    Sıra önemlidir: varlık kontrolü allowlist kontrolünden **sonra** yapılır.
    Aksi hâlde endpoint, izin verilmeyen bir path için "var/yok" bilgisi
    sızdıran bir dosya sistemi sondası hâline gelirdi.

    Args:
        session: Aktif veritabanı session'ı.
        name: Kullanıcıya gösterilecek project adı.
        path: Kullanıcının girdiği ham dizin yolu.
        description: Serbest açıklama.
        allowed_roots: İzin verilen project root'ları. Boş liste hiçbir
            path'in kabul edilmemesi anlamına gelir.

    Returns:
        Kaydedilmiş ``Project``.

    Raises:
        InvalidPathError: Path biçimsel olarak geçersizse.
        PathNotAllowedError: Path izin verilen root'ların dışındaysa.
        PathNotFoundError: Dizin mevcut değilse.
        PathIsNotADirectoryError: Path bir dizin değilse.
        ProjectAlreadyExistsError: Aynı dizin zaten kayıtlıysa.
    """
    normalized = normalize_filesystem_path(path)
    ensure_within_allowed_roots(normalized, allowed_roots)
    ensure_existing_directory(normalized)

    existing = find_project_by_path(session, normalized)
    if existing is not None:
        raise _duplicate_error(existing)

    project = Project(name=name.strip(), path=str(normalized), description=description)
    session.add(project)
    try:
        session.commit()
    except IntegrityError as exc:
        # Ön kontrol ile commit arasına giren eşzamanlı bir kayıt. Unique index
        # son savunmadır; onu da anlaşılır bir 409'a çeviriyoruz.
        session.rollback()
        conflicting = find_project_by_path(session, normalized)
        if conflicting is None:
            raise
        raise _duplicate_error(conflicting) from exc

    return project


def list_projects(session: Session, *, include_inactive: bool = False) -> list[Project]:
    """Kayıtlı project'leri ada göre sıralı döndürür.

    Pasif kayıtlar varsayılan olarak listelenmez; ``include_inactive`` ile
    istenebilir.
    """
    statement = select(Project)
    if not include_inactive:
        statement = statement.where(Project.is_active.is_(True))
    statement = statement.order_by(Project.name, Project.id)
    return list(session.scalars(statement))


def get_project(session: Session, project_id: int) -> Project:
    """Tek bir project kaydını döndürür.

    Raises:
        NotFoundError: Kayıt yoksa.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"Project bulunamadı: {project_id}")
    return project


def deactivate_project(session: Session, project_id: int) -> Project:
    """Project kaydını pasife alır.

    Dosya sistemine **dokunmaz**: project dizini, playbook'lar ve roller
    olduğu gibi kalır. Silinen tek şey kaydın aktif durumudur; geçmiş
    job'ların project referansı korunur.

    İşlem idempotenttir; zaten pasif bir kayıt için gereksiz UPDATE üretmez.

    Raises:
        NotFoundError: Kayıt yoksa.
    """
    project = get_project(session, project_id)
    if not project.is_active:
        return project

    project.is_active = False
    session.commit()
    return project


def resolve_project_root(project: Project, *, allowed_roots: Sequence[Path]) -> Path:
    """Kayıtlı project path'ini **kullanım anında** yeniden doğrular.

    Veritabanındaki path'e körü körüne güvenilmez: kayıt oluşturulduktan sonra
    dizin silinmiş, dosyaya dönüşmüş, kök dışına giden bir bağlantıyla
    değiştirilmiş veya allowlist yapılandırması daraltılmış olabilir. Kontroller
    kayıt anındakiyle **aynı kodla** çalıştırılır.

    Raises:
        PathNotAllowedError: Saklanan path artık allowlist dışındaysa.
        ProjectPathUnavailableError: Dizin yoksa veya dosyaya dönüştüyse.
    """
    root = normalize_filesystem_path(project.path)
    ensure_within_allowed_roots(root, allowed_roots)
    try:
        ensure_existing_directory(root)
    except PathNotFoundError as exc:
        raise ProjectPathUnavailableError(
            "Project dizini artık mevcut değil.",
            details={"project_id": project.id, "reason": "missing"},
        ) from exc
    except PathIsNotADirectoryError as exc:
        raise ProjectPathUnavailableError(
            "Project path'i artık bir dizin değil.",
            details={"project_id": project.id, "reason": "not_a_directory"},
        ) from exc
    return root


def list_project_playbooks(
    session: Session,
    project_id: int,
    *,
    allowed_roots: Sequence[Path],
    limits: ScanLimits,
) -> PlaybookScanResult:
    """Aktif bir project altındaki playbook adaylarını keşfeder.

    Veritabanındaki path'e körü körüne güvenilmez. Kayıt oluşturulduktan sonra
    dizin silinmiş, dosyaya dönüşmüş, kök dışına giden bir bağlantıyla
    değiştirilmiş veya allowlist yapılandırması daraltılmış olabilir. Bu yüzden
    kontroller kayıt anındakiyle **aynı kodla** yeniden çalıştırılır:

    1. Project bulunur ve aktif olduğu doğrulanır.
    2. Saklanan path yeniden normalize edilir (symlink dâhil çözülür).
    3. Allowlist yeniden uygulanır.
    4. Hâlâ mevcut bir dizin olduğu doğrulanır.

    Args:
        session: Aktif veritabanı session'ı.
        project_id: Project kaydının kimliği.
        allowed_roots: İzin verilen project root'ları.
        limits: Keşif sınırları.

    Returns:
        Deterministik sıralı, project köküne göreli path'ler içeren sonuç.

    Raises:
        NotFoundError: Project kaydı yoksa.
        ProjectInactiveError: Project pasifse.
        PathNotAllowedError: Saklanan path artık allowlist dışındaysa.
        ProjectPathUnavailableError: Dizin yoksa, dosyaya dönüştüyse veya
            tarama sırasında değiştiyse.
    """
    project = get_project(session, project_id)
    if not project.is_active:
        raise ProjectInactiveError(
            "Pasif project üzerinde playbook keşfi yapılamaz.",
            details={"project_id": project.id},
        )

    root = resolve_project_root(project, allowed_roots=allowed_roots)

    try:
        return discover_playbooks(root, project_id=project.id, limits=limits)
    except ScanRootUnavailableError as exc:
        raise ProjectPathUnavailableError(
            "Project dizini tarama sırasında değişti; sonuç güvenilir değil.",
            details={"project_id": project.id, "reason": "changed_during_scan"},
        ) from exc


def find_project_by_path(session: Session, normalized_path: Path) -> Project | None:
    """Normalize edilmiş bir path için kayıtlı project'i arar.

    Arama ``path`` üzerinde değil ondan türetilen ``path_key`` üzerinde yapılır;
    böylece Windows'ta farklı casing aynı kayda düşer.
    """
    statement = select(Project).where(Project.path_key == path_comparison_key(normalized_path))
    return session.scalars(statement).first()


def _duplicate_error(existing: Project) -> ProjectAlreadyExistsError:
    """Duplicate durumunu, kullanıcının ne yapacağını anlatan bir 409'a çevirir."""
    if existing.is_active:
        message = "Bu dizin zaten kayıtlı."
    else:
        message = "Bu dizin daha önce kaydedilmiş ve şu anda pasif durumda."
    return ProjectAlreadyExistsError(
        message,
        details={"project_id": existing.id, "is_active": existing.is_active},
    )
