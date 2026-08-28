"""Test yardımcıları."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from app.core.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
STUB_PARSER = TESTS_DIR / "inventory_parser_stub.py"
STUB_PING = TESTS_DIR / "ping_stub.py"


def stub_parser_command(behaviour: str, **options: object) -> list[str]:
    """Sahte inventory parser'ı çağıran bir **argüman listesi** üretir.

    Liste gerçek bir süreç başlatır: subprocess katmanı (timeout, çıktı boyutu,
    environment daraltması, argüman aktarımı) taklit edilmez, yalnızca Ansible'ın
    INI/YAML çözümlemesi yerine denetlenebilir bir çıktı konur.
    """
    command = [sys.executable, str(STUB_PARSER), "--behaviour", behaviour]
    for name, value in options.items():
        command.extend([f"--{name.replace('_', '-')}", str(value)])
    return command


def stub_ping_command(behaviour: str, **options: object) -> list[str]:
    """Sahte `ansible` ad-hoc komutunu çağıran bir **argüman listesi** üretir.

    Gerçek bir süreç başlatılır: argüman aktarımı, environment daraltması,
    timeout ve çıktı sınırı taklit edilmez; yalnız SSH bağlantısı taklit edilir.
    """
    command = [sys.executable, str(STUB_PING), "--behaviour", behaviour]
    for name, value in options.items():
        command.extend([f"--{name.replace('_', '-')}", str(value)])
    return command


def real_parser_available() -> bool:
    """Gerçek ``ansible-inventory`` bu platformda çalıştırılabiliyor mu.

    Ansible, Windows'u control node olarak desteklemez; paket kurulu olsa bile
    CLI POSIX'e özgü modüllere bağlı olduğu için başlatılamaz. Bu yüzden
    "kurulu mu" değil "çalışıyor mu" sorulur.
    """
    try:
        completed = subprocess.run(
            ["ansible-inventory", "--version"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def link_directory(link: Path, target: Path) -> Path:
    """``link`` konumunda ``target`` dizinine giden bir bağlantı oluşturur.

    Symlink escape testleri için gereklidir. Windows'ta ``os.symlink``
    yönetici yetkisi veya Developer Mode ister; yetki yoksa aynı çözümleme
    davranışına sahip bir directory junction'a düşülür. Hiçbiri mümkün
    değilse test atlanır — sessizce "geçti" sayılmaz.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return link
    except (OSError, NotImplementedError) as symlink_error:
        if sys.platform != "win32":
            pytest.skip(f"Symlink oluşturulamadı: {symlink_error}")

    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as junction_error:
        pytest.skip(f"Symlink ve junction oluşturulamadı: {junction_error}")
    return link


def alembic_config(database_url: str) -> Config:
    """Verilen DSN'e yönlendirilmiş bir Alembic yapılandırması üretir.

    ``sqlalchemy.url`` önceden set edildiği için ``alembic/env.py`` uygulamanın
    gerçek ayarlarına ve ``app-data`` dizinine dokunmaz.
    """
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def make_settings(**overrides: Any) -> Settings:
    """İzole test ayarları üretir.

    ``_env_file=None`` ile geliştiricinin yerel ``backend/.env`` dosyası
    devre dışı bırakılır; böylece testler makineye bağlı hâle gelmez.
    Bu parametre pydantic-settings'in özel init argümanıdır ve model
    alanı olmadığı için tip denetiminden muaf tutulur.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
