"""Ayarlar ve app-data dizin hazırlığı."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    EXECUTION_RUN_DIRNAME,
    EXECUTION_STALE_SAFETY_MARGIN_SECONDS,
    INTERNAL_ACTOR_PREFIX,
    LEGACY_PLAN_ACTOR,
    MAX_ACTOR_LENGTH,
    MAX_ORPHAN_RETENTION_SECONDS,
    MINIMUM_CLAIM_STALE_SECONDS,
    MINIMUM_HEARTBEATS_PER_LEASE,
    PLAYBOOK_RUNNER_MAX_EVENTS_CEILING,
    PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    PLAYBOOK_RUNNER_MIN_RESULT_BYTES,
    PROCESS_TERMINATION_GRACE_SECONDS,
    ensure_app_data_dirs,
)
from app.services.jobs import PreviewStore
from app.services.projects import ScanLimits
from tests.support import make_settings


def test_default_database_url_uses_app_data_dir(tmp_path: Path) -> None:
    settings = make_settings(app_data_dir=tmp_path / "app-data", database_url=None)

    url = settings.resolve_database_url()

    assert url.startswith("sqlite:///")
    assert url.endswith("app-data/database/app.db")


def test_explicit_database_url_is_preserved(tmp_path: Path) -> None:
    dsn = "postgresql+psycopg://ansibleops@localhost/ansibleops"
    settings = make_settings(app_data_dir=tmp_path, database_url=dsn)

    assert settings.resolve_database_url() == dsn


def test_ensure_app_data_dirs_creates_expected_layout(tmp_path: Path) -> None:
    settings = make_settings(app_data_dir=tmp_path / "app-data")

    ensure_app_data_dirs(settings)

    for name in ("database", "projects", "inventories", "jobs", "staging", "secrets"):
        assert (settings.app_data_dir / name).is_dir()


def test_ensure_app_data_dirs_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(app_data_dir=tmp_path / "app-data")

    ensure_app_data_dirs(settings)
    ensure_app_data_dirs(settings)

    assert settings.app_data_dir.is_dir()


def test_default_project_root_allowlist_is_app_data_projects(tmp_path: Path) -> None:
    """Yapılandırma yoksa yalnızca app-data/projects kabul edilir (T-102)."""
    settings = make_settings(app_data_dir=tmp_path / "app-data")

    assert settings.resolve_project_root_allowlist() == (
        (tmp_path / "app-data" / "projects").resolve(),
    )


def test_configured_project_roots_are_resolved_and_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "kok").mkdir()
    settings = make_settings(
        project_root_allowlist=[
            tmp_path / "kok",
            tmp_path / "kok",
            tmp_path / "baska" / ".." / "kok",
        ]
    )

    assert settings.resolve_project_root_allowlist() == ((tmp_path / "kok").resolve(),)


def test_relative_project_root_is_rejected() -> None:
    """Relative root sessizce çalışma dizinine göre çözülmemelidir."""
    with pytest.raises(ValidationError, match="absolute"):
        make_settings(project_root_allowlist=[Path("projeler")])


def test_default_inventory_root_allowlist_is_app_data_inventories(tmp_path: Path) -> None:
    """Yapılandırma yoksa yalnızca app-data/inventories kabul edilir (ADR-015).

    Varsayılan, uygulamanın ``ensure_app_data_dirs`` ile kendi oluşturduğu bir
    dizindir; yani varsayılan yapılandırma kullanılabilir bir kök bırakır.
    """
    settings = make_settings(app_data_dir=tmp_path / "app-data")

    assert settings.resolve_inventory_root_allowlist() == (
        (tmp_path / "app-data" / "inventories").resolve(),
    )


def test_inventory_and_project_roots_are_independent(tmp_path: Path) -> None:
    """İki allowlist birbirinin yerine geçmez."""
    (tmp_path / "projeler").mkdir()
    (tmp_path / "envanterler").mkdir()
    settings = make_settings(
        project_root_allowlist=[tmp_path / "projeler"],
        inventory_root_allowlist=[tmp_path / "envanterler"],
    )

    assert settings.resolve_project_root_allowlist() == ((tmp_path / "projeler").resolve(),)
    assert settings.resolve_inventory_root_allowlist() == ((tmp_path / "envanterler").resolve(),)


def test_configured_inventory_roots_are_resolved_and_deduplicated(tmp_path: Path) -> None:
    (tmp_path / "kok").mkdir()
    settings = make_settings(
        inventory_root_allowlist=[
            tmp_path / "kok",
            tmp_path / "kok",
            tmp_path / "baska" / ".." / "kok",
        ]
    )

    assert settings.resolve_inventory_root_allowlist() == ((tmp_path / "kok").resolve(),)


def test_relative_inventory_root_is_rejected() -> None:
    """Hata mesajı hangi ayarın hatalı olduğunu söyler."""
    with pytest.raises(ValidationError, match="inventory_root_allowlist.*absolute"):
        make_settings(inventory_root_allowlist=[Path("envanterler")])


def test_scan_limits_have_documented_defaults() -> None:
    """T-103 keşif sınırları ayarlanabilir ve varsayılanları belgelidir."""
    settings = make_settings()

    assert settings.playbook_scan_max_depth == 12
    assert settings.playbook_scan_max_entries == 20_000
    assert settings.playbook_scan_max_results == 500
    assert settings.playbook_scan_read_bytes == 65_536


def test_scan_limits_are_configurable() -> None:
    settings = make_settings(playbook_scan_max_depth=3, playbook_scan_max_results=10)

    limits = ScanLimits.from_settings(settings)

    assert limits.max_depth == 3
    assert limits.max_results == 10
    assert limits.max_entries == 20_000


@pytest.mark.parametrize(
    "field",
    [
        "playbook_scan_max_depth",
        "playbook_scan_max_entries",
        "playbook_scan_max_results",
        "playbook_scan_read_bytes",
    ],
)
def test_non_positive_scan_limit_is_rejected(field: str) -> None:
    """Sıfır limit keşfi sessizce boş sonuca çevirirdi."""
    with pytest.raises(ValidationError, match="en az 1"):
        make_settings(**{field: 0})


def test_claim_stale_below_the_safe_minimum_fails_at_startup() -> None:
    """Alt sınırın altındaki eşik sessizce yükseltilmez, uygulama açılmaz.

    Sessiz bir clamp, operatörün ayarladığını sandığı politika ile gerçekte
    uygulanan politikayı ayırır ve bunu fark ettirecek hiçbir iz bırakmaz.
    """
    with pytest.raises(ValidationError, match="ping_preview_claim_stale_seconds"):
        make_settings(ping_preview_claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS - 1)


def test_claim_stale_at_the_safe_minimum_is_accepted() -> None:
    settings = make_settings(ping_preview_claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS)

    assert settings.ping_preview_claim_stale_seconds == MINIMUM_CLAIM_STALE_SECONDS


@pytest.mark.parametrize(
    "field",
    [
        "ping_timeout_seconds",
        "job_stale_seconds",
        "ping_preview_claim_stale_seconds",
        "ping_preview_ttl_seconds",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_ping_duration_fails_at_startup(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match="sonlu"):
        make_settings(**{field: value})


def test_runtime_ping_timeout_derives_job_stale_minimum() -> None:
    ping_timeout = 240.0
    minimum = (
        ping_timeout + PROCESS_TERMINATION_GRACE_SECONDS + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
    )
    with pytest.raises(ValidationError, match="job_stale_seconds"):
        make_settings(
            ping_timeout_seconds=ping_timeout,
            job_stale_seconds=minimum - 1,
        )

    settings = make_settings(
        ping_timeout_seconds=ping_timeout,
        job_stale_seconds=minimum,
    )
    assert settings.job_stale_seconds == minimum


def test_claim_and_job_stale_thresholds_are_independent() -> None:
    ping_timeout = 240.0
    job_minimum = (
        ping_timeout + PROCESS_TERMINATION_GRACE_SECONDS + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
    )
    with pytest.raises(ValidationError, match="ping_preview_claim_stale_seconds"):
        make_settings(
            ping_timeout_seconds=ping_timeout,
            job_stale_seconds=job_minimum,
            ping_preview_claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS - 1,
        )

    settings = make_settings(
        ping_timeout_seconds=ping_timeout,
        job_stale_seconds=job_minimum,
        ping_preview_claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS,
    )
    assert settings.job_stale_seconds == job_minimum
    assert settings.ping_preview_claim_stale_seconds == MINIMUM_CLAIM_STALE_SECONDS


def test_default_settings_still_validate() -> None:
    settings = make_settings()
    assert settings.ping_timeout_seconds > 0
    assert settings.job_stale_seconds >= MINIMUM_CLAIM_STALE_SECONDS


def test_the_store_also_refuses_a_low_threshold_defensively(tmp_path: Path) -> None:
    """Depo doğrudan kurulduğunda da düşük değer reddedilir."""
    with pytest.raises(ValueError, match="claim_stale_seconds"):
        PreviewStore(
            tmp_path / "p",
            ttl_seconds=10.0,
            claim_stale_seconds=MINIMUM_CLAIM_STALE_SECONDS - 0.5,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_preview_store_rejects_non_finite_stale_threshold(tmp_path: Path, value: float) -> None:
    with pytest.raises(ValueError, match="sonlu"):
        PreviewStore(
            tmp_path / "p",
            ttl_seconds=10.0,
            claim_stale_seconds=value,
        )


def test_default_local_actor_is_a_configuration_label() -> None:
    """Gerçek authentication yoktur; OS kullanıcı adı veya IP üretilmez."""
    settings = make_settings()

    assert settings.local_actor == "local-single-user"


@pytest.mark.parametrize("value", ["", "   ", "a" * (MAX_ACTOR_LENGTH + 1)])
def test_invalid_local_actor_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="local_actor"):
        make_settings(local_actor=value)


def test_local_actor_is_stripped() -> None:
    settings = make_settings(local_actor="  ekip-lab  ")

    assert settings.local_actor == "ekip-lab"


@pytest.mark.parametrize(
    "value",
    [INTERNAL_ACTOR_PREFIX, LEGACY_PLAN_ACTOR, f"{INTERNAL_ACTOR_PREFIX}operator"],
)
def test_internal_actor_labels_cannot_be_configured(value: str) -> None:
    """Internal ön ek yapılandırmaya kapalıdır (R1-V3A).

    Uygulamanın kendi ürettiği sentinel'ler ile kullanıcı aktörleri aynı değer
    uzayını paylaşsaydı, sentinel'e bağlanmış kayıtlar gerçek bir istekle
    eşleşebilir hâle gelirdi. Değer sessizce düzeltilmez, reddedilir.
    """
    with pytest.raises(ValidationError, match="local_actor"):
        make_settings(local_actor=value)


# --- R1-V3C1A runner ayarları -----------------------------------------------


def test_runner_command_is_an_argument_list_with_a_documented_default() -> None:
    """Komut shell string değil, **argüman listesidir** (GUVENLIK.md bölüm 5)."""
    settings = make_settings()

    assert settings.ansible_runner_command == ["ansible-runner"]
    assert isinstance(settings.ansible_runner_command, list)


@pytest.mark.parametrize("value", [[], [""], ["   "], ["ansible-runner", " "]])
def test_empty_runner_command_is_rejected(value: list[str]) -> None:
    """Boş komut, çalıştırılacak bir şey olmadığı hâlde başarı gibi görünürdü."""
    with pytest.raises(ValidationError, match="ansible_runner_command"):
        make_settings(ansible_runner_command=value)


@pytest.mark.parametrize(
    "field",
    [
        "playbook_runner_max_stdout_bytes",
        "playbook_runner_max_raw_bytes",
        "playbook_runner_max_events",
        "playbook_runner_max_result_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1, -1_000])
def test_non_positive_runner_limit_is_rejected(field: str, value: int) -> None:
    """Sıfır veya negatif sınır çalıştırmayı anında keserdi."""
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "playbook_runner_max_stdout_bytes",
        "playbook_runner_max_raw_bytes",
        "playbook_runner_max_events",
        "playbook_runner_max_result_bytes",
    ],
)
def test_absurdly_large_runner_limit_is_rejected(field: str) -> None:
    """Hiçbir şeyi durdurmayan bir sınır, sınırın hiç olmamasından yanıltıcıdır."""
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: 10**12})


@pytest.mark.parametrize("value", [1, 40, 100, 256, PLAYBOOK_RUNNER_MIN_RESULT_BYTES - 1])
def test_a_result_budget_below_the_failure_envelope_is_rejected(value: int) -> None:
    """Sonuç bütçesinin tabanı ``1`` **değildir**; ölçülmüş bir alt sınırı vardır.

    Kök neden ölçüldü: normalizer bir sınır aşımında kısmi hiçbir veri taşımayan
    sabit bir fail-closed belge üretir. Bütçe o belgenin altına indiğinde
    normalizer belgeyi yine yayımlar (kendi sınır kontrolü yalnız **başarılı**
    sonuca uygulanır) ama aynı bütçeyi uygulayan okuyucu onu reddeder — yani
    production kendi geçerli çıktısını okuyamaz hâle gelir. ``40`` tam olarak bu
    durumu üreten, gerçekten yapılandırılabilmiş bir değerdi.

    Zarf şema sürümüyle birlikte büyür ve taban da onunla birlikte yükselir:
    ``schema_version=1`` zarfı 212, ``schema_version=2`` zarfı 267 byte'tır.
    ``256`` bu yüzden listeye eklendi — R1-V3J3A'dan önce geçerli olan eski
    taban, bugünün writer'ının en küçük belgesinin **altındadır** ve artık
    reddedilmelidir.
    """
    with pytest.raises(ValidationError, match="playbook_runner_max_result_bytes"):
        make_settings(playbook_runner_max_result_bytes=value)


def test_the_minimum_result_budget_is_accepted_at_its_boundary() -> None:
    """Taban değerin kendisi geçerlidir; reddedilen yalnız altıdır."""
    settings = make_settings(playbook_runner_max_result_bytes=PLAYBOOK_RUNNER_MIN_RESULT_BYTES)

    assert settings.playbook_runner_max_result_bytes == PLAYBOOK_RUNNER_MIN_RESULT_BYTES


@pytest.mark.parametrize(
    "field",
    [
        "playbook_runner_max_stdout_bytes",
        "playbook_runner_max_raw_bytes",
        "playbook_runner_max_events",
    ],
)
def test_other_runner_limits_keep_their_minimum_of_one(field: str) -> None:
    """Taban yalnız sonuç bütçesine özgüdür; diğer sınırlarda ``1`` hâlâ geçerli.

    Tabanı bütün runner sınırlarına yaymak, ölçülmüş tek bir gerekçeyi ilgisiz
    üç alana taşımak olurdu: bir stdout veya event sınırının sonuç belgesinin
    arıza zarfıyla hiçbir ilgisi yoktur.
    """
    settings = make_settings(**{field: 1})

    assert getattr(settings, field) == 1


def test_the_public_runner_ceilings_are_the_enforced_ceilings() -> None:
    """Public sabitler, validator'ın gerçekten uyguladığı tavanlardır.

    Sonucu **okuyan** taraf aynı sabitleri import eder; ayrı yazılmış bir kopya,
    ayarların geçerli saydığı bir yapılandırmanın okuma yolunda reddedildiği
    (ya da tersinin olduğu) noktada fark edilirdi.
    """
    assert PLAYBOOK_RUNNER_MIN_RESULT_BYTES == 320
    assert PLAYBOOK_RUNNER_MAX_EVENTS_CEILING == 500_000
    assert PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING == 20_000_000

    accepted = make_settings(
        playbook_runner_max_events=PLAYBOOK_RUNNER_MAX_EVENTS_CEILING,
        playbook_runner_max_result_bytes=PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING,
    )
    assert accepted.playbook_runner_max_events == PLAYBOOK_RUNNER_MAX_EVENTS_CEILING
    assert accepted.playbook_runner_max_result_bytes == PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING

    with pytest.raises(ValidationError, match="playbook_runner_max_events"):
        make_settings(playbook_runner_max_events=PLAYBOOK_RUNNER_MAX_EVENTS_CEILING + 1)
    with pytest.raises(ValidationError, match="playbook_runner_max_result_bytes"):
        make_settings(playbook_runner_max_result_bytes=PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING + 1)


def test_the_default_result_budget_stays_within_its_bounds() -> None:
    """Varsayılan bütçe kendi taban ve tavanının içindedir."""
    settings = make_settings()

    assert (
        PLAYBOOK_RUNNER_MIN_RESULT_BYTES
        <= settings.playbook_runner_max_result_bytes
        <= PLAYBOOK_RUNNER_MAX_RESULT_BYTES_CEILING
    )
    assert 1 <= settings.playbook_runner_max_events <= PLAYBOOK_RUNNER_MAX_EVENTS_CEILING


@pytest.mark.parametrize(
    "field",
    [
        "playbook_runner_timeout_seconds",
        "playbook_worker_lease_seconds",
        "playbook_worker_heartbeat_seconds",
        "execution_run_stale_seconds",
        "playbook_worker_poll_seconds",
        "execution_run_janitor_interval_seconds",
    ],
)
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_runner_duration_is_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "ceiling"),
    [
        ("playbook_worker_poll_seconds", 60.0),
        ("execution_run_janitor_interval_seconds", MAX_ORPHAN_RETENTION_SECONDS),
        ("execution_run_stale_seconds", MAX_ORPHAN_RETENTION_SECONDS),
    ],
)
def test_worker_intervals_are_bounded_and_never_silently_clamped(
    field: str, ceiling: float
) -> None:
    """Tavan aşıldığında değer sessizce düşürülmez; uygulama açılışta reddeder.

    Sessiz bir clamp, operatörün ayarladığını sandığı politika ile gerçekte
    uygulananı ayırır ve hiçbir iz bırakmaz.
    """
    with pytest.raises(ValidationError, match=field):
        make_settings(**{field: ceiling + 1})


def test_the_poll_interval_may_sit_exactly_on_its_ceiling() -> None:
    """Tam tavandaki bir poll aralığı geçerlidir: tavan reddedilen ilk değildir."""
    settings = make_settings(playbook_worker_poll_seconds=60.0)

    assert settings.playbook_worker_poll_seconds == 60.0


def test_worker_interval_defaults_are_usable() -> None:
    """Varsayılanlar tek tek geçerlidir ve worker'ı beklemede tutmaz."""
    settings = make_settings()

    assert 0 < settings.playbook_worker_poll_seconds <= 60.0
    assert 0 < settings.execution_run_janitor_interval_seconds <= MAX_ORPHAN_RETENTION_SECONDS


