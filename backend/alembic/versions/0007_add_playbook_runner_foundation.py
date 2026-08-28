"""add playbook runner foundation

Revision ID: 0007_add_playbook_runner_foundation
Revises: 0006_bind_execution_plans_to_actor_and_job
Create Date: 2026-08-16

R1-V3C1A runner temeli. Bu migration **hiçbir şey çalıştırmaz**; yalnızca
worker'ın ileride yaslanacağı şemayı ve invariantları kurar.

Üç grup değişiklik vardır:

1. **Sonuç alanları.** ``error_code`` terminal bir Job'ın makine tarafından
   okunabilir sebebini, ``result_truncated`` normalize sonucun sınıra takılıp
   kırpıldığını taşır.

2. **Global aktif PLAYBOOK concurrency = 1.** Kural veritabanı seviyesinde,
   kısmi unique index ile uygulanır (aşağıda).

3. **Worker ownership/lease.** ``worker_id``, ``heartbeat_at`` ve
   ``lease_expires_at`` üçlüsü, ``running`` bir PLAYBOOK satırının sahibini ve
   kirasını satırın kendisinden okunabilir kılar. Üç CHECK bunların birlikte
   dolu veya birlikte boş olmasını zorunlu tutar; dördüncüsü kiranın
   tazelendiği andan **sonra** dolmasını şart koşar
   (``lease_expires_at > heartbeat_at``).

**``JobStatus``'a ``timeout`` eklenmez.** Timeout ayrı bir durum değil,
başarısızlığın bir sebebidir ve ileride ``status = 'failed'`` +
``error_code = 'runner_timeout'`` olarak temsil edilir. Yeni bir enum üyesi,
mevcut bütün terminal-durum sorgularını ve ``finish_job`` çağrılarını sessizce
eksik bırakırdı.

**Neden index anahtarı ``job_type``?** Predicate zaten
``job_type = 'playbook'`` diyor; yani index'e giren bütün satırlarda anahtar
**aynı** değeri taşır. Böylece ikinci bir aktif PLAYBOOK satırı — inventory'si,
project'i veya durumu ne olursa olsun — yinelenen anahtar olur. ``pending`` ve
``running`` aynı predicate içindedir: biri sıradayken diğerinin başlaması da
engellenir. Aynı ifade SQLite ve PostgreSQL'de aynı invariantı üretir.

**Eski aktif satırların ele alınışı.** Bu migration'dan önce yazılmış
``pending``/``running`` PLAYBOOK Job'ları, arkasında çalışan hiçbir süreç
olmadan duruyordur: onları çalıştırılabilir bırakmak, operatörün onaylamadığı
bir anda kendiliğinden başlayan bir execution üretirdi. Ayrıca ikiden fazlası
varsa yeni index zaten kurulamazdı. Bu yüzden hepsi index'ten **önce** terminal
``failed`` durumuna alınır ve sebebi ``interrupted_by_upgrade`` olarak yazılır;
kayıt tarihsel iz olarak korunur, uydurma bir plan bağlanmaz.

Sebep kodu burada **string sabiti olarak** yazılır: migration geçmişi
dondurulmuş bir kayıttır ve uygulama sabiti ileride değişirse bu migration'ın
ürettiği veri değişmemelidir (0006 ile aynı gerekçe).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_add_playbook_runner_foundation"
down_revision: str | None = "0006_bind_execution_plans_to_actor_and_job"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Yükseltme sırasında kapatılan eski aktif Job'ların sebebi.
INTERRUPTED_BY_UPGRADE = "interrupted_by_upgrade"

ACTIVE_PLAYBOOK_INDEX = "uq_jobs_active_playbook_global"

# Model tarafındaki `_active_playbook` predicate'inin derlenmiş hâliyle birebir
# aynıdır; ayrışırlarsa `compare_metadata` boş kalmaz.
_ACTIVE_PLAYBOOK = sa.text("job_type = 'playbook' AND status IN ('pending', 'running')")


def upgrade() -> None:
    # --- 1. Sütunlar ------------------------------------------------------
    #
    # `result_truncated` önce nullable eklenir, mevcut satırlar doldurulur ve
    # ancak sonra NOT NULL'a çekilir (0006'daki `requested_by` ile aynı desen):
    # tek adımda NOT NULL eklemek dolu bir tabloda başarısız olurdu.
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("error_code", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "result_truncated", sa.Boolean(), nullable=True, server_default=sa.false()
            )
        )
        batch.add_column(sa.Column("worker_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.execute(sa.text("UPDATE jobs SET result_truncated = 0 WHERE result_truncated IS NULL"))

    with op.batch_alter_table("jobs") as batch:
        batch.alter_column(
            "result_truncated",
            existing_type=sa.Boolean(),
            existing_server_default=sa.false(),
            nullable=False,
        )

    # --- 2. Eski aktif PLAYBOOK satırları --------------------------------
    #
    # Index ve CHECK'lerden **önce** çalışır: ihlal eden bir satır kalsaydı
    # aşağıdaki adımlar düşerdi ve yükseltme elle veri düzeltmesine bağımlı
    # hâle gelirdi. Sahiplik alanları da açıkça boşaltılır; bu satırların
    # arkasında hiçbir worker yoktur.
    op.execute(
        sa.text(
            "UPDATE jobs SET status = 'failed', "
            "finished_at = COALESCE(finished_at, started_at, created_at), "
            "error_code = :reason, "
            "worker_id = NULL, heartbeat_at = NULL, lease_expires_at = NULL "
            "WHERE job_type = 'playbook' AND status IN ('pending', 'running')"
        ).bindparams(reason=INTERRUPTED_BY_UPGRADE)
    )

    # --- 3. Invariantlar --------------------------------------------------
    #
    # İsimler kısa verilir: `ck_%(table_name)s_%(constraint_name)s` kuralı
    # `ck_jobs_` ön ekini kendisi ekler ve model tarafıyla aynı adı üretir.
    with op.batch_alter_table("jobs") as batch:
        batch.create_check_constraint(
            "running_playbook_has_lease",
            "job_type <> 'playbook' OR status <> 'running' OR ("
            "worker_id IS NOT NULL "
            "AND heartbeat_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
        )
        batch.create_check_constraint(
            "idle_playbook_has_no_lease",
            "job_type <> 'playbook' OR status = 'running' OR ("
            "worker_id IS NULL "
            "AND heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL)",
        )
        batch.create_check_constraint(
            "ping_has_no_lease",
            "job_type <> 'ping' OR ("
            "worker_id IS NULL "
            "AND heartbeat_at IS NULL "
            "AND lease_expires_at IS NULL)",
        )
        # Alanların dolu olması yetmez: yazıldığı anda süresi geçmiş bir kira,
        # canlı bir execution'ı terk edilmiş gösterir ve işin ikinci bir worker
        # tarafından devralınmasına yol açardı.
        batch.create_check_constraint(
            "running_playbook_lease_outlives_heartbeat",
            "job_type <> 'playbook' OR status <> 'running' "
            "OR lease_expires_at > heartbeat_at",
        )

    op.create_index(
        ACTIVE_PLAYBOOK_INDEX,
        "jobs",
        ["job_type"],
        unique=True,
        sqlite_where=_ACTIVE_PLAYBOOK,
        postgresql_where=_ACTIVE_PLAYBOOK,
    )


def downgrade() -> None:
    op.drop_index(ACTIVE_PLAYBOOK_INDEX, table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(
            op.f("ck_jobs_running_playbook_lease_outlives_heartbeat"), type_="check"
        )
        batch.drop_constraint(op.f("ck_jobs_ping_has_no_lease"), type_="check")
        batch.drop_constraint(op.f("ck_jobs_idle_playbook_has_no_lease"), type_="check")
        batch.drop_constraint(op.f("ck_jobs_running_playbook_has_lease"), type_="check")
        batch.drop_column("lease_expires_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("worker_id")
        batch.drop_column("result_truncated")
        batch.drop_column("error_code")
