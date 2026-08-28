"""Uygulama yapılandırması ve çalışma dizini hazırlığı."""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

# MIMARI.md bölüm 5'teki app-data düzeni.
# `ping-previews`, T-204A'nın onay öncesi dondurulmuş snapshot state'ini tutar.
# `execution-plans`, R1-V2'nin dondurulmuş execution workspace'lerini tutar.
_DATA_SUBDIRS = (
    "database",
    "projects",
    "inventories",
    "jobs",
    "staging",
    "secrets",
    "ping-previews",
    "execution-plans",
    # Runner çalışma alanlarının kökü (R1-V3C1A). Burada **yalnızca kök**
    # oluşturulur; Job başına dizinleri runner'ın kendisi açar. Kökün önceden
    # var ve 0700 olması `runner_env`'in sözleşmesidir: kökü çalışma anında
    # `parents=True` ile üretmek, herhangi bir absolute yolun altına dizin
    # ağacı açabilen bir yüzey bırakırdı.
    "execution-runs",
    "ssh",
)

# Ping onay planında gösterilen ve T-204B'de SSH'e uygulanacak host key
# politikaları. `no` bilinçli olarak **yoktur**: host key doğrulaması
# kolaylık için kapatılmaz (GUVENLIK.md bölüm 13).
SSH_HOST_KEY_POLICIES = ("strict", "accept_new")

# Claim edilmiş bir preview state'i, normal preview TTL'si dolduğu için
# silinemez: o state T-204B'de çalışan bir execution'a ait olabilir. Bu yüzden
# ayrı ve daha yüksek bir eşik vardır ve bu değerin altına **yapılandırılamaz**.
#
# Alt sınır ping timeout + süreç sonlandırma grace süresi + belgelenmiş
# güvenlik payından türetilir. Settings model doğrulaması, ping timeout
# değiştirildiğinde bu ilişkiyi yeniden hesaplar.
#
# Değer sessizce yükseltilmez, reddedilir: yapılandırmayı sessizce değiştirmek,
# operatörün ayarladığını sandığı politika ile gerçekte uygulanan politikayı
# ayırır ve bunu fark ettirecek hiçbir iz bırakmaz.
PROCESS_TERMINATION_GRACE_SECONDS = 5.0
EXECUTION_STALE_SAFETY_MARGIN_SECONDS = 30.0
DEFAULT_PING_TIMEOUT_SECONDS = 30.0
LEGACY_MINIMUM_CLAIM_STALE_SECONDS = 300.0
MINIMUM_CLAIM_STALE_SECONDS = max(
    LEGACY_MINIMUM_CLAIM_STALE_SECONDS,
    (
        DEFAULT_PING_TIMEOUT_SECONDS
        + PROCESS_TERMINATION_GRACE_SECONDS
        + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
    ),
)

# `requested_by` için azami uzunluk. Gerçek authentication yoktur (ADR-011);
# değer yapılandırmadan gelen sabit bir etikettir. Sınır, T-204B'de eklenecek
# `jobs.requested_by` sütunuyla aynı olacak şekilde seçilmiştir.
MAX_ACTOR_LENGTH = 100

# Uygulamanın **kendi** ürettiği internal aktör etiketleri bu ön ekle başlar ve
# `local_actor` olarak asla kabul edilmez (aşağıdaki validator reddeder). Ayrım
# bir isimlendirme tercihi değil, güvenlik sınırıdır: internal bir etiketin
# yapılandırmadan gelen bir aktörle **çakışabilmesi**, o etikete bağlı kayıtların
# gerçek bir kullanıcı isteğiyle eşleşebilmesi anlamına gelirdi.
INTERNAL_ACTOR_PREFIX = "__"

# Aktör bağı olmadan (R1-V2) oluşturulmuş execution plan satırlarına migration
# sırasında yazılan sentinel. Hiçbir `local_actor` bu değeri alamayacağı için o
# satırlar hiçbir claim koşuluyla eşleşemez; migration ayrıca hepsini `expired`
# yapar (bkz. alembic 0006).
LEGACY_PLAN_ACTOR = f"{INTERNAL_ACTOR_PREFIX}legacy_unattributed_plan__"

# R1-V3C1A runner temeli.
#
# Bir worker'ın kirası (`lease`) en az bu kadar heartbeat aralığını kapsamalıdır.
# Tek bir aralığa eşit bir kira, gecikmiş **tek** bir heartbeat'i sahiplik kaybı
# gibi gösterir ve canlı bir execution'ın işi başka bir worker'a kaptırmasına yol
# açardı; buradaki pay o yarışı kapatır.
MINIMUM_HEARTBEATS_PER_LEASE = 3

# Runner çalışma alanlarının `app_data_dir` altındaki kökü. Job başına bir alt
# dizin bu kökün altında açılır; kök adı sabittir ve yapılandırmadan **serbest
# bir path olarak alınmaz** (MIMARI.md bölüm 5 düzeni).
EXECUTION_RUN_DIRNAME = "execution-runs"

