import { StatusMessage } from "../../../components/StatusMessage";
import { formatDateTime } from "../../../lib/format";
import type { PingHostStatus, PingRunResponse } from "../types";

interface PingResultPanelProps {
  run: PingRunResponse;
  /** Kullanıcıyı boş forma döndürür. **Hiçbir istek başlatmaz.** */
  onStartOver?: () => void;
}

/**
 * Host durumlarının kullanıcıya gösterilen karşılıkları.
 *
 * Anlam yalnızca renkle verilmez: her satırda durumun görünür metin karşılığı
 * bulunur; rozet ve satır rengi bunu pekiştiren ek bir katmandır.
 */
const HOST_STATUS_LABELS: Record<PingHostStatus, string> = {
  reachable: "Erişilebilir",
  unreachable: "Erişilemiyor",
  failed: "Başarısız",
  no_result: "Sonuç alınamadı",
};

/**
 * Bir host satırının, sonucu tabloya bakmadan da ayırt edilmesini sağlayan
 * semantik satır sınıfı.
 *
 * `unreachable` ve `failed` aynı "problem" sınıfını paylaşır: ikisi de kayda
 * değer bir sonuçtur ve karışık bir çalıştırmada (ör. 4 erişilebilir + 1
 * erişilemeyen) satır rengiyle de öne çıkmalıdır. `no_result` ayrı bir
 * sınıftadır: beklenen host için doğrulanabilir bir sonuç görülmediğini
 * gösterir — kök nedeni sınıflandırmaz (arıza da olabilir, tamamlanmamış bir
 * çalıştırma da). `reachable` için ek bir sınıf **yoktur** — varsayılan satır
 * yeterlidir.
 */
function hostRowClassName(status: PingHostStatus): string | undefined {
  switch (status) {
    case "unreachable":
    case "failed":
      return "table-row--problem";
    case "no_result":
      return "table-row--unknown";
    default:
      return undefined;
  }
}

/**
 * Host durumunun renkli rozeti.
 *
 * Metin `HOST_STATUS_LABELS`'tan **birebir** aynı gelir: rozet yalnızca
 * görsel bir kapsayıcıdır, metni değiştirmez. `status` derleme zamanında
 * kapalı bir birleşim türü olsa da yanıt çalışma zamanında doğrulanmaz; bu
 * yüzden bilinmeyen bir değer sessizce "nötr" rozete ve ham metne düşer,
 * `badge--undefined` gibi geçersiz bir sınıf üretmez.
 */
function PingHostStatusBadge({ status }: { status: PingHostStatus }) {
  const label = HOST_STATUS_LABELS[status];
  const modifier = label === undefined ? "neutral" : status;
  return <span className={`badge badge--${modifier}`}>{label ?? status}</span>;
}

/**
 * Tamamlanmış bir ping işinin sonucu.
 *
 * `status === "failed"` bir **API hatası değildir**: iş çalıştı, terminal duruma
 * geçti ve sonucu kaydedildi (ADR-019 Karar 7). Bu yüzden hata kutusu yerine
 * uyarı kutusu kullanılır ve özet ile host tablosu her iki durumda da gösterilir.
 *
 * Host mesajları backend'de redaction, path maskeleme ve bağlantı değeri
 * maskelemesinden geçmiştir; arayüz onları yalnızca metin olarak basar.
 */
export function PingResultPanel({ run, onStartOver }: PingResultPanelProps) {
  const isSuccessful = run.status === "successful";

  return (
    <div>
      {isSuccessful ? (
        <StatusMessage
          tone="success"
          headingLevel={4}
          title="Ping tamamlandı: tüm host'lar erişilebilir"
        >
          <p>
            İş başarıyla bitti. Bir işin başarılı sayılması için hem çıkış kodunun 0
            olması hem de beklenen bütün host'ların erişilebilir olması gerekir.
          </p>
        </StatusMessage>
      ) : (
        <StatusMessage
          tone="warning"
          headingLevel={4}
          title="Ping tamamlandı: bazı host kontrolleri başarısız"
        >
          <p>
            İş çalıştı ve sonucu kaydedildi; ancak en az bir host erişilemedi, hata
            verdi ya da hiç sonuç döndürmedi. Kök neden bu sonuçtan tek başına
            sınıflandırılamaz — hangi host'ların erişilemedi, başarısız ya da
            sonuçsuz olduğu aşağıdaki özette ve tabloda ayrı ayrı gösterilir.
          </p>
        </StatusMessage>
      )}

      <dl className="details">
        <dt>İş kimliği</dt>
        <dd>
          <code>{run.job_id}</code>
        </dd>

        <dt>Durum</dt>
        <dd>{isSuccessful ? "Başarılı" : "Başarısız"}</dd>

        <dt>Çıkış kodu</dt>
        <dd>{run.return_code === null ? "Bilinmiyor" : run.return_code}</dd>

        <dt>Limit</dt>
        <dd>
          {run.limit === null ? "Tüm inventory (limit verilmedi)" : <code>{run.limit}</code>}
        </dd>

        <dt>Başlangıç</dt>
        <dd>{formatDateTime(run.started_at)}</dd>

        <dt>Bitiş</dt>
        <dd>{formatDateTime(run.finished_at)}</dd>
      </dl>

      <h4>Özet</h4>
      <dl className="details">
        <dt>Toplam host</dt>
        <dd>{run.summary.total}</dd>

        <dt>Erişilebilir</dt>
        <dd>{run.summary.reachable}</dd>

        <dt>Erişilemiyor</dt>
        <dd>{run.summary.unreachable}</dd>

        <dt>Başarısız</dt>
        <dd>{run.summary.failed}</dd>

        <dt>Sonuç alınamadı</dt>
        <dd>{run.summary.no_result}</dd>
      </dl>

      <h4>Host sonuçları</h4>
      {run.hosts.length === 0 ? (
        <p className="muted">Sonuçta host kaydı bulunmuyor.</p>
      ) : (
        <table className="table">
          <caption className="visually-hidden">Host bazlı ping sonuçları</caption>
          <thead>
            <tr>
              <th scope="col">Host</th>
              <th scope="col">Durum</th>
              <th scope="col">Açıklama</th>
            </tr>
          </thead>
          <tbody>
            {run.hosts.map((host) => (
              <tr key={host.name} className={hostRowClassName(host.status)}>
                <th scope="row">
                  <code>{host.name}</code>
                </th>
                <td>
                  <PingHostStatusBadge status={host.status} />
                </td>
                <td>
                  {host.message === null ? (
                    <span className="muted">—</span>
                  ) : (
                    host.message
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {onStartOver && (
        <p>
          <button type="button" onClick={onStartOver}>
            Yeni önizleme oluştur
          </button>
        </p>
      )}
    </div>
  );
}
