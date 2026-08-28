"""Runner child environment'ı — ADR-021 Kapı A1 (R1-V3C1A).

`ansible-runner` kendisini başlatan sürecin environment'ını **miras alır**, bu
yüzden tek güvenilir sınır environment'ı sıfırdan kurmaktır. Buradaki testlerin
merkezinde tek bir ölçüm vardır: üretilen **anahtar kümesi tam eşitlikle**
beklenen kümeye eşit olmalıdır.

Neden tam eşitlik? "Şu değişken geçmiyor" biçiminde yazılmış bir test, yalnız
akla gelen adları kapsar; yarın parent'a eklenen yeni bir secret'ı hiç görmez.
Tam eşitlik ise **sayılmayan her anahtarı** başarısızlık sayar ve listeyi
büyütmeyi bilinçli bir karar hâline getirir.

Parent environment testlerde gerçekten kirletilir: sözlüğü taklit etmek, asıl
riski — modülün ``os.environ``'dan toplu kopyalama yapması — ölçmeden bırakırdı.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import io
import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
import token as token_module
import tokenize
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import EXECUTION_RUN_DIRNAME
from app.models import ExecutionMode
from app.services.execution import runner_env
from app.services.execution.runner_env import (
    ANSIBLE_CONFIG_FILENAME,
    DIRECTORY_MODE,
    FILE_MODE,
    INHERITED_ENV_NAMES,
    MAX_CLEANUP_DEPTH,
    RunDirectoryIdentity,
    RunnerEnvironmentError,
    build_runner_environment,
    list_execution_run_directories,
    remove_execution_run_directory,
)
from app.services.execution.runner_process import (
    RAW_DIRNAME,
    RunnerProcessLimits,
    run_playbook_process,
)
from app.services.execution.workspace import freeze_workspace

# Production'ın gerçek doğrulanmış runner komutu (`Settings.ansible_runner_command`
# varsayılanıyla aynı). `tests.test_runner_process.real_runner_available` **bilinçli
# olarak** buradan import edilmez: bu dosyanın kendi ölçümü, o modülün başka bir
# şeyi taklit edip etmediğinden bağımsız olmalıdır.
PRODUCTION_RUNNER_COMMAND = ["ansible-runner"]


def _production_runner_available() -> bool:
    """Gerçek ``ansible-runner`` (production komutu) bu platformda çalışıyor mu."""
    try:
        completed = subprocess.run(  # noqa: S603
            [*PRODUCTION_RUNNER_COMMAND, "--version"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# Parent'a enjekte edilen sentetik kirli değişkenler. Hiçbiri gerçek bir sır
# değildir; hepsi ürünün gerçekten taşıyabileceği **sınıfları** temsil eder.
POLLUTED_PARENT_ENV = {
    # Uygulamanın kendi ayarları ve sırları.
    "ANSIBLEOPS_DATABASE_URL": "postgresql+psycopg://user:sentinel-dsn@db/app",
    "ANSIBLEOPS_MASTER_KEY": "sentinel-master-key",
    "ANSIBLEOPS_LOCAL_ACTOR": "sentinel-actor",
    "DATABASE_URL": "postgresql://user:sentinel-plain-dsn@db/app",
    "MASTER_KEY": "sentinel-bare-master-key",
    # Ağın yeniden yönlendirilmesi.
    "HTTP_PROXY": "http://sentinel-proxy:3128",
    "HTTPS_PROXY": "http://sentinel-proxy:3128",
    "ALL_PROXY": "socks5://sentinel-proxy:1080",
    "NO_PROXY": "sentinel-no-proxy",
    "http_proxy": "http://sentinel-proxy:3128",
    "https_proxy": "http://sentinel-proxy:3128",
    "all_proxy": "socks5://sentinel-proxy:1080",
    "no_proxy": "sentinel-no-proxy",
    # Ajan üzerinden kimlik kullanımı.
    "SSH_AUTH_SOCK": "/tmp/sentinel-agent.sock",
    "SSH_AGENT_PID": "424242",
    # Bulut credential'ları.
    "AWS_ACCESS_KEY_ID": "SENTINELAKIA",
    "AWS_SECRET_ACCESS_KEY": "sentinel-aws-secret",
    "AWS_SESSION_TOKEN": "sentinel-aws-token",
    "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/sentinel-gcp.json",
    "AZURE_CLIENT_SECRET": "sentinel-azure-secret",
    "GITHUB_TOKEN": "sentinel-github-token",
    # Ansible davranışını sessizce değiştirebilecek rastgele ayarlar.
    "ANSIBLE_FORKS": "77",
    "ANSIBLE_HOST_KEY_CHECKING": "False",
    "ANSIBLE_ROLES_PATH": "/tmp/sentinel-roles",
    "ANSIBLE_CONFIG": "/tmp/sentinel-ansible.cfg",
    "ANSIBLE_VAULT_PASSWORD_FILE": "/tmp/sentinel-vault",
    "ANSIBLE_SSH_ARGS": "-o StrictHostKeyChecking=no",
    # Fact-cache environment politikası (R1-V3D0A): parent'ın fact-cache
    # seçimi kontrollü runner environment'ına miras alınmaz.
    "ANSIBLE_CACHE_PLUGIN": "jsonfile",
    "ANSIBLE_CACHE_PLUGIN_CONNECTION": "/tmp/sentinel-fact-cache",
    # Python'un import yolunun ele geçirilmesi.
    "PYTHONPATH": "/tmp/sentinel-python-path",
    "PYTHONSTARTUP": "/tmp/sentinel-startup.py",
    # Parent'ın kendi ev ve geçici alanı.
    "HOME": "/tmp/sentinel-parent-home",
    "TMPDIR": "/tmp/sentinel-parent-tmp",
}

# Miras alınması **beklenen** `PATH`. `sentinel` içermez, çünkü sızıntı
# taraması bütün environment metninde bu kelimeyi arar.
INHERITED_PATH = "/opt/inherited/bin:/usr/bin"

# Değer taramasının dışında tutulan **jenerik** literaller. Bunlar hiçbir
# kimlik taşımaz: uygulamanın kendi ürettiği bir ayar da aynı değeri alabilir
# (`ANSIBLE_RETRY_FILES_ENABLED = "False"`). Ad taraması bu değişkenleri zaten
# ayrıca kapsar; değer üzerinden eşleşme yalnız yanlış pozitif üretirdi.
NON_IDENTIFYING_VALUES = {"False", "True", "0", "1", ""}

# Uygulamanın **kendi** ürettiği kontrollü anahtarlar.
CONTROLLED_ENV_NAMES = {
    "HOME",
    "TMPDIR",
    "ANSIBLE_HOME",
    "ANSIBLE_LOCAL_TEMP",
    "ANSIBLE_SSH_CONTROL_PATH_DIR",
    "ANSIBLE_NOCOLOR",
    "ANSIBLE_FORCE_COLOR",
    "ANSIBLE_RETRY_FILES_ENABLED",
    "ANSIBLE_CACHE_PLUGIN",
    "PYTHONIOENCODING",
    "ANSIBLE_SSH_ARGS",
    "ANSIBLE_CONFIG",
}


@pytest.fixture
def polluted_parent(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Parent environment'ı sentinel değerlerle kirletir.

    Ayrıca miras alınan dar kümeye bilinen değerler konur; böylece "geçmesi
    gereken geçti mi" sorusu da ölçülebilir olur.
    """
    for name, value in POLLUTED_PARENT_ENV.items():
        monkeypatch.setenv(name, value)
    # Miras alınan değerler bilinçli olarak `sentinel` **içermez**: bunların
    # geçmesi beklenir ve sızıntı taramasında yanlış pozitif üretmemelidirler.
    monkeypatch.setenv("PATH", INHERITED_PATH)
    monkeypatch.setenv("LANG", "tr_TR.UTF-8")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    return dict(POLLUTED_PARENT_ENV)


@pytest.fixture
def frozen_project(tmp_path: Path) -> Path:
    """Dondurulmuş project ağacını taklit eden bir kök (``ansible.cfg`` yok)."""
    root = tmp_path / "workspace" / "project"
    root.mkdir(parents=True)
    (root / "site.yml").write_text("- hosts: all\n", encoding="utf-8")
    return root


@pytest.fixture
def known_hosts(tmp_path: Path) -> Path:
    path = tmp_path / "known_hosts"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    """Önceden var olan, 0700 ve doğru adlı bir execution run kökü.

    Kökü test'in kurması bilinçlidir: sözleşme gereği modül kökü
    **oluşturmaz** (onu `ensure_app_data_dirs` kurar) ve niteliklerini yalnız
    doğrular.
    """
    root = tmp_path / "app-data" / EXECUTION_RUN_DIRNAME
    root.mkdir(parents=True)
    root.chmod(0o700)
    return root


@pytest.fixture
def job_id() -> str:
    """Gerçek bir canonical UUID4 Job kimliği (`Job.id` ile aynı biçim)."""
    return str(uuid.uuid4())


RUNNER_ENV_SOURCE = Path("app/services/execution/runner_env.py")


def _executable_code(path: Path) -> tuple[str, set[str]]:
    """Modülün **çalışan** kodunu yorum ve string literalleri atarak çözümler.

    Ham metinde arama yapmak yanıltıcıdır: modülün docstring'i yasak yapıların
    adını zaten *anlatıyor*. Kapsam kilidi kodun kendisini ölçmelidir, onu
    açıklayan metni değil.

    Returns:
        ``(boşluksuz kod, kullanılan tanımlayıcı adları)`` ikilisi.
    """
    source = path.read_text(encoding="utf-8")
    compact: list[str] = []
    names: set[str] = set()
    for item in tokenize.generate_tokens(io.StringIO(source).readline):
        if item.type in (token_module.COMMENT, token_module.STRING):
            continue
        if item.type == token_module.NAME:
            names.add(item.string)
        compact.append(item.string)
    return "".join(compact), names