# Terk edilmiş bir runner çalışma dizininin **çalışan** bir serviste yerde
# kalabileceği azami süre. Bounded janitor sözleşmesinde provisional orphan TTL
# çalışan servis için en fazla bir saattir.
#
# Sınır tek bir alandan görülemez. Bir dizin ancak `execution_run_stale_seconds`
# kadar yaşlandıktan **sonra** eligible olur ve o andan sonraki ilk janitor turu
# en geç `execution_run_janitor_interval_seconds` sonra gelir; gerçek yer kalma
# süresi bu yüzden ikisinin **toplamıdır**. İki alan tek tek makul görünürken
# birlikte belgelenen tavanı katlayabilirdi.
#
# Sınır yalnız süreç ayaktayken geçerlidir: servis kapalıyken hiçbir janitor
# çalışmaz ve saat garantisi verilemez. Kapalı kalan bir servis için verilen söz
# farklıdır ve `_start_playbook_runtime` onu uygular — ilk açılışta eligible
# orphan'lar **hemen** taranır.
MAX_ORPHAN_RETENTION_SECONDS = 3600.0

# Runner sınırlarının public tavanları. Sonucu **okuyan** taraf da aynı
# değerleri doğrulamak zorundadır (:mod:`app.services.execution.result`); ayrı
# yazılmış bir kopya, ayarların kabul ettiği bir sınırın okuyucu tarafından
# reddedildiği (ya da tersinin olduğu) noktada fark edilirdi.
PLAYBOOK_RUNNER_MAX_EVENTS_CEILING = 500_000
PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING = 20_000_000

# Yayımlanan sonuç belgesinin **taban** bütçesi.
#
# Değer keyfî değil, ölçülmüş bir alt sınırdır. Normalizer bir sınır aşımında
# kısmi hiçbir veri taşımayan sabit bir fail-closed belge üretir; o belge —
# içinde tek bir event, tek bir host adı ve tek bir metin bulunmamasına rağmen —
# canonical compact biçimde sabit bir boyuttadır (boyut şemanın kendi sabit
# alanlarından gelir ve girdi ne kadar büyürse büyüsün büyümez).
#
# Bunun altında bir bütçe, "sınırı aştın" diyen belgenin kendisini de sınırın
# dışına düşürürdü: normalizer onu yine yayımlar (kendi sınır kontrolü yalnız
# **başarılı** sonuca uygulanır), okuyan taraf ise production'ın kendi ürettiği
# geçerli belgeyi reddederdi. Ölçüldü: ``playbook_runner_max_result_bytes=40``
# ile executor gerçekten geçerli bir ``result_limit_exceeded`` belgesi
# yayımlıyordu ve aynı 40 byte'ı uygulayan bir okuyucu onu okuyamazdı.
#
# Ölçülen boyut şema sürümüne bağlıdır ve **taban da onunla birlikte artar**:
#
# - ``schema_version=1`` zarfı: **212 byte** (ölçüldü). Taban 256'ydı.
# - ``schema_version=2`` zarfı: **267 byte** (ölçüldü; R1-V3J3A'nın eklediği
#   ``ansible_output``/``ansible_output_truncated`` alanları ``null``/``false``
#   değerleriyle bile 55 byte yer tutar).
#
# 267 taban 256'nın **üstüne** çıktığı için sabit yükseltildi: aksi hâlde
# production writer'ın en küçük geçerli belgesi, yapılandırılabilir en küçük
# bütçenin dışında kalırdı. 320, 267'nin üstünde en yakın makul taban (64'ün
# katı) ve bir sonraki şema alanı için — eski 256'nın 212'ye bıraktığı payla
# aynı ölçekte — pay bırakır. Boyut testte ölçülür, burada tahmin edilmez.
#
# Sonuç bütçesini gerçekten daraltmak isteyen bir yapılandırma bunun altına
# inemez; inebilseydi kısıtladığı tek şey hata zarfının okunabilirliği olurdu.
# Varsayılan (``1_000_000``) ve tavan değişmedi; eski ``schema_version=1``
# belgelerinin okunması bu tabandan etkilenmez, onlar zaten daha küçüktür.
PLAYBOOK_RUNNER_MIN_RESULT_BYTES = 320

# Runner sınırlarının üst tavanları. "Sınır var" diyen ama pratikte hiçbir şeyi
# durdurmayan bir değer, sınırın hiç olmamasından daha yanıltıcıdır.
_RUNNER_LIMIT_CEILINGS = {
    "playbook_runner_max_stdout_bytes": 50_000_000,
    "playbook_runner_max_raw_bytes": 500_000_000,
    "playbook_runner_max_events": PLAYBOOK_RUNNER_MAX_EVENTS_CEILING,
    "playbook_runner_max_result_bytes": PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
}

# Runner sınırlarının alt tabanları. Yalnız sonuç bütçesinin belgelenmiş bir
# tabanı vardır; diğer sınırlar için anlamlı olan tek alt sınır "sıfırdan
# büyük"tür ve o da varsayılan ``1``'dir.
_RUNNER_LIMIT_FLOORS = {
    "playbook_runner_max_result_bytes": PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
}
_DEFAULT_RUNNER_LIMIT_FLOOR = 1

