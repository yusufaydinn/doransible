/**
 * Planı onaya hazırlama akışı (R1-V2).
 *
 * Merkez iddialar:
 *
 * - Hazırlama yalnızca **geçerli bir önizleme** varken çağrılabilir.
 * - Tek kullanımlık token ekrana, storage'a, URL'ye veya cache'e **hiç** girmez.
 * - Hazırlanmış plan ekranda eskisinin yerini alır; "Onayla ve Çalıştır" hâlâ
 *   devre dışıdır ve hiçbir çalıştırma isteği oluşmaz.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";
import { executionPlan, preparedExecutionPlan } from "../../../test/executionFixtures";
import { activeProject, linkedInventory, playbookResult } from "../../../test/fixtures";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const PREPARE_PATH = `/api/projects/${activeProject.id}/execution-plans`;
const PLAN_BUTTON = "Planı Oluştur";
const PREPARE_BUTTON = "Onaya Hazırla";
const RUN_BUTTON = "Onayla ve Çalıştır";
const TOKEN = preparedExecutionPlan.plan_token;

/** Detay sayfasının bütün isteklerini karşılayan yönlendirici üretir. */
function responder(options: { prepared?: unknown }): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/playbooks")) {
      return jsonResponse(playbookResult);
    }
    if (request.url.includes("/api/inventories")) {
      return jsonResponse([linkedInventory]);
    }
    // Hazırlama adresi önizleme adresini kapsadığı için önce sınanır.
    if (request.url.includes("/execution-plans")) {
      return options.prepared ?? jsonResponse(preparedExecutionPlan, 201);
    }
    if (request.url.includes("/execution-plan")) {
      return jsonResponse(executionPlan);
    }
    return jsonResponse(activeProject);
  };
}

async function planSection(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 3, name: "Çalıştırma planı" });
  return heading.parentElement as HTMLElement;
}

/** Seçimleri yapar ve önizlemeyi üretir. */
async function showPreview(
  user: ReturnType<typeof userEvent.setup>,
): Promise<HTMLElement> {
  const section = await planSection();
  await user.selectOptions(
    await within(section).findByLabelText("Inventory"),
    String(linkedInventory.id),
  );
  await user.selectOptions(within(section).getByLabelText("Playbook"), "site.yml");
  await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
  await screen.findByRole("heading", { level: 4, name: "Execution planı" });
  return section;
}

function prepareRequests(requests: RecordedRequest[]): RecordedRequest[] {
  return requests.filter((request) => request.url.includes("/execution-plans"));
}

