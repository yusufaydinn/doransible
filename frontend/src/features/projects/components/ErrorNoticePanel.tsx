import { Link } from "react-router-dom";

import { StatusMessage } from "../../../components/StatusMessage";
import { describeApiError } from "../errorMessages";

interface ErrorNoticePanelProps {
  error: unknown;
  /** Verilirse "Tekrar dene" butonu gösterilir. */
  onRetry?: () => void;
  headingLevel?: 2 | 3 | 4;
}

/**
 * Bir API hatasını kullanıcıya uygulanabilir biçimde gösterir.
 *
 * Hata zarfının `details` alanı ham JSON olarak basılmaz; yalnızca bilinen
 * `project_id` bir bağlantıya dönüşür.
 */
export function ErrorNoticePanel({ error, onRetry, headingLevel }: ErrorNoticePanelProps) {
  const notice = describeApiError(error);

  return (
    <StatusMessage tone="error" title={notice.title} headingLevel={headingLevel}>
      <p>{notice.message}</p>

      {notice.relatedProjectId !== undefined && (
        <p>
          <Link to={`/projects/${notice.relatedProjectId}`}>Mevcut kaydı görüntüle</Link>
        </p>
      )}

      {onRetry && notice.retryable !== false && (
        <button type="button" onClick={onRetry}>
          Tekrar dene
        </button>
      )}
    </StatusMessage>
  );
}
