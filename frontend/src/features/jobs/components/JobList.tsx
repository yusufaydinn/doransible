import { Link } from "react-router-dom";

import { formatDateTime } from "../../../lib/format";
import { JOB_MODE_LABELS } from "../labels";
import type { PlaybookJobSummary } from "../types";
import { JobStatusBadge } from "./JobStatusBadge";

interface JobListProps {
  jobs: PlaybookJobSummary[];
}

/**
 * Bir sayfalık çalıştırma listesi; sayfa başına en fazla 25 kayıt
 * (bkz. `fetchJobs`). Sayfalar arası gezinme bu tabloyu değil, onu saran
 * `JobListPage`'i ilgilendirir.
 *
 * "Kip" sütunu kullanıcı dostu etiketi gösterir (R1-V3H2B): `normal` bir Job
 * burada da ham `check` varsayımıyla değil kendi `mode` alanından okunarak
 * listelenir.
 *
 * "Project"/"Inventory" sütunları artık kaydın **adını** gösterir, yalnız
 * `#id`'yi değil (R1-V3J0B2): backend'in döndürdüğü `project_name`/
 * `inventory_name` kendi mevcut detay linkiyle birlikte basılır; ID teknik
 * referans olarak ikincil (küçük, muted) kalır.
 */
export function JobList({ jobs }: JobListProps) {
  return (
    <div className="table-wrapper">
      <table className="table">
        <caption className="visually-hidden">Son çalıştırmalar</caption>
        <thead>
          <tr>
            <th scope="col">Durum</th>
            <th scope="col">Kip</th>
            <th scope="col">Playbook</th>
            <th scope="col">Project</th>
            <th scope="col">Inventory</th>
            <th scope="col">Oluşturulma</th>
            <th scope="col">
              <span className="visually-hidden">Detay</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.job_id}>
              <td>
                <JobStatusBadge status={job.status} />
              </td>
              <td>{JOB_MODE_LABELS[job.mode]}</td>
              <td>
                <code>{job.playbook_path}</code>
              </td>
              <td>
                <Link to={`/projects/${job.project_id}`}>{job.project_name}</Link>{" "}
                <span className="muted">#{job.project_id}</span>
              </td>
              <td>
                <Link to={`/inventories/${job.inventory_id}`}>{job.inventory_name}</Link>{" "}
                <span className="muted">#{job.inventory_id}</span>
              </td>
              <td>{formatDateTime(job.created_at)}</td>
              <td>
                <Link to={`/jobs/${job.job_id}`}>Detay</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
