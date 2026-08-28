"""Execution endpoint'leri (R1-V1 önizleme, R1-V2 hazırlama, R1-V3D1 çalıştırma).

Route yalnızca istek/cevap dönüşümü yapar; project bağı, playbook keşfi,
inventory doğrulaması, dondurma ve plan claim'i ``app.services.execution``
içindedir (route/service katman ayrımı sözleşmesi). Burada fingerprint hesaplanmaz, transaction
açılmaz ve Job satırı elle kurulmaz.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.execution import (
    ExecutionLaunchCreate,
    ExecutionLaunchResponse,
    ExecutionPlanCreate,
    ExecutionPlanResponse,
    PreparedExecutionPlanResponse,
)
from app.services.execution import (
    ExecutionPlan,
    build_execution_plan,
    launch_prepared_playbook_job,
    prepare_execution_plan,
)
from app.services.inventories import ParserLimits
from app.services.projects import ScanLimits

router = APIRouter(prefix="/projects", tags=["executions"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/{project_id}/execution-plan",
    response_model=ExecutionPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Execution planı üret",
)
def create_execution_plan(
    project_id: int,
    payload: ExecutionPlanCreate,
    session: SessionDep,
    settings: SettingsDep,
) -> ExecutionPlan:
    """Kullanıcının okuyacağı, seçtiği kipteki planı üretir (R1-V1, R1-V3H2A).

    Bu endpoint **hiçbir playbook çalıştırmaz**: `ansible-runner` çağrılmaz,
    Job kaydı, artifact veya plan state'i oluşturulmaz ve onay token'ı
    dağıtılmaz. Başlatılan tek alt süreç, inventory'yi okuyan
    `ansible-inventory`'dir.

    Cevaptaki ``executable=false`` bağlayıcıdır: plan ileride çalıştırılabilir
    bir onay olarak kabul edilemez.
    """
    return build_execution_plan(
        session,
        project_id,
        mode=payload.mode,
        inventory_id=payload.inventory_id,
        playbook_path=payload.playbook_path,
        project_roots=settings.resolve_project_root_allowlist(),
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        key_roots=settings.resolve_ssh_key_root_allowlist(),
        command=settings.ansible_inventory_command,
        parser_limits=ParserLimits.from_settings(settings),
        scan_limits=ScanLimits.from_settings(settings),
        host_key_policy=settings.ssh_host_key_policy,
    )


@router.post(
    "/{project_id}/execution-plans",
    response_model=PreparedExecutionPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Execution planını onaya hazırla",
)
def prepare_execution_plan_endpoint(
    project_id: int,
    payload: ExecutionPlanCreate,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> PreparedExecutionPlanResponse:
    """Planı dondurulmuş içerik üzerinden hazırlar ve tek kullanımlık token verir.

    Bu endpoint de **hiçbir playbook çalıştırmaz**: Job, artifact, SSH bağlantısı
    ve `ansible-runner` çağrısı yoktur. Token bir onay biletidir; onu tüketen tek
    yol :func:`launch_execution`'dır ve plan cevabı ``executable=false`` kalır —
    çalıştırma yetkisini plan özeti değil token taşır.

    ``Cache-Control: no-store``: cevap tek kullanımlık bir sır taşır ve hiçbir
    ara katmanda saklanmamalıdır.

    Aktör (``requested_by``) plana **sunucu ayarından** bağlanır (R1-V3A);
    istek gövdesi onu ne taşır ne de etkiler ve cevapta yer almaz.
    """
    response.headers["Cache-Control"] = "no-store"
    prepared = prepare_execution_plan(
        session,
        project_id,
        mode=payload.mode,
        inventory_id=payload.inventory_id,
        playbook_path=payload.playbook_path,
        requested_by=settings.local_actor,
        workspace_root=settings.resolve_execution_plan_dir(),
        project_roots=settings.resolve_project_root_allowlist(),
        inventory_roots=settings.resolve_inventory_root_allowlist(),
        key_roots=settings.resolve_ssh_key_root_allowlist(),
        command=settings.ansible_inventory_command,
        parser_limits=ParserLimits.from_settings(settings),
        scan_limits=ScanLimits.from_settings(settings),
        host_key_policy=settings.ssh_host_key_policy,
        ttl_seconds=settings.execution_plan_ttl_seconds,
    )
    return PreparedExecutionPlanResponse(
        # Raw token yalnızca burada serileştirilir; loglanmaz ve saklanmaz.
        plan_token=prepared.token,
        expires_at=prepared.expires_at,
        manifest_digest=prepared.manifest_digest,
        prepared=True,
        plan=ExecutionPlanResponse.model_validate(prepared.plan),
    )


@router.post(
    "/{project_id}/executions",
    response_model=ExecutionLaunchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Hazırlanmış planı çalıştırmaya al",
)
def launch_execution(
    project_id: int,
    payload: ExecutionLaunchCreate,
    session: SessionDep,
    settings: SettingsDep,
    response: Response,
) -> ExecutionLaunchResponse:
    """Onay token'ını tüketip ``pending`` PLAYBOOK Job'ını rezerve eder (R1-V3D1).

    Bu istek **hiçbir şey çalıştırmaz**: `ansible-runner` çağrılmaz, alt süreç
    açılmaz, SSH bağlantısı kurulmaz ve artifact üretilmez. Yapılan tek iş,
    planın atomik olarak claim edilmesi ve karşılığında kalıcı bir Job satırı
    oluşturulmasıdır; o Job'ı çalıştıracak tek şey ayrı bir arka plan worker'ıdır
    ve burada ne hazır olup olmadığına bakılır ne de tetiklenir.

    Bu yüzden ``201`` "execution başladı" demez, "Job kalıcı olarak oluşturuldu"
    der. Cevaptaki alanın adı da ``status`` değil ``initial_status``'tur: worker
    Job'ı bu cevap istemciye ulaşmadan alabilir.

    Üç değer istekten **alınmaz**, sunucu ayarından geçer: aktör, dondurulmuş
    workspace kökü ve SSH host key politikası. Politika özete girdiği için
    hazırlama ile çalıştırma arasında değişirse token hiçbir satırı eşleştirmez
    ve **tüketilmez** (``409 execution_plan_invalid``).

    ``Cache-Control: no-store`` yalnız **cevabın** saklanmamasını ister; bir
    response header'ının isteğin gövdesi üzerinde hiçbir yaptırımı yoktur.
    Token'ı asıl koruyan şey onun gövdede taşınmasıdır: URL'ye veya query
    string'e konsaydı, proxy ve erişim log'ları onu kendiliğinden ve kalıcı
    olarak kaydederdi. İkisi ayrı savunmalardır ve biri diğerinin yerini tutmaz.
    """
    response.headers["Cache-Control"] = "no-store"
    authorized = launch_prepared_playbook_job(
        session,
        token=payload.plan_token,
        mode=payload.mode,
        project_id=project_id,
        inventory_id=payload.inventory_id,
        playbook_path=payload.playbook_path,
        requested_by=settings.local_actor,
        workspace_root=settings.resolve_execution_plan_dir(),
        host_key_policy=settings.ssh_host_key_policy,
    )
    return ExecutionLaunchResponse(
        # Alanlar claim edilen plandan gelir, istek gövdesinden **kopyalanmaz**:
        # cevap, istemcinin ne istediğini değil sunucunun neyi kaydettiğini
        # tarif etmelidir. `mode` de bu kuralın **içindedir** (R1-V3H2A):
        # `payload.mode` yalnız claim koşuluna giren beklenen kiptir, cevaba
        # yazılan `authorized.mode` ise claim edilen plan satırının kipidir.
        #
        # Dönüşüm açıkça yapılır: domain katmanı kimliği `str` taşır, cevap
        # şeması `UUID4`. Pydantic zaten dönüştürürdü ama örtük bırakmak, tip
        # denetiminin iki katman arasındaki farkı görmesini engellerdi.
        job_id=uuid.UUID(authorized.job_id),
        job_type="playbook",
        initial_status="pending",
        mode=authorized.mode,
        project_id=authorized.project_id,
        inventory_id=authorized.inventory_id,
        playbook_path=authorized.playbook_path,
        accepted_at=authorized.claimed_at,
    )