# --- Orphan retention tavanı -------------------------------------------------


def test_the_default_settings_stay_inside_the_documented_orphan_retention() -> None:
    """Varsayılanlar **birlikte** belgelenen bir saatlik tavanın içindedir.

    Ölçüm toplam üzerindendir, çünkü bir dizinin yerde kalma süresi tek bir
    alanın değeri değildir: önce ``execution_run_stale_seconds`` kadar yaşlanıp
    eligible olur, sonra onu görecek ilk janitor turunu bekler.
    """
    settings = make_settings()
    retention = (
        settings.execution_run_stale_seconds + settings.execution_run_janitor_interval_seconds
    )

    assert retention <= MAX_ORPHAN_RETENTION_SECONDS
    # Ve pay bırakılmıştır: tam sınıra oturan bir varsayılan, en küçük bir
    # ayar değişikliğinde sözü bozardı.
    assert retention < MAX_ORPHAN_RETENTION_SECONDS
    assert settings.playbook_worker_enabled is False


def test_the_exact_retention_boundary_is_accepted() -> None:
    """Toplamı tam tavana eşit bir yapılandırma geçerlidir; sınır dışlayıcı değildir."""
    settings = make_settings(
        playbook_runner_timeout_seconds=60.0,
        execution_run_stale_seconds=3_000.0,
        execution_run_janitor_interval_seconds=600.0,
    )

    assert (
        settings.execution_run_stale_seconds + settings.execution_run_janitor_interval_seconds
        == MAX_ORPHAN_RETENTION_SECONDS
    )