_RUNNER_DURATION_CEILINGS = {
    "playbook_runner_timeout_seconds": 21_600.0,
    "playbook_worker_lease_seconds": 3_600.0,
    "playbook_worker_heartbeat_seconds": 600.0,
    # İki alan da tek başına orphan retention tavanını aşamaz; toplamlarına ise
    # `_orphan_retention_stays_within_its_ceiling` bakar. Tek tek 7 güne ya da
    # bir güne izin veren eski tavanlar, belgelenen bir saatlik sözün yanında
    # yalnızca yanıltıcı olurdu.
    "execution_run_stale_seconds": MAX_ORPHAN_RETENTION_SECONDS,
    "execution_run_janitor_interval_seconds": MAX_ORPHAN_RETENTION_SECONDS,
    # Boşta bekleyen worker'ın iki acquire denemesi arasındaki azami aralık.
    # Tavan bilinçlidir: dakikalarca uyuyan bir worker, onaylanmış bir
    # çalıştırmayı hiçbir gerekçe olmadan geciktirirdi.
    "playbook_worker_poll_seconds": 60.0,
}


class Settings(BaseSettings):
    """Environment üzerinden yüklenen uygulama ayarları.

    Bütün değişkenler ``ANSIBLEOPS_`` ön ekiyle okunur, örnek: ``ANSIBLEOPS_ENVIRONMENT``.
    """

    model_config = SettingsConfigDict(
        env_prefix="ANSIBLEOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DORAnsible"
    environment: str = "development"

    # Veritabanı, job artifact'leri ve staging alanının kökü.
    app_data_dir: Path = REPO_ROOT / "app-data"

    # Boş bırakılırsa app_data_dir altındaki SQLite dosyası kullanılır.
    # PostgreSQL'e geçişte bu değer doğrudan bir DSN ile doldurulur.
    database_url: str | None = None

    # GUVENLIK.md bölüm 10: development CORS allowlist ile sınırlıdır.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # GUVENLIK.md bölüm 4: project kayıtları yalnızca bu root'ların altından
    # yapılabilir. Boş bırakılırsa yalnızca `app_data_dir/projects` kabul edilir;
    # başka bir dizini kaydetmek bilinçli yapılandırma gerektirir.
    project_root_allowlist: list[Path] = []

    # Standalone (project'e bağlı olmayan) inventory dosyalarının kabul edildiği
    # root'lar. Project allowlist'inden ayrıdır (ADR-015): project kökü altında
    # duran her dosyanın kendiliğinden kaydedilebilir bir inventory sayılması
    # istenmez. Boş bırakılırsa yalnızca `app_data_dir/inventories` kabul edilir.
    # Project'e bağlı inventory bu listeye değil, project'in kendi köküne tabidir.
    inventory_root_allowlist: list[Path] = []

    # T-103 playbook keşif sınırları. Kötü hazırlanmış veya çok büyük dizin
    # ağaçlarına karşı korur; aşıldığında hata değil `truncated` işareti üretir.
    playbook_scan_max_depth: int = 12
    playbook_scan_max_entries: int = 20_000
    playbook_scan_max_results: int = 500
    playbook_scan_read_bytes: int = 65_536

    # T-202 inventory parser. Komut **argüman listesi** olarak tutulur; hiçbir
    # aşamada shell string'e çevrilmez (GUVENLIK.md bölüm 5). Ayrı bir venv'deki
    # veya sistem genelindeki `ansible-inventory` buradan gösterilebilir.
    ansible_inventory_command: list[str] = ["ansible-inventory"]
    inventory_parse_timeout_seconds: float = 30.0
    inventory_parse_max_output_bytes: int = 5_000_000

    # T-204A ping preview. Preview yalnızca plan üretir; hiçbir SSH bağlantısı
    # veya ansible ad-hoc çalıştırması yapmaz.
    #
    # `ping_preview_ttl_seconds`: onay planının geçerlilik süresi.
    # `ping_preview_claim_stale_seconds`: claim edilmiş bir state'in terk
    #   edilmiş sayılması için gereken süre. Bu eşik **normal TTL'den ayrıdır**;
    #   aksi hâlde başka bir isteğin süpürücüsü, çalışmakta olan bir
    #   execution'ın dondurulmuş snapshot'ını silebilirdi.
    #   `MINIMUM_CLAIM_STALE_SECONDS` altındaki bir değer sessizce
    #   yükseltilmez; uygulama **açılışta hata verir**.
    # `ping_preview_max_listed_hosts`: planda listelenecek azami host adı.
    #   `host_count` bu sınırdan bağımsız olarak **her zaman kesindir**.
    ping_preview_ttl_seconds: float = 300.0
    ping_preview_claim_stale_seconds: float = MINIMUM_CLAIM_STALE_SECONDS
    ping_preview_max_listed_hosts: int = 500

    # T-204B1: public confirm endpoint'inden bağımsız güvenli execution sınırı.
    ansible_ad_hoc_command: list[str] = ["ansible"]
    ping_timeout_seconds: float = DEFAULT_PING_TIMEOUT_SECONDS
    ping_max_output_bytes: int = 5_000_000
    ssh_connect_timeout_seconds: int = 10
    ping_forks: int = 10
    ssh_known_hosts_path: Path | None = None
    job_stale_seconds: float = 300.0

    # Bir işlemi kimin istediği. MVP 1 tek kullanıcılıdır (ADR-011) ve gerçek
    # authentication yoktur; bu yüzden değer yapılandırmadan gelen sabit bir
    # etikettir. OS kullanıcı adı veya istemci IP'si bilinçli olarak
    # **üretilmez**: ikisi de doğrulanmamış bir kimliğe doğrulanmış görünümü
    # verirdi. Preview meta'sına yazılır ve onay anında yeniden karşılaştırılır.
    local_actor: str = "local-single-user"

    # Inventory'de gösterilen private key dosyalarının bulunabileceği kökler.
    # Bu değer controller üzerinde dosya okutur; boş bırakılırsa yalnızca
    # `app_data_dir/secrets` kabul edilir.
    ssh_key_root_allowlist: list[Path] = []

    # `strict` | `accept_new`. Varsayılan strict'tir; `accept_new` TOFU'dur ve
    # ilk bağlantıda MITM'e açıktır (GUVENLIK.md bölüm 13).
    ssh_host_key_policy: str = "strict"

    # R1-V2 prepared execution plan (frozen workspace + tek kullanımlık token).
    #
    # `execution_plan_ttl_seconds`: hazırlanmış planın geçerlilik süresi.
    #   Kısa tutulur: dondurulmuş bir workspace ne kadar uzun claim edilebilir
    #   kalırsa, kullanıcının onayladığı içerik ile o an geçerli olan dünya
    #   arasındaki fark o kadar büyür.
    # `execution_plan_staging_stale_seconds`: yayımlanamadan çökmüş bir staging
    #   dizininin terk edilmiş sayılması için gereken yaş. Yaş kontrolü olmadan
    #   silmek, o an **yazılmakta olan** bir staging'i yok ederdi.
    execution_plan_ttl_seconds: float = 600.0
    execution_plan_staging_stale_seconds: float = 900.0

    # Playbook runner ve kalıcı execution sınırları. Bu ayarlar worker açıkken
    # gerçek Check/Normal Job'ları tüketir; worker kapalıyken şema ve recovery
    # yine yüklenir ama arka planda runner child process'i başlatılmaz.
    #
    # `ansible_runner_command`: T-202'deki desenle aynı — komut bir **argüman
    #   listesidir**, hiçbir aşamada shell string'e çevrilmez (GUVENLIK.md
    #   bölüm 5). Ayrı bir venv'deki `ansible-runner` buradan gösterilebilir.
    # `playbook_runner_timeout_seconds`: tek bir çalıştırmanın azami süresi.
    # `playbook_runner_max_stdout_bytes`: canlı stdout üst sınırı.
    # `playbook_runner_max_raw_bytes`: raw artifact dizininin azami boyutu.
    # `playbook_runner_max_events`: işlenecek azami runner event'i.
    # `playbook_runner_max_result_bytes`: kalıcı normalize sonucun üst sınırı.
    # `playbook_worker_lease_seconds`: bir worker'ın Job sahipliğinin süresi.
    # `playbook_worker_heartbeat_seconds`: sahipliğin tazelenme aralığı.
    # `execution_run_stale_seconds`: terk edilmiş bir runner çalışma dizininin
    #   toplanabilmesi için gereken yaş. Varsayılan, janitor aralığıyla
    #   **birlikte** `MAX_ORPHAN_RETENTION_SECONDS` içinde kalacak biçimde
    #   seçilmiştir (2700 + 600 = 3300); pay bilinçlidir.
    ansible_runner_command: list[str] = ["ansible-runner"]
    playbook_runner_timeout_seconds: float = 1800.0
    playbook_runner_max_stdout_bytes: int = 5_000_000
    playbook_runner_max_raw_bytes: int = 50_000_000
    playbook_runner_max_events: int = 20_000
    playbook_runner_max_result_bytes: int = 1_000_000
    playbook_worker_lease_seconds: float = 120.0
    playbook_worker_heartbeat_seconds: float = 30.0
    execution_run_stale_seconds: float = 2700.0

    # R1-V3C2C arka plan worker'ının iki bounded aralığı.
    #
    # `playbook_worker_poll_seconds`: boşta bekleyen worker'ın iki acquire
    #   denemesi arasında bekleyeceği süre. Bekleme stop event'i üzerinde
    #   yapılır, yani kapanış talebi beklemeyi **anında** uyandırır; bu değer
    #   yalnız "iş yokken ne sıklıkla sorulur" sorusunu yanıtlar.
    # `execution_run_janitor_interval_seconds`: açılıştaki ilk turdan sonra
    #   crash run janitor'ının tekrar çalıştırılma aralığı. Janitor worker'ın
    #   **kendi** thread'inde çalışır; hiçbir Job acquire etmez ve executor
    #   çağırmaz, bu yüzden aktif playbook concurrency'sini artırmaz. Ayrı
    #   olması zorunludur: blocking bir çalıştırmanın yanına konan janitor bu
    #   aralığa hiçbir üst sınır bırakmazdı.
    #
    # İkisi de sonlu, pozitif ve tavanlıdır; anlamsız bir değer sessizce
    # düzeltilmez, açılışta reddedilir (bu dosyadaki diğer eşiklerle aynı
    # gerekçe). Aralık ayrıca `execution_run_stale_seconds` ile **birlikte**
    # `MAX_ORPHAN_RETENTION_SECONDS` içinde kalmalıdır.
    playbook_worker_poll_seconds: float = 1.0
    execution_run_janitor_interval_seconds: float = 600.0

    # Worker **varsayılan olarak kapalıdır**. Ayar açıkça açılmadan hiçbir
    # kurulumda arka planda playbook çalıştıran bir döngü doğmamalıdır:
    # kapalıyken ne bir worker thread'i ne de tek bir executor çağrısı üretilir
    # (açılıştaki reconciliation ve janitor yine uygulanır).
    playbook_worker_enabled: bool = False

    @field_validator(
        "playbook_scan_max_depth",
        "playbook_scan_max_entries",
        "playbook_scan_max_results",
        "playbook_scan_read_bytes",
        "inventory_parse_max_output_bytes",
        "ping_preview_max_listed_hosts",
        "ping_max_output_bytes",
        "ssh_connect_timeout_seconds",
        "ping_forks",
    )
    @classmethod
    def _scan_limits_must_be_positive(cls, value: int) -> int:
        """Sıfır veya negatif limit keşfi sessizce boş sonuca çevirirdi."""
        if value < 1:
            raise ValueError("Keşif limiti en az 1 olmalıdır.")
        return value

    @field_validator("ping_max_output_bytes")
    @classmethod
    def _ping_output_must_be_bounded(cls, value: int) -> int:
        if value < 1024 or value > 20_000_000:
            raise ValueError("ping_max_output_bytes 1024..20000000 arasında olmalıdır.")
        return value

    @field_validator("ssh_connect_timeout_seconds")
    @classmethod
    def _ssh_timeout_must_be_bounded(cls, value: int) -> int:
        if value < 1 or value > 60:
            raise ValueError("ssh_connect_timeout_seconds 1..60 arasında olmalıdır.")
        return value

    @field_validator("ping_forks")
    @classmethod
    def _forks_must_be_bounded(cls, value: int) -> int:
        if value < 1 or value > 100:
            raise ValueError("ping_forks 1..100 arasında olmalıdır.")
        return value

    @field_validator("inventory_parse_timeout_seconds")
    @classmethod
    def _timeout_must_be_positive(cls, value: float) -> float:
        """Sıfır timeout, süreci başlar başlamaz öldürürdü."""
        if value <= 0:
            raise ValueError("Parser timeout değeri sıfırdan büyük olmalıdır.")
        return value

    @field_validator(
        "ping_timeout_seconds",
        "job_stale_seconds",
        "ping_preview_claim_stale_seconds",
        "ping_preview_ttl_seconds",
    )
    @classmethod
    def _ping_durations_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Ping süre değeri sonlu olmalıdır.")
        return value

    @field_validator("ping_timeout_seconds")
    @classmethod
    def _ping_timeout_must_be_bounded(cls, value: float) -> float:
        if value <= 0 or value > 240:
            raise ValueError("ping_timeout_seconds 0..240 arasında olmalıdır.")
        return value

    @field_validator("ping_preview_ttl_seconds")
    @classmethod
    def _preview_ttl_must_be_bounded(cls, value: float) -> float:
        """Onay planı ne anlık ne de süresiz geçerli olabilir.

        Sıfır TTL planı üretilir üretilmez geçersiz kılardı; çok uzun bir TTL
        ise dondurulmuş bir snapshot'ın günlerce onaylanabilir kalması demektir.
        """
        if value <= 0 or value > 3600:
            raise ValueError("ping_preview_ttl_seconds 0 ile 3600 saniye arasında olmalıdır.")
        return value

    @field_validator("execution_plan_ttl_seconds")
    @classmethod
    def _execution_plan_ttl_must_be_bounded(cls, value: float) -> float:
        """Hazırlanmış plan ne anlık ne de saatlerce geçerli olabilir.

        Alt sınır, kullanıcının planı okuyup onaylamasına yetecek kadar; üst
        sınır, dondurulmuş bir kopyanın dünyadan kopuk kalabileceği en uzun
        süre. Değer sessizce düzeltilmez, reddedilir.
        """
        if not math.isfinite(value) or value < 60 or value > 3600:
            raise ValueError("execution_plan_ttl_seconds 60 ile 3600 saniye arasında olmalıdır.")
        return value

    @field_validator("execution_plan_staging_stale_seconds")
    @classmethod
    def _execution_staging_stale_must_be_bounded(cls, value: float) -> float:
        """Staging temizliği yaş eşiğine bağlıdır; eşik sıfır olamaz."""
        if not math.isfinite(value) or value < 60 or value > 86_400:
            raise ValueError(
                "execution_plan_staging_stale_seconds 60 ile 86400 saniye arasında olmalıdır."
            )
        return value

    @field_validator("ping_preview_claim_stale_seconds")
    @classmethod
    def _claim_stale_must_reach_the_safe_minimum(cls, value: float) -> float:
        """Claim-stale eşiği güvenli alt sınırın altına ayarlanamaz.

        Değer sessizce yükseltilmez. Sessiz bir clamp, operatörün ayarladığını
        sandığı politika ile gerçekte uygulanan politikayı ayırır ve bunu fark
        ettirecek hiçbir iz bırakmaz; bu yüzden uygulama açılışta durur.
        """
        if value < MINIMUM_CLAIM_STALE_SECONDS:
            raise ValueError(
                "ping_preview_claim_stale_seconds en az "
                f"{MINIMUM_CLAIM_STALE_SECONDS:.0f} saniye olmalıdır."
            )
        return value

    @field_validator("local_actor")
    @classmethod
    def _actor_must_be_a_short_label(cls, value: str) -> str:
        """Aktör etiketi boş olamaz, sınırı aşamaz ve internal alana giremez.

        Internal ön ek (:data:`INTERNAL_ACTOR_PREFIX`) yapılandırmaya kapalıdır:
        uygulamanın kendi ürettiği sentinel'ler ile kullanıcı aktörleri aynı
        değer uzayını paylaşırsa, sentinel'e bağlı kayıtlar gerçek bir istekle
        eşleşebilir hâle gelirdi. Değer sessizce düzeltilmez, reddedilir.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("local_actor boş olamaz.")
        if len(stripped) > MAX_ACTOR_LENGTH:
            raise ValueError(f"local_actor en fazla {MAX_ACTOR_LENGTH} karakter olabilir.")
        if stripped.startswith(INTERNAL_ACTOR_PREFIX):
            raise ValueError(
                f"local_actor '{INTERNAL_ACTOR_PREFIX}' ile başlayamaz; bu ön ek "
                "uygulamanın internal aktör etiketlerine ayrılmıştır."
            )
        return stripped

    @field_validator("ssh_host_key_policy")
    @classmethod
    def _host_key_policy_must_be_known(cls, value: str) -> str:
        """Host key doğrulamasını kapatan bir değer kabul edilmez."""
        if value not in SSH_HOST_KEY_POLICIES:
            raise ValueError(
                f"ssh_host_key_policy yalnızca {' veya '.join(SSH_HOST_KEY_POLICIES)} olabilir."
            )
        return value

    @field_validator(
        "ansible_inventory_command",
        "ansible_ad_hoc_command",
        "ansible_runner_command",
    )
    @classmethod
    def _command_must_not_be_empty(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """Boş komut, çalıştırılacak bir şey olmadığı hâlde başarı gibi görünürdü."""
        if not value or not all(part.strip() for part in value):
            raise ValueError(f"{info.field_name} boş olamaz.")
        return value

    @field_validator(
        "playbook_runner_max_stdout_bytes",
        "playbook_runner_max_raw_bytes",
        "playbook_runner_max_events",
        "playbook_runner_max_result_bytes",
    )
    @classmethod
    def _runner_limits_must_be_bounded(cls, value: int, info: ValidationInfo) -> int:
        """Sıfır/negatif sınır çalıştırmayı anında keser, sınırsız sınır korumaz.

        Üst sınır bilinçlidir: "sınır var" diyen ama pratikte hiçbir şeyi
        durdurmayan bir değer, sınırın hiç olmamasından daha yanıltıcıdır.

        Alt sınır alana göre değişir. Sonuç bütçesinin tabanı
        :data:`PLAYBOOK_RUNNER_MIN_RESULT_BYTES`'tır: bunun altındaki bir değer,
        normalizer'ın sınır aşımında ürettiği sabit fail-closed belgeyi de
        yayımlanamaz hâle getirir ve okuyan tarafa production'ın kendi geçerli
        çıktısını reddettirirdi.
        """
        name = info.field_name or ""
        ceiling = _RUNNER_LIMIT_CEILINGS.get(name)
        if ceiling is None:  # pragma: no cover - validator yalnız bilinen alanlara bağlıdır
            raise ValueError(f"{name} için tanımlı bir üst sınır yok.")
        floor = _RUNNER_LIMIT_FLOORS.get(name, _DEFAULT_RUNNER_LIMIT_FLOOR)
        if value < floor:
            raise ValueError(f"{name} en az {floor} olmalıdır.")
        if value > ceiling:
            raise ValueError(f"{name} en fazla {ceiling} olabilir.")
        return value

    @field_validator(
        "playbook_runner_timeout_seconds",
        "playbook_worker_lease_seconds",
        "playbook_worker_heartbeat_seconds",
        "execution_run_stale_seconds",
        "playbook_worker_poll_seconds",
        "execution_run_janitor_interval_seconds",
    )
    @classmethod
    def _runner_durations_must_be_bounded(cls, value: float, info: ValidationInfo) -> float:
        """Runner süreleri sonlu, pozitif ve makul bir tavanın altında olmalıdır."""
        name = info.field_name or ""
        ceiling = _RUNNER_DURATION_CEILINGS.get(name)
        if ceiling is None:  # pragma: no cover - validator yalnız bilinen alanlara bağlıdır
            raise ValueError(f"{name} için tanımlı bir üst sınır yok.")
        if not math.isfinite(value):
            raise ValueError(f"{name} sonlu olmalıdır.")
        if value <= 0:
            raise ValueError(f"{name} sıfırdan büyük olmalıdır.")
        if value > ceiling:
            raise ValueError(f"{name} en fazla {ceiling:g} saniye olabilir.")
        return value

    @model_validator(mode="after")
    def _stale_thresholds_cover_execution(self) -> Settings:
        minimum = (
            self.ping_timeout_seconds
            + PROCESS_TERMINATION_GRACE_SECONDS
            + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
        )
        if self.job_stale_seconds < minimum:
            raise ValueError(f"job_stale_seconds en az {minimum:g} saniye olmalıdır.")
        if self.ping_preview_claim_stale_seconds < max(LEGACY_MINIMUM_CLAIM_STALE_SECONDS, minimum):
            raise ValueError(
                "ping_preview_claim_stale_seconds gerçek execution sınırını kapsamalıdır."
            )
        if self.job_stale_seconds > 86_400:
            raise ValueError("job_stale_seconds en fazla 86400 saniye olabilir.")
        return self

    @model_validator(mode="after")
    def _runner_thresholds_are_consistent(self) -> Settings:
        """Runner sürelerinin **birbirine göre** anlamlı olduğunu doğrular.

        Tek tek geçerli üç değer birlikte tutarsız olabilir; bu ilişkiler tek
        bir alanın validator'ından görülemez:

        - Heartbeat, kirasından kısa olmalıdır. Aksi hâlde kira, ilk tazeleme
          fırsatı gelmeden dolar ve sahiplik her turda kaybedilir.
        - Kira, birkaç heartbeat aralığını kapsamalıdır: tek bir gecikmiş
          heartbeat, canlı bir execution'ın işini kaptırmasına yol açmamalıdır.
        - Terk edilmiş sayma eşiği, en uzun meşru çalışmayı **ve** süreç
          sonlandırma payını aşmalıdır; aksi hâlde temizlik, hâlâ çalışan bir
          işin dizinini toplardı.

        Değerler sessizce düzeltilmez, reddedilir (bu dosyadaki diğer eşiklerle
        aynı gerekçe: sessiz bir clamp, operatörün ayarladığını sandığı politika
        ile gerçekte uygulananı ayırır ve hiçbir iz bırakmaz).
        """
        if self.playbook_worker_heartbeat_seconds >= self.playbook_worker_lease_seconds:
            raise ValueError(
                "playbook_worker_heartbeat_seconds, playbook_worker_lease_seconds "
                "değerinden küçük olmalıdır."
            )
        minimum_lease = MINIMUM_HEARTBEATS_PER_LEASE * self.playbook_worker_heartbeat_seconds
        if self.playbook_worker_lease_seconds < minimum_lease:
            raise ValueError(
                "playbook_worker_lease_seconds en az "
                f"{MINIMUM_HEARTBEATS_PER_LEASE} heartbeat aralığını "
                f"({minimum_lease:g} saniye) kapsamalıdır."
            )
        minimum_stale = (
            self.playbook_runner_timeout_seconds
            + PROCESS_TERMINATION_GRACE_SECONDS
            + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
        )
        if self.execution_run_stale_seconds <= minimum_stale:
            raise ValueError(
                f"execution_run_stale_seconds {minimum_stale:g} saniyeden büyük olmalıdır."
            )
        return self

    @model_validator(mode="after")
    def _orphan_retention_stays_within_its_ceiling(self) -> Settings:
        """Terk edilmiş bir çalışma dizininin azami yer kalma süresini sınırlar.

        Execution run cleanup sözleşmesi tek bir alanın değeri değildir: bir
        dizin önce ``execution_run_stale_seconds`` kadar
        yaşlanarak eligible olur, sonra onu görecek ilk janitor turunu bekler ve
        o tur en geç ``execution_run_janitor_interval_seconds`` sonra gelir.
        Gerçek üst sınır bu yüzden **toplamdır** ve ancak burada görülebilir.

        İki alan tek tek geçerliyken birlikte tavanı aşabilir; bu durumda değer
        sessizce kısılmaz, reddedilir (bu dosyadaki diğer eşiklerle aynı
        gerekçe). Yaş koşulunun kendisi — ``age > stale_seconds`` — janitor'ın
        sözleşmesidir ve burada değiştirilmez.

        Sınır **çalışan** bir servis içindir. Servis kapalıyken hiçbir janitor
        turu olmaz ve saat garantisi verilemez; o durum için verilen söz
        farklıdır: ilk açılışta eligible orphan'lar hemen taranır.

        Kural, ``_runner_thresholds_are_consistent``'in alt sınırıyla birlikte
        ``playbook_runner_timeout_seconds``'e de dolaylı bir tavan koyar: bir
        çalıştırmanın süresini uzatmak, çalışma dizinini "terk edilmiş" saymayı
        da geciktirmek zorundadır. İkisi birlikte sağlanamıyorsa yapılandırma
        sessizce yorumlanmaz; hangi ilişkinin bozulduğu hatada görünür.
        """
        retention = self.execution_run_stale_seconds + self.execution_run_janitor_interval_seconds
        if retention > MAX_ORPHAN_RETENTION_SECONDS:
            raise ValueError(
                "execution_run_stale_seconds + execution_run_janitor_interval_seconds "
                f"en fazla {MAX_ORPHAN_RETENTION_SECONDS:g} saniye olabilir."
            )
        return self

    @field_validator("project_root_allowlist", "inventory_root_allowlist", "ssh_key_root_allowlist")
    @classmethod
    def _roots_must_be_absolute(cls, value: list[Path], info: ValidationInfo) -> list[Path]:
        """Relative root'u reddeder.

        Relative bir root sürecin çalışma dizinine göre çözülür; bu, allowlist'i
        sessizce yanlış bir yere işaret ettirir.
        """
        for root in value:
            if not root.expanduser().is_absolute():
                raise ValueError(f"{info.field_name} absolute path içermelidir: {root}")
        return value

    def resolve_project_root_allowlist(self) -> tuple[Path, ...]:
        """İzin verilen project root'larını kanonik ve tekilleştirilmiş döndürür.

        Yapılandırma boşsa güvenli varsayılan olarak yalnızca
        ``app_data_dir/projects`` kullanılır (MIMARI.md bölüm 5).
        """
        configured = self.project_root_allowlist or [self.app_data_dir / "projects"]
        return _canonical_roots(configured)

    def resolve_inventory_root_allowlist(self) -> tuple[Path, ...]:
        """Standalone inventory için izin verilen root'ları döndürür.

        Yapılandırma boşsa güvenli varsayılan olarak yalnızca uygulamanın kendi
        ``app_data_dir/inventories`` dizini kullanılır (MIMARI.md bölüm 5).
        Bu dizin ``ensure_app_data_dirs`` tarafından oluşturulur, yani
        varsayılan yapılandırma **kullanılabilir** bir kök bırakır.
        """
        configured = self.inventory_root_allowlist or [self.app_data_dir / "inventories"]
        return _canonical_roots(configured)

    def resolve_ssh_key_root_allowlist(self) -> tuple[Path, ...]:
        """Inventory'de gösterilebilecek private key köklerini döndürür.

        Yapılandırma boşsa güvenli varsayılan olarak yalnızca
        ``app_data_dir/secrets`` kullanılır. Bu ayrım bilinçlidir: bir project
        veya inventory kökü altında duran her dosya kendiliğinden kullanılabilir
        bir SSH anahtarı sayılmaz.
        """
        configured = self.ssh_key_root_allowlist or [self.app_data_dir / "secrets"]
        return _canonical_roots(configured)

    def resolve_ping_preview_dir(self) -> Path:
        """Ping preview state kökü."""
        return self.app_data_dir / "ping-previews"

    def resolve_execution_plan_dir(self) -> Path:
        """Dondurulmuş execution workspace'lerinin kökü (R1-V2)."""
        return self.app_data_dir / "execution-plans"

    def resolve_execution_run_dir(self) -> Path:
        """Runner çalışma alanlarının kökü (R1-V3C1A).

        Kök her zaman ``app_data_dir`` altından **türetilir**; yapılandırmadan
        serbest bir path olarak alınmaz. Böylece runner'ın yazdığı her şey
        uygulamanın kendi 0700 veri alanında kalır.

        Dönen değer absolute ve kararlıdır: ``resolve()`` çağrılmaz, çünkü
        dizin henüz var olmayabilir ve sembolik bağ çözümü sonucu çalışma anına
        bağımlı kılardı. Bu fonksiyon **dizin oluşturmaz**.

        Raises:
            ValueError: ``app_data_dir`` relative ise. Relative bir kök sürecin
                çalışma dizinine göre çözülür ve runner alanını sessizce başka
                bir yere taşırdı.
        """
        root = (self.app_data_dir / EXECUTION_RUN_DIRNAME).expanduser()
        if not root.is_absolute():
            raise ValueError("execution run kökü absolute olmalıdır.")
        return root

    def resolve_ssh_known_hosts_path(self) -> Path:
        """Known-hosts yolunu yalnız app-data/ssh altında çözer."""
        candidate = self.ssh_known_hosts_path or self.app_data_dir / "ssh" / "known_hosts"
        if not candidate.expanduser().is_absolute():
            raise ValueError("ssh_known_hosts_path absolute olmalıdır.")
        resolved = candidate.expanduser().resolve()
        ssh_root = (self.app_data_dir / "ssh").resolve()
        if resolved.parent != ssh_root:
            raise ValueError("ssh_known_hosts_path app-data/ssh altında olmalıdır.")
        return candidate

    def resolve_database_url(self) -> str:
        """Etkin veritabanı DSN'ini döndürür.

        ``database_url`` verilmemişse ``app_data_dir/database/app.db`` üzerinde
        SQLite kullanılır (ADR-004).
        """
        if self.database_url:
            return self.database_url
        db_path = self.app_data_dir / "database" / "app.db"
        return f"sqlite:///{db_path.as_posix()}"


def _canonical_roots(configured: list[Path]) -> tuple[Path, ...]:
    """Root listesini kanonik hâle getirir ve tekilleştirir."""
    resolved = [root.expanduser().resolve() for root in configured]
    return tuple(dict.fromkeys(resolved))


def ensure_app_data_dirs(settings: Settings) -> None:
    """``app-data`` altındaki çalışma dizinlerini oluşturur ve izinlerini daraltır.

    GUVENLIK.md bölüm 11 gereği dizinler yalnızca sahibine açık olacak şekilde
    (0700) oluşturulur. POSIX olmayan platformlarda ``chmod`` etkisizdir ve
    sessizce atlanır.
    """
    settings.app_data_dir.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(settings.app_data_dir)
    for name in _DATA_SUBDIRS:
        path = settings.app_data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(path)


def _restrict_permissions(path: Path) -> None:
    """Dizin iznini POSIX üzerinde 0700 yapar."""
    if os.name != "posix":
        return
    os.chmod(path, 0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Süreç ömrü boyunca tek bir ``Settings`` örneği döndürür."""
    return Settings()
