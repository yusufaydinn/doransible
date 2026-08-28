import { StatusMessage } from "../../../components/StatusMessage";
import type { InventoryHost } from "../types";
import { HostVariableList } from "./HostVariableList";

interface InventoryHostListProps {
  hosts: InventoryHost[];
}

/**
 * Inventory'deki host'ları, grup üyelikleriyle ve değişkenleriyle listeler.
 *
 * Grup üyeliği backend tarafından `children` kenarları izlenerek **geçişli**
 * hesaplanır; arayüz bu listeyi yeniden hesaplamaz, geldiği sırayla gösterir
 * (MIMARI.md bölüm 7).
 */
export function InventoryHostList({ hosts }: InventoryHostListProps) {
  if (hosts.length === 0) {
    return (
      <StatusMessage tone="info" title="Bu inventory'de host yok">
        <p>
          Dosya ayrıştırıldı fakat tanımlı bir host bulunmuyor. Host satırlarının
          doğru grup altında yazıldığını kontrol edin.
        </p>
      </StatusMessage>
    );
  }

  return (
    <div className="table-wrapper">
      <table className="table">
        <caption className="visually-hidden">Host'lar, grupları ve değişkenleri</caption>
        <thead>
          <tr>
            <th scope="col">Host</th>
            <th scope="col">Gruplar</th>
            <th scope="col">Değişkenler</th>
          </tr>
        </thead>
        <tbody>
          {hosts.map((host) => (
            <tr key={host.name}>
              <th scope="row">
                <code>{host.name}</code>
              </th>
              <td>
                {host.groups.length === 0 ? (
                  <span className="muted">—</span>
                ) : (
                  host.groups.join(", ")
                )}
              </td>
              <td>
                <HostVariableList variables={host.variables} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