def test_a_retention_above_the_ceiling_is_rejected_instead_of_clamped() -> None:
    """Tek tek geçerli iki değer birlikte tavanı aşarsa yapılandırma reddedilir.

    Sessiz bir clamp burada özellikle pahalı olurdu: operatör "bir saat" diye
    belgelenmiş bir sözü kendi ayarlarıyla bozduğunu hiçbir yerde göremezdi.
    """
    with pytest.raises(ValidationError, match="execution_run_janitor_interval_seconds"):
        make_settings(
            playbook_runner_timeout_seconds=60.0,
            execution_run_stale_seconds=3_000.0,
            execution_run_janitor_interval_seconds=601.0,
        )


def test_a_long_runner_timeout_cannot_silently_stretch_retention() -> None:
    """Uzun bir çalıştırma tavanı, terk edilmiş dizini saatlerce yerde bırakamaz.

    Alt sınır (stale > timeout + grace + margin) ile üst sınır (stale + interval
    <= tavan) birlikte sağlanamıyorsa yapılandırma yorumlanmaz, reddedilir.
    """
    with pytest.raises(ValidationError):
        make_settings(
            playbook_runner_timeout_seconds=21_600.0,
            execution_run_stale_seconds=3_000.0,
            execution_run_janitor_interval_seconds=600.0,
        )


