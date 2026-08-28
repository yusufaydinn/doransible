import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { InventoryErrorPanel } from "../features/inventories/components/InventoryErrorPanel";
import { InventoryForm } from "../features/inventories/components/InventoryForm";
import { useCreateInventory } from "../features/inventories/hooks";
import { useProjects } from "../features/projects/hooks";
import type { Project } from "../features/projects/types";

export function NewInventoryPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const mutation = useCreateInventory();
  const projects = useProjects();

  return (
    <section>
      <div className="page-header">
        <h2>Yeni inventory kaydet</h2>
        <Link to="/inventories">Listeye dön</Link>
      </div>

      <p className="muted">
        Controller'da zaten var olan bir INI veya YAML inventory dosyasının kaydını tutar.
        Dosya oluşturulmaz, yüklenmez veya kopyalanmaz.
      </p>

      {mutation.isError && <InventoryErrorPanel error={mutation.error} />}

      {projects.isPending && <p role="status">Project listesi yükleniyor…</p>}

      {projects.isError && (
        <InventoryErrorPanel error={projects.error} onRetry={() => void projects.refetch()} />
      )}

      {projects.isSuccess && (
        <InventoryForm
          projects={projects.data}
          initialProjectId={resolveInitialProjectId(searchParams.get("project_id"), projects.data)}
          isSubmitting={mutation.isPending}
          onSubmit={(request) =>
            mutation.mutateAsync(request).then((inventory) => {
              navigate(`/inventories/${inventory.id}`, { replace: true });
            })
          }
        />
      )}
    </section>
  );
}

/**
 * `?project_id=` query parametresini ön seçim için çözer.
 *
 * Yalnızca pozitif tam sayı **ve** `useProjects()`'in döndürdüğü aktif
 * listede gerçekten bulunan bir kimlik kabul edilir. `useProjects()` yalnızca
 * aktif project'leri döndürdüğü için pasif bir project kimliği de bu kontrolde
 * kendiliğinden elenir. Bilinmeyen, bozuk veya negatif bir değer sessizce
 * `null`'a döner ve form standalone seçimiyle açılır — hiçbir şekilde POST
 * gövdesine taşınmaz.
 */
function resolveInitialProjectId(
  rawProjectId: string | null,
  activeProjects: Project[],
): number | null {
  if (rawProjectId === null || !/^\d+$/.test(rawProjectId)) {
    return null;
  }

  const parsed = Number(rawProjectId);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) {
    return null;
  }

  return activeProjects.some((project) => project.id === parsed) ? parsed : null;
}
