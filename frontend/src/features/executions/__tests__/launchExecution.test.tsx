/**
 * Hazırlanmış planı çalıştırmaya alma akışı (R1-V3D3).
 *
 * Merkez iddialar:
 *
 * - Launch butonu yalnızca hazırlanmış bir plan **ve** işaretli açık onay
 *   varken tıklanabilir; `window.confirm` kullanılmaz.
 * - İstek gövdesi tam olarak üç alan taşır ve `inventory_id`/`playbook_path`
 *   hazırlanmış planın kendi alanlarından gelir.
 * - Tek kullanımlık token ekrana, storage'a veya cache'e hiç girmez ve her
 *   sonuçtan (başarı veya hata) sonra bir daha kullanılmaz.
 * - Başarılı çalıştırma `/jobs/{job_id}` sayfasına yönlendirir.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  executionLaunchResponse,
  executionPlan,
  preparedExecutionPlan,
} from "../../../test/executionFixtures";
import { activeProject, linkedInventory, playbookResult } from "../../../test/fixtures";
import { pendingJob } from "../../../test/jobFixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const LAUNCH_PATH = `/api/projects/${activeProject.id}/executions`;
const PLAN_BUTTON = "Planı Oluştur";
const PREPARE_BUTTON = "Onaya Hazırla";
const RUN_BUTTON = "Onayla ve Çalıştır";
const APPROVE_LABEL = /açıkça onaylıyorum/i;
const TOKEN = preparedExecutionPlan.plan_token;

/** Detay sayfasının bütün isteklerini karşılayan yönlendirici üretir. */
function responder(options: { launch?: unknown } = {}): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/playbooks")) {
      return jsonResponse(playbookResult);
    }
    if (request.url.includes("/api/inventories")) {
      return jsonResponse([linkedInventory]);
    }
    // Launch adresi, prefix'i paylaşan hazırlama/önizleme adreslerinden önce
    // sınanır: `/executions` tekildir, `/execution-plan(s)` ile çakışmaz.
    if (request.url.includes("/executions")) {
      return options.launch ?? jsonResponse(executionLaunchResponse, 201);
    }
    if (request.url.includes("/execution-plans")) {
      return jsonResponse(preparedExecutionPlan, 201);
    }
    if (request.url.includes("/execution-plan")) {
      return jsonResponse(executionPlan);
    }
    if (request.url.endsWith(`/api/jobs/${executionLaunchResponse.job_id}`)) {
      // Terminal ve sonuçsuz bir Job döndürülür: bu test yönlendirmeyi
      // doğrular, pending/running polling'in kendisi `jobPages.test.tsx`'te
      // sınanır. Terminal olmayan bir durum burada gerçek 2 saniyelik bir
      // `setInterval` başlatır ve testten sonra sızabilirdi.
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

/** Seçimleri yapar, önizleme üretir ve planı onaya hazırlar. */
async function showPrepared(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  const section = await planSection();
  await user.selectOptions(
    await within(section).findByLabelText("Inventory"),
    String(linkedInventory.id),
  );
  await user.selectOptions(within(section).getByLabelText("Playbook"), "site.yml");
  await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
  await screen.findByRole("heading", { level: 4, name: "Execution planı" });
  await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
  await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
  return section;
}

function launchRequests(requests: RecordedRequest[]): RecordedRequest[] {
  return requests.filter((request) => request.url.endsWith(LAUNCH_PATH));
}

describe("Hazırlanmış planı çalıştırmaya alma", () => {
  it("hazırlanmış plan olmadan launch butonu hiç görünmez", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await planSection();
    await user.selectOptions(
      await within(section).findByLabelText("Inventory"),
      String(linkedInventory.id),
    );
    await user.selectOptions(within(section).getByLabelText("Playbook"), "site.yml");
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    expect(screen.queryByRole("button", { name: RUN_BUTTON })).toBeNull();
  });

  it("onay kutusu işaretlenmeden launch butonu devre dışıdır", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);

    const runButton = screen.getByRole("button", { name: RUN_BUTTON });
    expect(runButton).toBeDisabled();

    await user.click(runButton);
    expect(launchRequests(requests)).toHaveLength(0);
  });

  it("doğru adrese, yalnız üç alanlı gövdeyle POST eder", async () => {
    const { requests } = installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);

    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    await user.click(screen.getByRole("button", { name: RUN_BUTTON }));

    await waitFor(() => expect(launchRequests(requests)).toHaveLength(1));
    const [request] = launchRequests(requests);
    expect(request?.method).toBe("POST");
    expect(request?.body).toEqual({
      plan_token: TOKEN,
      mode: "check",
      inventory_id: preparedExecutionPlan.plan.inventory.id,
      playbook_path: preparedExecutionPlan.plan.playbook.path,
    });
  });

  it("çift tıklama tek istek üretir", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(responder({ launch: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);
    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));

    const runButton = screen.getByRole("button", { name: RUN_BUTTON });
    await user.click(runButton);
    await user.click(runButton);

    expect(launchRequests(requests)).toHaveLength(1);
    pending.resolve(jsonResponse(executionLaunchResponse, 201));
    await waitFor(() => expect(launchRequests(requests)).toHaveLength(1));
  });

  it("başarılı launch /jobs/{job_id} sayfasına yönlendirir", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);
    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    await user.click(screen.getByRole("button", { name: RUN_BUTTON }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Çalıştırma detayı" }),
    ).toBeInTheDocument();
    expect(screen.getByText(executionLaunchResponse.playbook_path)).toBeInTheDocument();
  });

  it("token'ı ekrana basmaz, storage'a ve cache'e yazmaz", async () => {
    installFetchMock(responder());
    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);
    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    await user.click(screen.getByRole("button", { name: RUN_BUTTON }));

    await screen.findByRole("heading", { level: 2, name: "Çalıştırma detayı" });

    expect(document.body.textContent).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(JSON.stringify(window.localStorage)).not.toContain(TOKEN);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(TOKEN);
    const cache = JSON.stringify(
      queryClient.getQueryCache().getAll().map((entry) => entry.state.data),
    );
    expect(cache).not.toContain(TOKEN);
  });

  it("launch hatasında token yeniden kullanılmaz ve planın yeniden hazırlanması istenir", async () => {
    const { requests } = installFetchMock(
      responder({
        launch: errorResponse(
          409,
          "execution_plan_invalid",
          "Hazırlanmış execution planı geçerli değil.",
          { reason: "invalid" },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);
    await user.click(screen.getByRole("checkbox", { name: APPROVE_LABEL }));
    await user.click(screen.getByRole("button", { name: RUN_BUTTON }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/yeniden hazırlayıp tekrar deneyin/i);
    // Ham `details` gösterilmez.
    expect(alert).not.toHaveTextContent('{"reason"');

    // Hazırlanmış panel kaybolur; kullanıcı yeniden hazırlamak zorundadır.
    expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
    expect(screen.queryByRole("button", { name: RUN_BUTTON })).toBeNull();
    expect(
      screen.getByRole("button", { name: PREPARE_BUTTON }),
    ).toBeInTheDocument();

    expect(launchRequests(requests)).toHaveLength(1);
  });

  it("checkbox ve launch butonu klavyeyle kullanılabilir", async () => {
    installFetchMock(responder());

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await showPrepared(user);

    const checkbox = screen.getByRole("checkbox", { name: APPROVE_LABEL });
    checkbox.focus();
    await user.keyboard(" ");
    expect(checkbox).toBeChecked();

    const runButton = screen.getByRole("button", { name: RUN_BUTTON });
    expect(runButton).toBeEnabled();
    runButton.focus();
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Çalıştırma detayı" }),
    ).toBeInTheDocument();
  });
});