def test_the_stale_lower_bound_still_covers_a_running_execution() -> None:
    """Retention tavanı, alt sınırı gevşetmez: çalışan bir işin dizini korunur."""
    minimum = 60.0 + PROCESS_TERMINATION_GRACE_SECONDS + EXECUTION_STALE_SAFETY_MARGIN_SECONDS

    with pytest.raises(ValidationError, match="execution_run_stale_seconds"):
        make_settings(
            playbook_runner_timeout_seconds=60.0,
            execution_run_stale_seconds=minimum,
            execution_run_janitor_interval_seconds=600.0,
        )

    settings = make_settings(
        playbook_runner_timeout_seconds=60.0,
        execution_run_stale_seconds=minimum + 1,
        execution_run_janitor_interval_seconds=600.0,
    )
    assert settings.execution_run_stale_seconds == minimum + 1


@pytest.mark.parametrize("heartbeat", [120.0, 121.0, 600.0])
def test_heartbeat_at_or_above_the_lease_is_rejected(heartbeat: float) -> None:
    """Kira, ilk tazeleme fırsatı gelmeden dolarsa sahiplik her turda kaybedilir."""
    with pytest.raises(ValidationError, match="playbook_worker_heartbeat_seconds"):
        make_settings(
            playbook_worker_lease_seconds=120.0,
            playbook_worker_heartbeat_seconds=heartbeat,
        )


