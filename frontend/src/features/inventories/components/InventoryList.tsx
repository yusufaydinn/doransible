import { Link } from "react-router-dom";

import { formatDateTime } from "../../../lib/format";
import { SOURCE_TYPE_LABELS } from "../labels";
import type { Inventory } from "../types";

interface InventoryListProps {
  inventories: Inventory[];
}

/**
 * Inventory kayıtlarını tablo olarak listeler.
 *
 * Tablo kullanılmasının nedeni erişilebilirliktir: ekran okuyucu her hücreyi
 * sütun başlığıyla birlikte okur.
 *
 * Yollar API'nin döndürdüğü hâliyle basılır; hiçbir yol parçası arayüzde
 * birleştirilmez.
 */
export function InventoryList({ inventories }: InventoryListProps) {
  return (
    <div className="table-wrapper">
      <table className="table">
        <caption className="visually-hidden">Kayıtlı inventory'ler</caption>
        <thead>
          <tr>
            <th scope="col">Ad</th>
            <th scope="col">Biçim</th>
            <th scope="col">Controller yolu</th>
            <th scope="col">Project</th>
            <th scope="col">Güncellenme</th>
          </tr>
        </thead>
        <tbody>
          {inventories.map((inventory) => (
            <tr key={inventory.id}>
              <th scope="row">
                <Link to={`/inventories/${inventory.id}`}>{inventory.name}</Link>
              </th>
              <td>{SOURCE_TYPE_LABELS[inventory.source_type] ?? inventory.source_type}</td>
              <td>
                <code>{inventory.path}</code>
              </td>
              <td>
                {inventory.project_id === null ? (
                  <span className="muted">Bağımsız</span>
                ) : (
                  <Link to={`/projects/${inventory.project_id}`}>
                    Project #{inventory.project_id}
                  </Link>
                )}
              </td>
              <td>{formatDateTime(inventory.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
