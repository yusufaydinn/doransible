import { StatusMessage } from "../../../components/StatusMessage";
import { formatDateTime } from "../../../lib/format";
import { usePingHistory } from "../hooks";
import type { PingHistoryItem, PingHistorySummary, PingJobStatus } from "../types";

/**
 * Özet sayaçlarının görünür etiketleri ve okundukları alan.
 *
 * Sıra sabittir ve `PingResultPanel`'deki özetle aynıdır: aynı beş sayı iki
 * ekranda farklı sırayla görünürse kullanıcı ikisini karşılaştıramaz. Etiketler
 * görünür metindir — sayının anlamı yalnızca renkle veya konumla verilmez.
 */
const SUMMARY_FIELDS: ReadonlyArray<{ key: keyof PingHistorySummary; label: string }> = [
  { key: "total", label: "Toplam" },
  { key: "reachable", label: "Erişilebilir" },
  { key: "unreachable", label: "Erişilemiyor" },
  { key: "failed", label: "Başarısız" },
  { key: "no_result", label: "Sonuç alınamadı" },
];

/** Terminal iş durumunun görünür metin karşılığı. */
const STATUS_LABELS: Record<PingJobStatus, string> = {
  successful: "Başarılı",
  failed: "Başarısız",
};

/**
 * Hata metni **sabittir** ve backend'den gelen hiçbir şeyi taşımaz.
 *
 * `ApiError`'ın `message`, `details` ve `code` alanları bu bölümde hiç
 * okunmaz: geçmiş okuması artifact yolu, dosya sistemi durumu veya belge
 * doğrulama ayrıntısı üretebilen bir yoldur ve bunların hiçbirinin kullanıcıya
 * gösterilecek bir karşılığı yoktur (GUVENLIK.md bölüm 3). Koda göre ayrışan
 * bir metin, ayrışmanın kendisiyle sunucu durumunu sızdırırdı.
 */
const ERROR_TITLE = "Ping geçmişi şu anda yüklenemedi.";

/**
 * Çıkış kodunun gösterimi.
 *
 * `null` gerçek bir durumdur: `ansible` hiç başlatılamadığında bir çıkış kodu
 * oluşmaz. Boş hücre "kod 0" ile karıştırılabileceği için açıkça yazılır.
 */
function returnCodeText(value: number | null): string {
  return value === null ? "Bilinmiyor" : String(value);
}

/** Durumun görünür metin taşıyan rozeti. */
function PingRunStatusBadge({ status }: { status: PingJobStatus }) {
  const label = STATUS_LABELS[status];
  const modifier = label === undefined ? "neutral" : status;
  return <span className={`badge badge--${modifier}`}>{label ?? status}</span>;
}

/**
 * Bir inventory'nin kalıcı ping ölçüm geçmişi (R1-V3J1B).
 *
 * **Gerçek zamanlı izleme değildir.** Bölüm hiçbir yoklama kurmaz, hiçbir ölçüm
 * başlatmaz ve hiçbir host'a bağlanmaz; yalnızca kullanıcının daha önce
 * başlattığı, tamamlanmış ölçümleri okur. Kendiliğinden tazelenen bir liste,
 * olmayan bir canlı akış varmış izlenimi verirdi.
 *
 * Bölüm inventory içeriği (`/hosts`) sorgusundan **bağımsızdır**: dosya
 * ayrıştırılamasa bile geçmiş okunabilir ve geçmiş okunamasa bile içerik
 * görünür kalır.
 *
 * Gösterilen yüzey backend'in döndürdüğünden **dar** tutulur: host adı, host
 * mesajı, artifact yolu ve aktör zaten cevapta yoktur; iş kimliği ise yalnızca
 * liste anahtarı olarak kullanılır, ekrana basılmaz.
 */
export function PingHistorySection({ inventoryId }: { inventoryId: number }) {
  const history = usePingHistory(inventoryId);

  return (
    <section className="section">
      <h3>Son ping ölçümü</h3>
      <p>
        Bu görünüm gerçek zamanlı izleme değildir; kullanıcı tarafından başlatılmış
        kalıcı ping ölçümlerini gösterir.
      </p>

      {history.isPending && <p role="status">Ping geçmişi yükleniyor…</p>}

      {history.isError && (
        <StatusMessage tone="error" title={ERROR_TITLE} headingLevel={4}>
          <p>
            Kayıtlı ölçümler okunamadı. Bu, daha önce alınmış ölçümlerin silindiği
            anlamına gelmez; yeniden deneyebilirsiniz.
          </p>
          <button type="button" onClick={() => void history.refetch()}>
            Yeniden dene
          </button>
        </StatusMessage>
      )}

      {history.isSuccess && <PingHistoryBody items={history.data.items} />}
    </section>
  );
}

/**
 * Geçmişin dolu ve boş hâli.
 *
 * `items` sunucuda en yeni ölçüm başta olacak biçimde sıralanır; istemci
 * yeniden sıralamaz ve kırpmaz. Böylece ekrandaki sıra, sunucunun kalıcı
 * kayıtlarındaki sıranın kendisidir.
 */
function PingHistoryBody({ items }: { items: PingHistoryItem[] }) {
  const latest = items[0];

  if (latest === undefined) {
    return <p className="muted">Henüz kaydedilmiş bir ping ölçümü yok.</p>;
  }

  return (
    <>
      <ul className="ping-history-meta">
        <li>
          Son ölçüm:{" "}
          <time dateTime={latest.finished_at}>{formatDateTime(latest.finished_at)}</time>
        </li>
        <li>
          Durum: <PingRunStatusBadge status={latest.status} />
        </li>
        <li>Çıkış kodu: {returnCodeText(latest.return_code)}</li>
      </ul>

      {/*
       * Sayaçlar sıfır olsa bile gösterilir: "erişilemeyen host yok" bilgisi,
       * satırın hiç bulunmamasından farklıdır ve kullanıcı için anlamlıdır.
       */}
      <dl className="ping-history-summary">
        {SUMMARY_FIELDS.map((field) => (
          <div className="ping-history-summary__card" key={field.key}>
            <dt className="ping-history-summary__label">{field.label}</dt>
            <dd className="ping-history-summary__value">{latest.summary[field.key]}</dd>
          </div>
        ))}
      </dl>

      <h4>Son ölçümler</h4>
      <div className="table-wrapper">
        <table className="table">
          <caption className="visually-hidden">
            Kaydedilmiş son ping ölçümleri, en yenisi başta
          </caption>
          <thead>
            <tr>
              <th scope="col">Bitiş zamanı</th>
              <th scope="col">Durum</th>
              <th scope="col">Toplam</th>
              <th scope="col">Erişilebilir</th>
              <th scope="col">Erişilemiyor</th>
              <th scope="col">Başarısız</th>
              <th scope="col">Sonuç alınamadı</th>
              <th scope="col">Çıkış kodu</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              // `job_id` yalnızca liste kimliğidir; ekranda gösterilmez.
              <tr key={item.job_id}>
                <th scope="row">
                  <time dateTime={item.finished_at}>{formatDateTime(item.finished_at)}</time>
                </th>
                <td>
                  <PingRunStatusBadge status={item.status} />
                </td>
                <td>{item.summary.total}</td>
                <td>{item.summary.reachable}</td>
                <td>{item.summary.unreachable}</td>
                <td>{item.summary.failed}</td>
                <td>{item.summary.no_result}</td>
                <td>{returnCodeText(item.return_code)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
