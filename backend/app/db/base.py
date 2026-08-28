"""Ortak SQLAlchemy declarative base."""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Constraint'lere deterministik isim verir. SQLite'ta ALTER TABLE sınırlı
# olduğu için Alembic batch mode isimlendirilmiş constraint'lere ihtiyaç
# duyar; PostgreSQL'e geçişte de isimler aynı kalır (ADR-004).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Bütün ORM modellerinin türediği base sınıf.

    Alembic autogenerate bu sınıfın ``metadata`` alanını hedef alır; yeni bir
    model eklendiğinde ``app.models`` içinden import edilmesi yeterlidir.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
