/**
 * Check/normal mode seçimi (R1-V3H2B).
 *
 * Merkez iddialar:
 *
 * - Varsayılan kip `check`'tir; kip erişilebilir bir radio grubuyla seçilir.
 * - Preview gövdesi güncel form seçiminden, prepare gövdesi **önizlenen**
 *   planın kendi alanlarından, launch gövdesi **hazırlanmış** planın kendi
 *   alanlarından kurulur — üçü de `mode` dahil tam olarak dört/üç alan taşır.
 * - Kip değişimi inventory/playbook değişimiyle aynı kuralı izler: önizleme,
 *   hazırlanmış plan, token ve onay senkron olarak düşer.
 * - Check ve normal onay metinleri, risk görünümü ve launch buton metni
 *   birbirinden farklıdır; normal launch onaysız kapalı kalır ve onay
 *   sonrası tek POST üretir.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  executionLaunchResponse,
  executionPlan,
  normalExecutionPlan,
  preparedExecutionPlan,
  preparedNormalExecutionPlan,
} from "../../../test/executionFixtures";
import { activeProject, linkedInventory, playbookResult } from "../../../test/fixtures";
import { pendingJob } from "../../../test/jobFixtures";
import {
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const PLAN_PATH = `/api/projects/${activeProject.id}/execution-plan`;
const PREPARE_PATH = `/api/projects/${activeProject.id}/execution-plans`;
const LAUNCH_PATH = `/api/projects/${activeProject.id}/executions`;
const PLAN_BUTTON = "Planı Oluştur";
const PREPARE_BUTTON = "Onaya Hazırla";
const CHECK_RUN_BUTTON = "Onayla ve Çalıştır";
const NORMAL_RUN_BUTTON = "Onayla ve Gerçek Değişiklikleri Uygula";
const APPROVE_LABEL = /açıkça onaylıyorum/i;

interface PlanRequestBody {
  mode?: string;
  [key: string]: unknown;
}

/**
 * Detay sayfasının bütün isteklerini karşılayan yönlendirici.
 *
 * Önizleme, hazırlama ve launch cevapları istek gövdesindeki `mode`'a göre
 * seçilir: aynı adres check ve normal için de kullanılır (backend de aynı
 * endpoint'i paylaşır).
 */
function responder(): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/playbooks")) {
      return jsonResponse(playbookResult);
    }
    if (request.url.includes("/api/inventories")) {
      return jsonResponse([linkedInventory]);
    }
    if (request.url.endsWith(LAUNCH_PATH)) {
      const body = request.body as PlanRequestBody;
      return jsonResponse(
        {
          ...executionLaunchResponse,
          mode: body?.mode === "normal" ? "normal" : "check",
        },
        201,
      );
    }
    // Hazırlama adresi önizleme adresini kapsadığı için önce sınanır.
    if (request.url.includes("/execution-plans")) {
      const body = request.body as PlanRequestBody;
      return jsonResponse(
        body?.mode === "normal" ? preparedNormalExecutionPlan : preparedExecutionPlan,
        201,
      );
    }
    if (request.url.includes("/execution-plan")) {
      const body = request.body as PlanRequestBody;
      return jsonResponse(body?.mode === "normal" ? normalExecutionPlan : executionPlan);
    }
    if (request.url.endsWith(`/api/jobs/${executionLaunchResponse.job_id}`)) {
      // Terminal ve sonuçsuz bir Job: bu dosya yalnızca launch akışını sınar,
      // Job detay sayfasının kendisini değil (bkz. jobPages.test.tsx).
      return jsonResponse({
        ...pendingJob,
        job_id: executionLaunchResponse.job_id,
        status: "successful",
        has_recorded_result: false,
      });
    }
    return jsonResponse(activeProject);
  };
}

async function planSection(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 3, name: "Çalıştırma planı" });
  return heading.parentElement as HTMLElement;
}

/** Inventory ve playbook seçer; kip mevcut seçimde kalır. */
async function selectInventoryAndPlaybook(
  user: ReturnType<typeof userEvent.setup>,
  section: HTMLElement,
): Promise<void> {
  await user.selectOptions(
    await within(section).findByLabelText("Inventory"),
    String(linkedInventory.id),
  );
  await user.selectOptions(within(section).getByLabelText("Playbook"), "site.yml");
}

/** Kip, inventory ve playbook seçilmiş; önizleme henüz üretilmemiş bölüm. */
async function selectBoth(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  const section = await planSection();
  await selectInventoryAndPlaybook(user, section);
  return section;
}

function requestsTo(requests: RecordedRequest[], path: string): RecordedRequest[] {
  return requests.filter((request) => request.url.endsWith(path));
}