def _build(  # type: ignore[no-untyped-def]
    run_root: Path,
    job_id: str,
    project: Path,
    known_hosts: Path,
    policy: str = "strict",
):
    return build_runner_environment(
        execution_run_root=run_root,
        job_id=job_id,
        frozen_project_root=project,
        ssh_policy=policy,
        known_hosts=known_hosts,
    )


# --- Kapı A1: anahtar kümesi -------------------------------------------------


def test_key_set_equals_the_allowlist_exactly(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Üretilen anahtar kümesi **tam eşitlikle** beklenen kümeye eşittir.

    Bu, Kapı A1'in asıl ölçümüdür: sayılmayan her anahtar başarısızlıktır, bu
    yüzden listeyi büyütmek bilinçli bir karar olmak zorundadır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)

    inherited = {name for name in INHERITED_ENV_NAMES if name in os.environ}
    assert set(result.environment) == inherited | CONTROLLED_ENV_NAMES


def test_no_polluted_parent_variable_reaches_the_child(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Ne sentinel adı ne de sentinel **değeri** çocuğa geçer.

    Değerler de aranır: bir sırrın başka bir ad altında taşınması, ad listesine
    bakan bir teste görünmezdi.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)

    for name, value in polluted_parent.items():
        # `HOME`, `TMPDIR` ve `ANSIBLE_*` adları kontrollü değerlerle yeniden
        # üretilir; yasak olan parent'ın **değerinin** taşınmasıdır.
        if name not in CONTROLLED_ENV_NAMES:
            assert name not in result.environment, name
        if value not in NON_IDENTIFYING_VALUES:
            assert value not in result.environment.values(), f"{name} değeri sızdı"

    serialized = "\n".join(f"{key}={value}" for key, value in result.environment.items())
    assert "sentinel" not in serialized.lower()


@pytest.mark.parametrize(
    "leaked",
    [
        "ANSIBLEOPS_DATABASE_URL",
        "ANSIBLEOPS_MASTER_KEY",
        "DATABASE_URL",
        "MASTER_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "GITHUB_TOKEN",
        "ANSIBLE_FORKS",
        "ANSIBLE_HOST_KEY_CHECKING",
        "ANSIBLE_ROLES_PATH",
        "ANSIBLE_VAULT_PASSWORD_FILE",
        "ANSIBLE_CACHE_PLUGIN_CONNECTION",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    ],
)
def test_named_dangerous_variables_are_absent(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
    leaked: str,
) -> None:
    """Ada göre de ölçülür: hangi sınıfın kapsandığı testten okunabilmelidir."""
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert leaked not in result.environment


def test_inherited_variables_pass_through_unchanged(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """`PATH`/`LANG`/`LC_ALL` kontrollü biçimde ve **değişmeden** geçer.

    Bunlar platformun kendi çalışması için gereklidir: `PATH` olmadan
    yorumlayıcı ve `ssh` bulunamaz.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["PATH"] == INHERITED_PATH
    assert result.environment["LANG"] == "tr_TR.UTF-8"
    assert result.environment["LC_ALL"] == "tr_TR.UTF-8"


def test_missing_inherited_variable_is_skipped_silently(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Parent'ta olmayan bir ad üretilmez; boş string uydurulmaz."""
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)

    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert "LC_ALL" not in result.environment
    assert "LANG" not in result.environment


