import { Link, useNavigate } from "react-router-dom";

import { ErrorNoticePanel } from "../features/projects/components/ErrorNoticePanel";
import { ProjectForm } from "../features/projects/components/ProjectForm";
import { useCreateProject } from "../features/projects/hooks";

export function NewProjectPage() {
  const navigate = useNavigate();
  const mutation = useCreateProject();

  return (
    <section>
      <div className="page-header">
        <h2>Yeni project</h2>
        <Link to="/projects">Listeye dön</Link>
      </div>

      <p className="muted">
        Var olan bir Ansible dizinini kaydeder. Dizin oluşturulmaz, kopyalanmaz ve
        değiştirilmez.
      </p>

      {mutation.isError && <ErrorNoticePanel error={mutation.error} />}

      <ProjectForm
        isSubmitting={mutation.isPending}
        onSubmit={(request) =>
          mutation.mutate(request, {
            onSuccess: (project) => {
              navigate(`/projects/${project.id}`, { replace: true });
            },
          })
        }
      />
    </section>
  );
}