def test_a_lease_shorter_than_a_few_heartbeats_is_rejected() -> None:
    """Kira birkaç heartbeat aralığını kapsamalıdır.

    Tek bir gecikmiş heartbeat, canlı bir execution'ın işini başka bir worker'a
    kaptırmasına yol açmamalıdır.
    """
    heartbeat = 30.0
    minimum = MINIMUM_HEARTBEATS_PER_LEASE * heartbeat

    with pytest.raises(ValidationError, match="playbook_worker_lease_seconds"):
        make_settings(
            playbook_worker_heartbeat_seconds=heartbeat,
            playbook_worker_lease_seconds=minimum - 1,
        )

    settings = make_settings(
        playbook_worker_heartbeat_seconds=heartbeat,
        playbook_worker_lease_seconds=minimum,
    )
    assert settings.playbook_worker_lease_seconds == minimum


def test_stale_threshold_must_exceed_timeout_plus_termination_grace() -> None:
    """Temizlik, hâlâ çalışan bir işin dizinini toplamamalıdır."""
    timeout = 600.0
    boundary = timeout + PROCESS_TERMINATION_GRACE_SECONDS + EXECUTION_STALE_SAFETY_MARGIN_SECONDS

    with pytest.raises(ValidationError, match="execution_run_stale_seconds"):
        make_settings(
            playbook_runner_timeout_seconds=timeout,
            execution_run_stale_seconds=boundary,
        )

    settings = make_settings(
        playbook_runner_timeout_seconds=timeout,
        execution_run_stale_seconds=boundary + 1,
    )
    assert settings.execution_run_stale_seconds == boundary + 1