def test_home_and_tmpdir_are_controlled_not_inherited(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """`HOME`/`TMPDIR` parent değeri değil, çalışma alanı altındaki dizinlerdir.

    Ansible'ın ``remote_tmp`` varsayılanı ``~/.ansible/tmp``'dir ve ``~`` passwd
    kaydından da çözülebildiği için ``ANSIBLE_LOCAL_TEMP`` tek başına yetmez
    (Kapı A ölçümü); ev dizini de kontrollü alana bağlanır.
    """
    run_dir = run_root / job_id
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["HOME"] == str(run_dir / "home")
    assert result.environment["TMPDIR"] == str(run_dir / "tmp")
    assert result.environment["HOME"] != POLLUTED_PARENT_ENV["HOME"]
    assert result.environment["TMPDIR"] != POLLUTED_PARENT_ENV["TMPDIR"]
    for name in ("HOME", "TMPDIR", "ANSIBLE_HOME", "ANSIBLE_LOCAL_TEMP"):
        assert Path(result.environment[name]).is_relative_to(run_dir), name


def test_controlled_ansible_values_are_fixed(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Renk, retry ve kodlama ayarları parent'tan değil, uygulamadan gelir."""
    run_dir = run_root / job_id
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["ANSIBLE_NOCOLOR"] == "1"
    assert result.environment["ANSIBLE_FORCE_COLOR"] == "0"
    assert result.environment["ANSIBLE_RETRY_FILES_ENABLED"] == "False"
    assert result.environment["PYTHONIOENCODING"] == "utf-8"
    assert result.environment["ANSIBLE_SSH_CONTROL_PATH_DIR"] == str(run_dir / "ssh-control")


# --- Fact-cache environment politikası (R1-V3D0A, ADR-021 Kapı D) -----------


def test_runner_environment_requests_memory_fact_cache(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """``build_runner_environment``'ın ürettiği sözlükte fact-cache isteği ``memory``dir.

    Bu yalnız bu modülün ürettiği environment sözlüğünü ölçer. Sonraki
    ``ansible-runner`` override'ını ne kanıtlar ne de reddeder. Raw
    ``jsonfile`` override'ı kurulu paket kaynak incelemesi ve ayrı,
    cleanup-disabled bir reprodüksiyonla doğrulanmıştır — bu testin kapsamı
    dışındadır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["ANSIBLE_CACHE_PLUGIN"] == "memory"


def test_parent_cache_selection_is_not_inherited_by_runner_environment(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Parent'ın ``jsonfile`` seçimi ve bağlantı sentinel'i child environment'a geçmez.

    Parent'a konan ``ANSIBLE_CACHE_PLUGIN=jsonfile`` yine de bu modülün
    ürettiği sözlükte ``memory``'ye döner ve ``ANSIBLE_CACHE_PLUGIN_CONNECTION``
    sentinel'i child environment'ta hiçbir anahtar altında bulunmaz. Bu yalnız
    ``build_runner_environment`` çıktısını ölçer; ``ansible-runner``'ın
    runtime'da bu environment'ı kendi raw ``jsonfile`` cache'iyle sonradan
    ezdiği bu testin kapsamı dışındadır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["ANSIBLE_CACHE_PLUGIN"] == "memory"
    assert "ANSIBLE_CACHE_PLUGIN_CONNECTION" not in result.environment
    assert polluted_parent["ANSIBLE_CACHE_PLUGIN_CONNECTION"] not in result.environment.values()


def test_project_config_does_not_change_the_requested_fact_cache_environment(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """Dondurulmuş project ``fact_caching=jsonfile`` istese de istek yine ``memory``.

    Project config'i kategorik olarak kapatılmaz (ADR-022 Karar 3): dosya yine
    ``ANSIBLE_CONFIG`` olarak kullanılır. Bu test yalnız üretilen environment
    sözlüğünü ölçer — project'in başka hiçbir ayarı bu yolda etkilenmez.
    Gerçek production-chain testi
    (``test_a_real_production_runner_run_never_honors_the_projects_fact_cache_connection``)
    yalnız project'in sentinel connection yolunun kullanılmadığını ölçer; raw
    cache'i gözlemlemez. ``ansible-runner`` CLI'nin kendi raw ``jsonfile``
    override'ı ve bunun nedenselliği ayrı bir reprodüksiyon/kaynak
    incelemesiyle doğrulanmıştır; bu test ya da production-chain testi
    tarafından değil.
    """
    sentinel_connection = tmp_path / "saldirgan-fact-cache"
    config = frozen_project / ANSIBLE_CONFIG_FILENAME
    config.write_text(
        "[defaults]\n"
        "fact_caching = jsonfile\n"
        f"fact_caching_connection = {sentinel_connection}\n"
        "gathering = smart\n"
        "roles_path = ./roles\n",
        encoding="utf-8",
    )

    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["ANSIBLE_CACHE_PLUGIN"] == "memory"
    assert "ANSIBLE_CACHE_PLUGIN_CONNECTION" not in result.environment
    # Project config hâlâ seçilir ve başka hiçbir ayarı kapatılmaz.
    assert result.environment["ANSIBLE_CONFIG"] == str(config)
    assert result.uses_project_config is True


@pytest.mark.skipif(
    not _production_runner_available(),
    reason="Production runner komutu (`ansible-runner`) bu platformda çalıştırılamıyor.",
)
def test_a_real_production_runner_run_never_honors_the_projects_fact_cache_connection(
    run_root: Path,
    tmp_path: Path,
    known_hosts: Path,
) -> None:
    """Gerçek production zinciriyle uçtan uca ölçüm: project'in **kendi seçtiği**
    fact-cache bağlantı yolu hiçbir zaman kullanılmaz.

    Zincir taklit edilmez: :func:`build_runner_environment` gerçek çalışma
    alanını kurar, :func:`freeze_workspace` gerçek dondurma primitive'iyle
    ``project/`` + ``inventory/hosts.yml`` + ``manifest.json`` düzenini üretir
    ve :func:`run_playbook_process` — production'ın kendi doğrulanmış
    ``ansible-runner`` komutuyla — gerçek ``ansible-runner run`` sürecini
    başlatır; o da kendi içinde gerçek ``ansible-playbook``'u çalıştırır.

    Project ``ansible.cfg``'si ``fact_caching=jsonfile`` ve saldırgan bir
    ``fact_caching_connection`` yolu istiyor; playbook ``gather_facts: true``
    çalıştırıyor. Sentinel bağlantı dizini **run kökünün tamamen dışında**,
    ``tmp_path`` altındadır: ürünün ham (`raw`) alan temizliği yalnız
    ``run_dir/raw``'a dokunur, bu yüzden sentinel'in oluşmaması cleanup'ın bir
    yan etkisi olamaz — ürün onu hiç görmez, hiç açmaz, hiç silmez. Tek
    açıklama, gerçek çalıştırma sırasında hiç yazılmamış olmasıdır: bu, bir
    operatörün kendi ``ansible.cfg``'si üzerinden fact-cache'i **keyfi bir
    dosya sistemi yoluna** yönlendirmesinin engellendiğinin kanıtıdır.

    Çalıştırmanın gerçekten gerçekleştiği (`vacuous` bir "dosya yok" iddiası
    olmadığı) ham runner event akışından — son event'in ``playbook_on_stats``
    olduğu ve host'un gerçekten ``ok`` sayıldığı — ayrıca doğrulanır.

    **Ölçülmemiş, bilinen bir sınır (R1-V3D0A-AUDIT-FIX1 bulgusu).** Bu test
    yalnız project'in **kendi belirttiği** bağlantı yolunun kullanılmadığını
    kanıtlar; `ansible-runner` CLI'sinin (`ansible_runner.config._base.BaseConfig`,
    2.4.3, `fact_cache_type` constructor varsayılanı `'jsonfile'` ve CLI'de bunu
    değiştirecek bir seçenek **yok**) kendi iç mantığı, ``ANSIBLE_CACHE_PLUGIN``
    ve ``ANSIBLE_CACHE_PLUGIN_CONNECTION``'ı — bu modülün ürettiği environment de
    dâhil — koşulsuz olarak kendi ``<raw>/<ident>/fact_cache`` yoluna ezer
    (ölçüldü: mutation açık/kapalı fark etmeksizin ``raw/<ident>/fact_cache/s1_<host>``
    her ikisinde de oluşuyor). Bu yol `run_playbook_process`'in kendi ham alan
    temizliğiyle (`_remove_raw_directory`, her yolda) silinir; ama çalışma
    sırasında disk üzerinde kısa süreli düz metin fact verisi vardır ve bu
    modülün (`runner_env.py`) hiçbir environment değişkeni bunu engelleyemez —
    ezme, `ansible-runner` içinde, environment kurulduktan **sonra** olur.
    Düzeltme bu dilimin kapsamının (yalnız `runner_env.py`) dışındadır.
    """
    plan_root = tmp_path / "app-data" / "execution-plans"
    source_project = tmp_path / "kaynak-proje"
    source_project.mkdir(parents=True)
    sentinel_connection = tmp_path / "saldirgan-fact-cache"
    (source_project / ANSIBLE_CONFIG_FILENAME).write_text(
        "[defaults]\n"
        "fact_caching = jsonfile\n"
        f"fact_caching_connection = {sentinel_connection}\n"
        "gathering = smart\n",
        encoding="utf-8",
    )
    (source_project / "site.yml").write_text(
        "- hosts: probehost\n"
        "  gather_facts: true\n"
        "  tasks:\n"
        "    - ansible.builtin.debug:\n"
        "        msg: probe\n",
        encoding="utf-8",
    )

    frozen = freeze_workspace(
        plan_root,
        project_root=source_project,
        inventory_snapshot=json.dumps(
            {
                "all": {
                    "hosts": {
                        "probehost": {
                            "ansible_connection": "local",
                            # Interpreter discovery'yi sabitler: `gather_facts: true`
                            # aksi hâlde bazı çalıştırmalarda stdout'a düz metin bir
                            # `[WARNING]: ... discovered Python interpreter ...`
                            # satırı karıştırabilir ve JSON-per-line ölçümünü
                            # deterministik olmayan biçimde bozardı. Bu değişken
                            # üretimin kendi inventory snapshot allowlist'inin
                            # parçasıdır (`app/services/ansible/inventory_snapshot.py`).
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    frozen_project_root = plan_root / frozen.workspace_id / "project"
    frozen_inventory_path = plan_root / frozen.workspace_id / "inventory" / "hosts.yml"

    # `job_id` `workspace_id`'den bağımsız, canonical UUID4 bir Job kimliğidir
    # (production'da ikisi ayrı kavramlardır).
    job_id = str(uuid.uuid4())
    environment = build_runner_environment(
        execution_run_root=run_root,
        job_id=job_id,
        frozen_project_root=frozen_project_root,
        ssh_policy="strict",
        known_hosts=known_hosts,
    )

    result = run_playbook_process(
        command=PRODUCTION_RUNNER_COMMAND,
        runner_environment=environment,
        job_id=job_id,
        frozen_project_root=frozen_project_root,
        frozen_inventory_path=frozen_inventory_path,
        playbook_path="site.yml",
        mode=ExecutionMode.CHECK,
        limits=RunnerProcessLimits(
            timeout_seconds=180.0, max_stdout_bytes=5_000_000, max_raw_bytes=50_000_000
        ),
    )

    assert result.timed_out is False
    assert result.oversized_stream is None
    assert result.raw_limit_exceeded is False
    assert result.return_code == 0, result.stderr_text

    # Çocuğun gerçekten çalıştığının kanıtı: gerçek runner event akışı gerçek
    # bir başarı ile biter. Bu olmadan "dosya yok" iddiası hiç çalışmamış bir
    # süreç üzerinden de yeşil geçerdi.
    events = [json.loads(line) for line in result.stdout_text.splitlines() if line.strip()]
    assert events, "runner hiç event üretmedi"
    assert events[-1]["event"] == "playbook_on_stats"
    # `gather_facts: true` + `debug` görevi: iki `ok`.
    assert events[-1]["event_data"]["ok"] == {"probehost": 2}
    assert events[-1]["event_data"]["failures"] == {}

    # `raw` alanı ürünün kendi ürünüdür ve her yolda silinir; bu, sentinel
    # ölçümünün konusu değildir.
    assert not (environment.run_dir / RAW_DIRNAME).exists()

    # Asıl ölçüm: production runner çalıştıktan sonra sentinel bağlantı hiç
    # oluşmamıştır. Sentinel `run_dir`'ın dışındadır; ürünün hiçbir cleanup
    # yolu buraya dokunmaz, bu yüzden yokluğu cleanup'ın değil, gerçek
    # çalıştırmanın kanıtıdır.
    assert not sentinel_connection.exists(), "jsonfile fact-cache sentinel dizini olustu"

    remove_execution_run_directory(run_root, job_id)


# --- SSH argümanları ---------------------------------------------------------


def test_ssh_args_come_from_the_trusted_primitive(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    polluted_parent: dict[str, str],
    frozen_project: Path,
    known_hosts: Path,
) -> None:
    """`ANSIBLE_SSH_ARGS` serbest metinden üretilmez ve parent'tan alınmaz.

    Parent'ta host key doğrulamasını kapatan bir değer duruyor; çocuğa geçen
    değer uygulamanın kendi izolasyon primitive'inden gelmelidir.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    rendered = result.environment["ANSIBLE_SSH_ARGS"]

    assert "StrictHostKeyChecking=no" not in rendered
    assert "StrictHostKeyChecking=yes" in rendered
    assert "IdentityAgent=none" in rendered
    assert "BatchMode=yes" in rendered
    assert str(known_hosts) in rendered


def test_accept_new_policy_is_rendered_as_such(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    result = _build(run_root, job_id, frozen_project, known_hosts, policy="accept_new")

    assert "StrictHostKeyChecking=accept-new" in result.environment["ANSIBLE_SSH_ARGS"]


def test_unknown_ssh_policy_is_rejected(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Host key doğrulamasını kapatan bir politika kabul edilmez."""
    with pytest.raises(ValueError):
        _build(run_root, job_id, frozen_project, known_hosts, policy="no")


def test_environment_takes_no_override_parameter() -> None:
    """`extra`/override parametresi yoktur: allowlist tek çağrıyla delinemez.

    Çağıranın environment'a serbestçe anahtar ekleyebilmesi, Kapı A1'in tam
    eşitlik ölçümünü anlamsız kılardı.
    """
    import inspect as inspect_module

    signature = inspect_module.signature(build_runner_environment)

    assert set(signature.parameters) == {
        # Çağıran bir çalışma dizini **seçemez**; yalnız kökü ve Job kimliğini
        # verir, dizin adı bunlardan türetilir (R1-V3C1AF).
        "execution_run_root",
        "job_id",
        "frozen_project_root",
        "ssh_policy",
        "known_hosts",
    }
    assert "run_dir" not in signature.parameters
    # Hepsi keyword-only: konumsal bir sözlük "environment" gibi geçirilemez.
    assert all(
        parameter.kind is inspect_module.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    # Hiçbiri ham token/secret adı taşımaz.
    assert not {"token", "secret", "extra", "env", "environment", "overrides"} & set(
        signature.parameters
    )


def test_module_never_copies_the_parent_environment() -> None:
    """Toplu kopyalama, allowlist'i tek satırda geçersiz kılardı."""
    code, _ = _executable_code(RUNNER_ENV_SOURCE)

    for forbidden in ("os.environ.copy()", "dict(os.environ)", "**os.environ"):
        assert forbidden not in code, forbidden

    # `os.environ` yalnız **ad ad** okumak için kullanılır; başka bir kullanımı
    # (iterasyon, güncelleme, kopyalama) kodda bulunmamalıdır.
    assert code.count("os.environ") == 2  # `name in os.environ` ve `os.environ[name]`


# --- Project ansible.cfg -----------------------------------------------------


def test_project_ansible_cfg_is_selected_when_present(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Project'in kendi config'i kategorik olarak kapatılmaz (ADR-022 Karar 3).

    Role, collection ve plugin yollarını project'in kendi config'i tarif eder;
    onu görmezden gelmek, operatörün kendi Ansible project'ini çalıştırılamaz
    hâle getirirdi.
    """
    config = frozen_project / ANSIBLE_CONFIG_FILENAME
    config.write_text("[defaults]\nroles_path = ./roles\n", encoding="utf-8")

    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert result.environment["ANSIBLE_CONFIG"] == str(config)
    assert result.uses_project_config is True
    # Çalışma alanına gölge bir config yazılmaz.
    assert not (tmp_path / "run" / ANSIBLE_CONFIG_FILENAME).exists()


def test_an_empty_private_config_is_written_when_the_project_has_none(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Config yoksa 0600 izinli, **boş** bir dosya üretilir.

    ``ANSIBLE_CONFIG`` boş bırakılsaydı Ansible cwd, ev dizini ve
    ``/etc/ansible/ansible.cfg`` üzerinden keşif yapar ve çalıştırma dondurulmuş
    içeriğin dışındaki bir yapılandırmaya bağlanırdı.
    """
    run_dir = run_root / job_id
    result = _build(run_root, job_id, frozen_project, known_hosts)

    config = run_dir / ANSIBLE_CONFIG_FILENAME
    assert result.environment["ANSIBLE_CONFIG"] == str(config)
    assert result.uses_project_config is False
    assert config.read_text(encoding="utf-8") == ""
    assert stat.S_IMODE(config.stat().st_mode) == FILE_MODE


def test_the_config_choice_only_looks_at_the_frozen_root(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Özgün project ağacı bu yolda hiç açılmaz.

    Onaylanan içerik dondurulmuş kopyadır ve yalnız o kopya manifest ile
    doğrulanmıştır; özgün ağaçtaki bir config seçilseydi, çalıştırma
    onaylanmamış bir yapılandırmaya bağlanırdı.
    """
    original = tmp_path / "original-project"
    original.mkdir()
    (original / ANSIBLE_CONFIG_FILENAME).write_text("[defaults]\nforks = 77\n", encoding="utf-8")

    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert str(original) not in result.environment["ANSIBLE_CONFIG"]
    assert result.uses_project_config is False


def test_a_symlinked_project_config_is_rejected(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Symlink config, dondurulmuş ağacın dışını okutabilirdi: fail-closed."""
    outside = tmp_path / "outside.cfg"
    outside.write_text("[defaults]\nforks = 99\n", encoding="utf-8")
    os.symlink(outside, frozen_project / ANSIBLE_CONFIG_FILENAME)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert error.value.details == {"reason": "ansible_config_symlink"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO yalnız POSIX'te")
def test_a_special_file_project_config_is_rejected(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """FIFO bir config, açılışta süreci bloke edebilirdi: fail-closed."""
    os.mkfifo(frozen_project / ANSIBLE_CONFIG_FILENAME)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert error.value.details == {"reason": "ansible_config_not_regular"}


def test_a_missing_frozen_project_fails_closed(
    run_root: Path, job_id: str, tmp_path: Path, known_hosts: Path
) -> None:
    """Dondurulmuş kök yoksa environment yarım kurulup devam edilmez."""
    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, tmp_path / "yok", known_hosts)

    assert error.value.details == {"reason": "frozen_project_unreadable"}


def test_the_error_leaks_no_paths(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Hata ne path ne config içeriği taşır."""
    os.symlink(tmp_path / "gizli-hedef.cfg", frozen_project / ANSIBLE_CONFIG_FILENAME)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert str(tmp_path) not in error.value.message
    assert str(tmp_path) not in str(error.value.details)


# --- Çalışma alanı izinleri --------------------------------------------------


def test_controlled_directories_are_private(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kök ve bütün sabit alt dizinler 0700'dür.

    ``mkdir`` mode'u umask ile maskelendiği için izin ayrıca ``chmod`` ile
    sabitlenir; kalıtılan bir umask alanı başkalarına okunur bırakabilirdi.
    """
    run_dir = run_root / job_id
    _build(run_root, job_id, frozen_project, known_hosts)

    assert stat.S_IMODE(run_dir.stat().st_mode) == DIRECTORY_MODE
    for name in ("home", "tmp", "ansible-home", "ansible-tmp", "ssh-control"):
        child = run_dir / name
        assert child.is_dir(), name
        assert stat.S_IMODE(child.stat().st_mode) == DIRECTORY_MODE, name


def test_a_permissive_umask_does_not_widen_the_workspace(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """İzinler umask'e bırakılmaz."""
    previous = os.umask(0o000)
    try:
        run_dir = run_root / job_id
        _build(run_root, job_id, frozen_project, known_hosts)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(run_dir.stat().st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE((run_dir / ANSIBLE_CONFIG_FILENAME).stat().st_mode) == FILE_MODE


# --- Çalışma alanı sınırı (R1-V3C1AF) ---------------------------------------


def test_an_existing_job_directory_is_never_reused(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Aynı kimlikte duran bir dizin yeniden kullanılmaz.

    Yeniden kullanmak, önceki bir çalıştırmadan kalan raw artifact ve geçici
    dosyaların yeni execution'ın sonucuna karışmasına izin verirdi. İkinci çağrı
    fail-closed düşer ve mevcut içeriğe **dokunmaz**.
    """
    first = _build(run_root, job_id, frozen_project, known_hosts)
    marker = first.run_dir / "onceki-artifact"
    marker.write_text("eski", encoding="utf-8")

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert error.value.details == {"reason": "run_dir_already_exists"}
    # Kalıntı silinmez de üzerine yazılmaz da: temizlik bu modülün işi değildir.
    assert marker.read_text(encoding="utf-8") == "eski"


@pytest.mark.parametrize(
    "existing",
    ["directory", "file", "symlink"],
)
def test_any_existing_entry_blocks_the_job_directory(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
    existing: str,
) -> None:
    """Dizin, dosya veya symlink — hangisi olursa olsun izlenmez ve reddedilir."""
    target = run_root / job_id
    if existing == "directory":
        target.mkdir()
    elif existing == "file":
        target.write_text("", encoding="utf-8")
    else:
        outside = tmp_path / "disarida"
        outside.mkdir()
        os.symlink(outside, target)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert error.value.details == {"reason": "run_dir_already_exists"}
    if existing == "symlink":
        # Symlink izlenmedi: hedefin altına hiçbir şey açılmadı.
        assert list((tmp_path / "disarida").iterdir()) == []


def test_a_relative_execution_run_root_is_rejected(frozen_project: Path, known_hosts: Path) -> None:
    """Relative kök sürecin çalışma dizinine göre çözülür; kabul edilmez."""
    with pytest.raises(RunnerEnvironmentError) as error:
        _build(Path(EXECUTION_RUN_DIRNAME), str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_not_absolute"}


def test_a_relative_frozen_project_root_is_rejected(
    run_root: Path, job_id: str, known_hosts: Path
) -> None:
    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, Path("relative-project"), known_hosts)

    assert error.value.details == {"reason": "frozen_project_root_not_absolute"}


def test_a_root_with_an_unexpected_name_is_rejected(
    tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kök adı sabittir.

    Serbest bir kök adı, çağıranın herhangi bir dizini "execution run kökü"
    diye geçirip altına Job dizinleri açtırmasına izin verirdi.
    """
    impostor = tmp_path / "tmp"
    impostor.mkdir()
    impostor.chmod(0o700)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(impostor, str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_unexpected_name"}
    # Ada bakılırken dosya sistemine hiç dokunulmadı.
    assert list(impostor.iterdir()) == []


@pytest.mark.parametrize(
    "job_name",
    [
        "not-a-uuid",
        "../escape",
        "",
        ".",
        "..",
        # UUID1: canonical biçimde ama version 4 değil.
        "c232ab00-9414-11ec-b3c8-9e6bdeced846",
        # Büyük harfli: canonical gösterim küçük harftir.
        "6F9619FF-8B86-D011-B42D-00CF4FC964FF",
        # Geçerli UUID4'e ek takılmış.
        "5f2a1b3c-4d5e-4f6a-8b9c-0d1e2f3a4b5c/..",
    ],
)
def test_a_non_canonical_job_id_is_rejected(
    run_root: Path, frozen_project: Path, known_hosts: Path, job_name: str
) -> None:
    """Job dizini adı yalnız uygulamanın ürettiği canonical UUID4 olabilir.

    Serbest bir ad, kök altında keyfi bir girdinin — veya ``..`` ile kök
    **dışındaki** bir girdinin — hedeflenmesine izin verirdi.
    """
    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_name, frozen_project, known_hosts)

    assert error.value.details == {"reason": "job_id_not_canonical"}
    # Hiçbir dizin oluşmadı.
    assert list(run_root.iterdir()) == []


def test_a_symlinked_execution_run_root_is_rejected(
    tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kökün yerine konmuş bir symlink izlenmez (``O_NOFOLLOW``)."""
    real = tmp_path / "gercek-kok"
    real.mkdir()
    real.chmod(0o700)
    link = tmp_path / EXECUTION_RUN_DIRNAME
    os.symlink(real, link)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(link, str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_unavailable"}
    # Symlink hedefinin altına hiçbir şey açılmadı.
    assert list(real.iterdir()) == []


def test_a_missing_execution_run_root_is_not_created(
    tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kök **oluşturulmaz**; yokluğu fail-closed hatadır.

    Kökü çalışma anında ``parents=True`` ile üretmek, herhangi bir absolute
    yolun altına dizin ağacı açabilen bir yüzey bırakırdı.
    """
    missing = tmp_path / "hic-olmayan" / EXECUTION_RUN_DIRNAME

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(missing, str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_unavailable"}
    assert not (tmp_path / "hic-olmayan").exists()


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777, 0o750])
def test_a_world_or_group_readable_root_is_rejected(
    tmp_path: Path, frozen_project: Path, known_hosts: Path, mode: int
) -> None:
    """Kökün izni **düzeltilmez**, doğrulanır.

    Kökü sessizce 0700'e çekmek, yanlış kurulmuş bir kurulumu kabul etmek ve
    operatöre hiçbir iz bırakmamak olurdu.
    """
    root = tmp_path / EXECUTION_RUN_DIRNAME
    root.mkdir()
    root.chmod(mode)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(root, str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_not_private"}
    assert stat.S_IMODE(root.stat().st_mode) == mode
    assert list(root.iterdir()) == []


def test_a_file_in_place_of_the_root_is_rejected(
    tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kök normal bir dizin olmalıdır."""
    root = tmp_path / EXECUTION_RUN_DIRNAME
    root.write_text("", encoding="utf-8")

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(root, str(uuid.uuid4()), frozen_project, known_hosts)

    assert error.value.details == {"reason": "execution_run_root_unavailable"}


def test_nothing_is_created_or_chmodded_outside_the_root(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Bütün yazma ve izin değişikliği kökün altında kalır.

    Önceki biçim ``run_dir`` olarak verilen **herhangi** bir yolu
    ``mkdir(parents=True)`` ile açıp path tabanlı ``chmod`` uyguluyordu; bu test
    o yüzeyin kapandığını ölçer.
    """
    before = {
        path: path.stat().st_mode
        for path in sorted(tmp_path.rglob("*"))
        if not path.is_relative_to(run_root)
    }

    result = _build(run_root, job_id, frozen_project, known_hosts)

    after = {
        path: path.stat().st_mode
        for path in sorted(tmp_path.rglob("*"))
        if not path.is_relative_to(run_root)
    }
    assert after == before

    # Üretilen her path kökün altındadır.
    assert result.run_dir == run_root / job_id
    for name in (
        "HOME",
        "TMPDIR",
        "ANSIBLE_HOME",
        "ANSIBLE_LOCAL_TEMP",
        "ANSIBLE_SSH_CONTROL_PATH_DIR",
    ):
        assert Path(result.environment[name]).is_relative_to(run_root), name
    # Kökün altında **yalnız** bu Job'ın dizini vardır.
    assert [entry.name for entry in run_root.iterdir()] == [job_id]

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, Path("relative-project"), known_hosts)
    assert error.value.details == {"reason": "frozen_project_root_not_absolute"}


# --- Hazırlığın failure-atomikliği (R1-V3C1C2-AUDIT-FIX1) --------------------
#
# Job dizini `mkdir` edildikten sonra düşen bir hazırlık, kalıntıyı geride
# bırakırsa aynı kimlikle yapılacak bir sonraki hazırlığı `run_dir_already_exists`
# ile düşürür — ve kalıntıyı toplayan bir janitor bu dilimde yoktur. Aşağıdaki
# testler o yüzden tek bir şeyi ölçer: **arıza dalından sonra kök boştur.**


def test_an_unknown_ssh_policy_leaves_no_run_directory(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Politika reddi Job dizini açıldıktan **sonra** gelir; alan geri verilir."""
    with pytest.raises(ValueError):
        _build(run_root, job_id, frozen_project, known_hosts, policy="no")

    assert not (run_root / job_id).exists()
    assert list(run_root.iterdir()) == []
    # Kök korunur ve izni değişmez.
    assert stat.S_IMODE(run_root.stat().st_mode) == DIRECTORY_MODE


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing-project"])
def test_a_rejected_environment_leaves_no_run_directory(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
    kind: str,
) -> None:
    """Config ve dondurulmuş kök reddi de kalıntı bırakmaz."""
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO yalnız POSIX'te")

    project = frozen_project
    if kind == "symlink":
        outside = tmp_path / "disarida.cfg"
        outside.write_text("[defaults]\nforks = 99\n", encoding="utf-8")
        os.symlink(outside, frozen_project / ANSIBLE_CONFIG_FILENAME)
    elif kind == "fifo":
        os.mkfifo(frozen_project / ANSIBLE_CONFIG_FILENAME)
    else:
        project = tmp_path / "yok-olan-workspace" / "project"

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, project, known_hosts)

    # Dış hata sözleşmesi **değişmedi**: çağıranın gördüğü sebep asıl reddin
    # kendisidir, temizliğin sonucu değil.
    assert error.value.details in (
        {"reason": "ansible_config_symlink"},
        {"reason": "ansible_config_not_regular"},
        {"reason": "frozen_project_unreadable"},
    )
    assert not (run_root / job_id).exists()
    assert list(run_root.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [
        RunnerEnvironmentError("temizlenemedi", details={"reason": "run_dir_not_removed"}),
        # Sözleşme dışı, sıradan bir hata. Yakalanmasaydı hazırlığın **asıl**
        # reddinin yerine geçer ve teşhis temizlik katmanını işaret ederdi.
        RuntimeError("temizlik sirasinda beklenmeyen hata"),
    ],
    ids=["contract", "unexpected"],
)
def test_a_failed_environment_cleanup_is_fail_closed_and_leaks_nothing(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """Temizlik de düşerse hazırlık başarılı sayılmaz ve hata sabit kalır.

    Kalıntıyı asıl reddin arkasında gizlemek, bir sonraki hazırlığı düşürecek
    alanı görünmez kılardı; bu yüzden sebep ``run_dir_not_cleaned``'e döner.
    Sonuç, temizliğin **hangi** hatayla düştüğünden bağımsızdır: sözleşme içi
    bir ret ile sıradan bir ``RuntimeError`` aynı dış cevabı üretir.
    """
    os.symlink(tmp_path / "gizli-hedef.cfg", frozen_project / ANSIBLE_CONFIG_FILENAME)

    def _refuse(*_args: object, **_kwargs: object) -> bool:
        raise failure

    monkeypatch.setattr(runner_env, "remove_execution_run_directory", _refuse)

    with pytest.raises(RunnerEnvironmentError) as error:
        _build(run_root, job_id, frozen_project, known_hosts)

    assert error.value.details == {"reason": "run_dir_not_cleaned"}
    rendered = f"{error.value} {error.value.details}"
    assert str(run_root) not in rendered
    assert str(tmp_path) not in rendered
    assert job_id not in rendered
    # Temizliğin kendi metni dışarı **sızmaz**; sebep sabit sözlükten gelir.
    assert str(failure) not in rendered
    # Asıl ret zincirde korunur: teşhis kaybolmaz, yalnız dış cevap sabitlenir.
    cause = error.value.__cause__
    assert isinstance(cause, RunnerEnvironmentError)
    assert cause.details == {"reason": "ansible_config_symlink"}
    # Kalıntı gerçekten duruyor: fail-closed sonuç doğruyu söylüyor.
    assert (run_root / job_id).is_dir()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit], ids=["sigint", "exit"])
def test_a_cleanup_interrupt_is_not_swallowed(
    run_root: Path,
    job_id: str,
    tmp_path: Path,
    frozen_project: Path,
    known_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    """Temizlik en iyi çabadır ama kesme sinyalini **yutmaz**.

    ``BaseException``'ı da bastıran bir "her şeyi yut" bloğu, süreci
    durdurulamaz hâle getirirdi. Sınır ``Exception``'dadır.
    """
    os.symlink(tmp_path / "gizli-hedef.cfg", frozen_project / ANSIBLE_CONFIG_FILENAME)

    def _interrupt(*_args: object, **_kwargs: object) -> bool:
        raise interrupt

    monkeypatch.setattr(runner_env, "remove_execution_run_directory", _interrupt)

    with pytest.raises(interrupt):
        _build(run_root, job_id, frozen_project, known_hosts)


def test_no_cleanup_helper_suppresses_a_base_exception() -> None:
    """Kod, kesme sinyallerini sıradan bir hata gibi yutan biçim taşımaz.

    Davranış testleri tek tek yolları ölçer; bu ölçüm, yarın eklenecek yeni bir
    yardımcının aynı hatayı sessizce tekrarlamasını engeller.
    """
    code, _ = _executable_code(RUNNER_ENV_SOURCE)

    assert "suppress(BaseException)" not in code
    assert "exceptBaseException:pass" not in code
    # `BaseException` tek bir dalda geçer ve o dal istisnayı yeniden yükseltir;
    # temizlik yardımcısı `Exception` sınırında durur.
    assert code.count("exceptBaseException:") == 1
    # Temizlik yardımcısı `Exception` sınırında durur: modüldeki tek geniş
    # yakalama odur, geri kalan her dal dar (`OSError`, `FileNotFoundError` ...).
    assert code.count("exceptException:") == 1


def test_an_interrupt_still_returns_the_workspace_and_stays_an_interrupt(
    run_root: Path,
    job_id: str,
    frozen_project: Path,
    known_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KeyboardInterrupt`` alan geri verilir ama başka bir hataya çevrilmez.

    İkinci bir kesmeyi yutmak ya da onu ``RunnerEnvironmentError`` gibi
    göstermek, süreci durdurulamaz veya teşhisi yanlış hâle getirirdi.
    """

    def _interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_env, "build_ssh_arguments", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        _build(run_root, job_id, frozen_project, known_hosts)

    assert list(run_root.iterdir()) == []


# --- Çalışma alanının kaldırılması (R1-V3C1C2B2A) ----------------------------
#
# Temizliğin riski hazırlığın riskiyle simetriktir: hazırlık "yanlış yere
# yazmak", temizlik "yanlış yeri silmek" biçiminde başarısız olur. Bu yüzden
# aşağıdaki testler her senaryoda yalnız beklenen ağacın gittiğini değil,
# **dokunulmaması gereken her şeyin birebir kaldığını** da ölçer.


def _fingerprint(base: Path) -> dict[str, tuple[int, str]]:
    """Ağacın biçim, izin ve içerik parmak izi.

    Symlink **izlenmez**: bağlantının kendisi hedefi çözülmeden kaydedilir,
    yoksa dış hedefin korunduğu iddiası bağlantının kendisiyle karışırdı.
    """
    found: dict[str, tuple[int, str]] = {}
    for path in sorted(base.rglob("*")):
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            payload = f"symlink:{os.readlink(path)}"
        elif stat.S_ISDIR(status.st_mode):
            payload = "dir"
        elif stat.S_ISREG(status.st_mode):
            payload = f"file:{path.read_bytes()!r}"
        else:
            payload = f"special:{stat.S_IFMT(status.st_mode)}"
        found[str(path.relative_to(base))] = (stat.S_IMODE(status.st_mode), payload)
    return found


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Kökün **dışında**, hiçbir senaryoda dokunulmaması gereken bir ağaç."""
    root = tmp_path / "disarida"
    (root / "alt").mkdir(parents=True)
    (root / "alt" / "kiymetli.txt").write_text("dokunulmadi", encoding="utf-8")
    (root / "kiymetli.txt").write_text("dokunulmadi", encoding="utf-8")
    (root / "alt").chmod(0o755)
    (root / "kiymetli.txt").chmod(0o644)
    return root


def test_cleanup_removes_a_real_runner_environment(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Gerçek :func:`build_runner_environment` çıktısı tamamen kaldırılır.

    Elle kurulmuş bir dizin değil, hazırlığın kendi ürettiği ağaç silinir:
    sözleşmenin iki ucu ancak birlikte ölçülünce anlamlıdır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    assert result.run_dir.is_dir()
    assert (result.run_dir / ANSIBLE_CONFIG_FILENAME).is_file()

    assert remove_execution_run_directory(run_root, job_id) is True

    assert not result.run_dir.exists()
    # Kökün kendisi korunur ve izni **değişmez**.
    assert run_root.is_dir()
    assert stat.S_IMODE(run_root.stat().st_mode) == DIRECTORY_MODE
    assert list(run_root.iterdir()) == []


def test_nested_files_and_directories_are_removed(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Çalışma alanının içine yazılan normal ağaç da gider."""
    result = _build(run_root, job_id, frozen_project, known_hosts)
    deep = result.run_dir / "raw" / "artifacts" / "1" / "job_events"
    deep.mkdir(parents=True)
    (deep / "1-abc.json").write_text('{"event": "playbook_on_start"}', encoding="utf-8")
    (deep / "2-def.json").write_text('{"event": "playbook_on_stats"}', encoding="utf-8")
    (result.run_dir / "home" / ".ansible").mkdir()
    (result.run_dir / "home" / ".ansible" / "cp").write_text("", encoding="utf-8")

    assert remove_execution_run_directory(run_root, job_id) is True

    assert list(run_root.iterdir()) == []


def test_an_inner_symlink_is_unlinked_and_its_target_survives(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path, outside: Path
) -> None:
    """İçerideki bağlantının **kendisi** silinir; gösterdiği dış hedef kalır.

    Bu, temizliğin çalışma alanının dışına taşmasının en kısa yoludur:
    ``rmtree`` benzeri bir yürüyüş, bağlantıyı dizin sanıp altını boşaltırdı.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    os.symlink(outside, result.run_dir / "dis-dizin")
    os.symlink(outside / "kiymetli.txt", result.run_dir / "home" / "dis-dosya")
    # Kırık bir bağlantı da aynı biçimde yalnız kendisi olarak kaldırılır.
    os.symlink(outside / "hic-olmayan", result.run_dir / "kirik")

    before = _fingerprint(outside)

    assert remove_execution_run_directory(run_root, job_id) is True

    assert list(run_root.iterdir()) == []
    assert _fingerprint(outside) == before
    assert (outside / "alt" / "kiymetli.txt").read_text(encoding="utf-8") == "dokunulmadi"


def test_inner_special_entries_are_removed_without_being_opened(
    run_root: Path,
    job_id: str,
    frozen_project: Path,
    known_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIFO ve socket girdileri açılmadan, dışarı taşmadan kaldırılır.

    FIFO'yu **açmak** okuyucu/yazıcı beklerken bloklardı; temizliğin bir
    girdiyle süresiz asılı kalmaması, açmamasıyla sağlanır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    os.mkfifo(result.run_dir / "boru", 0o600)
    os.mkfifo(result.run_dir / "tmp" / "ic-boru", 0o600)

    # AF_UNIX yolu kısa tutulur: bind, uzun mutlak yolları kabul etmez.
    monkeypatch.chdir(result.run_dir / "ssh-control")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind("soket")
    assert stat.S_ISSOCK((result.run_dir / "ssh-control" / "soket").lstat().st_mode)

    assert remove_execution_run_directory(run_root, job_id) is True

    assert list(run_root.iterdir()) == []


def test_another_job_directory_is_untouched(
    run_root: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Yalnız adı verilen Job'un ağacı gider; komşusu birebir kalır."""
    doomed = str(uuid.uuid4())
    keeper = str(uuid.uuid4())
    _build(run_root, doomed, frozen_project, known_hosts)
    kept = _build(run_root, keeper, frozen_project, known_hosts)
    (kept.run_dir / "home" / "iz.txt").write_text("kalmali", encoding="utf-8")

    before = _fingerprint(kept.run_dir)

    assert remove_execution_run_directory(run_root, doomed) is True

    assert [entry.name for entry in run_root.iterdir()] == [keeper]
    assert _fingerprint(kept.run_dir) == before


def test_everything_outside_the_root_survives_byte_for_byte(
    run_root: Path, job_id: str, tmp_path: Path, frozen_project: Path, known_hosts: Path
) -> None:
    """Kökün dışındaki hiçbir girdi, içerik veya izin değişmez."""
    sentinel = tmp_path / "sentinel"
    (sentinel / "alt").mkdir(parents=True)
    (sentinel / "alt" / "veri.bin").write_bytes(b"\x00sentinel\xff")
    (sentinel / "alt").chmod(0o701)
    (sentinel / "alt" / "veri.bin").chmod(0o640)
    _build(run_root, job_id, frozen_project, known_hosts)

    before = {
        str(path.relative_to(tmp_path)): (path.lstat().st_mode, path.is_dir())
        for path in sorted(tmp_path.rglob("*"))
        if not path.is_relative_to(run_root)
    }

    assert remove_execution_run_directory(run_root, job_id) is True

    after = {
        str(path.relative_to(tmp_path)): (path.lstat().st_mode, path.is_dir())
        for path in sorted(tmp_path.rglob("*"))
        if not path.is_relative_to(run_root)
    }
    assert after == before
    assert (sentinel / "alt" / "veri.bin").read_bytes() == b"\x00sentinel\xff"


def test_a_relative_root_is_rejected_by_cleanup() -> None:
    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(Path(EXECUTION_RUN_DIRNAME), str(uuid.uuid4()))

    assert error.value.details == {"reason": "execution_run_root_not_absolute"}


def test_a_root_with_an_unexpected_name_is_rejected_by_cleanup(tmp_path: Path) -> None:
    """Kök adı sabittir; serbest bir ad keyfi bir dizini silme hedefi yapardı."""
    impostor = tmp_path / "tmp"
    impostor.mkdir()
    impostor.chmod(0o700)
    job_id = str(uuid.uuid4())
    (impostor / job_id / "icerik").mkdir(parents=True)
    before = _fingerprint(impostor)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(impostor, job_id)

    assert error.value.details == {"reason": "execution_run_root_unexpected_name"}
    assert _fingerprint(impostor) == before


def test_a_symlinked_root_is_rejected_by_cleanup(tmp_path: Path) -> None:
    """Kökün yerine konmuş bağlantı izlenmez; hedefinin altı silinmez."""
    real = tmp_path / "gercek-kok"
    job_id = str(uuid.uuid4())
    (real / job_id / "icerik").mkdir(parents=True)
    real.chmod(0o700)
    link = tmp_path / EXECUTION_RUN_DIRNAME
    os.symlink(real, link)
    before = _fingerprint(real)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(link, job_id)

    assert error.value.details == {"reason": "execution_run_root_unavailable"}
    assert _fingerprint(real) == before


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777, 0o750])
def test_a_root_with_the_wrong_permission_is_rejected_and_not_chmodded(
    tmp_path: Path, mode: int
) -> None:
    """İzin **düzeltilmez**: yanlış kurulmuş bir kök sessizce kabul edilmez."""
    root = tmp_path / EXECUTION_RUN_DIRNAME
    root.mkdir()
    job_id = str(uuid.uuid4())
    (root / job_id).mkdir()
    root.chmod(mode)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(root, job_id)

    assert error.value.details == {"reason": "execution_run_root_not_private"}
    assert stat.S_IMODE(root.stat().st_mode) == mode
    assert (root / job_id).is_dir()


@pytest.mark.parametrize(
    "job_name",
    [
        "not-a-uuid",
        "../escape",
        "",
        ".",
        "..",
        "c232ab00-9414-11ec-b3c8-9e6bdeced846",
        "6F9619FF-8B86-D011-B42D-00CF4FC964FF",
        "5f2a1b3c-4d5e-4f6a-8b9c-0d1e2f3a4b5c/..",
    ],
)
def test_a_non_canonical_job_id_is_rejected_by_cleanup(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path, job_name: str
) -> None:
    """Silme hedefi yalnız canonical UUID4 olabilir.

    ``.`` ve ``..`` bilhassa ölçülür: serbest bir ad, kökün **kendisini** veya
    üstündeki bir dizini hedef hâline getirirdi.
    """
    _build(run_root, job_id, frozen_project, known_hosts)
    before = _fingerprint(run_root)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_name)

    assert error.value.details == {"reason": "job_id_not_canonical"}
    assert run_root.is_dir()
    assert _fingerprint(run_root) == before


@pytest.mark.parametrize("kind", ["symlink", "file", "fifo"])
def test_a_non_directory_job_entry_fails_closed(run_root: Path, outside: Path, kind: str) -> None:
    """Job adında dizin olmayan bir girdi varsa hiçbir şey silinmez.

    Girdinin **kendisi de** kaldırılmaz: beklenen nesnenin yerinde başka bir
    şeyin durması, temizliğin sessizce üstesinden geleceği bir durum değildir.
    """
    job_id = str(uuid.uuid4())
    entry = run_root / job_id
    if kind == "symlink":
        os.symlink(outside, entry)
    elif kind == "file":
        entry.write_text("veri", encoding="utf-8")
    else:
        os.mkfifo(entry, 0o600)
    before = _fingerprint(outside)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id)

    assert error.value.details == {"reason": "run_dir_not_a_directory"}
    assert entry.exists() or entry.is_symlink()
    # Bağlantının hedefi ne izlendi ne de boşaltıldı.
    assert _fingerprint(outside) == before


def test_a_missing_job_directory_is_a_safe_no_op(run_root: Path, job_id: str) -> None:
    """``missing_ok=True`` altında yokluk hata değildir; ``False`` döner."""
    assert remove_execution_run_directory(run_root, job_id) is False

    assert run_root.is_dir()
    assert stat.S_IMODE(run_root.stat().st_mode) == DIRECTORY_MODE
    assert list(run_root.iterdir()) == []


def test_a_missing_job_directory_is_an_explicit_error_when_not_allowed(
    run_root: Path, job_id: str
) -> None:
    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id, missing_ok=False)

    assert error.value.details == {"reason": "run_dir_missing"}
    assert list(run_root.iterdir()) == []


def test_cleanup_is_idempotent(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """İkinci çağrı güvenli no-op'tur; kök yine korunur."""
    _build(run_root, job_id, frozen_project, known_hosts)

    assert remove_execution_run_directory(run_root, job_id) is True
    assert remove_execution_run_directory(run_root, job_id) is False

    assert run_root.is_dir()
    assert stat.S_IMODE(run_root.stat().st_mode) == DIRECTORY_MODE


def test_a_tree_at_the_depth_limit_is_still_removed(run_root: Path, job_id: str) -> None:
    """Sınırın **tam üstünde** duran ağaç hâlâ kaldırılır (sınır kapsayıcıdır)."""
    job_dir = run_root / job_id
    job_dir.mkdir(mode=DIRECTORY_MODE)
    job_dir.joinpath(*["d"] * MAX_CLEANUP_DEPTH).mkdir(parents=True)

    assert remove_execution_run_directory(run_root, job_id) is True

    assert list(run_root.iterdir()) == []


def test_a_deeper_tree_is_rejected_and_nothing_is_deleted(run_root: Path, job_id: str) -> None:
    """Derinlik sınırı aşılırsa tarama düşer ve ağaç **olduğu gibi** kalır."""
    job_dir = run_root / job_id
    job_dir.mkdir(mode=DIRECTORY_MODE)
    job_dir.joinpath(*["d"] * (MAX_CLEANUP_DEPTH + 1)).mkdir(parents=True)
    (job_dir / "iz.txt").write_text("kalmali", encoding="utf-8")
    before = _fingerprint(job_dir)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id)

    assert error.value.details == {"reason": "run_dir_too_deep"}
    assert _fingerprint(job_dir) == before


def test_the_entry_count_is_bounded_and_nothing_is_deleted(
    run_root: Path,
    job_id: str,
    frozen_project: Path,
    known_hosts: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Girdi sınırı aşılırsa hiçbir şey silinmez.

    Sınır **taramada** uygulandığı için ihlal, silme başlamadan görülür; tek
    geçişli bir temizlik burada yarısı silinmiş bir ağaç bırakırdı.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    total = len(_fingerprint(result.run_dir))
    monkeypatch.setattr(runner_env, "MAX_CLEANUP_ENTRIES", total - 1)
    before = _fingerprint(result.run_dir)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id)

    assert error.value.details == {"reason": "run_dir_too_many_entries"}
    assert _fingerprint(result.run_dir) == before

    # Sınır tam yettiğinde aynı ağaç sorunsuz kaldırılır.
    monkeypatch.setattr(runner_env, "MAX_CLEANUP_ENTRIES", total)
    assert remove_execution_run_directory(run_root, job_id) is True
    assert list(run_root.iterdir()) == []


def test_the_cleanup_error_leaks_no_paths(run_root: Path, outside: Path) -> None:
    """Hata yalnız sabit bir sebep kodu taşır; path ve içerik yazılmaz."""
    job_id = str(uuid.uuid4())
    os.symlink(outside, run_root / job_id)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id)

    rendered = f"{error.value} {error.value.details}"
    assert str(run_root) not in rendered
    assert str(outside) not in rendered
    assert job_id not in rendered
    assert set(error.value.details or {}) == {"reason"}


# --- Kökün sınırlı listelenmesi ve nesne kimliği (R1-V3C2B, AUDIT-FIX1) ------
#
# Listeleme ile silme arasında iki ayrı pencere vardır ve ikisi de gerçektir:
# kök beklenmedik büyüklükte olabilir (sınırsız bir yürüyüş) ve bir adın altında
# duran nesne **değişmiş** olabilir (yanlış hedefin silinmesi). Aşağıdaki
# testler ikisini de kaynak metnine bakmadan, davranışla ölçer.


def _identity(path: Path) -> RunDirectoryIdentity:
    """Bir girdinin nesne kimliği; symlink izlenmez."""
    return runner_env._identity_of(path.lstat())


def _guarded_scandir(monkeypatch: pytest.MonkeyPatch, *, limit: int) -> tuple[list[str], list[str]]:
    """``os.scandir`` iterator'ını sayan ve sınırı **testin kendisinde** dayatan sarmalayıcı.

    Sınır burada da uygulanır: primitive sınırın ötesinde bir girdi tüketirse
    test o anda düşer. Sonradan "kaç girdi okunmuştu" diye bakmak, sınırsız bir
    yürüyüşün önce yapılıp sonra ölçülmesi olurdu.

    Returns:
        ``(consumed, closed)`` — tüketilen adlar ve kapanış olayları.
    """
    consumed: list[str] = []
    closed: list[str] = []
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            self.close()

        def __iter__(self) -> Any:
            return self

        def __next__(self) -> Any:
            entry = self._inner.__next__()
            consumed.append(entry.name)
            assert len(consumed) <= limit + 1, "sınırın ötesinde girdi tüketildi"
            return entry

        def close(self) -> None:
            closed.append("closed")
            self._inner.close()

    def guarded(target: Any) -> Any:
        # Yalnız descriptor-relative çağrı sarmalanır; testin kendi `pathlib`
        # kullanımı sayıma karışmamalıdır.
        if isinstance(target, int):
            return _Counting(real_scandir(target))
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", guarded)
    return consumed, closed


def test_the_root_enumeration_stops_one_entry_past_the_limit(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sınır tüketimin kendisindedir: kök hiçbir zaman tümüyle belleğe alınmaz.

    Önce listeleyip sonra uzunluğa bakmak "sınırlı" sayılmaz: milyonlarca
    girdili bir kök, sınır kontrolü hiç çalışmadan materialize edilirdi. Sınır
    aşıldığında ayrıca tek bir ``stat`` bile yapılmamış olmalıdır — sınıflandırma
    ancak sınır geçildikten sonra başlar.
    """
    for _ in range(6):
        (run_root / str(uuid.uuid4())).mkdir(mode=DIRECTORY_MODE)
    limit = 2
    monkeypatch.setattr(runner_env, "MAX_RUN_ROOT_ENTRIES", limit)
    consumed, closed = _guarded_scandir(monkeypatch, limit=limit)

    stats: list[str] = []
    real_stat = os.stat

    def counting_stat(*args: Any, **kwargs: Any) -> Any:
        if "dir_fd" in kwargs:
            stats.append(str(args[0]))
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)

    with pytest.raises(RunnerEnvironmentError) as error:
        list_execution_run_directories(run_root)

    assert error.value.details == {"reason": "execution_run_root_too_many_entries"}
    # Fazladan okunan tek ad, sınırın aşıldığının **kanıtıdır**; daha fazlası
    # okunmamıştır.
    assert len(consumed) == limit + 1
    assert stats == []
    assert closed == ["closed"], "iterator hata yolunda da kapatıldı"
    assert len(list(run_root.iterdir())) == 6


def test_a_root_exactly_at_the_limit_is_listed_and_the_iterator_is_closed(
    run_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sınır kapsayıcıdır ve başarı yolunda da iterator kapatılır."""
    names = sorted(str(uuid.uuid4()) for _ in range(3))
    for name in names:
        (run_root / name).mkdir(mode=DIRECTORY_MODE)
    monkeypatch.setattr(runner_env, "MAX_RUN_ROOT_ENTRIES", len(names))
    consumed, closed = _guarded_scandir(monkeypatch, limit=len(names))

    listing = list_execution_run_directories(run_root)

    assert [entry.job_id for entry in listing.candidates] == names
    assert sorted(consumed) == names
    assert closed == ["closed"]


def _calls_in(function: Any) -> set[str]:
    """Bir fonksiyonun gövdesinde **çağrılan** adlar.

    Ölçüm ham metinde arama yapmaz: docstring'in kendisi yasak yapıların adını
    zaten *anlatıyor* ve metin araması onları bulurdu. AST yalnız gerçekten
    çağrılanı görür.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    return called


def test_the_root_enumeration_never_materialises_the_whole_directory() -> None:
    """Listeleme yolu ``os.listdir`` veya ``list(os.scandir(...))`` kullanmaz.

    Sözleşmenin bu yarısı davranışla ölçülemez: kökü tümüyle listeleyen bir
    uygulama da küçük bir test kökünde doğru sonucu üretirdi. İddia yalnız
    listeleme yolunu kapsar — modülün geri kalanındaki ağaç yürüyüşü kendi
    derinlik/girdi sınırlarına tabidir ve bu iddianın konusu değildir.
    """
    calls = _calls_in(runner_env.list_execution_run_directories) | _calls_in(
        runner_env._bounded_entry_names
    )

    assert "scandir" in calls
    for forbidden in ("listdir", "list", "glob", "rglob", "walk", "iterdir", "rmtree"):
        assert forbidden not in calls, forbidden


def test_the_listing_binds_each_candidate_to_the_object_it_saw(run_root: Path) -> None:
    """Aday, adının yanında listelenen **nesnenin** kimliğini de taşır."""
    job_id = str(uuid.uuid4())
    (run_root / job_id).mkdir(mode=DIRECTORY_MODE)

    listing = list_execution_run_directories(run_root)

    assert [entry.job_id for entry in listing.candidates] == [job_id]
    assert listing.candidates[0].identity == _identity(run_root / job_id)


def test_a_replacement_directory_with_the_same_name_is_not_removed(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Aynı adla oluşturulmuş **yeni** bir dizin, eski kimlikle silinemez.

    Ad bir kimlik değildir. Eski alan kaldırılıp aynı canonical adla yeni bir
    çalışma alanı açılmışsa, o alan bir sonraki denemenin kendisi olabilir;
    listeleme sırasında görülen nesnenin kararı onun için verilmemiştir.
    """
    _build(run_root, job_id, frozen_project, known_hosts)
    listed = _identity(run_root / job_id)
    assert remove_execution_run_directory(run_root, job_id, expected_identity=listed) is True

    # Aynı ad, **yeni** ve gerçek bir dizin.
    replacement = run_root / job_id
    replacement.mkdir(mode=DIRECTORY_MODE)
    (replacement / "yeni-deneme.txt").write_text("dokunulmadi", encoding="utf-8")
    before = _fingerprint(run_root)
    assert _identity(replacement) != listed

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id, expected_identity=listed)

    assert error.value.details == {"reason": "run_dir_identity_changed"}
    assert _fingerprint(run_root) == before
    assert (replacement / "yeni-deneme.txt").read_text(encoding="utf-8") == "dokunulmadi"


def test_a_wrong_expected_identity_deletes_nothing_at_all(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Uyuşmayan kimlikte ağaç **kısmen bile** silinmez ve hata sızdırmaz.

    Kontrol dizin açılmadan önce durur: uyuşmayan nesne ne taranır, ne
    boşaltılır, ne de tek bir girdisi kaldırılır.
    """
    result = _build(run_root, job_id, frozen_project, known_hosts)
    (result.run_dir / "home" / "iz.txt").write_text("kalmali", encoding="utf-8")
    before = _fingerprint(run_root)
    listed = _identity(result.run_dir)
    wrong = dataclasses.replace(listed, inode=listed.inode + 1)

    with pytest.raises(RunnerEnvironmentError) as error:
        remove_execution_run_directory(run_root, job_id, expected_identity=wrong)

    assert error.value.details == {"reason": "run_dir_identity_changed"}
    assert _fingerprint(run_root) == before

    rendered = f"{error.value} {error.value.details}"
    assert str(run_root) not in rendered
    assert job_id not in rendered
    assert "kalmali" not in rendered


def test_the_matching_identity_still_removes_the_tree(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """Kimlik doğruysa temizlik sözleşmesi aynen işler."""
    result = _build(run_root, job_id, frozen_project, known_hosts)

    assert (
        remove_execution_run_directory(
            run_root, job_id, expected_identity=_identity(result.run_dir)
        )
        is True
    )

    assert list(run_root.iterdir()) == []


def test_the_identity_check_is_optional_and_off_by_default(
    run_root: Path, job_id: str, frozen_project: Path, known_hosts: Path
) -> None:
    """``expected_identity`` verilmezse davranış **değişmez**.

    Hedefini aynı çağrıda kendisi oluşturan executor yolu bir kimlik taşımaz ve
    taşımak zorunda da değildir; genişletme geriye dönük uyumludur.
    """
    _build(run_root, job_id, frozen_project, known_hosts)

    assert remove_execution_run_directory(run_root, job_id) is True

    assert list(run_root.iterdir()) == []


# --- Kapsam kilidi -----------------------------------------------------------


def test_the_cleanup_walks_no_free_paths() -> None:
    """Temizlik ``rmtree``, glob veya serbest path yürüyüşü kullanmaz.

    Bu yapıların hepsi çözülmüş path metni üzerinden çalışır; aradaki bir
    bağlantı veya değiş-tokuş, silmeyi çalışma alanının dışına taşıyabilirdi.
    Sözleşmenin bu yarısı davranışla ölçülemez — yalnız kodun kendisinden
    okunur.
    """
    code, names = _executable_code(RUNNER_ENV_SOURCE)

    forbidden_names = {
        "shutil",
        "rmtree",
        "copytree",
        "walk",
        "glob",
        "iglob",
        "rglob",
        "iterdir",
        "removedirs",
        "rmtree_safe",
    }
    assert not (forbidden_names & names), forbidden_names & names
    # Ağaç işlemleri descriptor-relative'dir ve symlink hiçbir yerde izlenmez.
    assert "parents=True" not in code
    assert "follow_symlinks=True" not in code
    assert code.count("follow_symlinks=False") == code.count("os.stat(")
    assert "os.O_NOFOLLOW" in code


def test_the_module_starts_no_process_and_touches_no_database() -> None:
    """R1-V3C1A yalnız temeli kurar: süreç, runner ve veritabanı yoktur.

    Kapsam sınırı kodun kendisinden okunabilmelidir; "eklemedik" demek, bir
    sonraki turda sessizce eklenmesini engellemez.
    """
    code, names = _executable_code(RUNNER_ENV_SOURCE)

    forbidden_names = {
        "subprocess",
        "ansible_runner",
        "Popen",
        "fork",
        "forkpty",
        "execv",
        "execvp",
        "execve",
        "spawnv",
        "posix_spawn",
        "system",
        "popen",
        "Session",
        "sessionmaker",
        "create_engine",
        "select",
        "text",
    }
    assert not (forbidden_names & names), forbidden_names & names
    assert "run_bounded_process" not in names
    for forbidden in ("os.fork", "os.exec", "os.spawn", "os.system", "os.popen"):
        assert forbidden not in code, forbidden


def test_the_slice_adds_no_runner_subprocess_or_route_surface() -> None:
    """R1-V3C1A kapsam kilidi: bu dilim yalnız **temeli** kurar.

    Üç iddia birlikte ölçülür, çünkü üçü de "sessizce eklendi" biçiminde
    kaybolabilecek sınırlardır:

    1. `ansible_runner` uygulama kodunda **hiç import edilmez**. Ayar adı olarak
       geçen `ansible_runner_command` bir string'dir, bir import değildir.
    2. `subprocess`i import eden tek modül, T-202'den beri var olan ortak
       sınırlı çalıştırma katmanıdır. Execution paketi alt süreç açmaz.
    3. **Bu dilim** HTTP yüzeyine hiçbir şey eklemez: router dosyalarının kümesi
       aynıdır ve toplam route sayısı yalnız sonraki dilimlerin açıkça eklediği
       kadar büyümüştür (aşağıdaki tarihçe). Sayının kendisi bir sözleşmedir;
       sessizce eklenen bir yol testi düşürür.
    """
    app_root = Path("app")
    modules = sorted(app_root.rglob("*.py"))
    assert modules, "uygulama kaynakları bulunamadı"

    runner_importers: list[str] = []
    subprocess_importers: list[str] = []
    for module in modules:
        _, names = _executable_code(module)
        if "ansible_runner" in names:
            runner_importers.append(str(module))
        if "subprocess" in names:
            subprocess_importers.append(str(module))

    assert runner_importers == []
    # Tek istisna bilinçlidir ve bu dilimde değişmemiştir.
    assert subprocess_importers == ["app/services/ansible/process.py"]

    # Execution paketinin tamamı süreçten uzak durur.
    for module in sorted(Path("app/services/execution").rglob("*.py")):
        _, names = _executable_code(module)
        assert "subprocess" not in names, str(module)
        assert "ansible_runner" not in names, str(module)

    # Router dosyaları yalnız sonraki dilimlerin eklediği kadar büyür.
    routes = sorted(Path("app/api/routes").glob("*.py"))
    assert {route.name for route in routes} == {
        "__init__.py",
        "controller_paths.py",
        "executions.py",
        "health.py",
        "inventories.py",
        "jobs.py",
        "projects.py",
    }
    # R1-V3D1 tek bir endpoint ekledi (`POST .../executions`); toplam 15 → 16.
    # R1-V3D2B üç Job okuma GET'i ekledi; toplam 16 → 19.
    # R1-V3J0C tek bir salt-okunur endpoint ekledi (`GET .../controller-paths`);
    # toplam 19 → 20.
    # R1-V3J1 kalıcı ping geçmişi için tek bir salt-okunur endpoint ekledi
    # (`GET /api/inventories/{inventory_id}/ping-runs`); toplam 20 → 21.
    # R1-V3J2 yalnız frontend cursor pagination'dı ve R1-V3J3A yalnız mevcut
    # sonuç cevabını genişletti; ikisi de route eklemedi.
    decorators = sum(module.read_text(encoding="utf-8").count("@router.") for module in routes)
    assert decorators == 21
