import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  emptyInventoryContents,
  inventoryContents,
  linkedInventory,
  standaloneInventory,
} from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;

/** Detay + host isteklerini karşılayan yönlendirici üretir. */
function responder(options: {
  inventory?: unknown;
  hosts?: unknown;
}): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/hosts")) {
      return options.hosts;
    }
    // Ping geçmişi bu dosyanın konusu değil; boş liste bölümün metadata ve
    // içerik ölçümlerine karışmasını önler.
    if (request.url.includes("/ping-runs")) {
      return jsonResponse({ inventory_id: linkedInventory.id, items: [] });
    }
    return options.inventory;
  };
}

/** Başarılı bir detay + içerik cevabı kurar. */
function installSuccess() {
  return installFetchMock(
    responder({
      inventory: jsonResponse(linkedInventory),
      hosts: jsonResponse(inventoryContents),
    }),
  );
}

/**
 * Tabloları erişilebilir adlarıyla ayırır.
 *
 * Aynı host adı hem grup tablosunda (grubun üyesi olarak) hem host tablosunda
 * geçer; sorgular bu yüzden ilgili tabloya daraltılır.
 */
function groupTable(): HTMLElement {
  return screen.getByRole("table", { name: "Gruplar ve host'ları" });
}

function hostTable(): HTMLElement {
  return screen.getByRole("table", { name: "Host'lar, grupları ve değişkenleri" });
}

/** İçerik yüklenene kadar bekler. */
async function waitForContents(): Promise<void> {
  await screen.findByRole("heading", { level: 3, name: "Host'lar" });
}

function rowFor(table: HTMLElement, name: string): HTMLElement {
  const row = within(table).getByText(name).closest("tr");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

describe("Inventory detayı — metadata", () => {
  it("kayıt bilgilerini gösterir", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);

    expect(
      await screen.findByRole("heading", { level: 2, name: linkedInventory.name }),
    ).toBeInTheDocument();
    expect(screen.getByText(linkedInventory.path)).toBeInTheDocument();
    expect(screen.getByText("Biçim").nextElementSibling).toHaveTextContent("INI");
    expect(screen.getByText("Oluşturulma").nextElementSibling).toHaveTextContent("2026");
  });

  it("bağlı project'e bağlantı verir", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
    expect(
      screen.getByRole("link", { name: `Project #${linkedInventory.project_id}` }),
    ).toHaveAttribute("href", `/projects/${linkedInventory.project_id}`);
  });

  it("standalone kaydı bağımsız olarak gösterir", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse(standaloneInventory),
        hosts: jsonResponse(emptyInventoryContents),
      }),
    );

    renderApp(`/inventories/${standaloneInventory.id}`);

    await screen.findByRole("heading", { level: 2, name: standaloneInventory.name });
    expect(screen.getByText("Bağlı project").nextElementSibling).toHaveTextContent(
      "Bağımsız (bir project'e bağlı değil)",
    );
  });

  it("bağlı kayıtta project'in çalıştırma planına giden bir sonraki adım gösterir (R1-V3F0)", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
    expect(
      screen.getByRole("link", { name: "project'in çalıştırma planına gidin" }),
    ).toHaveAttribute("href", `/projects/${linkedInventory.project_id}`);
  });

  it("standalone kayıtta plan akışında kullanılamayacağını açıkça belirtir (R1-V3F0)", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse(standaloneInventory),
        hosts: jsonResponse(emptyInventoryContents),
      }),
    );

    renderApp(`/inventories/${standaloneInventory.id}`);

    await screen.findByRole("heading", { level: 2, name: standaloneInventory.name });
    expect(screen.getByRole("note")).toHaveTextContent(/yalnızca bir project'e bağlı/i);
  });

  it("içerik okunurken metadata görünür ve ayrı bir yükleniyor durumu vardır", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse(linkedInventory),
        hosts: new Promise(() => {}),
      }),
    );

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
    expect(screen.getByText("Inventory içeriği okunuyor…")).toBeInTheDocument();
  });

  it("bozuk tarih değerleri arayüzü çökertmez", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse({
          ...linkedInventory,
          created_at: "kesinlikle-tarih-degil",
          updated_at: null,
        }),
        hosts: jsonResponse(inventoryContents),
      }),
    );

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: linkedInventory.name });
    expect(screen.getByText("Oluşturulma").nextElementSibling).toHaveTextContent(
      "Bilinmiyor",
    );
    expect(screen.queryByText("Invalid Date")).not.toBeInTheDocument();
  });
});

