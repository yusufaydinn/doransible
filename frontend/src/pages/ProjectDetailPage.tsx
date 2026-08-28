import { Link, useParams } from "react-router-dom";

import { StatusMessage } from "../components/StatusMessage";
import { StepHeading } from "../components/StepHeading";
import { ExecutionPlanSection } from "../features/executions/components/ExecutionPlanSection";
import { useProjectInventories } from "../features/executions/hooks";
import { DeactivateProjectPanel } from "../features/projects/components/DeactivateProjectPanel";
import { ErrorNoticePanel } from "../features/projects/components/ErrorNoticePanel";
import { PlaybookList } from "../features/projects/components/PlaybookList";
import { usePlaybooks, useProject } from "../features/projects/hooks";
import { ApiError } from "../lib/apiClient";
import { formatDateTime } from "../lib/format";
import type { PlaybookEntry, Project } from "../features/projects/types";

export function ProjectDetailPage() {
  const { projectId } = useParams();
  const parsedId = parseProjectId(projectId);

  if (parsedId === null) {
    return <ProjectNotFound />;
  }

  return <ProjectDetail projectId={parsedId} />;
}

function ProjectDetail({ projectId }: { projectId: number }) {
  const project = useProject(projectId);
  // Keşif tek yerde okunur: hem liste hem plan formu aynı sorguyu paylaşır,
  // böylece sayfa açılışında ikinci bir keşif isteği çıkmaz.
  const playbooks = usePlaybooks(projectId, { enabled: project.data?.is_active ?? false });

  if (project.isPending) {
    return <p role="status">Project bilgileri yükleniyor…</p>;
  }

  if (project.isError) {
    if (project.error instanceof ApiError && project.error.status === 404) {
      return <ProjectNotFound />;
    }
    return (
      <ErrorNoticePanel
        error={project.error}
        onRetry={() => void project.refetch()}
        headingLevel={2}
      />
    );
  }

  return (
    <section>
      <div className="page-header">
        <h2>{project.data.name}</h2>
        <Link to="/projects">Listeye dön</Link>
      </div>

      <p className="muted">
        Bu sayfada sırasıyla keşfedilen playbook'ları görün, bu project'e bir
        inventory bağlayın ve Check (Ansible --check) veya Normal modda bir plan
        oluşturup açıkça onayladıktan sonra çalıştırın.
      </p>

      <ProjectSummary project={project.data} />

      <section className="section">
        <StepHeading index={1}>Playbook'lar</StepHeading>
        <PlaybookSection project={project.data} playbooks={playbooks} />
      </section>

      <section className="section">
        <StepHeading index={2}>Inventory</StepHeading>
        <BoundInventoriesSection project={project.data} />
      </section>

      <section className="section">
        <StepHeading index={3}>Çalıştırma planı</StepHeading>
        <ExecutionPlanSection
          project={project.data}
          playbooks={discoveredPlaybooks(playbooks)}
        />
      </section>

      <DeactivateProjectPanel project={project.data} />
    </section>
  );
}

function ProjectSummary({ project }: { project: Project }) {
  return (
    <dl className="details">
      <dt>Durum</dt>
      <dd>{project.is_active ? "Aktif" : "Pasif (kayıt saklanıyor)"}</dd>

      <dt>Controller yolu</dt>
      <dd>
        <code>{project.path}</code>
      </dd>

      <dt>Açıklama</dt>
      <dd>{project.description ?? "Açıklama girilmemiş"}</dd>

      <dt>Oluşturulma</dt>
      <dd>{formatDateTime(project.created_at)}</dd>

      <dt>Güncellenme</dt>
      <dd>{formatDateTime(project.updated_at)}</dd>
    </dl>
  );
}

/**
 * Playbook keşfi bölümü.
 *
 * Pasif project'te backend zaten `project_inactive` döndürür; gereksiz istek
 * atmamak için sorgu hiç başlatılmaz ve durum doğrudan açıklanır.
 */
