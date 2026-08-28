import { Link } from "react-router-dom";

import { StatusMessage } from "../components/StatusMessage";
import { InventoryErrorPanel } from "../features/inventories/components/InventoryErrorPanel";
import { InventoryList } from "../features/inventories/components/InventoryList";
import { useInventories } from "../features/inventories/hooks";

export function InventoryListPage() {
  const inventories = useInventories();

  return (
    <section>
      <div className="page-header">
        <h2>Inventory'ler</h2>
        <Link to="/inventories/new">Yeni inventory kaydet</Link>
      </div>

      <p className="muted">
        Inventory, controller'daki bir INI veya YAML dosyasını temsil eder. Uygulama bu
        dosyayı kopyalamaz; yalnızca kaydını tutar ve içeriğini istendiğinde okur.
      </p>

      {inventories.isPending && <p role="status">Inventory'ler yükleniyor…</p>}

      {inventories.isError && (
        <InventoryErrorPanel
          error={inventories.error}
          onRetry={() => void inventories.refetch()}
        />
      )}

      {inventories.isSuccess &&
        (inventories.data.length === 0 ? (
          <StatusMessage tone="info" title="Henüz inventory kaydı yok">
            <p>
              Kayıtlı bir inventory bulunmuyor. Inventory dosyanız controller'da izin
              verilen bir dizinde durmalı; kayıt eklendikten sonra host ve grupları
              buradan inceleyebilirsiniz.
            </p>
            <p>
              <Link to="/inventories/new">Yeni inventory kaydet</Link>
            </p>
          </StatusMessage>
        ) : (
          <InventoryList inventories={inventories.data} />
        ))}
    </section>
  );
}
