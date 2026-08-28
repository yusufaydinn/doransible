import { JOB_STATUS_LABELS } from "../labels";
import type { JobStatus } from "../types";

interface JobStatusBadgeProps {
  status: JobStatus;
}

/**
 * Job durumunun renkli rozeti.
 *
 * Metin `JOB_STATUS_LABELS`'tan **birebir** aynı gelir: yalnızca görsel bir
 * kapsayıcıdır, mevcut `screen.getByText("Çalışıyor")` gibi testleri
 * bozmaz (span, doğrudan metin düğümünü taşıyan en içteki eleman olur).
 */
export function JobStatusBadge({ status }: JobStatusBadgeProps) {
  return <span className={`badge badge--${status}`}>{JOB_STATUS_LABELS[status]}</span>;
}
