import { Link } from "react-router-dom";

import { formatDateTime } from "../../../lib/format";
import type { Project } from "../types";

interface ProjectListProps {
  projects: Project[];
}

/**
 * Project kayıtlarını tablo olarak listeler.
 *
 * Tablo kullanılmasının nedeni erişilebilirliktir: ekran okuyucu her hücreyi
 * sütun başlığıyla birlikte okur.
 */
export function ProjectList({ projects }: ProjectListProps) {
  return (
    <div className="table-wrapper">
      <table className="table">
        <caption className="visually-hidden">Kayıtlı aktif project'ler</caption>
        <thead>
          <tr>
            <th scope="col">Ad</th>
            <th scope="col">Controller yolu</th>
            <th scope="col">Açıklama</th>
            <th scope="col">Güncellenme</th>
          </tr>
        </thead>
        <tbody>
          {projects.map((project) => (
            <tr key={project.id}>
              <th scope="row">
                <Link to={`/projects/${project.id}`}>{project.name}</Link>
              </th>
              <td>
                <code>{project.path}</code>
              </td>
              <td>{project.description ?? "—"}</td>
              <td>{formatDateTime(project.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
