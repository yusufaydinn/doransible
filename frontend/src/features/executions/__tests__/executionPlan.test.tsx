/**
 * Execution plan önizlemesi (R1-V1).
 *
 * Merkez iddia: bu ekran **hiçbir şey çalıştırmaz**. "Onayla ve Çalıştır"
 * gerçekten devre dışıdır, tıklandığında istek çıkmaz ve başarı taklidi yapmaz.
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
import { executionPlan, truncatedExecutionPlan } from "../../../test/executionFixtures";
import { activeProject, linkedInventory, playbookResult } from "../../../test/fixtures";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const PLAN_PATH = `/api/projects/${activeProject.id}/execution-plan`;
const PLAN_BUTTON = "Planı Oluştur";
const RUN_BUTTON = "Onayla ve Çalıştır";

const secondInventory = { ...linkedInventory, id: 9, name: "Hazırlık sunucuları" };

/** Detay sayfasının bütün isteklerini karşılayan yönlendirici üretir. */
function responder(options: {
  inventories?: unknown;
  plan?: unknown;
}): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/playbooks")) {
      return jsonResponse(playbookResult);
    }
    if (request.url.includes("/api/inventories")) {
      return options.inventories ?? jsonResponse([linkedInventory, secondInventory]);
    }
    if (request.url.includes("/execution-plan")) {
      return options.plan;
    }
    return jsonResponse(activeProject);
  };
}

/** Plan bölümünü döndürür. */
async function planSection(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { level: 3, name: "Çalıştırma planı" });
  return heading.parentElement as HTMLElement;
}

/** Inventory ve playbook seçimini yapar. */
async function selectBoth(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  const section = await planSection();
  await user.selectOptions(
    await within(section).findByLabelText("Inventory"),
    String(linkedInventory.id),
  );
  await user.selectOptions(within(section).getByLabelText("Playbook"), "site.yml");
  return section;
}

