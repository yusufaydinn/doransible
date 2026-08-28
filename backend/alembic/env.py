"""Alembic migration ortamı.

DSN ve app-data dizini uygulamanın kendi ayarlarından okunur; böylece
migration ile runtime aynı veritabanını kullanır.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Modellerin Base.metadata'ya kaydolması için import edilir (autogenerate).
import app.models  # noqa: F401
from app.core.config import ensure_app_data_dirs, get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Migration için kullanılacak DSN'i belirler.

    Çağıran taraf (örneğin testler) ``sqlalchemy.url`` değerini önceden
    ayarlamışsa ona dokunulmaz. Aksi hâlde uygulamanın kendi ayarları
    kullanılır ve app-data dizinleri hazırlanır.
    """
    injected = config.get_main_option("sqlalchemy.url", None)
    if injected:
        return injected

    settings = get_settings()
    ensure_app_data_dirs(settings)
    return settings.resolve_database_url()


config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Bağlantı açmadan SQL üretir."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Engine üzerinden migration çalıştırır."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite ALTER TABLE sınırlıdır; batch mode PostgreSQL'de de güvenlidir.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
