import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { inventoryContents, linkedInventory } from "../../../test/fixtures";
import { installFetchMock, jsonResponse, renderApp } from "../../../test/harness";

/** Liste, detay ve içerik isteklerinin hepsini karşılar. */
function installFullApi() {
  return installFetchMock((request) => {
    if (request.url.endsWith("/hosts")) {
      return jsonResponse(inventoryContents);
    }
    if (request.url.includes("/ping-runs")) {
      return jsonResponse({ inventory_id: linkedInventory.id, items: [] });
    }
    if (request.url.endsWith("/api/inventories")) {
      return jsonResponse([linkedInventory]);
    }
    return jsonResponse(linkedInventory);
  });
}

describe("Inventory gezinmesi", () => {
  it("ana gezinmede inventory bağlantısı vardır", async () => {
    installFullApi();

    renderApp("/inventories");

    expect(await screen.findByRole("link", { name: "Inventory'ler" })).toHaveAttribute(
      "href",
      "/inventories",
    );
  });

  it("listeden detaya ve geri gezinilebilir", async () => {
    installFullApi();

    renderApp("/inventories");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: linkedInventory.name }));
    expect(
      await screen.findByRole("heading", { level: 2, name: linkedInventory.name }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    expect(
      await screen.findByRole("heading", { level: 2, name: "Inventory'ler" }),
    ).toBeInTheDocument();
  });

  it("inventory detayından bağlı project'e geçilebilir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/hosts")) {
        return jsonResponse(inventoryContents);
      }
      // Project detayındaki plan formunun okuduğu, project'e bağlı inventory
      // listesi (R1-V1). Tek kayıt okumasından query string ile ayrılır.
      if (request.url.includes("/api/inventories?")) {
        return jsonResponse([]);
      }
      if (request.url.includes("/ping-runs")) {
        return jsonResponse({ inventory_id: linkedInventory.id, items: [] });
      }
      if (request.url.includes("/api/inventories/")) {
        return jsonResponse(linkedInventory);
      }
      // Project detayı ve playbook keşfi.
      if (request.url.endsWith("/playbooks")) {
        return jsonResponse({
          project_id: linkedInventory.project_id,
          playbooks: [],
          skipped_unreadable_files: 0,
          skipped_unreadable_directories: 0,
          truncated: false,
          scanned_at: "2026-07-28T10:00:00Z",
        });
      }
      return jsonResponse({
        id: linkedInventory.project_id,
        name: "Web sunucuları",
        path: "/srv/ansible/web",
        description: null,
        is_active: true,
        created_at: "2026-07-01T09:00:00Z",
        updated_at: "2026-07-20T12:30:00Z",
      });
    });

    renderApp(`/inventories/${linkedInventory.id}`);
    const user = userEvent.setup();

    await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
    await user.click(
      screen.getByRole("link", { name: `Project #${linkedInventory.project_id}` }),
    );

    expect(
      await screen.findByRole("heading", { level: 2, name: "Web sunucuları" }),
    ).toBeInTheDocument();
  });

  it("bilinmeyen inventory alt adresi 404 sayfasına düşer", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/inventories/5/boyle-bir-sayfa-yok");

    expect(
      await screen.findByRole("heading", { name: "Sayfa bulunamadı" }),
    ).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });
});
