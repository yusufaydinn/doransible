import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activeProject } from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  installNetworkFailure,
  jsonResponse,
  renderApp,
} from "../../../test/harness";

describe("Project listesi", () => {
  it("başarılı veriyi tablo olarak gösterir", async () => {
    installFetchMock(() => jsonResponse([activeProject]));

    renderApp("/projects");

    const link = await screen.findByRole("link", { name: activeProject.name });
    expect(link).toHaveAttribute("href", `/projects/${activeProject.id}`);
    expect(screen.getByText(activeProject.path)).toBeInTheDocument();
    expect(screen.getByText("Nginx ve sertifika yönetimi")).toBeInTheDocument();

    // Güncellenme zamanı okunabilir biçimde görünür (ham ISO dizesi değil).
    const row = link.closest("tr");
    expect(row).toHaveTextContent("2026");
    expect(row).not.toHaveTextContent(activeProject.updated_at);
  });

  it("boş listede açıklayıcı empty state gösterir", async () => {
    installFetchMock(() => jsonResponse([]));

    renderApp("/projects");

    expect(await screen.findByText("Henüz project kaydı yok")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "İlk project'i ekle" })).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("yükleniyor durumunu ayrı gösterir", async () => {
    installFetchMock(() => jsonResponse([activeProject]));

    renderApp("/projects");

    expect(screen.getByText("Project'ler yükleniyor…")).toBeInTheDocument();
    await screen.findByRole("link", { name: activeProject.name });
  });

  it("liste API hatasını kullanıcıya görünür kılar", async () => {
    installFetchMock(() =>
      errorResponse(500, "internal_error", "Beklenmeyen bir sunucu hatası."),
    );

    renderApp("/projects");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("İşlem tamamlanamadı");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("ağ hatasında uygulanabilir mesaj ve çalışan bir tekrar denemesi sunar", async () => {
    installNetworkFailure();

    renderApp("/projects");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend'e ulaşılamadı");
    expect(alert).toHaveTextContent(/Backend servisinin çalıştığını doğrulayıp/);

    // Backend geri geldiğinde "Tekrar dene" gerçekten yeni bir istek atar.
    installFetchMock(() => jsonResponse([activeProject]));
    await userEvent.click(screen.getByRole("button", { name: "Tekrar dene" }));

    await waitFor(() => {
      expect(screen.getByRole("link", { name: activeProject.name })).toBeInTheDocument();
    });
  });

  it("yalnızca aktif kayıtları isteyen sade bir GET gönderir", async () => {
    const { requests } = installFetchMock(() => jsonResponse([]));

    renderApp("/projects");
    await screen.findByText("Henüz project kaydı yok");

    expect(requests).toHaveLength(1);
    expect(requests[0]?.method).toBe("GET");
    expect(requests[0]?.url).toMatch(/\/api\/projects$/);
  });
});
