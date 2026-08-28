import { StatusMessage } from "../../../components/StatusMessage";
import { formatDateTime } from "../../../lib/format";
import type { PingPlan } from "../types";

interface PingPlanPanelProps {
  plan: PingPlan;
  /** Onayın son geçerlilik anı (ISO). */
  expiresAt: string;
}

/**
 * Kullanıcının onaylayacağı ping planı.
 *
 * Yalnızca planın taşıdığı güvenli alanlar gösterilir. Adres, port, kullanıcı,
 * private key yolu ve diğer host değişkenleri backend tarafından bilinçli
 * olarak **verilmez** (GUVENLIK.md bölüm 3); arayüz de bunları başka bir
 * kaynaktan tamamlamaya çalışmaz.
 *
 * Metin mutlak güvence vermez: ping uzak hostta geçici modül dosyası ve süreç
 * oluşturur, yani gerçek execution'dır (ADR-018 Karar 1).
 */
export function PingPlanPanel({ plan, expiresAt }: PingPlanPanelProps) {
  return (
    <div className="panel">
      <dl>
        <dt>Inventory</dt>
        <dd>{plan.inventory.name}</dd>

        <dt>Bağlam</dt>
        <dd>{describeBinding(plan)}</dd>

        <dt>İşlem</dt>
        <dd>
          Ansible ping — <code>{plan.operation}</code>
        </dd>

        <dt>Etkisi</dt>
        <dd>{plan.operation_effect}</dd>

        <dt>Limit</dt>
        <dd>
          {plan.limit === null ? (
            "Tüm inventory (limit verilmedi)"
          ) : (
            <code>{plan.limit}</code>
          )}
        </dd>

        <dt>Hedef host sayısı</dt>
        <dd>{plan.host_count}</dd>

        <dt>Bağlantı</dt>
        <dd>
          <code>{plan.connection}</code> — hedeflere SSH ile bağlanılır.
        </dd>

        <dt>Host anahtarı politikası</dt>
        <dd>{describeHostKeyPolicy(plan)}</dd>

        <dt>Yetki yükseltme (become)</dt>
        <dd>
          {plan.become
            ? "Kullanılıyor"
            : "Kullanılmıyor — uzak hostta sudo/become istenmez."}
        </dd>

        <dt>Onayın son geçerlilik anı</dt>
        <dd>{formatDateTime(expiresAt)}</dd>
      </dl>

      {/*
       * Liste bir başlıkla değil, görünür bir etiketle bağlanır: bu panel zaten
       * bir h4 ("Onay bekleyen plan") altındadır ve ikinci bir h4 başlık
       * düzeyini yanlış gösterirdi.
       */}
      <p className="plan__hosts-label" id="ping-plan-hosts-label">
        Hedeflenen host'lar
      </p>
      {plan.hosts.length === 0 ? (
        <p className="muted">Planda gösterilecek host adı yok.</p>
      ) : (
        <ul className="host-names" aria-labelledby="ping-plan-hosts-label">
          {plan.hosts.map((host) => (
            <li key={host}>
              <code>{host}</code>
            </li>
          ))}
        </ul>
      )}

      {plan.hosts_truncated && (
        <p className="muted">
          Liste kısaltıldı: yukarıda {plan.hosts.length} host adı görünüyor, ping{" "}
          {plan.host_count} host üzerinde çalışacak. Hedef sayısı listeden bağımsız
          olarak kesindir.
        </p>
      )}

      {plan.host_key_policy === "accept_new" && (
        <StatusMessage
          tone="warning"
          headingLevel={4}
          title="İlk görülen host anahtarı sorgulanmadan kabul edilecek"
        >
          <p>
            Sunucu <code>accept_new</code> politikasıyla çalışıyor. Daha önce
            görülmemiş bir host'un anahtarı doğrulama istenmeden kaydedilir (TOFU —
            trust on first use). Bu pencerede araya giren bir taraf kendini hedef host
            gibi tanıtabilir. Bilinen bir anahtarın değişmesi yine reddedilir.
          </p>
        </StatusMessage>
      )}

      {plan.become && (
        <StatusMessage tone="warning" headingLevel={4} title="Bu plan yetki yükseltme içeriyor">
          <p>
            Plan <code>become</code> kullanıldığını bildiriyor. Onaylamadan önce bunun
            beklediğiniz davranış olduğunu doğrulayın.
          </p>
        </StatusMessage>
      )}
    </div>
  );
}

function describeBinding(plan: PingPlan): string {
  if (plan.inventory.binding === "project") {
    const name = plan.inventory.project_name;
    const id = plan.inventory.project_id;
    const label = name ?? (id === null ? "adı bilinmiyor" : `#${id}`);
    return `Bir project'e bağlı (${label})`;
  }
  return "Bağımsız (bir project'e bağlı değil)";
}

function describeHostKeyPolicy(plan: PingPlan): string {
  if (plan.host_key_policy === "strict") {
    return (
      "Katı (strict) — sunucudaki known_hosts dosyasında kayıtlı olmayan ya da " +
      "değişmiş bir host anahtarı reddedilir ve bağlantı kurulmaz."
    );
  }
  return (
    "İlk kullanımda kabul (accept_new) — daha önce görülmemiş bir host anahtarı " +
    "sorgulanmadan kaydedilir."
  );
}
