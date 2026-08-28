import { StatusMessage } from "../../../components/StatusMessage";
import type { PingErrorNotice } from "../errorMessages";

interface PingErrorPanelProps {
  notice: PingErrorNotice;
  /** Kullanıcıyı boş forma döndürür. **Hiçbir istek başlatmaz.** */
  onStartOver?: () => void;
  headingLevel?: 2 | 3 | 4;
}

/**
 * Ping hata bildirimini gösterir.
 *
 * Bileşen `ApiError` nesnesini veya `details` alanını **hiç görmez**: yalnızca
 * `describePingError` çıktısındaki, tip korumasından geçmiş alanları basar. Ham
 * JSON, exception metni ve token hiçbir koşulda ekrana gelmez.
 *
 * Burada "tekrar dene" eylemi bilinçli olarak **yoktur**. Ping hataları aynı
 * onayla tekrarlanamaz; kullanıcının tek güvenli yolu boş forma dönüp yeni bir
 * önizleme oluşturmaktır ve bu ayrı bir karardır.
 */
export function PingErrorPanel({ notice, onStartOver, headingLevel }: PingErrorPanelProps) {
  return (
    <StatusMessage tone="error" title={notice.title} headingLevel={headingLevel}>
      <p>{notice.message}</p>

      {notice.parserMessage !== undefined && (
        <>
          <p className="muted" id="ping-parser-message-label">
            Ansible'ın açıklaması:
          </p>
          <pre className="parser-message" aria-labelledby="ping-parser-message-label">
            {notice.parserMessage}
          </pre>
        </>
      )}

      {notice.jobId !== undefined && (
        <p>
          İlgili iş kaydı: <code>{notice.jobId}</code>
        </p>
      )}

      {notice.retryable === false && (
        <p>
          <strong>Bu isteği otomatik olarak tekrar etmeyin.</strong> Sunucudaki iş
          kaydından durumu doğrulamadan yeni bir çalıştırma başlatmayın.
        </p>
      )}

      {notice.requiresNewPreview === true && (
        <p>
          Onaylar tek kullanımlıktır ve bu onay tükendi. Ping çalıştırmak isterseniz
          baştan yeni bir önizleme oluşturup planı yeniden onaylamanız gerekir.
        </p>
      )}

      {onStartOver && (
        <button type="button" onClick={onStartOver}>
          Yeni önizleme oluştur
        </button>
      )}
    </StatusMessage>
  );
}