function PlaybookSection({
  project,
  playbooks,
}: {
  project: Project;
  playbooks: ReturnType<typeof usePlaybooks>;
}) {
  if (!project.is_active) {
    return (
      <StatusMessage tone="info" title="Pasif project'te keşif yapılmaz">
        <p>
          Bu project pasif durumda olduğu için playbook listesi alınmadı. Controller'daki
          dosyalar yerinde duruyor.
        </p>
      </StatusMessage>
    );
  }

  if (playbooks.isPending) {
    return <p role="status">Playbook'lar aranıyor…</p>;
  }

  if (playbooks.isError) {
    return (
      <ErrorNoticePanel error={playbooks.error} onRetry={() => void playbooks.refetch()} />
    );
  }

  return <PlaybookList result={playbooks.data} />;
}

/**
 * Bu project'e bağlı inventory'lerin özeti.
 *
 * `useProjectInventories` çalıştırma planı formunun da okuduğu sorgudur
 * (aynı `queryKey`); TanStack Query bu iki çağrıyı tek istekte birleştirir,
 * bu yüzden burada ayrıca göstermek ikinci bir ağ isteği doğurmaz.
 */
function BoundInventoriesSection({ project }: { project: Project }) {
  const inventories = useProjectInventories(project.id, { enabled: project.is_active });

  if (!project.is_active) {
    return (
      <StatusMessage tone="info" title="Pasif project'te inventory bağlantısı gösterilmez">
        <p>
          Bu project pasif durumda olduğu için bağlı inventory listesi alınmadı.
        </p>
      </StatusMessage>
    );
  }

  if (inventories.isPending) {
    return <p role="status">Bağlı inventory'ler yükleniyor…</p>;
  }

  if (inventories.isError) {
    return (
      <ErrorNoticePanel error={inventories.error} onRetry={() => void inventories.refetch()} />
    );
  }

  if (inventories.data.length === 0) {
    return (
      <StatusMessage tone="info" title="Bu project'e bağlı inventory yok">
        <p>
          Plan oluşturmak için önce bu project'e bağlı bir inventory kaydedin.
          Inventory dosyası controller'da, bu project'in dizini altında zaten var
          olmalıdır — dosya burada oluşturulmaz veya yüklenmez.
        </p>
        <p>
          <Link to={`/inventories/new?project_id=${project.id}`}>
            Bu project için inventory kaydet
          </Link>
        </p>
      </StatusMessage>
    );
  }

  return (
    <>
      <p className="muted">
        {inventories.data.length} inventory bu project'e bağlı. Aşağıdaki
        çalıştırma planı formunda bunlardan biri seçilir.
      </p>
      <ul className="inline-list">
        {inventories.data.map((inventory) => (
          <li key={inventory.id}>
            <Link to={`/inventories/${inventory.id}`}>{inventory.name}</Link>
          </li>
        ))}
      </ul>
      <p>
        <Link to={`/inventories/new?project_id=${project.id}`}>
          Başka bir inventory kaydet
        </Link>
      </p>
    </>
  );
}

/**
 * Plan formuna verilecek playbook listesi.
 *
 * Keşif henüz tamamlanmadıysa veya hata verdiyse liste boştur: form o durumda
 * seçim sunmaz ve hiçbir istek göndermez.
 */
function discoveredPlaybooks(playbooks: ReturnType<typeof usePlaybooks>): PlaybookEntry[] {
  return playbooks.data?.playbooks ?? [];
}

function ProjectNotFound() {
  return (
    <StatusMessage tone="error" title="Project bulunamadı" headingLevel={2}>
      <p>
        Bu kimlikle bir project kaydı yok. Adres yanlış yazılmış veya kayıt hiç
        oluşturulmamış olabilir.
      </p>
      <p>
        <Link to="/projects">Project listesine dön</Link>
      </p>
    </StatusMessage>
  );
}

/** URL parametresini pozitif tam sayıya çevirir; aksi hâlde `null`. */
function parseProjectId(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}
