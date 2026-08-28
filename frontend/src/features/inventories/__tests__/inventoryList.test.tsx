import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { activeProject, linkedInventory, standaloneInventory } from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  installNetworkFailure,
  jsonResponse,
  renderApp,
} from "../../../test/harness";

const LIST_ROUTE = "/inventories";

describe("Inventory listesi", () => {
  it("kayıtları ad, biçim ve yol ile listeler", async () => {
    const { requests } = installFetchMock(() =>
      jsonResponse([linkedInventory, standaloneInventory]),
    );

    renderApp(LIST_ROUTE);

    expect(
      await screen.findByRole("link", { name: linkedInventory.name }),
    ).toHaveAttribute("href", `/inventories/${linkedInventory.id}`);
    expect(screen.getByText(linkedInventory.path)).toBeInTheDocument();
    expect(screen.getByText(standaloneInventory.path)).toBeInTheDocument();
    expect(screen.getByText("INI")).toBeInTheDocument();
    expect(screen.getByText("YAML")).toBeInTheDocument();

    // Liste isteği yalnızca kayıt uç noktasına gider; dosya içeriği okunmaz.
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatchObject({
      method: "GET",
      url: expect.stringMatching(/\/api\/inventories$/) as unknown as string,
    });
  });

  it("project bağı olan ve olmayan kayıtları ayırt eder", async () => {
    installFetchMock(() => jsonResponse([linkedInventory, standaloneInventory]));

    renderApp(LIST_ROUTE);

    const linkedRow = (await screen.findByText(linkedInventory.name)).closest("tr");
    expect(linkedRow).not.toBeNull();
    expect(
      within(linkedRow as HTMLElement).getByRole("link", {
        name: `Project #${activeProject.id}`,
      }),
    ).toHaveAttribute("href", `/projects/${activeProject.id}`);

    const standaloneRow = screen.getByText(standaloneInventory.name).closest("tr");
    expect(standaloneRow).toHaveTextContent("Bağımsız");
  });

  it("boş listeyi açıklar", async () => {
    installFetchMock(() => jsonResponse([]));

    renderApp(LIST_ROUTE);

    expect(await screen.findByText("Henüz inventory kaydı yok")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("yükleniyor durumunu gösterir", async () => {
    installFetchMock(() => new Promise(() => {}));

    renderApp(LIST_ROUTE);

    expect(await screen.findByText("Inventory'ler yükleniyor…")).toBeInTheDocument();
  });

  it("liste API hatasını kullanıcıya açıklar", async () => {
    installFetchMock(() => errorResponse(500, "internal_error", "Sunucu içi hata oluştu."));

    renderApp(LIST_ROUTE);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("İşlem tamamlanamadı");
    expect(screen.getByRole("button", { name: "Tekrar dene" })).toBeInTheDocument();
  });

  it("backend kapalıyken ağ hatasını açıklar", async () => {
    installNetworkFailure();

    renderApp(LIST_ROUTE);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend'e ulaşılamadı");
    expect(alert).toHaveTextContent(/Backend servisinin çalıştığını doğrulayıp/i);
  });
});
