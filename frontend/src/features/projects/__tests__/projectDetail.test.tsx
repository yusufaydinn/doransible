import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  activeProject,
  emptyPlaybookResult,
  inactiveProject,
  linkedInventory,
  playbookResult,
} from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const PLAYBOOK_TABLE_NAME = "Keşfedilen playbook'lar";

/**
 * Keşif tablosunu döndürür.
 *
 * Playbook yolları sayfada iki yerde görünür: bu tabloda ve plan formunun
 * seçim listesinde (R1-V1). Bu dosyadaki iddialar **tabloya** aittir, bu yüzden
 * sorgular tabloyla sınırlanır.
 */
async function playbookTable(): Promise<HTMLElement> {
  return screen.findByRole("table", { name: PLAYBOOK_TABLE_NAME });
}

/** Detay + playbook + plan formu isteklerini karşılayan yönlendirici üretir. */
function responder(options: {
  project?: unknown;
  playbooks?: unknown;
  inventories?: unknown;
}): (request: RecordedRequest) => unknown {
  return (request) => {
    if (request.url.endsWith("/playbooks")) {
      return options.playbooks;
    }
    // Detay sayfasındaki plan formu, project'e bağlı inventory'leri okur
    // (R1-V1). Bu testler o listeyle ilgilenmez; varsayılan boştur.
    if (request.url.includes("/api/inventories")) {
      return options.inventories ?? jsonResponse([]);
    }
    return options.project;
  };
}

