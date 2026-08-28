"""Path normalizasyonu ve güvenlik kontrolleri.

GUVENLIK.md bölüm 4 gereği bütün project, inventory, staging ve artifact
yolları veritabanına yazılmadan önce normalize edilir. Normalizasyon,
karşılaştırmayı (duplicate tespiti) ve sonraki adımdaki allowlist
kontrolünü anlamlı kılar.

Bu modül bilinçli olarak bağımlılıksızdır (yalnızca stdlib + hata tipleri):
model, servis ve API katmanlarının hepsi çağırabilsin diye yan etkisi ve
veritabanı erişimi yoktur.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from app.core.errors import AppError, ValidationFailedError

MAX_PATH_LENGTH = 1024


class InvalidPathError(ValidationFailedError):
    """Verilen path normalize edilemedi veya kabul edilebilir değil."""

    code = "invalid_path"


class PathNotAllowedError(AppError):
    """Path, izin verilen project root'larının hiçbirinin altında değil.

    Girdi biçimsel olarak geçerlidir; sunucu erişimi politika gereği
    reddeder. Bu yüzden 422 değil 403 döner.
    """

    status_code = 403
    code = "path_not_allowed"


class PathNotFoundError(ValidationFailedError):
    """Path dosya sisteminde mevcut değil."""

    code = "path_not_found"


class PathIsNotADirectoryError(ValidationFailedError):
    """Path mevcut ancak dizin değil."""

    code = "path_not_a_directory"


class PathIsNotAFileError(ValidationFailedError):
    """Path mevcut ancak normal bir dosya değil."""

    code = "path_not_a_file"


def normalize_filesystem_path(raw: str) -> Path:
    """Kullanıcıdan gelen bir dosya sistemi yolunu kanonik hâle getirir.

    Project kökü, inventory dosyası, staging ve artifact yolları aynı
    normalizasyondan geçer (GUVENLIK.md bölüm 4); kural setinin tek bir yerde
    durması, farklı domainlerde farklı davranan iki normalizasyon oluşmasını
    engeller.

    Uygulanan adımlar:

    1. Baştaki/sondaki boşluklar atılır.
    2. ``~`` kullanıcı dizinine genişletilir.
    3. Path'in absolute olması zorunludur; relative path reddedilir.
    4. ``resolve()`` ile ``..``, ``.`` ve symlink'ler çözülür.

    Relative path kabul edilmez: sunucu sürecinin çalışma dizinine göre
    çözülürdü ve kullanıcının kastettiği yer olmazdı.

    Args:
        raw: Kullanıcının girdiği ham path.

    Returns:
        Absolute ve çözülmüş ``Path``.

    Raises:
        InvalidPathError: Path boşsa, NUL bayt içeriyorsa, relative ise,
            çok uzunsa veya işletim sistemi tarafından çözülemiyorsa.
    """
    if "\x00" in raw:
        raise InvalidPathError("Path geçersiz karakter içeriyor.")

    stripped = raw.strip()
    if not stripped:
        raise InvalidPathError("Path boş olamaz.")

    if len(stripped) > MAX_PATH_LENGTH:
        raise InvalidPathError(f"Path {MAX_PATH_LENGTH} karakterden uzun olamaz.")

    try:
        expanded = Path(stripped).expanduser()
    except RuntimeError as exc:
        raise InvalidPathError("Kullanıcı dizini belirlenemedi.") from exc

    if not expanded.is_absolute():
        raise InvalidPathError("Path absolute olmalıdır. Örnek: /srv/ansible/projeler/web")

    try:
        resolved = expanded.resolve()
    except OSError as exc:
        raise InvalidPathError("Path çözümlenemedi.") from exc

    if len(str(resolved)) > MAX_PATH_LENGTH:
        raise InvalidPathError(f"Çözümlenmiş path {MAX_PATH_LENGTH} karakterden uzun.")

    return resolved


def path_comparison_key(path: Path | str) -> str:
    """Aynı dizini gösteren farklı yazımları tek bir karşılaştırma anahtarına indirger.

    Windows dosya sistemleri case-insensitive'dir: ``C:\\Projeler`` ve
    ``c:\\projeler`` aynı dizindir. ``os.path.normcase`` Windows'ta küçük
    harfe çevirir ve ayraçları normalize eder. POSIX'te ise farklı casing
    gerçekten farklı dizin demektir; orada ``normcase`` girdiyi değiştirmez.

    Bu anahtar veritabanında ``projects.path_key`` sütununda saklanır ve
    unique index onun üzerindedir; böylece duplicate kontrolü çalışılan
    platformun dosya sistemi semantiğine uyar.
    """
    return os.path.normcase(str(path))


def ensure_within_allowed_roots(candidate: Path, allowed_roots: Sequence[Path]) -> Path:
    """Path'in izin verilen root'lardan birinin altında kaldığını doğrular.

    GUVENLIK.md bölüm 4'teki kontrol sırası uygulanır: hem aday hem root
    ``resolve()`` edilir ve karşılaştırma ``is_relative_to`` ile parça
    bazında yapılır. String prefix karşılaştırması kullanılmaz; ``/srv/ansible``
    root'u ``/srv/ansible-evil`` yolunu kapsıyor gibi görünmemelidir.

    Symlink kaçışı burada ayrıca ele alınmaz çünkü ``candidate``
    :func:`normalize_filesystem_path` tarafından zaten çözülmüştür: izin verilen
    root içindeki bir symlink dışarıyı gösteriyorsa aday path dışarıdaki
    gerçek hedefe çözülür ve bu kontrol onu reddeder.

    Args:
        candidate: Normalize edilmiş (absolute, çözülmüş) aday path.
        allowed_roots: İzin verilen project root'ları.

    Returns:
        Doğrulanmış ``candidate``.

    Raises:
        PathNotAllowedError: Allowlist boşsa veya path hiçbir root'un
            altında değilse. Boş allowlist "her şey serbest" değil,
            "hiçbir şey serbest değil" anlamına gelir (fail-closed).
    """
    if not allowed_roots:
        raise PathNotAllowedError("İzin verilen root tanımlı değil; hiçbir path kabul edilemez.")

    for root in allowed_roots:
        resolved_root = root.expanduser().resolve()
        if candidate == resolved_root or candidate.is_relative_to(resolved_root):
            return candidate

    # Mesaj sunucudaki izinli dizinleri açıklamaz (GUVENLIK.md bölüm 3) ve
    # hangi allowlist'in (project mi inventory mi) uygulandığını da söylemez;
    # fonksiyon her ikisi için de kullanılır.
    raise PathNotAllowedError("Path, izin verilen köklerin dışında.")


def ensure_existing_directory(candidate: Path) -> Path:
    """Path'in mevcut bir dizin olduğunu doğrular.

    Args:
        candidate: Normalize edilmiş aday path.

    Returns:
        Doğrulanmış ``candidate``.

    Raises:
        PathNotFoundError: Path dosya sisteminde yoksa.
        PathIsNotADirectoryError: Path mevcut ama dizin değilse.
    """
    if not candidate.exists():
        raise PathNotFoundError("Path dosya sisteminde bulunamadı.")
    if not candidate.is_dir():
        raise PathIsNotADirectoryError("Path bir dizin olmalıdır.")
    return candidate


def ensure_existing_file(candidate: Path) -> Path:
    """Path'in mevcut ve normal bir dosya olduğunu doğrular.

    Bu kontrol **daima** allowlist kontrolünden sonra çağrılmalıdır: aksi hâlde
    endpoint, izin verilmeyen bir path için "var/yok" ayrımı üreterek dosya
    sistemi sondasına dönüşür (GUVENLIK.md bölüm 4).

    Dizin ile normal olmayan dosyalar (FIFO, soket, aygıt) aynı hatayı üretir;
    ikisi de okunabilir bir inventory dosyası değildir.

    Args:
        candidate: Normalize edilmiş aday path.

    Returns:
        Doğrulanmış ``candidate``.

    Raises:
        PathNotFoundError: Path dosya sisteminde yoksa.
        PathIsNotAFileError: Path mevcut ama normal bir dosya değilse.
    """
    if not candidate.exists():
        raise PathNotFoundError("Path dosya sisteminde bulunamadı.")
    if not candidate.is_file():
        raise PathIsNotAFileError("Path bir dosya olmalıdır.")
    return candidate
