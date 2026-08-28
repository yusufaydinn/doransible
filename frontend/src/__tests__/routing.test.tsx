import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activeProject, emptyPlaybookResult } from "../test/fixtures";
import { installFetchMock, jsonResponse, renderApp } from "../test/harness";

describe("Gezinme", () => {
  it("bilinmeyen adres 404 sayfasını gösterir", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/boyle-bir-sayfa-yok");

    expect(
      await screen.findByRole("heading", { name: "Sayfa bulunamadı" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ana sayfaya dön" })).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });

  it("uygulama içi bağlantılar tam sayfa yenilemeden çalışır", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/playbooks")) {
        return jsonResponse(emptyPlaybookResult);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([activeProject]);
      }
      // Project detayındaki plan formunun okuduğu inventory listesi (R1-V1).
      if (request.url.includes("/api/inventories")) {
        return jsonResponse([]);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/projects");
    const user = userEvent.setup();

    // React Router `<Link>` gerçek gezinmeyi engeller; jsdom'da tam sayfa
    // yenileme "Not implemented: navigation" hatası verirdi.
    await user.click(await screen.findByRole("link", { name: activeProject.name }));
    expect(
      await screen.findByRole("heading", { level: 2, name: activeProject.name }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Project'ler" })).toBeInTheDocument();
  });

  it("genel bakış sayfası korunur ve project'lere bağlantı verir", async () => {
    installFetchMock(() =>
      jsonResponse({
        status: "ok",
        app_name: "DORAnsible",
        version: "0.1.0",
        environment: "development",
      }),
    );

    renderApp("/");

    expect(await screen.findByText("DORAnsible", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Project'lere git" })).toHaveAttribute(
      "href",
      "/projects",
    );
  });

  it("genel bakış sayfası dört adımlık akışı ve controller yolu uyarısını gösterir", async () => {
    installFetchMock(() =>
      jsonResponse({
        status: "ok",
        app_name: "DORAnsible",
        version: "0.1.0",
        environment: "development",
      }),
    );

    renderApp("/");

    await screen.findByText("DORAnsible", { selector: "dd" });

    // Marka adı header'da h1 olarak görünür (R1-V3F1 rebrand).
    expect(
      screen.getByRole("heading", { level: 1, name: "DORAnsible" }),
    ).toBeInTheDocument();

    // Dört adım da başlıklarıyla görünür ve kendi hedefine bağlanır.
    expect(
      screen.getByRole("link", { name: "Inventory'lere git" }),
    ).toHaveAttribute("href", "/inventories");
    expect(screen.getByRole("link", { name: "Çalıştırmalara git" })).toHaveAttribute(
      "href",
      "/jobs",
    );

    // Yolların DORAnsible controller'a ait olduğu açıkça belirtilir; controller'ın
    // ne olduğu da açıklanır.
    expect(screen.getByRole("note")).toHaveTextContent(/DORAnsible controller/i);
    expect(screen.getByRole("note")).toHaveTextContent(/controller'a.*ait/i);
  });

  it("\"Playbook çalıştırın\" adımı stale check-only yönlendirmesi yerine check/normal seçimini anlatır (R1-V3J0B1)", async () => {
    installFetchMock(() =>
      jsonResponse({
        status: "ok",
        app_name: "DORAnsible",
        version: "0.1.0",
        environment: "development",
      }),
    );

    renderApp("/");

    await screen.findByText("DORAnsible", { selector: "dd" });

    expect(
      screen.getByText(/Check \(Ansible --check\) veya Normal modda bir plan/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/check-mode bir plan oluşturun/i)).not.toBeInTheDocument();
  });

  it(
    "akış adımlarında sıra numarası yalnız rozette görünür, başlık metninde tekrarlanmaz (AUDIT-FIX1)",
    async () => {
      installFetchMock(() =>
        jsonResponse({
          status: "ok",
          app_name: "DORAnsible",
          version: "0.1.0",
          environment: "development",
        }),
      );

      renderApp("/");

      // Başlık metni numarasız: "1. Project ekleyin" değil, "Project ekleyin".
      const heading = await screen.findByRole("heading", {
        level: 3,
        name: "Project ekleyin",
      });
      // Numara yalnızca dekoratif rozette basılıdır; başlığın kendi metninde
      // bir rakamla başlamaz.
      expect(heading.textContent?.trim().startsWith("1")).toBe(false);

      // Rozet `aria-hidden` olduğu için erişilebilirlik ağacında hiç yer
      // almaz — ekran okuyucu numarayı bir daha duymaz.
      const badge = heading
        .closest(".flow-step")
        ?.querySelector(".flow-step__badge");
      expect(badge).not.toBeNull();
      expect(badge).toHaveAttribute("aria-hidden", "true");
      expect(badge?.textContent).toBe("1");
    },
  );

  it("marka bağlantısında tek h1 vardır ve span içine yerleştirilmez (AUDIT-FIX1)", async () => {
    installFetchMock(() =>
      jsonResponse({
        status: "ok",
        app_name: "DORAnsible",
        version: "0.1.0",
        environment: "development",
      }),
    );

    const { container } = renderApp("/");
    await screen.findByText("DORAnsible", { selector: "dd" });

    // Sayfada tam olarak bir h1 var.
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);

    // `<span>` yalnızca "phrasing content" alabilir; başlık (`h1`) bunun
    // parçası değildir. `<span><h1>` geçersiz bir yapı olurdu — burada h1
    // bunun yerine flow content kabul eden bir `<div>` içindedir.
    expect(container.querySelector("span > h1")).toBeNull();

    // Marka, ana sayfaya giden erişilebilir bir bağlantı olarak kalır.
    const brandLink = screen.getByRole("link", { name: /DORAnsible/ });
    expect(brandLink).toHaveAttribute("href", "/");
    expect(brandLink.querySelector("h1")).not.toBeNull();
  });
});
