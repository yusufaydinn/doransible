"""Inventory CRUD ve içerik servisi (T-201, T-202).

Servis iki sorumluluk taşır:

- **T-201:** inventory dosyasının kaydını yönetmek ve path güvenliğini
  uygulamak.
- **T-202:** kayıtlı bir inventory'nin host ve grup içeriğini, Ansible'ın
  kendi parser'ıyla ve maskelenmiş biçimde sunmak.

İş mantığı bilinçli olarak route katmanının dışındadır (route/service katman ayrımı sözleşmesi).
Fonksiyonlar session'ı ve izin verilen root listesini parametre olarak alır;
böylece HTTP katmanı olmadan da test edilebilirler.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models import Inventory, InventorySourceType, Project
from app.services.inventories.parser import (
    InventoryContents,
    ParserLimits,
    normalize_inventory,
    run_inventory_parser,
)
from app.services.projects.service import ProjectInactiveError, get_project
from app.services.security.paths import (
    PathIsNotAFileError,
    PathNotFoundError,
    ensure_existing_file,
    ensure_within_allowed_roots,
    normalize_filesystem_path,
)


class InventoryOutsideProjectError(AppError):
    """Inventory dosyası, bağlanmak istenen project kökünün dışında.

    Girdi biçimsel olarak geçerlidir; reddedilme sebebi politikadır, bu yüzden
    422 değil 403 döner (``path_not_allowed`` ile aynı sınıf hata).
    """

    status_code = 403
    code = "inventory_path_outside_project"


class InventoryPathUnavailableError(AppError):
    """Kayıtlı inventory dosyası artık kullanılabilir durumda değil.

    ``details["reason"]`` makine tarafından okunabilir sebebi taşır:
    ``missing`` veya ``not_a_file``. Kayıt oluşturma sırasındaki 422'den
    ayrılır: burada girdi değil, **kaydın dünyası** değişmiştir
    (``project_path_unavailable`` ile aynı yaklaşım).
    """

    status_code = 409
    code = "inventory_path_unavailable"


def create_inventory(
    session: Session,
    *,
    name: str,
    path: str,
    source_type: InventorySourceType,
    project_id: int | None = None,
    inventory_roots: Sequence[Path],
    project_roots: Sequence[Path],
) -> Inventory:
    """Var olan bir inventory dosyasını kaydeder.

    Dosyanın hangi sınıra tabi olduğu, project bağı istenip istenmediğine göre
    belirlenir (ADR-015):

    - **Standalone** (``project_id is None``): dosya ``inventory_roots``
      altında olmalıdır. Project root'ları burada geçerli değildir; bir project
      dizininin altındaki her dosya kendiliğinden kaydedilebilir inventory
      sayılmaz.
    - **Project'e bağlı**: dosya önce genel ``project_roots`` altında, sonra
      da ilgili aktif project'in **kendi kökü** altında olmalıdır.
      ``inventory_roots`` bu akışı ne genişletir ne daraltır.

    Kontrol sırası GUVENLIK.md bölüm 4'e uyar:

    ```text
    standalone:      normalize → inventory allowlist → dosya var mı / dosya mı

    project'e bağlı: normalize
                     → project allowlist (candidate)
                     → project var mı / aktif mi
                     → project kökü yeniden doğrulanır
                     → candidate project kökünün içinde mi
                     → dosya var mı / dosya mı
    ```

    Project akışında genel allowlist kontrolü **project sorgulanmadan önce**
    yapılır. Aksi hâlde endpoint, izin verilen alanın tamamen dışındaki bir
    path için project kaydının var olup olmadığını (404 ile 403 farkından)
    sızdırırdı; yetkisiz bir istemci project id'lerini bu farkla tarayabilirdi.

    Varlık kontrolü **en sonda** yapılır. Aksi hâlde endpoint, izin verilmeyen
    bir path için "var/yok" bilgisi sızdıran bir dosya sistemi sondası hâline
    gelirdi. Aynı sebeple izinsiz bir path için var olan ve olmayan dosya aynı
    403 cevabını üretir.

    Args:
        session: Aktif veritabanı session'ı.
        name: Kullanıcıya gösterilecek inventory adı.
        path: Kullanıcının girdiği ham dosya yolu.
        source_type: ``ini`` veya ``yaml``.
        project_id: Bağlanacak project; ``None`` ise inventory standalone'dur.
        inventory_roots: Standalone inventory için izin verilen root'lar.
            Boş liste hiçbir path'in kabul edilmemesi anlamına gelir
            (fail-closed).
        project_roots: Project kayıtları için izin verilen root'lar. Project
            bağı kurulurken project'in kayıtlı path'i bunlara karşı yeniden
            doğrulanır.

    Returns:
        Kaydedilmiş ``Inventory``.

    Raises:
        InvalidPathError: Path biçimsel olarak geçersizse.
        PathNotAllowedError: Standalone dosya inventory root'larının dışındaysa;
            project'e bağlı dosya project root'larının dışındaysa; veya
            project'in kayıtlı kökü artık project root'larının dışındaysa.
        NotFoundError: ``project_id`` verilmiş ama project yoksa.
        ProjectInactiveError: Bağlanmak istenen project pasifse.
        InventoryOutsideProjectError: Dosya project kökünün dışındaysa.
        PathNotFoundError: Dosya mevcut değilse.
        PathIsNotAFileError: Path mevcut ama normal bir dosya değilse.
    """
    normalized = normalize_filesystem_path(path)
    project = _ensure_path_within_boundary(
        session,
        normalized,
        project_id=project_id,
        inventory_roots=inventory_roots,
        project_roots=project_roots,
    )
    ensure_existing_file(normalized)

    inventory = Inventory(
        name=name.strip(),
        path=str(normalized),
        source_type=source_type,
        project_id=project.id if project is not None else None,
    )
    session.add(inventory)
    session.commit()
    return inventory


def list_inventories(session: Session, *, project_id: int | None = None) -> list[Inventory]:
    """Kayıtlı inventory'leri ada göre sıralı döndürür.

    Args:
        session: Aktif veritabanı session'ı.
        project_id: Verilirse yalnızca o project'e bağlı kayıtlar döner.
            Verilmezse standalone kayıtlar dâhil hepsi listelenir.
    """
    statement = select(Inventory)
    if project_id is not None:
        statement = statement.where(Inventory.project_id == project_id)
    statement = statement.order_by(Inventory.name, Inventory.id)
    return list(session.scalars(statement))


def get_inventory(session: Session, inventory_id: int) -> Inventory:
    """Tek bir inventory kaydını döndürür.

    Raises:
        NotFoundError: Kayıt yoksa.
    """
    inventory = session.get(Inventory, inventory_id)
    if inventory is None:
        raise NotFoundError(f"Inventory bulunamadı: {inventory_id}")
    return inventory


def get_inventory_hosts(
    session: Session,
    inventory_id: int,
    *,
    inventory_roots: Sequence[Path],
    project_roots: Sequence[Path],
    command: Sequence[str],
    limits: ParserLimits,
) -> InventoryContents:
    """Kayıtlı bir inventory'nin host ve grup içeriğini döndürür (T-202).

    Yalnızca **veritabanındaki kayıt** kullanılır; çağıran taraf path veya
    komut veremez. Kayıt anındaki kontroller kalıcı bir garanti olmadığı için
    (Tur 9'dan gelen kalıcı kural 8) path kullanım anında yeniden doğrulanır:
    dosya silinmiş, bir bağlantıyla kök dışına yönlendirilmiş, allowlist
    daraltılmış veya bağlı project pasife alınmış olabilir.

    Sıra, kayıt oluşturmadakiyle aynıdır:

    ```text
    normalize
    → güvenlik sınırı (standalone: inventory allowlist,
                       bağlı: project allowlist + project kökü)
    → dosya hâlâ var mı / dosya mı
    → parser
    → normalize + redaction
    ```

    Args:
        session: Aktif veritabanı session'ı.
        inventory_id: Kayıtlı inventory'nin kimliği.
        inventory_roots: Standalone inventory için izin verilen root'lar.
        project_roots: Project kayıtları için izin verilen root'lar.
        command: Parser komutu (argüman listesi).
        limits: Parser timeout ve çıktı boyutu sınırları.

    Returns:
        Kararlı sıralı ve maskelenmiş ``InventoryContents``.

    Raises:
        NotFoundError: Inventory (veya bağlı project) kaydı yoksa.
        PathNotAllowedError: Kayıtlı path artık izinli alanın dışındaysa.
        ProjectInactiveError: Bağlı project pasife alınmışsa.
        InventoryOutsideProjectError: Dosya artık project kökünün dışındaysa.
        InventoryPathUnavailableError: Dosya silinmişse veya dosya olmaktan
            çıkmışsa.
        InventoryParserUnavailableError: `ansible-core` kurulu değilse.
        InventoryParseTimeoutError: Parser zaman aşımına uğrarsa.
        InventoryParseFailedError: Dosya ayrıştırılamazsa.
        InventoryParserOutputTooLargeError: Çıktı sınırı aşılırsa.
        InventoryParserInvalidOutputError: Çıktı beklenen JSON değilse.
    """
    inventory = get_inventory(session, inventory_id)
    resolved = resolve_inventory_path(
        session,
        inventory,
        inventory_roots=inventory_roots,
        project_roots=project_roots,
    )
    raw_output = run_inventory_parser(resolved, command=command, limits=limits)
    return normalize_inventory(raw_output, inventory_id=inventory.id)


def resolve_inventory_path(
    session: Session,
    inventory: Inventory,
    *,
    inventory_roots: Sequence[Path],
    project_roots: Sequence[Path],
) -> Path:
    """Kayıtlı bir inventory'nin path'ini kullanım anında yeniden doğrular.

    Kontroller kayıt anındakiyle **aynı kodla** çalıştırılır; iki yerde iki
    farklı kural oluşmasını engeller.

    Returns:
        Doğrulanmış, normalize edilmiş dosya yolu.

    Raises:
        PathNotAllowedError: Path artık izinli alanın dışındaysa.
        NotFoundError: Bağlı project kaydı yoksa.
        ProjectInactiveError: Bağlı project pasifse.
        InventoryOutsideProjectError: Dosya project kökünün dışındaysa.
        InventoryPathUnavailableError: Dosya artık yoksa veya dosya değilse.
    """
    normalized = normalize_filesystem_path(inventory.path)
    _ensure_path_within_boundary(
        session,
        normalized,
        project_id=inventory.project_id,
        inventory_roots=inventory_roots,
        project_roots=project_roots,
    )

    try:
        ensure_existing_file(normalized)
    except PathNotFoundError as exc:
        raise InventoryPathUnavailableError(
            "Inventory dosyası artık mevcut değil.",
            details={"inventory_id": inventory.id, "reason": "missing"},
        ) from exc
    except PathIsNotAFileError as exc:
        raise InventoryPathUnavailableError(
            "Inventory path'i artık bir dosya değil.",
            details={"inventory_id": inventory.id, "reason": "not_a_file"},
        ) from exc

    return normalized


def _ensure_path_within_boundary(
    session: Session,
    normalized: Path,
    *,
    project_id: int | None,
    inventory_roots: Sequence[Path],
    project_roots: Sequence[Path],
) -> Project | None:
    """Path'in geçerli güvenlik sınırı içinde kaldığını doğrular (ADR-015).

    Standalone kayıtlar inventory root'larına, project'e bağlı kayıtlar önce
    project allowlist'ine sonra ilgili aktif project'in kendi köküne tabidir.

    Genel allowlist kontrolü project sorgusundan **önce** gelir: aksi hâlde
    403/404 farkı, izin verilmeyen bir path üzerinden project kaydının var olup
    olmadığını sızdıran bir oracle olurdu.

    Returns:
        Bağ istendiyse doğrulanmış ``Project``, aksi hâlde ``None``.
    """
    if project_id is None:
        ensure_within_allowed_roots(normalized, inventory_roots)
        return None

    ensure_within_allowed_roots(normalized, project_roots)
    project = _resolve_linkable_project(session, project_id)
    _ensure_within_project(normalized, project=project, project_roots=project_roots)
    return project


def _resolve_linkable_project(session: Session, project_id: int) -> Project:
    """Bağlanabilir (var olan ve aktif) bir project döndürür.

    Pasif project'e yeni inventory bağlanamaz: pasif kayıt "artık
    kullanılmıyor" demektir ve ona bağlı yeni bir kaynak oluşturmak bu
    işareti sessizce geçersiz kılardı.

    Raises:
        NotFoundError: Project kaydı yoksa.
        ProjectInactiveError: Project pasifse.
    """
    project = get_project(session, project_id)
    if not project.is_active:
        raise ProjectInactiveError(
            "Pasif project'e inventory bağlanamaz.",
            details={"project_id": project.id},
        )
    return project


def _ensure_within_project(
    candidate: Path,
    *,
    project: Project,
    project_roots: Sequence[Path],
) -> None:
    """Inventory dosyasının project kökü içinde kaldığını doğrular.

    Veritabanındaki project path'ine körü körüne güvenilmez: kayıt
    oluşturulduktan sonra dizin bir bağlantıyla değiştirilmiş veya allowlist
    daraltılmış olabilir. Bu yüzden kök yeniden normalize edilir ve project
    allowlist'i kayıt anındakiyle **aynı kodla** tekrar uygulanır.

    Karşılaştırma ``is_relative_to`` iledir; string prefix karşılaştırması
    ``<project>-evil`` gibi kardeş dizinleri yanlışlıkla içeri alırdı.

    Raises:
        PathNotAllowedError: Saklanan project path'i artık project
            allowlist'inin dışındaysa.
        InventoryOutsideProjectError: Dosya project kökünün dışındaysa.
    """
    project_root = normalize_filesystem_path(project.path)
    ensure_within_allowed_roots(project_root, project_roots)

    if not candidate.is_relative_to(project_root):
        # Mesaj sunucudaki gerçek yolları tekrarlamaz (GUVENLIK.md bölüm 3).
        raise InventoryOutsideProjectError(
            "Inventory dosyası, bağlanmak istenen project kökünün dışında.",
            details={"project_id": project.id},
        )
