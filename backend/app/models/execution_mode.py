"""Execution mode'un **tek** doğruluk kaynağı (R1-V3H1A).

Bir execution planı ve ondan doğan Job, aynı çalıştırma kipini taşımak
zorundadır. Kip iki ayrı yerde iki ayrı string union veya iki ayrı enum olarak
tanımlansaydı, biri genişletildiğinde diğeri sessizce eski kalır ve plan ile
Job'ın "aynı" kipi taşıdığı iddiası tip düzeyinde hiçbir şey ifade etmezdi.
Bu yüzden hem :class:`~app.models.execution_plan.ExecutionPlanRecord` hem
:class:`~app.models.job.Job` buradaki tek tanımı kullanır.

Modül bilinçli olarak **yalnız** stdlib ve SQLAlchemy'ye bağlıdır: iki model de
onu import ettiği için, buraya konacak herhangi bir model import'u döngü
üretirdi.

**Kip artık zincirin tamamına bağlıdır** (R1-V3H1A → H1B1 → H1B2A → H1B2B).
Plan → fingerprint/claim → Job → acquire → executor → runner argv boyunca aynı
değer, her adımda yeniden yorumlanmadan taşınır: ``build_runner_arguments``
zorunlu, default'suz bir ``mode`` parametresi alır ve ``ExecutionMode.CHECK``
için ``--cmdline=--check``'i tam bir kez ekler, ``ExecutionMode.NORMAL`` için
hiç eklemez — argv'nin geri kalanı iki kipte de birebir aynıdır. Geçersiz bir
çalışma zamanı değeri (``None``, düz bir ``"check"``/``"normal"`` metni ya da
başka bir nesne) fail-closed reddedilir; hiçbir yol sessizce ``check``'e
düşmez.

**Public yüzey hâlâ değişmedi.** İstemcinin kip söyleyebileceği bir alan
yoktur; plan/launch request şemaları ve UI hâlâ yalnız ``check`` üretir ve
kullanıcı normal mode'u seçemez. ``normal`` şu an yalnız internal servis
çağrılarıyla temsil edilebilir. Public API/UI'da mode seçimi, mode'a özgü açık
onay ve check planının normal Job'a çevrilememesi bir sonraki dilimin
(R1-V3H2) kapsamındadır.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum

# Enum tipinin adı; CHECK constraint isimlendirmesi buradan türer
# (`ck_%(table_name)s_%(constraint_name)s` → `ck_jobs_execution_mode`).
EXECUTION_MODE_ENUM_NAME = "execution_mode"


class ExecutionMode(StrEnum):
    """Bir planın ve ondan doğan Job'ın execution mode'u.

    Ayrım tek bir yerde durur — `ansible-runner` argv'si:

    - ``check``: argv'ye ``--cmdline=--check`` **eklenen** mode.
    - ``normal``: ``--check`` **eklenmeyen** mode.

    Bugüne kadarki tek üretim mode'u ``check``'tir.

    ``check`` bir yan etkisizlik garantisi **değildir** ve hedefte değişiklik
    olmayacağını söylemez. ``--check`` altında ne olacağı playbook'un
    kullandığı modüllere bağlıdır; bu enum yalnız hangi argv'nin kurulacağını
    isimlendirir, çalıştırmanın sonucu hakkında hiçbir iddia taşımaz.
    """

    CHECK = "check"
    NORMAL = "normal"


def execution_mode_enum() -> Enum:
    """İki tablonun da kullandığı sütun tipi.

    ``create_constraint=True`` sayesinde izin verilen değer kümesi uygulama
    katmanında değil **veritabanında** durur: doğrudan yazılan bir satır da
    ``check``/``normal`` dışına çıkamaz.
    """
    return Enum(
        ExecutionMode,
        name=EXECUTION_MODE_ENUM_NAME,
        native_enum=False,
        length=16,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
