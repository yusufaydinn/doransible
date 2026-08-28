"""bind execution plans to an actor and to a job

Revision ID: 0006_bind_execution_plans_to_actor_and_job
Revises: 0005_create_execution_plans_table
Create Date: 2026-08-16

İki bağ kurulur:

1. ``execution_plans.requested_by`` — planı hazırlayan aktör. Claim koşulunun
   parçası olacağı için ``NOT NULL``'dır.
2. ``jobs.execution_plan_id`` — Job'u yetkilendiren claim edilmiş plan. Nullable
   (ping işlerinde ``NULL``), unique (bir plandan tek Job) ve ``RESTRICT``.

Bağ iki CHECK ile veritabanında **uygulanır**: etkin (pending/running) bir
PLAYBOOK Job'ı planını, project'ini ve playbook'unu taşımak zorundadır ve
``limit`` taşıyamaz; bir ping Job'ı ise plana bağlanamaz. Kural yalnız servis
katmanında dursaydı, doğrudan yazılan bir satır onaysız ama çalıştırılabilir
görünen bir iş üretirdi.

**Eski satırların ele alınışı.** R1-V2'de hazırlanmış planların aktör bağı
yoktur. Bu satırlara gerçek bir aktör *uydurmak*, doğrulanmamış bir kimliğe
doğrulanmış görünüm vermek olurdu; boş bırakmak ise sütunu ``NULL``'a açardı.
Bu yüzden iki şey birlikte yapılır:

- ``requested_by`` alanına, hiçbir ``local_actor`` değerinin alamayacağı bir
  internal sentinel yazılır (``app.core.config.LEGACY_PLAN_ACTOR``; ön eki
  yapılandırma doğrulayıcısı tarafından reddedilir).
- ``prepared`` ve ``claimed`` legacy satırlar ``expired`` yapılır.

Böylece eski bir token ne aktör eşleşmesinden ne de durum koşulundan geçebilir;
workspace'leri de açılıştaki mevcut expired temizliği tarafından toplanır.

Sentinel değeri burada **string sabiti olarak** yazılır. Migration geçmişi
dondurulmuş bir kayıttır: uygulama sabiti ileride değişirse bu migration'ın
ürettiği veri değişmemelidir.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_bind_execution_plans_to_actor_and_job"
down_revision: str | None = "0005_create_execution_plans_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `app.core.config.LEGACY_PLAN_ACTOR` ile aynı değer; bilinçli olarak import
# edilmez (yukarıdaki not).
LEGACY_PLAN_ACTOR = "__legacy_unattributed_plan__"


def upgrade() -> None:
    # --- execution_plans.requested_by -------------------------------------
    #
    # Önce nullable eklenir, mevcut satırlar doldurulur, sonra NOT NULL'a
    # çekilir: tek adımda NOT NULL eklemek dolu bir tabloda başarısız olurdu.
    with op.batch_alter_table("execution_plans") as batch:
        batch.add_column(sa.Column("requested_by", sa.String(100), nullable=True))

    op.execute(
        sa.text("UPDATE execution_plans SET requested_by = :actor WHERE requested_by IS NULL")
        .bindparams(actor=LEGACY_PLAN_ACTOR)
        .execution_options(autocommit=False)
    )
    # Aktör bağı olmayan hiçbir plan claim edilebilir kalmaz.
    op.execute(
        sa.text(
            "UPDATE execution_plans SET status = 'expired' "
            "WHERE requested_by = :actor AND status IN ('prepared', 'claimed')"
        ).bindparams(actor=LEGACY_PLAN_ACTOR)
    )

    with op.batch_alter_table("execution_plans") as batch:
        batch.alter_column(
            "requested_by", existing_type=sa.String(100), nullable=False
        )

    # --- jobs.execution_plan_id -------------------------------------------
    #
    # Batch mode SQLite'ta tabloyu yeniden kurar; `uq_jobs_active_ping_inventory`
    # kısmi unique index'i WHERE yan tümcesiyle birlikte korunur (migration
    # testi bunu doğrular).
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("execution_plan_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            op.f("fk_jobs_execution_plan_id_execution_plans"),
            "execution_plans",
            ["execution_plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            op.f("uq_jobs_execution_plan_id"), ["execution_plan_id"]
        )

    # Etkin ama yetkilendirilmemiş legacy PLAYBOOK satırları kapatılır.
    #
    # Bu satırlar plan bağı **olmadan** yazılmıştır ve aşağıdaki CHECK'i
    # ihlal ederler. İkisi de yapılmaz: satırlara uydurma bir execution planı
    # bağlamak, onaylanmamış bir işe onaylanmış görünüm verirdi; migration'ı
    # düşürmek ise yükseltmeyi elle veri düzeltmesine bağımlı kılardı. Bunun
    # yerine iş, hiç çalıştırılmamış sayılarak terminal `failed` durumuna
    # alınır — CHECK yalnız pending/running satırları bağladığı için kayıt
    # tarihsel iz olarak korunur.
    op.execute(
        sa.text(
            "UPDATE jobs SET status = 'failed', finished_at = COALESCE(finished_at, "
            "COALESCE(started_at, created_at)) "
            "WHERE job_type = 'playbook' AND status IN ('pending', 'running') "
            "AND execution_plan_id IS NULL"
        )
    )

    # CHECK'ler veriden **sonra** eklenir: batch mode tabloyu yeniden kurarken
    # mevcut satırları kopyalar ve ihlal eden bir satır kalsaydı yükseltme
    # burada düşerdi.
    # İsimler kısa verilir: `ck_%(table_name)s_%(constraint_name)s` kuralı
    # `ck_jobs_` ön ekini kendisi ekler ve model tarafıyla aynı adı üretir.
    with op.batch_alter_table("jobs") as batch:
        batch.create_check_constraint(
            "active_playbook_is_authorized",
            "job_type <> 'playbook' OR status NOT IN ('pending', 'running') OR ("
            "execution_plan_id IS NOT NULL "
            "AND project_id IS NOT NULL "
            "AND playbook_path IS NOT NULL "
            "AND limit_pattern IS NULL)",
        )
        batch.create_check_constraint(
            "ping_has_no_execution_plan",
            "job_type <> 'ping' OR execution_plan_id IS NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_ping_has_no_execution_plan"), type_="check")
        batch.drop_constraint(
            op.f("ck_jobs_active_playbook_is_authorized"), type_="check"
        )
        batch.drop_constraint(op.f("uq_jobs_execution_plan_id"), type_="unique")
        batch.drop_constraint(
            op.f("fk_jobs_execution_plan_id_execution_plans"), type_="foreignkey"
        )
        batch.drop_column("execution_plan_id")

    with op.batch_alter_table("execution_plans") as batch:
        batch.drop_column("requested_by")