describe("Project detayı", () => {
  it("project bilgilerini gösterir", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(playbookResult),
      }),
    );

    renderApp(DETAIL_ROUTE);

    expect(
      await screen.findByRole("heading", { level: 2, name: activeProject.name }),
    ).toBeInTheDocument();
    expect(screen.getByText(activeProject.path)).toBeInTheDocument();
    expect(screen.getByText("Nginx ve sertifika yönetimi")).toBeInTheDocument();
    expect(screen.getByText("Aktif")).toBeInTheDocument();
    // Zaman bilgileri okunabilir biçimde görünür.
    expect(screen.getByText("Oluşturulma").nextElementSibling).toHaveTextContent("2026");
    // Dizin yolunun sahibi açıkça controller'dır (R1-V3J0B1).
    expect(screen.getByText("Controller yolu")).toBeInTheDocument();
  });

  it("stale check-only yönlendirme yerine check/normal seçimini anlatır (R1-V3J0B1)", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(playbookResult),
      }),
    );

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: activeProject.name });

    expect(
      screen.getByText(/Check \(Ansible --check\) veya Normal modda bir plan/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/check-mode bir plan oluşturup/i)).not.toBeInTheDocument();
  });

  it("playbook'ları project'e göreli yollarla listeler", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(playbookResult),
      }),
    );

    renderApp(DETAIL_ROUTE);

    const table = await playbookTable();
    expect(within(table).getByText("playbooks/tasks/deploy.yml")).toBeInTheDocument();
    expect(within(table).getByText("site.yml")).toBeInTheDocument();

    // Sunucudaki mutlak yol playbook satırlarına asla birleştirilmez.
    expect(screen.queryByText(`${activeProject.path}/site.yml`)).not.toBeInTheDocument();
    const row = within(table).getByText("site.yml").closest("tr");
    expect(row).not.toHaveTextContent(activeProject.path);
  });

  it("playbook araması sürerken ayrı bir yükleniyor durumu gösterir", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: new Promise(() => {}),
      }),
    );

    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 2, name: activeProject.name });
    expect(screen.getByText("Playbook'lar aranıyor…")).toBeInTheDocument();
  });

  it("boş playbook sonucunu açıklar", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(emptyPlaybookResult),
      }),
    );

    renderApp(DETAIL_ROUTE);

    expect(await screen.findByText("Bu project'te playbook bulunamadı")).toBeInTheDocument();
    expect(screen.queryByText("site.yml")).not.toBeInTheDocument();
  });

  it("truncated sonucunda liste kırpıldı uyarısı gösterir", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse({ ...playbookResult, truncated: true }),
      }),
    );

    renderApp(DETAIL_ROUTE);

    const warning = await screen.findByText("Liste kırpıldı");
    expect(warning.closest("[role='alert']")).toHaveTextContent(
      /tarama sınırına ulaşıldı ve liste eksik/i,
    );
    // Kırpma uyarısı bulunan playbook'ları gizlemez.
    expect(within(await playbookTable()).getByText("site.yml")).toBeInTheDocument();
  });

  it("okunamayan dosya ve dizin sayaçlarını ayrı ayrı açıklar", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse({
          ...playbookResult,
          skipped_unreadable_files: 3,
          skipped_unreadable_directories: 2,
        }),
      }),
    );

    renderApp(DETAIL_ROUTE);

    await screen.findByText("Bazı girdiler okunamadı");
    expect(screen.getByText("3 dosya okunamadı ve listeye alınmadı.")).toBeInTheDocument();
    expect(screen.getByText("2 alt dizin listelenemedi ve taranamadı.")).toBeInTheDocument();
  });

  it("sayaçlar sıfırken gereksiz uyarı göstermez", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(playbookResult),
      }),
    );

    renderApp(DETAIL_ROUTE);

    within(await playbookTable()).getByText("site.yml");
    expect(screen.queryByText("Bazı girdiler okunamadı")).not.toBeInTheDocument();
    expect(screen.queryByText("Liste kırpıldı")).not.toBeInTheDocument();
  });

  it("project_inactive hatasını uygulanabilir biçimde gösterir", async () => {
    // Kayıt aktif göründüğü hâlde backend pasif diyorsa (başka bir sekmede
    // pasife alınmış olabilir) hata kullanıcıya açıklanır.
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: errorResponse(
          409,
          "project_inactive",
          "Pasif project üzerinde playbook keşfi yapılamaz.",
          { project_id: activeProject.id },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Project pasif durumda");
    expect(alert).toHaveTextContent(/geçmişe referans olarak saklanıyor/i);
  });

  it.each([
    ["missing", "Project dizini controller'da bulunamadı"],
    ["not_a_directory", "Project yolu artık bir dizin değil"],
    ["changed_during_scan", "Dizin tarama sırasında değişti"],
  ])("project_path_unavailable/%s durumunu açıklar", async (reason, expected) => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: errorResponse(
          409,
          "project_path_unavailable",
          "Project dizini artık mevcut değil.",
          { project_id: activeProject.id, reason },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expected);
    expect(alert).not.toHaveTextContent("reason");
  });

  it("pasif project'te keşif isteği hiç gönderilmez", async () => {
    const { requests } = installFetchMock(() => jsonResponse(inactiveProject));

    renderApp(`/projects/${inactiveProject.id}`);

    await screen.findByText("Pasif project'te keşif yapılmaz");
    expect(requests.some((request) => request.url.endsWith("/playbooks"))).toBe(false);
    expect(screen.getByText("Pasif (kayıt saklanıyor)")).toBeInTheDocument();
  });

  it("bilinmeyen project kimliğinde Project bulunamadı durumu gösterir", async () => {
    installFetchMock(() => errorResponse(404, "not_found", "Project bulunamadı: 999"));

    renderApp("/projects/999");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Project bulunamadı" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Project listesine dön" })).toBeInTheDocument();
  });

  it("sayısal olmayan kimlik için istek atmadan Project bulunamadı gösterir", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/projects/abc");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Project bulunamadı" }),
    ).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });

  it("bozuk tarih değerleri arayüzü çökertmez", async () => {
    installFetchMock(
      responder({
        project: jsonResponse({
          ...activeProject,
          created_at: "kesinlikle-tarih-degil",
          updated_at: null,
        }),
        playbooks: jsonResponse({
          ...playbookResult,
          scanned_at: 12345,
          playbooks: [{ ...playbookResult.playbooks[0], modified_at: "", size_bytes: null }],
        }),
      }),
    );

    renderApp(DETAIL_ROUTE);

    // Sayfa render olur ve içerik görünür.
    expect(
      await screen.findByRole("heading", { level: 2, name: activeProject.name }),
    ).toBeInTheDocument();
    expect(
      within(await playbookTable()).getByText("playbooks/tasks/deploy.yml"),
    ).toBeInTheDocument();

    // Çözümlenemeyen değerler yerine anlaşılır bir yer tutucu gösterilir.
    expect(screen.getByText("Oluşturulma").nextElementSibling).toHaveTextContent("Bilinmiyor");
    expect(screen.getByText("Güncellenme").nextElementSibling).toHaveTextContent("Bilinmiyor");
    expect(screen.queryByText("Invalid Date")).not.toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });
});

describe("Project detayı — bağlı inventory özeti (R1-V3F0)", () => {
  it("bağlı inventory yoksa kayıt eklemeye yönlendirir", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(emptyPlaybookResult),
        inventories: jsonResponse([]),
      }),
    );

    renderApp(DETAIL_ROUTE);

    expect(await screen.findByText("Bu project'e bağlı inventory yok")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Bu project için inventory kaydet" }),
    ).toHaveAttribute("href", `/inventories/new?project_id=${activeProject.id}`);
  });

  it("bağlı inventory'leri kendi detay sayfalarına bağlantı vererek listeler", async () => {
    installFetchMock(
      responder({
        project: jsonResponse(activeProject),
        playbooks: jsonResponse(emptyPlaybookResult),
        inventories: jsonResponse([linkedInventory]),
      }),
    );

    renderApp(DETAIL_ROUTE);

    expect(
      await screen.findByRole("link", { name: linkedInventory.name }),
    ).toHaveAttribute("href", `/inventories/${linkedInventory.id}`);
  });

  it("pasif project'te inventory bölümü istek göndermez", async () => {
    const { requests } = installFetchMock(() => jsonResponse(inactiveProject));

    renderApp(`/projects/${inactiveProject.id}`);

    await screen.findByText("Pasif project'te inventory bağlantısı gösterilmez");
    expect(requests.some((request) => request.url.includes("/api/inventories"))).toBe(false);
  });
});
