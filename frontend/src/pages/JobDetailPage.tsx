import { Link, useParams } from "react-router-dom";

import { StatusMessage } from "../components/StatusMessage";
import { JobResultPanel } from "../features/jobs/components/JobResultPanel";
import { JobSummaryPanel } from "../features/jobs/components/JobSummaryPanel";
import { describeJobError } from "../features/jobs/errorMessages";
import { useJob, useJobResult } from "../features/jobs/hooks";
import type { PlaybookJobSummary } from "../features/jobs/types";
import { ApiError } from "../lib/apiClient";

export function JobDetailPage() {
  const { jobId } = useParams();

  if (jobId === undefined) {
    return <JobNotFound />;
  }

  return <JobDetail jobId={jobId} />;
}

function JobDetail({ jobId }: { jobId: string }) {
  const job = useJob(jobId);

  if (job.isPending) {
    return <p role="status">Çalıştırma bilgileri yükleniyor…</p>;
  }

  if (job.isError) {
    if (job.error instanceof ApiError && job.error.status === 404) {
      return <JobNotFound />;
    }
    const notice = describeJobError(job.error);
    return (
      <StatusMessage tone="error" title={notice.title} headingLevel={2}>
        <p>{notice.message}</p>
        <button type="button" onClick={() => void job.refetch()}>
          Tekrar dene
        </button>
      </StatusMessage>
    );
  }

  return (
    <section>
      <div className="page-header">
        <h2>Çalıştırma detayı</h2>
        <Link to="/jobs">Listeye dön</Link>
      </div>

      <JobSummaryPanel job={job.data} />

      <section className="section">
        <h3>Sonuç</h3>
        <JobResultSection job={job.data} />
      </section>
    </section>
  );
}

/**
 * Sonuç bölümü.
 *
 * `useJobResult` her render'da çağrılır (hook kuralları); istek yalnız Job
 * terminal (`successful`/`failed`) ve `has_recorded_result === true` olduğunda
 * gönderilir — karar hook'un kendi `enabled` mantığındadır.
 */
function JobResultSection({ job }: { job: PlaybookJobSummary }) {
  const result = useJobResult(job);
  const eligible =
    (job.status === "successful" || job.status === "failed") && job.has_recorded_result;

  if (!eligible) {
    return <p className="muted">{describeIneligibleResult(job)}</p>;
  }

  return <JobResultPanel result={result} />;
}

function describeIneligibleResult(job: PlaybookJobSummary): string {
  if (job.status === "pending" || job.status === "running") {
    return "Çalıştırma tamamlanana kadar sonuç okunmaz.";
  }
  if (job.status === "canceled") {
    return "İptal edilen çalıştırmalar için sonuç yoktur.";
  }
  return "Bu çalıştırma için kayıtlı bir sonuç yok.";
}

function JobNotFound() {
  return (
    <StatusMessage tone="error" title="Çalıştırma kaydı bulunamadı" headingLevel={2}>
      <p>Bu kimlikle bir çalıştırma kaydı yok. Adres yanlış yazılmış olabilir.</p>
      <p>
        <Link to="/jobs">Çalıştırma listesine dön</Link>
      </p>
    </StatusMessage>
  );
}