describe("Execution plan önizlemesi", () => {
  it("seçim yapılmadan istek göndermez ve buton kilitlidir", async () => {
    const { requests } = installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    const section = await planSection();
    const button = await within(section).findByRole("button", { name: PLAN_BUTTON });
    expect(button).toBeDisabled();

    await user.click(button);

    expect(requests.filter((request) => request.url.includes("/execution-plan"))).toHaveLength(0);
  });

  it("yalnız inventory seçilince hâlâ istek göndermez", async () => {
    const { requests } = installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    const section = await planSection();
    await user.selectOptions(
      await within(section).findByLabelText("Inventory"),
      String(linkedInventory.id),
    );

    expect(within(section).getByRole("button", { name: PLAN_BUTTON })).toBeDisabled();
    expect(requests.filter((request) => request.url.includes("/execution-plan"))).toHaveLength(0);
  });

  it("doğru adrese, yalnız inventory ve playbook taşıyan gövdeyle POST eder", async () => {
    const { requests } = installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);

    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    const planRequests = requests.filter((request) => request.url.includes("/execution-plan"));
    expect(planRequests).toHaveLength(1);
    const [planRequest] = planRequests;
    expect(planRequest?.method).toBe("POST");
    expect(planRequest?.url.endsWith(PLAN_PATH)).toBe(true);
    // Gövde tam olarak üç alan taşır: mode (varsayılan check), inventory_id,
    // playbook_path. limit/tags bu dilimde hâlâ gönderilmez.
    expect(planRequest?.body).toEqual({
      mode: "check",
      inventory_id: linkedInventory.id,
      playbook_path: "site.yml",
    });
  });

  it("planın alanlarını ve check mode olduğunu gösterir", async () => {
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    const panel = (await screen.findByRole("heading", { level: 4, name: "Execution planı" }))
      .parentElement as HTMLElement;
    expect(panel).toHaveTextContent(activeProject.name);
    expect(panel).toHaveTextContent(linkedInventory.name);
    expect(panel).toHaveTextContent("site.yml");
    expect(panel).toHaveTextContent("check");
    expect(panel).toHaveTextContent(/check mode/i);
    expect(panel).toHaveTextContent("ssh");
    expect(panel).toHaveTextContent(/Hedef host sayısı/);
    expect(panel).toHaveTextContent(/Yok — inventory'nin tamamı hedefleniyor/);
    expect(panel).toHaveTextContent(
      /Güvenilir bir playbook kendi task'larında yine de become kullanabilir/,
    );
    for (const host of executionPlan.hosts) {
      expect(within(panel).getByText(host)).toBeInTheDocument();
    }
  });

  it("check mode'u mutlak bir güvence gibi anlatmaz", async () => {
    // R0 ölçümünde `check_mode: false` taşıyan task'ın check altında gerçekten
    // çalıştığı görülmüştür: check mode tek başına zararsızlık garantisi
    // değildir. Metnin taşıdığı tek güvence, önizlemenin hiçbir şey
    // çalıştırmaması olmalıdır.
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    const panel = (await screen.findByRole("heading", { level: 4, name: "Execution planı" }))
      .parentElement as HTMLElement;
    expect(panel).toHaveTextContent(
      "Check mode tek başına güvenlik veya değişiklik yapılmayacağı garantisi değildir.",
    );
    expect(panel).toHaveTextContent("Bu önizleme hiçbir playbook çalıştırmaz.");
    expect(panel).toHaveTextContent("Bu önizleme hedeflere bağlantı kurmaz.");

    // Yanlış güvence ve gelecek zamanlı execution iddiası kalmamalı.
    expect(panel).not.toHaveTextContent(/simüle/i);
    expect(panel).not.toHaveTextContent(/uygulamak yerine/i);
    expect(panel).not.toHaveTextContent(/bağlanılacak/i);
    expect(panel).not.toHaveTextContent(/çalıştırılacak/i);

    // Gerekçe hâlâ kısa: Gate ayrıntısı arayüzde anlatılmaz.
    expect(panel).not.toHaveTextContent(/Kapı [A-D]|Gate [A-D]|ADR-021/);
  });

  it("sunucudaki mutlak yolları göstermez", async () => {
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    // Inventory kaydının `path` alanı arayüzde hiç basılmaz.
    expect(screen.queryByText(linkedInventory.path)).not.toBeInTheDocument();
    expect(section).not.toHaveTextContent(linkedInventory.path);
  });

  it("kırpılmış host listesini açıkça anlatır", async () => {
    installFetchMock(responder({ plan: jsonResponse(truncatedExecutionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    const panel = (await screen.findByRole("heading", { level: 4, name: "Execution planı" }))
      .parentElement as HTMLElement;
    expect(panel).toHaveTextContent(/Liste kısaltıldı/);
    expect(panel).toHaveTextContent(/100 host adı görünüyor/);
    expect(panel).toHaveTextContent(/125 host'u kapsıyor/);
  });

  it("hazırlanmamış önizlemede çalıştırma butonu hiç görünmez", async () => {
    // "Onayla ve Çalıştır" yalnızca hazırlanmış (onaya alınmış) bir plan
    // varken anlamlıdır: launch, tek kullanımlık bir plan token'ı gerektirir
    // ve önizleme aşamasında henüz token yoktur (bkz. launchExecution.test.tsx).
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    expect(screen.queryByRole("button", { name: RUN_BUTTON })).not.toBeInTheDocument();
    // Başarı taklidi yok: ekranda "çalıştırıldı" benzeri bir sonuç oluşmaz.
    expect(screen.queryByText(/çalıştırıldı/i)).not.toBeInTheDocument();
  });

  it("seçim değişince eski planı ekranda bırakmaz", async () => {
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    await user.selectOptions(
      within(section).getByLabelText("Playbook"),
      "playbooks/tasks/deploy.yml",
    );

    expect(
      screen.queryByRole("heading", { level: 4, name: "Execution planı" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: RUN_BUTTON })).not.toBeInTheDocument();
  });

  it("inventory değişince de eski plan temizlenir", async () => {
    installFetchMock(responder({ plan: jsonResponse(executionPlan) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));
    await screen.findByRole("heading", { level: 4, name: "Execution planı" });

    await user.selectOptions(
      within(section).getByLabelText("Inventory"),
      String(secondInventory.id),
    );

    expect(
      screen.queryByRole("heading", { level: 4, name: "Execution planı" }),
    ).not.toBeInTheDocument();
  });

  it("istek sürerken butonu kilitler ve durumu duyurur", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(responder({ plan: pending.promise }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: "Planı Oluştur" }));

    const busyButton = await within(section).findByRole("button", {
      name: "Plan hazırlanıyor…",
    });
    expect(busyButton).toBeDisabled();
    expect(within(section).getByRole("status")).toHaveTextContent(
      /hiçbir host'a bağlanılmaz/i,
    );

    await user.click(busyButton);
    pending.resolve(jsonResponse(executionPlan));

    await screen.findByRole("heading", { level: 4, name: "Execution planı" });
    expect(requests.filter((request) => request.url.includes("/execution-plan"))).toHaveLength(1);
  });

  it("backend hatasını uygulanabilir biçimde gösterir ve planı açmaz", async () => {
    installFetchMock(
      responder({
        plan: errorResponse(
          422,
          "playbook_not_discovered",
          "Bu playbook project'in keşif sonucunda yok.",
          { project_id: activeProject.id },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    expect(await screen.findByText("Playbook listede yok")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 4, name: "Execution planı" }),
    ).not.toBeInTheDocument();
    // Ham `details` JSON'u ekrana basılmaz.
    expect(section).not.toHaveTextContent("project_id");
  });

  it("bağımsız inventory hatasını açıklar", async () => {
    installFetchMock(
      responder({
        plan: errorResponse(
          409,
          "inventory_not_linked_to_project",
          "Bu inventory seçilen project'e bağlı değil.",
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    const section = await selectBoth(user);
    await user.click(within(section).getByRole("button", { name: PLAN_BUTTON }));

    expect(await screen.findByText("Inventory bu project'e bağlı değil")).toBeInTheDocument();
  });

  it("project'e bağlı inventory yoksa seçim yerine açıklama gösterir", async () => {
    const { requests } = installFetchMock(responder({ inventories: jsonResponse([]) }));

    renderApp(DETAIL_ROUTE);

    const section = await planSection();
    await waitFor(() =>
      expect(section).toHaveTextContent(/Bu project'e bağlı inventory yok/),
    );
    expect(within(section).queryByLabelText("Inventory")).not.toBeInTheDocument();
    expect(requests.filter((request) => request.url.includes("/execution-plan"))).toHaveLength(0);
  });

  it("inventory listesi hatasını gösterir ve tekrar denemeyi sunar", async () => {
    installFetchMock(
      responder({
        inventories: errorResponse(500, "internal_error", "Beklenmeyen bir hata oluştu."),
      }),
    );

    renderApp(DETAIL_ROUTE);

    const section = await planSection();
    await waitFor(() =>
      expect(within(section).getByRole("button", { name: "Tekrar dene" })).toBeInTheDocument(),
    );
    expect(within(section).queryByRole("button", { name: PLAN_BUTTON })).toBeDisabled();
  });

  it("pasif project'te plan bölümü istek göndermez", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.includes("/api/inventories")) {
        return jsonResponse([linkedInventory]);
      }
      return jsonResponse({ ...activeProject, is_active: false });
    });

    renderApp(DETAIL_ROUTE);

    const section = await planSection();
    expect(section).toHaveTextContent(/Pasif project'te plan üretilmez/);
    expect(requests.filter((request) => request.url.includes("/api/inventories"))).toHaveLength(0);
  });
});
