import { StatusMessage } from "../../../components/StatusMessage";
import type { InventoryGroup } from "../types";

interface InventoryGroupListProps {
  groups: InventoryGroup[];
}

/**
 * Inventory gruplarını ve her grubun etkin host listesini gösterir.
 *
 * Listedeki host'lar alt gruplardan gelenleri de içerir: backend grup üyeliğini
 * geçişli hesaplar. Bu yüzden `all` grubu tipik olarak bütün host'ları taşır ve
 * bu bir tekrar değil, Ansible'ın gerçek davranışıdır.
 */
export function InventoryGroupList({ groups }: InventoryGroupListProps) {
  if (groups.length === 0) {
    return (
      <StatusMessage tone="info" title="Bu inventory'de grup yok">
        <p>Dosya ayrıştırıldı fakat tanımlı bir grup bulunmuyor.</p>
      </StatusMessage>
    );
  }

  return (
    <div className="table-wrapper">
      <table className="table">
        <caption className="visually-hidden">Gruplar ve host'ları</caption>
        <thead>
          <tr>
            <th scope="col">Grup</th>
            <th scope="col">Host sayısı</th>
            <th scope="col">Host'lar</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <tr key={group.name}>
              <th scope="row">
                <code>{group.name}</code>
              </th>
              <td>{group.hosts.length}</td>
              <td>
                {group.hosts.length === 0 ? (
                  <span className="muted">Bu grupta host yok</span>
                ) : (
                  group.hosts.join(", ")
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
