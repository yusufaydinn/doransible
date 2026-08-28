"""Ping preview state deposu (T-204A).

GUVENLIK.md bölüm 2 ve 7 gereği gerçek execution öncesinde kullanıcı yetkili
planı görmeli ve açıkça onaylamalıdır. Ansible ping uzak hostta modül dosyası
ve süreç oluşturur; bu gerçek execution'dır. Bu yüzden akış ikiye ayrılır:
**preview** planı üretir, **confirm** (T-204B) onu çalıştırır.

Preview ile confirm arasında dondurulmuş snapshot'ın diskte durması gerektiği
için state dosya sistemindedir. İkinci bir veritabanı tablosu kurulmadı: aynı
yaşam döngüsünü iki yerde tutmak "satır var, dosya yok" desenkronizasyonu
üretirdi ve atomik tek-kullanım garantisi POSIX ``rename`` ile zaten mevcuttur.

Güvenlik özellikleri:

- Token 256 bit (:func:`secrets.token_bytes`), padding'siz base64url.
- Sunucuda **token değil yalnızca SHA-256 özeti** adres olarak kullanılır;
  diski okuyan biri kullanılabilir bir token elde edemez.
- Bütün dosya sistemi işlemleri **descriptor-relative**'dir. Preview kökü bir
  kez ``O_DIRECTORY | O_NOFOLLOW`` ile açılır ve sonraki her adım o
  descriptor'a göre (``dir_fd``) ilerler. Path birleştirip yeniden çözmek
  kontrol ile kullanım arasında değiş-tokuşa açıktır; ``Path.is_dir()`` /
  ``Path.is_symlink()`` gibi arka arkaya yapılan kontroller güvenlik garantisi
  **sayılmaz**.
- State önce doğrulanabilir adlı bir *building* dizininde tam olarak hazırlanır,
  fsync edilir ve ancak sonra atomik ``rename`` ile yayımlanır. Yarım state
  hiçbir zaman geçerli bir preview olarak görünmez.
- Claim, ``rename`` ile yapılır: iki eşzamanlı istekten yalnızca biri kazanır.
- Claim edilmiş state döndürülmeden önce snapshot yeniden hash'lenir ve
  meta'daki digest ile :func:`hmac.compare_digest` kullanılarak karşılaştırılır.
- Temizlik istemciden gelen hiçbir adı kullanmaz: yalnızca uygulamanın ürettiği
  64 haneli digest, tam biçim regex'i, kök descriptor'ı ve ``O_NOFOLLOW``.

**Ölçülmüş açık ve kapatılması.** Denetimde, yayımlanmış bir ``<digest>``
dizini dışarıdaki bir dizini gösteren symlink ile değiştirildiğinde eski
path-tabanlı uygulamanın symlink'i ``.claimed-<uuid>`` adına taşıdığı, dış
hedefe ``claim.json`` yazdığı ve temizliğin dışarıyı etkilediği gösterildi.
Descriptor-relative akış bu sınıfı yapısal olarak kapatır: hiçbir adım
symlink'i izlemez ve kök dışına çıkamaz.

**Platform sınırı.** Güvenli primitive'ler yalnızca POSIX'te vardır. Bulunmazsa
zayıf bir fallback ile devam **edilmez**; ping preview fail-closed biçimde
``ping_preview_unavailable`` üretir. Bu, ADR-017'deki "Windows control node
desteklenmez" sınırıyla tutarlıdır.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import MINIMUM_CLAIM_STALE_SECONDS
from app.core.errors import AppError

# 256 bit entropi. Base64url (padding'siz) karşılığı tam 43 karakterdir.
TOKEN_BYTES = 32
TOKEN_LENGTH = 43

META_FILENAME = "meta.json"
SNAPSHOT_FILENAME = "inventory-targets.yml"
CLAIM_FILENAME = "claim.json"

# Preview dizininde bulunmasına izin verilen dosyalar. Temizlik yalnızca bu
# adları siler; beklenmeyen bir içerik sessizce yok edilmez.
KNOWN_FILENAMES = (META_FILENAME, SNAPSHOT_FILENAME, CLAIM_FILENAME)

# Okuma üst sınırları. Dosyaları uygulama kendisi yazar; sınır, bozulmuş veya
# dışarıdan şişirilmiş bir dosyanın süreci belleğe boğmasını engeller.
MAX_META_BYTES = 65_536
MAX_SNAPSHOT_BYTES = 5_000_000

# Meta'da bulunması **zorunlu** alanlar. Eksik veya bozuk meta güvenilir bir
# record üretemez; state tüketilip temizlenir.
REQUIRED_META_FIELDS = (
    "schema_version",
    "created_at",
    "expires_at",
    "inventory_id",
    "requested_by",
    "limit",
    "host_count",
    "host_key_policy",
    "operation",
    "snapshot_sha256",
)

SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_OPERATION = "ansible.builtin.ping"
SUPPORTED_HOST_KEY_POLICIES = frozenset({"strict", "accept_new"})
MAX_META_LIMIT_LENGTH = 4096

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CLAIMED_PATTERN = re.compile(
    r"^[0-9a-f]{64}\.claimed-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_BUILDING_PATTERN = re.compile(r"^building-[0-9a-f]{32}$")

# Descriptor-relative çalışmak için gereken syscall'lar. Biri bile yoksa güvenli
# akış kurulamaz ve zayıf bir fallback'e düşülmez.
_REQUIRED_DIR_FD_FUNCTIONS = (os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir)


class PreviewStoreUnavailableError(AppError):
    """Preview state oluşturulamadı, okunamadı veya temizlenemedi.

    Altyapı hatasıdır; kullanıcıya dosya sistemi ayrıntısı gösterilmez. Bu hata
    sessizce yutulmaz: temizlenemeyen bir state diskte kalır ve bunu ``204``
    ile örtmek, sorunu fark edilemez hâle getirirdi.
    """

    status_code = 500
    code = "ping_preview_unavailable"


class PreviewNotFoundError(AppError):
    """Token bilinmiyor, süresi geçmiş, bağlamla eşleşmiyor veya kullanılmış.

    Dört durum tek kodla döner. Kullanılmış bir token'ın dizini silindiği için
    "kullanılmış" ile "hiç var olmamış" her zaman ayırt **edilemez**; kalıcı
    bir tombstone tutmak yalnızca yeni bir kalıcı iz üretirdi. Bu yüzden
    ``already_used`` diye bir garanti verilmez ve ``reason`` yalnızca
    ``expired``, ``mismatch`` veya ``invalid`` olur.
    """

    status_code = 409
    code = "ping_preview_invalid"


class _RootMissingError(Exception):
    """Preview kökü henüz yok. Yalnızca modül içinde kullanılır."""


class _DirectorySwappedError(OSError):
    """Açık descriptor ile isimdeki girdi artık aynı nesne değil."""


@dataclass(frozen=True)
class PreviewRecord:
    """Claim edilmiş bir preview state'inin doğrulanmış içeriği.

    Bilinçli olarak **path taşımaz**. Path taşımak, tüketicinin (T-204B) aynı
    dizine yeniden path üzerinden erişmesini davet ederdi; bu modülün kapattığı
    açık tam olarak budur. Dondurulmuş snapshot'ın kendisi zaten burada,
    doğrulanmış olarak bulunur.

    Attributes:
        directory_name: Kök altındaki claim edilmiş dizinin adı. Uygulama
            tarafından üretilmiştir; istemciden gelen hiçbir parça içermez.
        meta: Zorunlu alanları doğrulanmış state meta verisi.
        snapshot_text: SHA-256 özeti meta ile eşleşen dondurulmuş snapshot.
    """

    directory_name: str
    meta: dict[str, Any]
    snapshot_text: str


class PreviewStore:
    """Dosya sistemi üzerinde ping preview state'i yönetir.

    Args:
        root: ``app-data/ping-previews`` dizini.
        ttl_seconds: Yayımlanmış bir preview'ın geçerlilik süresi.
        claim_stale_seconds: Claim edilmiş bir state'in terk edilmiş sayılması
            için gereken süre.

    Raises:
        ValueError: ``claim_stale_seconds`` güvenli alt sınırın altındaysa.
            Değer sessizce yükseltilmez: yapılandırmayı sessizce değiştirmek,
            operatörün ayarladığını sandığı politikayla gerçekte uygulanan
            politikayı ayırırdı.
    """

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: float,
        claim_stale_seconds: float,
    ) -> None:
        if not math.isfinite(ttl_seconds) or not math.isfinite(claim_stale_seconds):
            raise ValueError("Preview süre değerleri sonlu olmalıdır.")
        if claim_stale_seconds < MINIMUM_CLAIM_STALE_SECONDS:
            raise ValueError(
                f"claim_stale_seconds en az {MINIMUM_CLAIM_STALE_SECONDS:.0f} saniye olmalıdır."
            )
        self._root = root
        self._ttl_seconds = ttl_seconds
        self._claim_stale_seconds = claim_stale_seconds

    @property
    def root(self) -> Path:
        """Preview kökü."""
        return self._root

    @property
    def ttl_seconds(self) -> float:
        """Yayımlanmış preview'ın geçerlilik süresi."""
        return self._ttl_seconds

    # --- Yayımlama ---------------------------------------------------------

    def publish(
        self,
        *,
        meta: dict[str, Any],
        snapshot_text: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        """State'i hazırlar ve atomik olarak yayımlar; token'ı döndürür.

        State önce ``building-<32 hex>`` adlı bir dizinde tam olarak kurulur,
        dosyalar fsync edilir, ardından **kök descriptor'ına göre** atomik
        ``rename`` ile digest adına taşınır. Yarım hazırlanmış bir state hiçbir
        zaman geçerli preview adı altında görünmez.

        Returns:
            ``(token, expires_at)`` ikilisi. Token yalnızca burada döner ve
            sunucuda saklanmaz.

        Raises:
            PreviewStoreUnavailableError: State yazılamazsa veya güvenli dosya
                sistemi primitive'leri bu platformda yoksa.
        """
        moment = now or datetime.now(UTC)
        expires_at = moment + timedelta(seconds=self._ttl_seconds)
        token = _generate_token()
        digest = token_digest(token)

        payload = dict(meta)
        payload["created_at"] = moment.isoformat()
        payload["expires_at"] = expires_at.isoformat()
        payload["snapshot_sha256"] = _snapshot_digest(snapshot_text)

        building = f"building-{secrets.token_hex(16)}"
        with self._root_descriptor(create=True) as root_fd:
            try:
                os.mkdir(building, 0o700, dir_fd=root_fd)
            except OSError as exc:
                raise PreviewStoreUnavailableError("Ping önizleme durumu oluşturulamadı.") from exc
            try:
                with _child_directory(root_fd, building) as build_fd:
                    os.fchmod(build_fd, 0o700)
                    _write_private_file(build_fd, SNAPSHOT_FILENAME, snapshot_text)
                    _write_private_file(build_fd, META_FILENAME, _to_json(payload))
                    os.fsync(build_fd)
                os.rename(building, digest, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                os.fsync(root_fd)
            except OSError as exc:
                self._remove_entry(root_fd, building)
                raise PreviewStoreUnavailableError("Ping önizleme durumu oluşturulamadı.") from exc

        return token, expires_at

    # --- Claim -------------------------------------------------------------

    def claim(
        self,
        token: str,
        *,
        inventory_id: int,
        requested_by: str,
        now: datetime | None = None,
    ) -> PreviewRecord:
        """Token'ı atomik olarak claim eder ve doğrulanmış state'i döndürür.

        Claim, kök descriptor'ına göre yapılan bir ``rename``'dir. Aynı kaynağı
        hedefleyen iki eşzamanlı çağrıdan yalnızca biri başarılı olur.

        State döndürülmeden önce dört şey doğrulanır: meta'nın zorunlu
        alanları, son kullanma zamanı, planın bağlandığı ``inventory_id`` ile
        aktör, ve snapshot'ın SHA-256 özeti. Bunlardan biri tutmazsa state
        tüketilmiş sayılır, temizlenir ve ``ping_preview_invalid`` döner —
        token tekrar kullanılabilir bırakılmaz.

        Args:
            token: İstemcinin gönderdiği onay token'ı.
            inventory_id: İsteğin URL'sinden gelen inventory kimliği.
            requested_by: Geçerli aktör (:attr:`Settings.local_actor`).
            now: Test edilebilirlik için geçerli an.

        Raises:
            PreviewNotFoundError: Token biçimsiz, bilinmiyor, süresi geçmiş,
                bağlamla eşleşmiyor veya daha önce kullanılmışsa.
            PreviewStoreUnavailableError: Altyapı arızasında (izin, I/O, kök
                güvenliği). ``rename``'in **her** hatası 409 sayılmaz.
        """
        moment = now or datetime.now(UTC)
        digest = self._validated_digest(token)
        claimed = f"{digest}.claimed-{uuid.uuid4()}"

        try:
            with self._root_descriptor(create=False) as root_fd:
                self._rename_for_claim(root_fd, digest, claimed)
                meta, snapshot_text = self._read_claimed_state(root_fd, claimed, moment)
                reason = _validation_failure(
                    meta,
                    snapshot_text,
                    moment=moment,
                    inventory_id=inventory_id,
                    requested_by=requested_by,
                )
                if reason is not None:
                    # State tüketilmiştir; temizlik başarısız olsa bile token
                    # ölüdür ve artık claim edilemez. Ancak temizlenemeyen
                    # state bir altyapı arızasıdır; cancel bunu idempotent 204
                    # olarak örtemez.
                    self._remove_invalid_entry(root_fd, claimed)
                    raise PreviewNotFoundError(_REASON_MESSAGES[reason], details={"reason": reason})
        except _RootMissingError as exc:
            # Kök yoksa hiçbir state var olamaz; bu bilinmeyen token ile aynıdır.
            raise _unknown_preview() from exc

        return PreviewRecord(directory_name=claimed, meta=meta, snapshot_text=snapshot_text)

    def discard(self, record: PreviewRecord) -> None:
        """Claim edilmiş bir state'i dar kapsamlı biçimde siler.

        Raises:
            PreviewStoreUnavailableError: State temizlenemezse. Sessizce
                başarı gösterilmez; aksi hâlde beklenmeyen bir dosya yüzünden
                diskte kalan state fark edilemezdi.
        """
        try:
            with self._root_descriptor(create=False) as root_fd:
                removed = self._remove_entry(root_fd, record.directory_name)
        except _RootMissingError as exc:
            raise PreviewStoreUnavailableError("Ping önizleme durumu temizlenemedi.") from exc
        if not removed:
            raise PreviewStoreUnavailableError("Ping önizleme durumu temizlenemedi.")

    # --- Süpürme -----------------------------------------------------------

    def sweep(self, *, now: datetime | None = None) -> int:
        """Terk edilmiş state'leri toplar ve silinen girdi sayısını döndürür.

        Üç sınıf temizlenir:

        - **Yayımlanmış ama claim edilmemiş** preview: ``meta.expires_at``
          geçmişse.
        - **Claim edilmiş** state: yalnızca ayrı ve daha yüksek claim-stale
          eşiğini aşmışsa. Normal preview TTL'si burada **geçerli değildir**;
          aksi hâlde başka bir isteğin süpürücüsü, o an çalışan bir
          execution'ın dondurulmuş snapshot'ını silebilirdi.
        - **Building** dizini: yayımlanamadan çökmüş hazırlık artığı.

        Tembel çalışır (arka plan zamanlayıcı yoktur). Tek tek girdilerdeki
        hatalar yutulur — süpürme, asıl isteğin başarısını engellememelidir.
        Kökün **güvenli biçimde açılamaması** ise yutulmaz: o, temizliğin değil
        deponun kendisinin arızasıdır.
        """
        moment = now or datetime.now(UTC)
        removed = 0
        try:
            with self._root_descriptor(create=False) as root_fd:
                for name in self._directory_names(root_fd):
                    if self._is_collectible(root_fd, name, moment):
                        removed += int(self._remove_entry(root_fd, name))
        except _RootMissingError:
            return 0
        return removed

    # --- İç yardımcılar ----------------------------------------------------

    def _is_collectible(self, root_fd: int, name: str, moment: datetime) -> bool:
        """Girdi terk edilmiş sayılır mı.

        Eşleşmeyen adlar, symlink'ler ve dosyalar bilinçli olarak korunur:
        adı uygulamanın ürettiği biçimlerden birine uymayan hiçbir şeye
        dokunulmaz.
        """
        if _DIGEST_PATTERN.fullmatch(name):
            return self._is_expired(root_fd, name, moment)
        if _CLAIMED_PATTERN.fullmatch(name):
            return self._is_claim_stale(root_fd, name, moment)
        if _BUILDING_PATTERN.fullmatch(name):
            return self._age_seconds(root_fd, name, moment) > self._claim_stale_seconds
        return False

    def _validated_digest(self, token: str) -> str:
        """Token biçimini doğrular ve digest'i döndürür.

        İstemciden gelen değer **hiçbir zaman** doğrudan path parçası olmaz;
        ad yalnızca uygulamanın ürettiği 64 haneli digest'ten kurulur.
        """
        if not _TOKEN_PATTERN.fullmatch(token):
            raise _unknown_preview()
        digest = token_digest(token)
        if not _DIGEST_PATTERN.fullmatch(digest):  # pragma: no cover - sha256 garantisi
            raise _unknown_preview()
        return digest

    def _rename_for_claim(self, root_fd: int, digest: str, claimed: str) -> None:
        """Yayımlanmış state'i claim adına taşır ve hatayı doğru sınıflandırır.

        ``rename``'in **her** hatası "token geçersiz" değildir: kaynak gerçekten
        yoksa veya dizin değilse token bilinmiyor demektir (409); izin, I/O ve
        diğer arızalar ise altyapı hatasıdır (500) ve 409 ile örtülmemelidir.
        """
        try:
            os.rename(digest, claimed, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise _unknown_preview() from exc
        except OSError as exc:
            raise PreviewStoreUnavailableError("Ping önizleme durumu okunamadı.") from exc

    def _read_claimed_state(
        self, root_fd: int, claimed: str, moment: datetime
    ) -> tuple[dict[str, Any], str]:
        """Claim edilmiş dizini güvenli descriptor üzerinden okur.

        Dizin ``O_DIRECTORY | O_NOFOLLOW`` ile açılır ve açık descriptor'ın
        kimliği isimdeki girdinin kimliğiyle karşılaştırılır; kontrol ile
        kullanım arasında yapılan bir değiş-tokuş böylece yakalanır. Dosyalar da
        ``O_NOFOLLOW`` ile açılır: bilinen bir ada konmuş symlink izlenmez.
        """
        try:
            with _child_directory(root_fd, claimed) as state_fd:
                raw_meta = _read_private_file(state_fd, META_FILENAME, MAX_META_BYTES)
                snapshot_text = _read_private_file(state_fd, SNAPSHOT_FILENAME, MAX_SNAPSHOT_BYTES)
                _write_private_file(
                    state_fd,
                    CLAIM_FILENAME,
                    _to_json({"claimed_at": moment.isoformat()}),
                )
                os.fsync(state_fd)
        except (OSError, ValueError) as exc:
            self._remove_entry(root_fd, claimed)
            raise PreviewStoreUnavailableError("Ping önizleme durumu okunamadı.") from exc

        meta = _decode_meta(raw_meta)
        if meta is None:
            self._remove_invalid_entry(root_fd, claimed)
            raise PreviewNotFoundError(_REASON_MESSAGES["mismatch"], details={"reason": "mismatch"})
        return meta, snapshot_text

    def _remove_invalid_entry(self, root_fd: int, name: str) -> None:
        """Geçersiz state'i temizler; başarısızlığı 409/204 ile örtmez."""
        if not self._remove_entry(root_fd, name):
            raise PreviewStoreUnavailableError("Ping önizleme durumu temizlenemedi.")

    @contextlib.contextmanager
    def _root_descriptor(self, *, create: bool) -> Iterator[int]:
        """Preview kökünü ``O_DIRECTORY | O_NOFOLLOW`` ile açar.

        Kökün kendisi bir symlink ise açma ``ELOOP`` ile başarısız olur ve
        işlem fail-closed biçimde ``ping_preview_unavailable`` üretir.
        """
        _require_secure_filesystem()
        if create:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PreviewStoreUnavailableError("Ping önizleme kökü hazırlanamadı.") from exc
        try:
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise _RootMissingError from exc
        except OSError as exc:
            raise PreviewStoreUnavailableError(
                "Ping önizleme kökü güvenli biçimde açılamadı."
            ) from exc
        try:
            with contextlib.suppress(OSError):
                os.fchmod(root_fd, 0o700)
            yield root_fd
        finally:
            os.close(root_fd)

    def _directory_names(self, root_fd: int) -> list[str]:
        """Kökün doğrudan alt **dizinlerinin** adları; symlink izlenmez."""
        try:
            names = os.listdir(root_fd)
        except OSError:  # pragma: no cover - kök yeni açıldı
            return []
        found = []
        for name in names:
            try:
                status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(status.st_mode):
                found.append(name)
        return found

    def _read_meta(self, root_fd: int, name: str) -> dict[str, Any] | None:
        """Girdinin meta verisini güvenli descriptor üzerinden okur."""
        try:
            with _child_directory(root_fd, name) as state_fd:
                raw = _read_private_file(state_fd, META_FILENAME, MAX_META_BYTES)
        except (OSError, ValueError):
            return None
        return _decode_meta(raw)

    def _is_expired(self, root_fd: int, name: str, moment: datetime) -> bool:
        """Yayımlanmış bir preview'ın süresi dolmuş mu."""
        meta = self._read_meta(root_fd, name)
        if meta is None:
            # Meta okunamıyorsa state zaten kullanılamaz; yaşına bakılır.
            return self._age_seconds(root_fd, name, moment) > self._ttl_seconds
        expires_at = _parse_timestamp(meta.get("expires_at"))
        return expires_at is None or expires_at <= moment

    def _is_claim_stale(self, root_fd: int, name: str, moment: datetime) -> bool:
        """Claim edilmiş bir state terk edilmiş sayılır mı.

        Birincil kaynak ``claim.json`` içindeki ``claimed_at``'tir. Dosya henüz
        yazılmamış olabilir (rename ile yazma arasındaki kısa pencere); o
        durumda dosya sistemi zamanına düşülür.
        """
        claimed_at = self._claimed_at(root_fd, name)
        if claimed_at is not None:
            return (moment - claimed_at).total_seconds() > self._claim_stale_seconds
        return self._age_seconds(root_fd, name, moment) > self._claim_stale_seconds

    def _claimed_at(self, root_fd: int, name: str) -> datetime | None:
        """``claim.json`` içindeki claim zamanını okur."""
        try:
            with _child_directory(root_fd, name) as state_fd:
                raw = _read_private_file(state_fd, CLAIM_FILENAME, MAX_META_BYTES)
        except (OSError, ValueError):
            return None
        data = _decode_meta(raw)
        if data is None:
            return None
        return _parse_timestamp(data.get("claimed_at"))

    def _age_seconds(self, root_fd: int, name: str, moment: datetime) -> float:
        """Girdinin son değişiklik zamanına göre yaşı.

        ``mtime`` kullanılır: dizine son yazma anını gösteren zaman odur.
        ``ctime`` bilinçli olarak alınmaz — ``rename`` onu güncellediği için
        claim edilmiş bir state'in ``ctime``'ı claim anını, ``claim.json``
        yazımıyla birlikte ``mtime``'ı da aynı anı yansıtır.
        """
        try:
            status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            return 0.0
        return moment.timestamp() - status.st_mtime

    def _remove_entry(self, root_fd: int, name: str) -> bool:
        """Tek bir preview dizinini **dar kapsamlı** biçimde siler.

        ``shutil.rmtree`` bilinçli olarak kullanılmaz. Silme yalnızca kök
        descriptor'ına göre, adı bilinen biçimlerden birine uyan bir girdide ve
        yalnızca :data:`KNOWN_FILENAMES` içindeki dosyalar için yapılır.
        Beklenmeyen bir içerik varsa ``rmdir`` başarısız olur ve dizin
        **korunur**; sessizce yok edilmez.

        ``unlink`` symlink'i izlemez: bilinen bir ada konmuş symlink'in
        kendisi silinir, gösterdiği dış hedefe dokunulmaz.

        Returns:
            Dizin gerçekten silindiyse ``True``.
        """
        if not _is_known_shape(name):
            return False
        try:
            with _child_directory(root_fd, name) as state_fd:
                for filename in KNOWN_FILENAMES:
                    try:
                        os.unlink(filename, dir_fd=state_fd)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        return False
                # Ada göre yapılacak `rmdir` öncesinde kimlik yeniden
                # doğrulanır: az önce boşalttığımız dizinle silinecek girdinin
                # aynı nesne olduğu burada kanıtlanır.
                _assert_same_entry(state_fd, root_fd, name)
        except OSError:
            return False
        try:
            os.rmdir(name, dir_fd=root_fd)
        except OSError:
            return False
        return True


def token_digest(token: str) -> str:
    """Token'ın SHA-256 özetini döndürür.

    Sunucuda saklanan tek adres budur; token'ın kendisi hiçbir dosyaya,
    log satırına veya hata cevabına yazılmaz.
    """
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def secure_filesystem_available() -> bool:
    """Descriptor-relative güvenli primitive'ler bu platformda var mı."""
    if os.name != "posix":
        return False
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return False
    return all(function in os.supports_dir_fd for function in _REQUIRED_DIR_FD_FUNCTIONS)


_REASON_MESSAGES = {
    "invalid": "Ping önizlemesi geçerli değil. Planı yeniden oluşturun.",
    "expired": "Ping önizlemesinin süresi doldu. Planı yeniden oluşturun.",
    "mismatch": "Ping önizlemesi bu istekle eşleşmiyor. Planı yeniden oluşturun.",
}


def _unknown_preview() -> PreviewNotFoundError:
    """Bilinmeyen, biçimsiz veya kullanılmış token için ortak hata."""
    return PreviewNotFoundError(_REASON_MESSAGES["invalid"], details={"reason": "invalid"})


def _require_secure_filesystem() -> None:
    """Güvenli primitive'ler yoksa fail-closed davranır.

    Zayıf bir fallback (path birleştirme + ardışık ``is_symlink`` kontrolleri)
    bilinçli olarak **yoktur**: o kontroller kullanım anında değiş-tokuşa
    açıktır ve güvenlik garantisi sayılamaz.
    """
    if not secure_filesystem_available():
        raise PreviewStoreUnavailableError(
            "Ping önizlemesi bu platformda güvenli biçimde çalıştırılamıyor."
        )


@contextlib.contextmanager
def _child_directory(parent_fd: int, name: str) -> Iterator[int]:
    """Alt dizini ``O_DIRECTORY | O_NOFOLLOW`` ile açar ve kimliğini doğrular."""
    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _assert_same_entry(child_fd, parent_fd, name)
        yield child_fd
    finally:
        os.close(child_fd)


def _assert_same_entry(child_fd: int, parent_fd: int, name: str) -> None:
    """Açık descriptor ile isimdeki girdinin aynı nesne olduğunu doğrular.

    ``O_NOFOLLOW`` açma anında symlink'i reddeder; bu kontrol ise açmadan sonra
    yapılan bir değiş-tokuşu yakalar. ``(st_dev, st_ino)`` ikilisi dosya
    sistemi genelinde bir nesneyi tekilleştirir.
    """
    opened = os.fstat(child_fd)
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _DirectorySwappedError("preview girdisi kayboldu") from exc
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise _DirectorySwappedError("preview girdisi değiştirildi")


def _write_private_file(dir_fd: int, name: str, content: str) -> None:
    """Dosyayı dizin descriptor'ına göre, 0600 izniyle ve fsync ederek yazar.

    ``O_EXCL``: var olan bir dosyanın üzerine yazılmaz. ``O_NOFOLLOW``: aynı ada
    konmuş bir symlink izlenmez.
    """
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _read_private_file(dir_fd: int, name: str, max_bytes: int) -> str:
    """Dosyayı dizin descriptor'ına göre, symlink izlemeden okur.

    Raises:
        ValueError: İçerik üst sınırı aşarsa veya UTF-8 değilse.
        OSError: Dosya yoksa, symlink ise veya okunamazsa.
    """
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"{name} beklenenden büyük")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} UTF-8 değil") from exc


def _is_known_shape(name: str) -> bool:
    """Ad, uygulamanın ürettiği biçimlerden birine uyuyor mu."""
    return bool(
        _DIGEST_PATTERN.fullmatch(name)
        or _CLAIMED_PATTERN.fullmatch(name)
        or _BUILDING_PATTERN.fullmatch(name)
    )


def _decode_meta(raw: str) -> dict[str, Any] | None:
    """JSON metnini sözlüğe çevirir; bozuksa ``None`` döndürür."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _validation_failure(
    meta: dict[str, Any],
    snapshot_text: str,
    *,
    moment: datetime,
    inventory_id: int,
    requested_by: str,
) -> str | None:
    """Claim edilen state'in kullanılabilir olmama sebebini döndürür.

    Sıra bilinçlidir: önce yapı, sonra süre, sonra bağlam, en son içerik. Bütün
    başarısızlıklar state'i tüketir; bu yüzden aralarındaki fark bir oracle
    üretmez.

    Returns:
        ``None`` (geçerli), ``"expired"`` veya ``"mismatch"``.
    """
    if any(field not in meta for field in REQUIRED_META_FIELDS):
        # Eksik veya bozuk meta güvenilir bir record üretemez. "Bozuk" ile
        # "eşleşmiyor" ayrımı dışarı verilmez: ikisi de tek bir sonuç doğurur.
        return "mismatch"
    created_at = _parse_timestamp(meta.get("created_at"))
    expires_at = _parse_timestamp(meta.get("expires_at"))
    if not _meta_structure_is_valid(
        meta,
        created_at=created_at,
        expires_at=expires_at,
        snapshot_text=snapshot_text,
    ):
        return "mismatch"
    assert expires_at is not None  # `_meta_structure_is_valid` ile kanıtlandı.
    if expires_at <= moment:
        return "expired"
    if meta.get("inventory_id") != inventory_id:
        return "mismatch"
    if meta.get("requested_by") != requested_by:
        return "mismatch"
    recorded = meta.get("snapshot_sha256")
    if not isinstance(recorded, str) or not _DIGEST_PATTERN.fullmatch(recorded):
        return "mismatch"
    if not hmac.compare_digest(recorded, _snapshot_digest(snapshot_text)):
        return "mismatch"
    return None


def _meta_structure_is_valid(
    meta: dict[str, Any],
    *,
    created_at: datetime | None,
    expires_at: datetime | None,
    snapshot_text: str,
) -> bool:
    """Meta alanlarının tür, değer ve snapshot tutarlılığını doğrular."""
    schema_version = meta.get("schema_version")
    inventory_id = meta.get("inventory_id")
    requested_by = meta.get("requested_by")
    limit = meta.get("limit")
    host_count = meta.get("host_count")

    if (
        type(schema_version) is not int
        or schema_version != SUPPORTED_SCHEMA_VERSION
        or created_at is None
        or expires_at is None
        or expires_at <= created_at
        or type(inventory_id) is not int
        or inventory_id < 1
        or not isinstance(requested_by, str)
        or not requested_by
        or len(requested_by) > 100
        or not _valid_meta_limit(limit)
        or type(host_count) is not int
        or host_count < 1
        or meta.get("host_key_policy") not in SUPPORTED_HOST_KEY_POLICIES
        or meta.get("operation") != SUPPORTED_OPERATION
    ):
        return False
    return _snapshot_host_count(snapshot_text) == host_count


def _valid_meta_limit(value: Any) -> bool:
    """Meta limit'i `None` veya sınırlandırılmış, boş olmayan metin olmalıdır."""
    return value is None or (
        isinstance(value, str) and bool(value) and len(value) <= MAX_META_LIMIT_LENGTH
    )


def _snapshot_host_count(snapshot_text: str) -> int | None:
    """Target snapshot'ın beklenen dar yapısındaki host sayısını döndürür."""
    try:
        document = json.loads(snapshot_text)
    except ValueError:
        return None
    if not isinstance(document, dict) or set(document) != {"all"}:
        return None
    all_group = document.get("all")
    if not isinstance(all_group, dict) or set(all_group) != {"hosts"}:
        return None
    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict) or not all(
        isinstance(name, str) and isinstance(variables, dict) for name, variables in hosts.items()
    ):
        return None
    return len(hosts)


def _snapshot_digest(snapshot_text: str) -> str:
    """Dondurulmuş snapshot'ın SHA-256 özeti."""
    return hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()


def _to_json(payload: dict[str, Any]) -> str:
    """State dosyalarının kararlı JSON gösterimi."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _generate_token() -> str:
    """256 bitlik, padding'siz base64url token üretir."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")


def _parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 zaman damgasını timezone bilgisiyle çözer."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