describe("Execution planını onaya hazırlama", () => {
  it("önizleme yokken hazırlama düğmesi hiç görünmez", async () => {
    const { requests } = installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await planSection();
    await user.selectOptions(
      await within(section).findByLabelText("Inventory"),
      String(linkedInventory.id),
    );

    expect(within(section).queryByRole("button", { name: PREPARE_BUTTON })).toBeNull();
    expect(prepareRequests(requests)).toHaveLength(0);
  });

  it("doğru adrese, yalnız inventory ve playbook taşıyan gövdeyle POST eder", async () => {
    const { requests } = installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);

    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
    const [request] = prepareRequests(requests);
    expect(prepareRequests(requests)).toHaveLength(1);
    expect(request?.method).toBe("POST");
    expect(request?.url.endsWith(PREPARE_PATH)).toBe(true);
    expect(request?.body).toEqual({
      mode: "check",
      inventory_id: linkedInventory.id,
      playbook_path: "site.yml",
    });
  });

  it("çift tıklama tek istek üretir", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(responder({ prepared: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);

    const button = within(section).getByRole("button", { name: PREPARE_BUTTON });
    await user.click(button);
    await user.click(button);

    expect(prepareRequests(requests)).toHaveLength(1);
    pending.resolve(jsonResponse(preparedExecutionPlan, 201));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
    expect(prepareRequests(requests)).toHaveLength(1);
  });

  it("token'ı ekrana basmaz, storage'a ve URL'ye yazmaz", async () => {
    installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
    expect(window.location.href).not.toContain(TOKEN);
    expect(JSON.stringify(window.localStorage)).not.toContain(TOKEN);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(TOKEN);
  });

  it("token query cache'ine girmez", async () => {
    installFetchMock(responder({}));

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
    const cache = JSON.stringify(
      queryClient.getQueryCache().getAll().map((entry) => entry.state.data),
    );
    expect(cache).not.toContain(TOKEN);
    expect(cache).not.toContain(preparedExecutionPlan.manifest_digest);
  });

  it("hazırlanmış plan eski önizlemenin yerini alır", async () => {
    installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    // Önizleme planı üç host taşır; dondurulmuş plan iki host taşır.
    expect(within(section).getByText("web02")).toBeInTheDocument();

    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    const prepared = (
      await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" })
    ).parentElement as HTMLElement;
    expect(screen.queryByRole("heading", { level: 4, name: "Execution planı" })).toBeNull();
    expect(within(prepared).queryByText("web02")).toBeNull();
    expect(within(prepared).getByText("web01")).toBeInTheDocument();
    // Dondurulmuş içeriğin parmak izi kısa biçimde gösterilir.
    expect(within(prepared).getByText("a1b2c3d4e5f6")).toBeInTheDocument();
    expect(prepared.textContent).not.toContain(preparedExecutionPlan.manifest_digest);
  });

  it("geçerlilik süresini gösterir", async () => {
    installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    const prepared = (
      await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" })
    ).parentElement as HTMLElement;
    expect(within(prepared).getByText("Geçerlilik")).toBeInTheDocument();
    expect(prepared).toHaveTextContent(/2026/);
    expect(prepared).toHaveTextContent(/tarihine kadar/);
  });

  it("hazırlandıktan sonra onay kutusu işaretlenmeden çalıştırma butonu devre dışıdır", async () => {
    const { requests } = installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    const runButton = screen.getByRole("button", { name: RUN_BUTTON });
    const checkbox = screen.getByRole("checkbox", { name: /açıkça onaylıyorum/i });
    expect(runButton).toBeDisabled();
    expect(checkbox).not.toBeChecked();

    const before = requests.length;
    await user.click(runButton);
    expect(requests).toHaveLength(before);

    await user.click(checkbox);
    expect(runButton).toBeEnabled();
  });

  it("seçim değişince hazırlanmış state temizlenir", async () => {
    installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    await user.selectOptions(
      within(section).getByLabelText("Playbook"),
      "playbooks/tasks/deploy.yml",
    );

    await waitFor(() => {
      expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
    });
    expect(screen.queryByRole("heading", { level: 4, name: "Execution planı" })).toBeNull();
    expect(screen.queryByRole("button", { name: PREPARE_BUTTON })).toBeNull();
  });

  it("yeni önizleme eski hazırlığı geçersiz kılar", async () => {
    installFetchMock(responder({}));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });

    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
  });

  it("seçim değişince bayat hazırlama cevabı hiçbir şeyi geri getirmez", async () => {
    // Regresyon: uçuştaki hazırlama, seçim değiştikten sonra dönüp eski
    // dondurulmuş planı ekrana ve token'ı belleğe geri yazabiliyordu.
    const pending = deferred<unknown>();
    installFetchMock(responder({ prepared: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    await user.selectOptions(
      within(section).getByLabelText("Playbook"),
      "playbooks/tasks/deploy.yml",
    );
    pending.resolve(jsonResponse(preparedExecutionPlan, 201));
    await pending.promise;

    await waitFor(() => {
      expect(within(section).getByLabelText("Playbook")).toHaveValue(
        "playbooks/tasks/deploy.yml",
      );
    });
    expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
    expect(screen.queryByRole("heading", { level: 4, name: "Execution planı" })).toBeNull();
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("yeni önizleme başladıysa bayat hazırlama cevabı önizlemenin yerini almaz", async () => {
    const pending = deferred<unknown>();
    installFetchMock(responder({ prepared: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    // Aynı seçimle yeni bir önizleme: eski hazırlık geçersizdir.
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    pending.resolve(jsonResponse(preparedExecutionPlan, 201));
    await pending.promise;

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { level: 4, name: "Execution planı" }),
      ).toBeInTheDocument();
    });
    expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("bayat hazırlama hatası yeni durumu hata durumuna çevirmez", async () => {
    const pending = deferred<unknown>();
    installFetchMock(responder({ prepared: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    await user.selectOptions(
      within(section).getByLabelText("Playbook"),
      "playbooks/tasks/deploy.yml",
    );
    pending.resolve(
      errorResponse(409, "execution_workspace_unsafe", "Project ağacı symlink içeriyor.", {
        reason: "symlink",
      }),
    );
    await pending.promise;

    await waitFor(() => {
      expect(within(section).getByLabelText("Playbook")).toHaveValue(
        "playbooks/tasks/deploy.yml",
      );
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("Project dondurulamıyor")).toBeNull();
  });

  it("hazırlama hatasını uygulanabilir biçimde gösterir", async () => {
    installFetchMock(
      responder({
        prepared: errorResponse(
          409,
          "execution_workspace_unsafe",
          "Project ağacı symlink içeriyor.",
          { reason: "symlink" },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Project dondurulamıyor");
    expect(alert).toHaveTextContent(/symlink/i);
    // Ham `details` gösterilmez.
    expect(alert).not.toHaveTextContent('{"reason"');
    // Hata sonrası hazırlanmış plan görünmez ve önizleme yerinde kalır.
    expect(screen.queryByRole("heading", { level: 4, name: "Plan onaya hazır" })).toBeNull();
    expect(
      screen.getByRole("heading", { level: 4, name: "Execution planı" }),
    ).toBeInTheDocument();
  });

  it("süresi dolmuş plan hatasını açıklar", async () => {
    installFetchMock(
      responder({
        prepared: errorResponse(
          409,
          "execution_plan_invalid",
          "Hazırlanmış execution planı geçerli değil.",
          { reason: "invalid" },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Hazırlanan planın süresi doldu");
  });

  it("hazırlama sürerken durum bildirir ve düğme kilitlenir", async () => {
    const pending = deferred<unknown>();
    installFetchMock(responder({ prepared: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await showPreview(user);
    await user.click(within(section).getByRole("button", { name: PREPARE_BUTTON }));

    expect(within(section).getByRole("button", { name: "Hazırlanıyor…" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/hiçbir şey çalıştırılmaz/i);

    pending.resolve(jsonResponse(preparedExecutionPlan, 201));
    await screen.findByRole("heading", { level: 4, name: "Plan onaya hazır" });
  });
});