describe("Çalıştırma kipi seçimi", () => {
  it("varsayılan kip check'tir", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const section = await planSection();

    expect(within(section).getByRole("radio", { name: "Check" })).toBeChecked();
    expect(within(section).getByRole("radio", { name: "Normal" })).not.toBeChecked();
  });

  it("kip radio'ları klavyeyle kullanılabilir", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await planSection();

    const normalRadio = within(section).getByRole("radio", { name: "Normal" });
    normalRadio.focus();
    expect(normalRadio).toHaveFocus();
    expect(normalRadio).not.toBeChecked();

    await user.keyboard(" ");
    expect(normalRadio).toBeChecked();
  });

  it("normal seçilince önizleme isteği yalnız üç alanlı ve mode: normal gövdeyle POST eder", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("radio", { name: "Normal" }));

    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    const planRequests = requestsTo(requests, PLAN_PATH);
    expect(planRequests).toHaveLength(1);
    expect(planRequests[0]?.body).toEqual({
      mode: "normal",
      inventory_id: linkedInventory.id,
      playbook_path: "site.yml",
    });
  });

  it("normal seçilince hazırlama isteği mode: normal taşır ve önizlenen plandan kurulur", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("radio", { name: "Normal" }));
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    const prepareRequests = requestsTo(requests, PREPARE_PATH);
    expect(prepareRequests).toHaveLength(1);
    // Gövde form state'inden değil, ekranda gösterilen önizleme planının
    // (`normalExecutionPlan`) kendi alanlarından kurulur.
    expect(prepareRequests[0]?.body).toEqual({
      mode: normalExecutionPlan.mode,
      inventory_id: normalExecutionPlan.inventory.id,
      playbook_path: normalExecutionPlan.playbook.path,
    });
  });

  it("normal seçilince çalıştırma isteği mode: normal taşır ve hazırlanmış plandan kurulur", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("radio", { name: "Normal" }));
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    await user.click(screen.getByRole("button", { name: NORMAL_RUN_BUTTON }));

    await waitFor(() => expect(requestsTo(requests, LAUNCH_PATH)).toHaveLength(1));
    const [request] = requestsTo(requests, LAUNCH_PATH);
    // Gövde formda o an seçili olan kipten değil, hazırlanmış planın kendi
    // alanlarından (`preparedNormalExecutionPlan.plan`) kurulur.
    expect(request?.body).toEqual({
      plan_token: preparedNormalExecutionPlan.plan_token,
      mode: preparedNormalExecutionPlan.plan.mode,
      inventory_id: preparedNormalExecutionPlan.plan.inventory.id,
      playbook_path: preparedNormalExecutionPlan.plan.playbook.path,
    });
  });

  it("kip değişince önizleme ekranda kalmaz", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    await user.click(within(section).getByRole("radio", { name: "Normal" }));

    expect(
      screen.queryByRole("heading", { level: 4, name: "Execution planı" }),
    ).not.toBeInTheDocument();
  });

  it("kip değişince hazırlanmış plan, onay ve token da düşer", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));

    await user.click(within(section).getByRole("radio", { name: "Normal" }));

    expect(
      screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: CHECK_RUN_BUTTON })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: NORMAL_RUN_BUTTON })).not.toBeInTheDocument();
    // Önizleme de düşmüştür: "Onaya Hazırla" düğmesi de kaybolur.
    expect(screen.queryByRole("button", { name: PREPARE_BUTTON })).not.toBeInTheDocument();
  });

  it("check ve normal onay metinleri, risk uyarısı ve launch buton metni birbirinden farklıdır", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    // Check: mevcut dürüst uyarı korunur, ayrıca risk banner'ı yok.
    expect(
      screen.getByText(/Check mode değişiklik yapılmayacağını garanti etmez/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: CHECK_RUN_BUTTON })).toBeInTheDocument();
    expect(screen.queryByText(/Bu bir deneme değildir/)).not.toBeInTheDocument();

    // Normal moda geç: check onaya hazır panel kip değişimiyle düşer.
    await user.click(within(section).getByRole("radio", { name: "Normal" }));
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    const warning = screen.getByRole("alert");
    expect(warning).toHaveTextContent(/gerçek değişiklik/i);
    expect(warning).toHaveTextContent(/dosya/i);
    expect(warning).toHaveTextContent(/paket/i);
    expect(warning).toHaveTextContent(/servis/i);
    expect(warning).toHaveTextContent(/bağlantı kesilebilir/i);
    expect(warning).toHaveTextContent(/kısmi/i);
    expect(warning).toHaveTextContent(/rollback/i);

    expect(
      screen.getByText(/Bu normal mode çalıştırması hedefte gerçek değişiklik uygulayacak/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Check mode değişiklik yapılmayacağını garanti etmez/),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: NORMAL_RUN_BUTTON })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: CHECK_RUN_BUTTON })).not.toBeInTheDocument();
  });

  it("normal launch onaysız kapalı kalır, onay sonrası tek POST üretir", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("radio", { name: "Normal" }));
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    const runButton = screen.getByRole("button", { name: NORMAL_RUN_BUTTON });
    expect(runButton).toBeDisabled();

    await user.click(runButton);
    expect(requestsTo(requests, LAUNCH_PATH)).toHaveLength(0);

    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    expect(runButton).toBeEnabled();

    await user.click(runButton);
    await user.click(runButton);

    await waitFor(() => expect(requestsTo(requests, LAUNCH_PATH)).toHaveLength(1));
  });

  it(
    "form check'te kalsa bile prepare/launch gövdesi form state'ini değil " +
      "sunucudan dönen planın mode'unu taşır (regresyon)",
    async () => {
      // Ayırt edici kurgu: form hiç değiştirilmez (check kalır) ama sunucu
      // önizleme ve hazırlama cevaplarında bilerek `mode: "normal"` döner. Bu
      // gerçekçi bir sunucu davranışı değildir; amaç, prepare/launch
      // gövdesinin kaynağını form state'inden **ayırt edici** biçimde
      // ölçmektir. `handlePrepare` `previewedPlan.mode` yerine formun `mode`
      // state'ini, `handleLaunch` `prepare.plan.mode` yerine yine formun
      // `mode` state'ini okuyacak biçimde bozulursa, form check'te kaldığı
      // için gövdeler "check" taşır ve aşağıdaki `toMatchObject({ mode:
      // "normal" })` beklentileri kırılır.
      const { requests } = installFetchMock((request) => {
        if (request.url.endsWith("/playbooks")) {
          return jsonResponse(playbookResult);
        }
        if (request.url.includes("/api/inventories")) {
          return jsonResponse([linkedInventory]);
        }
        if (request.url.endsWith(LAUNCH_PATH)) {
          return jsonResponse({ ...executionLaunchResponse, mode: "normal" }, 201);
        }
        // Hazırlama adresi önizleme adresini kapsadığı için önce sınanır.
        if (request.url.includes("/execution-plans")) {
          return jsonResponse(preparedNormalExecutionPlan, 201);
        }
        if (request.url.includes("/execution-plan")) {
          return jsonResponse(normalExecutionPlan);
        }
        if (request.url.endsWith(`/api/jobs/${executionLaunchResponse.job_id}`)) {
          return jsonResponse({
            ...pendingJob,
            job_id: executionLaunchResponse.job_id,
            status: "successful",
            has_recorded_result: false,
          });
        }
        return jsonResponse(activeProject);
      });

      renderApp(DETAIL_ROUTE);
      const user = userEvent.setup();
      const section = await selectBoth(user);
      // Form kasıtlı olarak check'te bırakılır; radio hiç tıklanmaz.
      expect(within(section).getByRole("radio", { name: "Check" })).toBeChecked();

      await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
      await screen.findByRole("heading", { level: 4, name: "Execution planı" });

      // Preview isteği hâlâ formun check'ini taşır; sunucu buna rağmen
      // normal bir plan döndürür (yukarıdaki mock).
      const [previewRequest] = requestsTo(requests, PLAN_PATH);
      expect(previewRequest?.body).toMatchObject({ mode: "check" });

      await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
      await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

      const [prepareRequest] = requestsTo(requests, PREPARE_PATH);
      expect(prepareRequest?.body).toMatchObject({ mode: "normal" });
      // Form gerçekten check'te kalmıştır: divergans yalnızca sunucu
      // cevabından kaynaklanır, kullanıcı seçiminden değil.
      expect(within(section).getByRole("radio", { name: "Check" })).toBeChecked();

      // Hazırlanmış plan normal olduğu için normal risk banner/onay akışı
      // görünür: onaysız buton kapalı, onay sonrası launch tek POST üretir.
      const warning = screen.getByRole("alert");
      expect(warning).toHaveTextContent(/gerçek değişiklik/i);
      const runButton = screen.getByRole("button", { name: NORMAL_RUN_BUTTON });
      expect(runButton).toBeDisabled();

      await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
      await user.click(runButton);

      await waitFor(() => expect(requestsTo(requests, LAUNCH_PATH)).toHaveLength(1));
      const [launchRequest] = requestsTo(requests, LAUNCH_PATH);
      expect(launchRequest?.body).toMatchObject({ mode: "normal" });
    },
  );
});
