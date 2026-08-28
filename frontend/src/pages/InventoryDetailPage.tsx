import { Link, useParams } from "react-router-dom";

import { StatusMessage } from "../components/StatusMessage";
import { InventoryErrorPanel } from "../features/inventories/components/InventoryErrorPanel";
import { InventoryGroupList } from "../features/inventories/components/InventoryGroupList";
import { InventoryHostList } from "../features/inventories/components/InventoryHostList";
import { PingHistorySection } from "../features/inventories/components/PingHistorySection";
import { PingSection } from "../features/inventories/components/PingSection";
import { useInventory, useInventoryHosts } from "../features/inventories/hooks";
import { SOURCE_TYPE_LABELS } from "../features/inventories/labels";
import { ApiError } from "../lib/apiClient";
import { formatDateTime } from "../lib/format";
import type { Inventory } from "../features/inventories/types";

export function InventoryDetailPage() {
  const { inventoryId } = useParams();
  const parsedId = parseInventoryId(inventoryId);

  if (parsedId === null) {
    return <InventoryNotFound />;
  }

  return <InventoryDetail inventoryId={parsedId} />;
}

function InventoryDetail({ inventoryId }: { inventoryId: number }) {
  const inventory = useInventory(inventoryId);

  if (inventory.isPending) {
    return <p role="status">Inventory bilgileri yükleniyor…</p>;
  }

  if (inventory.isError) {
    if (inventory.error instanceof ApiError && inventory.error.status === 404) {
      return <InventoryNotFound />;
    }
    return (
      <InventoryErrorPanel
        error={inventory.error}
        onRetry={() => void inventory.refetch()}
        headingLevel={2}
      />
    );
  }

  return (
    <section>
      <div className="page-header">
        <h2>{inventory.data.name}</h2>
        <Link to="/inventories">Listeye dön</Link>
      </div>

      <InventorySummary inventory={inventory.data} />

      <NextStepCallout inventory={inventory.data} />

      <ContentsSection inventoryId={inventory.data.id} />

      {/*
       * Geçmiş bölümü de içerik sorgusundan bağımsızdır ve **salt okunurdur**:
       * hiçbir ölçüm başlatmaz, hiçbir yoklama kurmaz. Ping formunun hemen
       * üstünde durur çünkü kullanıcının ilk sorusu "en son ne oldu"dur;
       * yeni bir ölçüm başlatmak ancak bundan sonra gelen bir karardır.
       *
       * `key` verilmez: bileşenin kimliğe bağlı yerel state'i yoktur, veri
       * yalnızca kimliği taşıyan query key'den gelir.
       */}
      <PingHistorySection inventoryId={inventory.data.id} />

      {/*
       * Ping bölümü içerik sorgusundan **bağımsızdır**: dosya ayrıştırılamasa
       * bile erişilebilirlik testi denenebilir. Bu yüzden `ContentsSection`
       * içine değil, metadata dalının kardeşi olarak yerleştirilir.
       *
       * `key` inventory kimliğidir: kimlik değiştiğinde bileşen yeniden
       * kurulur. Böylece bir inventory için oluşturulmuş plan, onay token'ı
       * veya sonuç bir sonraki inventory ekranına taşınmaz. Eski instance'ın
       * unmount'u token'ı temizler ve canlılık bayrağını kapatır; o ekrandan
       * geç dönen bir istek yeni ekrana state veya token yazamaz.
       */}
      <PingSection key={inventory.data.id} inventory={inventory.data} />
    </section>
  );
}

function InventorySummary({ inventory }: { inventory: Inventory }) {
  return (
    <dl className="details">
      <dt>Biçim</dt>
      <dd>{SOURCE_TYPE_LABELS[inventory.source_type] ?? inventory.source_type}</dd>

      <dt>Controller yolu</dt>
      <dd>
        <code>{inventory.path}</code>
      </dd>

      <dt>Bağlı project</dt>
      <dd>
        {inventory.project_id === null ? (
          "Bağımsız (bir project'e bağlı değil)"
        ) : (
          <Link to={`/projects/${inventory.project_id}`}>
            Project #{inventory.project_id}
          </Link>
        )}
      </dd>

      <dt>Oluşturulma</dt>
      <dd>{formatDateTime(inventory.created_at)}</dd>

      <dt>Güncellenme</dt>
      <dd>{formatDateTime(inventory.updated_at)}</dd>
    </dl>
  );
}

/**
 * Sonraki adımı gösteren kısa yönlendirme.
 *
 * Bağlı bir inventory doğrudan o project'in çalıştırma planına yönlendirir.
 * Bağımsız (standalone) bir inventory'nin plan/çalıştırma akışında **kullanılamadığı**
 * açıkça belirtilir; aksi hâlde kullanıcı "neden bu inventory plan formunda
 * görünmüyor" sorusuyla baş başa kalır (bkz. `ExecutionPlanSection`'daki
 * `inventory_not_linked_to_project` kısıtı).
 */
function NextStepCallout({ inventory }: { inventory: Inventory }) {
  if (inventory.project_id === null) {
    return (
      <div className="callout" role="note">
        Bu inventory <strong>bağımsız</strong>: herhangi bir project'e bağlı değil.
        Playbook çalıştırma planında yalnızca bir project'e bağlı inventory'ler
        seçilebilir; bu kaydı bir çalıştırmada kullanmak için project'e bağlı
        yeni bir kayıt oluşturmanız gerekir.
      </div>
    );
  }

  return (
    <div className="callout" role="note">
      Bu inventory bir project'e bağlı. Bu inventory ile playbook çalıştırmak için{" "}
      <Link to={`/projects/${inventory.project_id}`}>project'in çalıştırma planına gidin</Link>.
    </div>
  );
}

/**
 * Grup ve host bölümü.
 *
 * İçerik okuma sunucuda ayrı bir süreç çalıştırır; bu yüzden kendi yükleniyor
 * ve hata durumları vardır ve metadata görünümünü düşürmez. Dosya
 * ayrıştırılamasa bile kaydın kendisi görünür kalır.
 */
function ContentsSection({ inventoryId }: { inventoryId: number }) {
  const contents = useInventoryHosts(inventoryId);

  if (contents.isPending) {
    return (
      <section className="section">
        <h3>İçerik</h3>
        <p role="status">Inventory içeriği okunuyor…</p>
      </section>
    );
  }

  if (contents.isError) {
    return (
      <section className="section">
        <h3>İçerik</h3>
        <InventoryErrorPanel
          error={contents.error}
          onRetry={() => void contents.refetch()}
        />
      </section>
    );
  }

  return (
    <>
      <section className="section">
        <h3>Gruplar</h3>
        <InventoryGroupList groups={contents.data.groups} />
      </section>

      <section className="section">
        <h3>Host'lar</h3>
        <InventoryHostList hosts={contents.data.hosts} />
        <p className="muted">
          Host değişkenlerinde secret görünümlü değerler controller'da maskelenir ve
          arayüze yalnızca maskeli hâliyle gelir.
        </p>
      </section>
    </>
  );
}

function InventoryNotFound() {
  return (
    <StatusMessage tone="error" title="Inventory bulunamadı" headingLevel={2}>
      <p>
        Bu kimlikle bir inventory kaydı yok. Adres yanlış yazılmış veya kayıt hiç
        oluşturulmamış olabilir.
      </p>
      <p>
        <Link to="/inventories">Inventory listesine dön</Link>
      </p>
    </StatusMessage>
  );
}

/** URL parametresini pozitif tam sayıya çevirir; aksi hâlde `null`. */
function parseInventoryId(value: string | undefined): number | null {
  if (value === undefined || !/^\d+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}
