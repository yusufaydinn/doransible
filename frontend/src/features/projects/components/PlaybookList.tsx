import { StatusMessage } from "../../../components/StatusMessage";
import { formatBytes, formatDateTime } from "../../../lib/format";
import type { PlaybookListResponse } from "../types";

interface PlaybookListProps {
  result: PlaybookListResponse;
}

/**
 * Keşfedilen playbook'ları ve keşfin sınırlarını gösterir.
 *
 * Yollar API'nin döndürdüğü hâliyle, project köküne **göreli** basılır.
 * Sunucudaki mutlak yol ile birleştirilmez: mutlak yol arayüzde hiç bulunmaz
 * ve uydurulan bir yol kullanıcıyı yanıltırdı.
 */
export function PlaybookList({ result }: PlaybookListProps) {
  if (result.playbooks.length === 0) {
    return (
      <>
        <StatusMessage tone="info" title="Bu project'te playbook bulunamadı">
          <p>
            Dizin tarandı fakat playbook'a benzeyen bir <code>.yml</code> veya{" "}
            <code>.yaml</code> dosyası görülmedi. Role içindeki task dosyaları ve
            değişken dizinleri bilinçli olarak listelenmez.
          </p>
        </StatusMessage>
        <ScanNotes result={result} />
      </>
    );
  }

  return (
    <>
      <div className="table-wrapper">
        <table className="table">
          <caption className="visually-hidden">Keşfedilen playbook'lar</caption>
          <thead>
            <tr>
              <th scope="col">Project içindeki yol</th>
              <th scope="col">Boyut</th>
              <th scope="col">Değiştirilme</th>
            </tr>
          </thead>
          <tbody>
            {result.playbooks.map((playbook) => (
              <tr key={playbook.path}>
                <th scope="row">
                  <code>{playbook.path}</code>
                </th>
                <td>{formatBytes(playbook.size_bytes)}</td>
                <td>{formatDateTime(playbook.modified_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="muted">
        {result.playbooks.length} playbook bulundu. Tarama zamanı:{" "}
        {formatDateTime(result.scanned_at)}
      </p>

      <ScanNotes result={result} />
    </>
  );
}

/** Kırpma ve okunamayan girdi uyarıları. */
function ScanNotes({ result }: PlaybookListProps) {
  const skippedFiles = result.skipped_unreadable_files;
  const skippedDirectories = result.skipped_unreadable_directories;

  return (
    <>
      {result.truncated && (
        <StatusMessage tone="warning" title="Liste kırpıldı">
          <p>
            Project çok fazla dosya içerdiği için tarama sınırına ulaşıldı ve liste eksik.
            Görünmeyen playbook'lar olabilir. Project kökünü daha dar bir dizine almak
            veya controller'daki tarama sınırlarını yükseltmek gerekir.
          </p>
        </StatusMessage>
      )}

      {(skippedFiles > 0 || skippedDirectories > 0) && (
        <StatusMessage tone="warning" title="Bazı girdiler okunamadı">
          <p>
            Tarama tamamlandı fakat her şey incelenemedi. Genellikle sebep dosya
            izinleridir; controller'daki izinleri kontrol edin.
          </p>
          <ul>
            {skippedFiles > 0 && (
              <li>{skippedFiles} dosya okunamadı ve listeye alınmadı.</li>
            )}
            {skippedDirectories > 0 && (
              <li>{skippedDirectories} alt dizin listelenemedi ve taranamadı.</li>
            )}
          </ul>
        </StatusMessage>
      )}
    </>
  );
}
