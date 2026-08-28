import { EXECUTION_MODE_LABELS } from "../../../lib/executionMode";
import { formatBytes, formatDateTime } from "../../../lib/format";
import type { ExecutionPlan } from "../types";

interface ExecutionPlanPanelProps {
  plan: ExecutionPlan;
}

/**
 * Check-mode execution planının özeti.
 *
 * Yalnızca planın taşıdığı alanlar gösterilir. Sunucudaki mutlak yollar,
 * hostvar'lar, bağlantı adresleri ve private key bilgileri backend tarafından
 * bilinçli olarak **verilmez** (GUVENLIK.md bölüm 3); arayüz de bunları başka
 * bir kaynaktan tamamlamaya çalışmaz — inventory kaydının `path` alanı burada
 * kullanılmaz.
 *
 * Panel bir onay yüzeyi **değildir**: plan çalıştırılabilir bir izin taşımaz ve
 * bu dilimde çalıştırma yolu hiç yoktur.
 *
 * Check mode kullanıcıya bir güvenlik güvencesi olarak **anlatılmaz**: R0
 * ölçümünde `check_mode: false` taşıyan bir task'ın check altında gerçekten
 * çalıştığı görülmüştür. Metin bu yüzden yalnız planlanan modu söyler ve
 * güvenceyi önizlemenin hiçbir şey çalıştırmamasına dayandırır.
 *
 * `normal` mode (R1-V3H2B) tam tersi yönde dürüsttür: metin, bu modun
 * hedefte gerçek değişiklik uygulayacağını açıkça söyler ve bunu bir uyarı
 * gibi değil düz bir gerçek olarak sunar.
 */
export function ExecutionPlanPanel({ plan }: ExecutionPlanPanelProps) {
  return (
    <div className="panel">
      <dl>
        <dt>Project</dt>
        <dd>{plan.project.name}</dd>

        <dt>Inventory</dt>
        <dd>
          {plan.inventory.name}
          {plan.inventory.binding === "project" && " (bu project'e bağlı)"}
        </dd>

        <dt>Playbook</dt>
        <dd>
          <code>{plan.playbook.path}</code> — {formatBytes(plan.playbook.size_bytes)}, son
          değişiklik {formatDateTime(plan.playbook.modified_at)}
        </dd>

        <dt>Çalıştırma biçimi</dt>
        <dd>
          <span>
            Planlanan mod: <code>{plan.mode}</code> — {EXECUTION_MODE_LABELS[plan.mode]}.
          </span>{" "}
          <span className="muted">
            {plan.mode === "check"
              ? "Check mode tek başına güvenlik veya değişiklik yapılmayacağı garantisi değildir. Bu önizleme hiçbir playbook çalıştırmaz."
              : "Normal mode hedefte gerçek değişiklik uygular: dosya, paket veya servis durumu değişebilir. Bu önizleme henüz hiçbir playbook çalıştırmaz."}
          </span>
        </dd>

        <dt>Bağlantı</dt>
        <dd>
          <span>
            Planlanan bağlantı: <code>{plan.connection}</code>.
          </span>{" "}
          <span className="muted">Bu önizleme hedeflere bağlantı kurmaz.</span>
        </dd>

        <dt>Hedef host sayısı</dt>
        <dd>
          {plan.host_count}
          {plan.hosts_truncated &&
            ` (aşağıda ilk ${plan.hosts.length} tanesi listelendi)`}
        </dd>

        <dt>Limit</dt>
        <dd>
          {plan.limit === null
            ? "Yok — inventory'nin tamamı hedefleniyor. Bu dilimde limit girilemez."
            : plan.limit}
        </dd>

        <dt>Etiketler (tags / skip_tags)</dt>
        <dd>
          {plan.tags === null && plan.skip_tags === null
            ? "Yok — bu dilimde etiket seçilemez."
            : `${plan.tags ?? "-"} / ${plan.skip_tags ?? "-"}`}
        </dd>

        <dt>Yetki yükseltme (become)</dt>
        <dd>
          <span>
            CLI seviyesinde become: <code>{plan.become ? "ekleniyor" : "eklenmiyor"}</code>.
          </span>{" "}
          <span className="muted">
            {plan.become
              ? "Uzak hostta yetki yükseltme istenecek."
              : "Güvenilir bir playbook kendi task'larında yine de become kullanabilir; bu yalnızca uygulamanın CLI'ye --become eklemediğini gösterir."}
          </span>
        </dd>

        <dt>Host anahtarı politikası</dt>
        <dd>
          <code>{plan.host_key_policy}</code>
        </dd>

        <dt>Planın üretilme zamanı</dt>
        <dd>{formatDateTime(plan.generated_at)}</dd>
      </dl>

      <p className="plan__hosts-label" id="execution-plan-hosts-label">
        Hedeflenen host'lar
      </p>
      {plan.hosts.length === 0 ? (
        <p className="muted">Planda gösterilecek host adı yok.</p>
      ) : (
        <ul className="host-names" aria-labelledby="execution-plan-hosts-label">
          {plan.hosts.map((host) => (
            <li key={host}>
              <code>{host}</code>
            </li>
          ))}
        </ul>
      )}

      {plan.hosts_truncated && (
        <p className="muted">
          Liste kısaltıldı: yukarıda {plan.hosts.length} host adı görünüyor, plan{" "}
          {plan.host_count} host'u kapsıyor. Hedef sayısı listeden bağımsız olarak
          kesindir.
        </p>
      )}
    </div>
  );
}