def test_the_worker_is_disabled_by_default() -> None:
    """Ayar açıkça açılmadan arka planda playbook çalıştıran bir döngü doğmaz."""
    assert make_settings().playbook_worker_enabled is False


def test_default_runner_settings_are_mutually_consistent() -> None:
    """Varsayılanlar tek tek değil, **birlikte** de geçerli olmalıdır."""
    settings = make_settings()

    assert settings.playbook_worker_heartbeat_seconds < settings.playbook_worker_lease_seconds
    assert settings.playbook_worker_lease_seconds >= (
        MINIMUM_HEARTBEATS_PER_LEASE * settings.playbook_worker_heartbeat_seconds
    )
    assert settings.execution_run_stale_seconds > (
        settings.playbook_runner_timeout_seconds
        + PROCESS_TERMINATION_GRACE_SECONDS
        + EXECUTION_STALE_SAFETY_MARGIN_SECONDS
    )


def test_execution_run_root_is_derived_from_app_data_dir(tmp_path: Path) -> None:
    """Kök yapılandırmadan serbest bir path olarak alınmaz, türetilir.

    Böylece runner'ın yazdığı her şey uygulamanın kendi 0700 veri alanında
    kalır.
    """
    settings = make_settings(app_data_dir=tmp_path / "app-data")

    root = settings.resolve_execution_run_dir()

    assert root == tmp_path / "app-data" / EXECUTION_RUN_DIRNAME
    assert root.is_absolute()
    # Kararlıdır: ikinci çağrı aynı değeri döndürür ve dizin **oluşturulmaz**.
    assert settings.resolve_execution_run_dir() == root
    assert not root.exists()


def test_a_relative_app_data_dir_has_no_execution_run_root() -> None:
    """Relative bir kök, runner alanını sessizce başka bir yere taşırdı."""
    settings = make_settings(app_data_dir=Path("app-data"))

    with pytest.raises(ValueError, match="absolute"):
        settings.resolve_execution_run_dir()
