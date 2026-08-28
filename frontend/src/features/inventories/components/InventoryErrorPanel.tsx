import { Link } from "react-router-dom";

import { StatusMessage } from "../../../components/StatusMessage";
import { describeInventoryError } from "../errorMessages";

interface InventoryErrorPanelProps {
  error: unknown;
  /** Verilirse "Tekrar dene" butonu gösterilir. */
  onRetry?: () => void;
  headingLevel?: 2 | 3 | 4;
}

/**
 * Bir inventory API hatasını kullanıcıya uygulanabilir biçimde gösterir.
 *
 * Hata zarfının `details` alanı ham JSON olarak basılmaz; yalnızca tip
 * korumasından geçen bilinen alanlar metne veya bağlantıya dönüşür.
 * `parserMessage` backend tarafından temizlenmiş bir açıklamadır ve ayrı bir
 * blokta, olduğu gibi gösterilir.
 */
export function InventoryErrorPanel({
  error,
  onRetry,
  headingLevel,
}: InventoryErrorPanelProps) {
  const notice = describeInventoryError(error);

  return (
    <StatusMessage tone="error" title={notice.title} headingLevel={headingLevel}>
      <p>{notice.message}</p>

      {notice.parserMessage !== undefined && (
        <>
          <p className="muted" id="parser-message-label">
            Ansible'ın açıklaması:
          </p>
          <pre className="parser-message" aria-labelledby="parser-message-label">
            {notice.parserMessage}
          </pre>
        </>
      )}

      {notice.relatedInventoryId !== undefined && (
        <p>
          <Link to={`/inventories/${notice.relatedInventoryId}`}>
            Inventory kaydını görüntüle
          </Link>
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