describe("Inventory detayı — grup ve host görünümü", () => {
  it("grupları host sayısı ve host adlarıyla listeler", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    const webGroupRow = rowFor(groupTable(), "web");
    expect(webGroupRow).toHaveTextContent("web01, web02");
    expect(within(webGroupRow).getByText("2")).toBeInTheDocument();

    // `all` grubu alt gruplardan gelen host'ları da taşır.
    expect(rowFor(groupTable(), "all")).toHaveTextContent("db01, web01, web02");
  });

  it("host'ları grup üyelikleriyle listeler", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    // Grup üyeliği backend'in hesapladığı geçişli liste olarak görünür.
    expect(rowFor(hostTable(), "web01")).toHaveTextContent("all, web");
    expect(rowFor(hostTable(), "db01")).toHaveTextContent("all, database");
  });

  it("normal host değişkenlerini ad ve değeriyle gösterir", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    const webRow = rowFor(hostTable(), "web01");
    expect(within(webRow).getByText("ansible_host")).toBeInTheDocument();
    expect(within(webRow).getByText("10.0.0.10")).toBeInTheDocument();
    expect(within(webRow).getByText("deploy")).toBeInTheDocument();

    // Sayısal değer de okunabilir biçimde basılır.
    expect(within(rowFor(hostTable(), "db01")).getByText("22")).toBeInTheDocument();
  });

  it("maskelenmiş secret değeri yalnızca maskeyle görünür", async () => {
    // Fixture zaten maskeli gelir; test maskenin açılmadığını ve yerine gerçek
    // bir değerin uydurulmadığını doğrular.
    installSuccess();

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    const webRow = rowFor(hostTable(), "web01");
    const label = within(webRow).getByText("ansible_password");
    expect(label).toBeInTheDocument();

    // Maskelenmiş değişkenin satırında maskeden başka bir değer yoktur.
    const variableRow = label.closest(".variables__row") as HTMLElement;
    expect(variableRow).toHaveTextContent(/^ansible_password\*\*\* \(gizlendi\)$/);
    expect(within(variableRow).getByText("***")).toBeInTheDocument();
  });

  it("değişkeni olmayan host için açıklama gösterir", async () => {
    installSuccess();

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    expect(
      within(rowFor(hostTable(), "web02")).getByText("Değişken tanımlı değil"),
    ).toBeInTheDocument();
  });

  it("iç içe değişken yapısını çökmeden gösterir", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse(linkedInventory),
        hosts: jsonResponse({
          ...inventoryContents,
          hosts: [
            {
              name: "web01",
              groups: ["all"],
              variables: {
                // Backend iç içe yapıları da maskeler; burada maskeli hâli gelir.
                ansible_ssh_common_args: { token: "***" },
                ports: [80, 443],
                enabled: true,
                notes: null,
              },
            },
          ],
        }),
      }),
    );

    renderApp(DETAIL_ROUTE);
    await waitForContents();

    const webRow = rowFor(hostTable(), "web01");
    expect(within(webRow).getByText('{"token":"***"}')).toBeInTheDocument();
    expect(within(webRow).getByText("[80,443]")).toBeInTheDocument();
    expect(within(webRow).getByText("true")).toBeInTheDocument();
    expect(within(webRow).getByText("null")).toBeInTheDocument();
  });

  it("boş grup ve host listelerini açıklar", async () => {
    installFetchMock(
      responder({
        inventory: jsonResponse(standaloneInventory),
        hosts: jsonResponse(emptyInventoryContents),
      }),
    );

    renderApp(`/inventories/${standaloneInventory.id}`);

    expect(await screen.findByText("Bu inventory'de grup yok")).toBeInTheDocument();
    expect(screen.getByText("Bu inventory'de host yok")).toBeInTheDocument();
  });
});

describe("Inventory detayı — bulunamayan kayıt", () => {
  it("bilinmeyen kimlikte Inventory bulunamadı durumu gösterir", async () => {
    installFetchMock(() => errorResponse(404, "not_found", "Inventory bulunamadı: 999"));

    renderApp("/inventories/999");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Inventory bulunamadı" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Inventory listesine dön" }),
    ).toBeInTheDocument();
  });

  it("sayısal olmayan kimlik için hiç istek atmaz", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/inventories/abc");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Inventory bulunamadı" }),
    ).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });
});
