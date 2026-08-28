import { Link } from "react-router-dom";

import { StatusMessage } from "../components/StatusMessage";
import { ErrorNoticePanel } from "../features/projects/components/ErrorNoticePanel";
import { ProjectList } from "../features/projects/components/ProjectList";
import { useProjects } from "../features/projects/hooks";

export function ProjectListPage() {
  const projects = useProjects();

  return (
    <section>
      <div className="page-header">
        <h2>Project'ler</h2>
        <Link to="/projects/new">Yeni project ekle</Link>
      </div>

      <p className="muted">
        Project, controller'daki bir Ansible dizinini temsil eder. Uygulama bu dizini
        kopyalamaz; yalnızca kaydını tutar.
      </p>

      {projects.isPending && <p role="status">Project'ler yükleniyor…</p>}

      {projects.isError && (
        <ErrorNoticePanel error={projects.error} onRetry={() => void projects.refetch()} />
      )}

      {projects.isSuccess &&
        (projects.data.length === 0 ? (
          <StatusMessage tone="info" title="Henüz project kaydı yok">
            <p>
              Ansible dosyalarınızın bulunduğu controller'daki dizini kaydederek başlayın.
              Kaydedilen dizindeki playbook'lar otomatik olarak keşfedilir.
            </p>
            <p>
              <Link to="/projects/new">İlk project'i ekle</Link>
            </p>
          </StatusMessage>
        ) : (
          <ProjectList projects={projects.data} />
        ))}
    </section>
  );
}
