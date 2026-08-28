import { useQuery } from "@tanstack/react-query";

import { FlowSteps, type FlowStep } from "../components/FlowSteps";
import { fetchHealth } from "../features/health/api";
import { API_BASE_URL, ApiError } from "../lib/apiClient";

/**
 * Ana akış: Project → Inventory → Playbook çalıştırma → Sonuç.
 *
 * "Project'lere git" bağlantısı bilinçli olarak birebir korunur
 * (`routing.test.tsx`): ilk adımın kendi CTA'sıdır.
 */
const FLOW_STEPS: FlowStep[] = [
  {
    title: "Project ekleyin",
    description:
      "Project, controller'daki bir Ansible dizinini temsil eder. Dizin kopyalanmaz; " +
      "yalnızca kaydı tutulur ve içindeki playbook'lar otomatik keşfedilir.",
    to: "/projects",
    linkLabel: "Project'lere git",
  },
  {
    title: "Inventory bağlayın",
    description:
      "Inventory, controller'da zaten var olan bir INI/YAML dosyasının kaydıdır. Bir " +
      "project'e bağlandığında o project'in çalıştırma planında seçilebilir olur.",
    to: "/inventories",
    linkLabel: "Inventory'lere git",
  },
  {
    title: "Playbook çalıştırın",
    description:
      "Project detayında bir playbook ve inventory seçin; Check (Ansible --check) veya " +
      "Normal modda bir plan oluşturun, gözden geçirin ve yalnızca açıkça onayladıktan " +
      "sonra çalıştırın.",
    to: "/projects",
    linkLabel: "Bir project seçip planlayın",
  },
  {
    title: "Sonucu inceleyin",
    description:
      "Her çalıştırma canlı durumu, host bazlı özeti ve olay kaydını Çalıştırmalar " +
      "sayfasında bırakır.",
    to: "/jobs",
    linkLabel: "Çalıştırmalara git",
  },
];

export function HomePage() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    retry: false,
  });

  return (
    <section>
      <h2>Genel bakış</h2>
      <p>
        DORAnsible, mevcut Ansible otomasyonlarınızı güvenli biçimde çalıştırmanıza
        ve gözlemlemenize yardımcı olur. Aşağıdaki dört adımı sırayla izleyin.
      </p>

      <div className="callout" role="note">
        <strong>Önemli:</strong> DORAnsible controller, backend'in ve Ansible
        süreçlerinin çalıştığı makinedir. Bu uygulamada girilen tüm dosya/dizin yolları{" "}
        <strong>controller'a</strong> aittir; controller ile bu sayfayı açtığınız
        tarayıcı cihazı aynı makineyse bu, kendi bilgisayarınızdaki yollar olabilir.
        Hiçbir dosya yüklenmez, oluşturulmaz veya kopyalanmaz; yalnızca controller'da
        zaten var olan yolların kaydı tutulur.
      </div>

      <FlowSteps steps={FLOW_STEPS} />

      <h3>Sistem durumu</h3>
      <p className="muted">
        API adresi: <code>{API_BASE_URL}</code>
      </p>

      {health.isPending && <p>Kontrol ediliyor…</p>}

      {health.isError && (
        <div className="panel panel--error" role="alert">
          <strong>Backend'e bağlanılamadı.</strong>
          <p>{errorMessage(health.error)}</p>
          <button type="button" onClick={() => void health.refetch()}>
            Tekrar dene
          </button>
        </div>
      )}

      {health.isSuccess && (
        <div className="panel panel--ok">
          <strong>Bağlantı kuruldu.</strong>
          <dl>
            <dt>Durum</dt>
            <dd>{health.data.status}</dd>
            <dt>Uygulama</dt>
            <dd>{health.data.app_name}</dd>
            <dt>Sürüm</dt>
            <dd>{health.data.version}</dd>
            <dt>Ortam</dt>
            <dd>{health.data.environment}</dd>
          </dl>
        </div>
      )}
    </section>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "Bilinmeyen bir hata oluştu.";
}
