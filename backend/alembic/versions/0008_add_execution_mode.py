"""add execution mode to plans and jobs

Revision ID: 0008_add_execution_mode
Revises: 0007_add_playbook_runner_foundation
Create Date: 2026-08-19

R1-V3H1A. Bu migration **hiçbir şey çalıştırmaz** ve hiçbir akışı
değiştirmez; yalnızca execution mode'u kalıcı ve tiplendirilmiş bir sütun
hâline getirir.

Mode'un tek karşılığı `ansible-runner` argv'sidir: ``check``, argv'ye
``--cmdline=--check`` **eklenen** mode'dur; ``normal``, eklenmeyen mode'dur.
``check`` bir yan etkisizlik garantisi **değildir** ve hedefte değişiklik
olmayacağını söylemez.

İki tabloya aynı sütun eklenir:

- ``execution_plans.mode`` — planın *onayladığı* mode.
- ``jobs.mode`` — Job'un *taşıdığı* mode.

İkisi de ``NOT NULL``'dır, yalnız ``check`` veya ``normal`` değerini alabilir
ve hem ORM hem sunucu tarafı varsayılanı ``check``'tir. Sütun tipi tek bir
yerden gelir (``app.models.execution_mode.ExecutionMode``); iki ayrı string
union tanımlansaydı biri genişletildiğinde diğeri sessizce eski kalırdı.

**Backfill neden ``check``?** Çünkü bugüne kadarki production yolunun
**tamamı** ``--check`` ile çalışıyordu: ``PLAN_MODE`` sabiti ``"check"``,
request/response şemaları ``Literal["check"]`` ve runner argv'si koşulsuz
``--cmdline=--check`` taşır. Mevcut satırların hepsi bu argv ile üretilmiştir;
onları ``normal`` işaretlemek, hiç kurulmamış bir argv'yi tarihe yazmak
olurdu. Gerekçe budur — bu satırların hedeflerinde kesinlikle değişiklik
olmadığı iddiası **değildir**. Sunucu varsayılanının da ``check`` olması aynı
yöne bakar: mode'u belirtmeyen bir yazım — eski bir istemci, elle atılmış bir
INSERT — ``normal``'a sessizce yükselmez.

**PING + normal veritabanı tarafından reddedilir.** Mode'un karşılığı yalnız
`ansible-runner` playbook argv'sindedir; ping o yoldan geçmez ve sabit bir
ad-hoc argv (``ansible all -i <snapshot> -m ping ...``) kurar — orada
``--check`` diye bir bayrak hiç söz konusu değildir. ``mode = 'normal'``
taşıyan bir ping satırı bu yüzden hiçbir argv farkına karşılık gelmez ve
mode'a bakan her sorgu tarafından yanlış sınıflandırılırdı. PLAYBOOK
satırları her iki mode'u da taşıyabilir.

**Plan ↔ Job mode eşitliği burada kurulmaz.** İki tabloyu birbirine bağlayan
bir trigger, SQLite ve PostgreSQL'de iki ayrı dilde yazılmayı ve migration
içinde bakım edilmeyi gerektirirdi. Eşitlik H1B'de, Job satırı plan
kaydından üretilirken mode'un **kopyalanmasıyla** sağlanacaktır.

**Hiçbir legacy satır expire edilmez, silinmez, durumu/token'ı/claim'i
değiştirilmez.** Bu migration'ın tek veri etkisi yeni sütunu ``check`` ile
doldurmaktır.

Değerler burada **string sabiti olarak** yazılır: migration geçmişi
dondurulmuş bir kayıttır ve uygulama enum'u ileride genişlerse bu
migration'ın ürettiği şema ve veri değişmemelidir (0006 ve 0007 ile aynı
gerekçe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_add_execution_mode"
down_revision: str | None = "0007_add_playbook_runner_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kipi taşıyan tablolar.
TABLES = ("execution_plans", "jobs")

# Bugüne kadarki production yolunun tamamının kullandığı mode; hem backfill
# hem sunucu varsayılanı.
DEFAULT_MODE = "check"

# `native_enum=False` olduğu için sütun her iki dialect'te de VARCHAR(16)
# olarak iner ve PostgreSQL'de ayrıca yönetilmesi gereken bir tip yaratılmaz
# (`job_type`/`job_status` ile aynı desen). İzin verilen değer kümesi,
# aşağıda açıkça adlandırılmış CHECK ile kurulur; böylece kısıt adı
# `ck_%(table_name)s_%(constraint_name)s` kuralı üzerinden model tarafıyla
# birebir aynı olur.
MODE_TYPE = sa.Enum(
    "check",
    "normal",
    name="execution_mode",
    native_enum=False,
    length=16,
    create_constraint=False,
)

# Model tarafında `Enum(..., create_constraint=True)`'ın ürettiği ifadenin
# birebir aynısı.
_MODE_VALUES = "mode IN ('check', 'normal')"

# Ping argv'sinde `--check` diye bir bayrak yoktur; `normal` bir ping satırı
# hiçbir argv farkına karşılık gelmez.
_PING_IS_CHECK_ONLY = "job_type <> 'ping' OR mode = 'check'"


def upgrade() -> None:
    # --- 1. Sütunlar ------------------------------------------------------
    #
    # Önce nullable eklenir, mevcut satırlar doldurulur ve ancak sonra NOT
    # NULL'a çekilir (0006 `requested_by`, 0007 `result_truncated` ile aynı
    # desen): tek adımda NOT NULL eklemek dolu bir tabloda başarısız olurdu.
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column(
                    "mode",
                    MODE_TYPE,
                    nullable=True,
                    server_default=DEFAULT_MODE,
                )
            )

    # --- 2. Backfill ------------------------------------------------------
    #
    # Server default zaten eski satırları doldurur; UPDATE bunu dialect
    # davranışına bırakmamak için açıkça tekrarlar. Yalnız yeni sütuna
    # dokunur: status, token_hash, claimed_at, expires_at, actor, workspace
    # ve artifact alanları olduğu gibi kalır.
    for table in TABLES:
        op.execute(
            sa.text(f"UPDATE {table} SET mode = :mode WHERE mode IS NULL").bindparams(
                mode=DEFAULT_MODE
            )
        )

    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "mode",
                existing_type=MODE_TYPE,
                existing_server_default=sa.text(f"'{DEFAULT_MODE}'"),
                nullable=False,
            )

    # --- 3. Invariantlar --------------------------------------------------
    #
    # İsimler kısa verilir: `ck_%(table_name)s_%(constraint_name)s` kuralı
    # tablo ön ekini kendisi ekler ve model tarafıyla aynı adı üretir.
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.create_check_constraint("execution_mode", _MODE_VALUES)

    with op.batch_alter_table("jobs") as batch:
        batch.create_check_constraint("ping_is_check_only", _PING_IS_CHECK_ONLY)


def downgrade() -> None:
    # CHECK'ler sütundan **önce** düşürülür: kısıt yerinde dururken sütunu
    # kaldırmak, batch mode'un yeniden kurduğu tabloda artık var olmayan bir
    # sütuna bakan bir ifade bırakırdı.
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_ping_is_check_only"), type_="check")

    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(op.f(f"ck_{table}_execution_mode"), type_="check")

    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("mode")
