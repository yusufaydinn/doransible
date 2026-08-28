import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activeProject, emptyPlaybookResult } from "../../../test/fixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

/** Detay sayfasının ihtiyaç duyduğu istekleri karşılayan ortak yönlendirici. */
function detailResponder(request: RecordedRequest) {
  if (request.url.endsWith(`/api/projects/${activeProject.id}/playbooks`)) {
    return jsonResponse(emptyPlaybookResult);
  }
  if (request.url.endsWith(`/api/projects/${activeProject.id}`)) {
    return jsonResponse(activeProject);
  }
  return undefined;
}

async function fillForm(values?: { name?: string; path?: string; description?: string }) {
  const user = userEvent.setup();
  if (values?.name !== undefined) {
    await user.type(screen.getByLabelText("Project adı"), values.name);
  }
  if (values?.path !== undefined) {
    await user.type(screen.getByLabelText("Controller'daki dizin yolu"), values.path);
  }
  if (values?.description !== undefined) {
    await user.type(screen.getByLabelText("Açıklama (isteğe bağlı)"), values.description);
  }
  return user;
}

describe("Yeni project formu", () => {
  it("path alanının controller'daki dizini gösterdiğini açıkça anlatır", () => {
    installFetchMock(() => undefined);

    renderApp("/projects/new");

    // Açıklama metni alanla `aria-describedby` üzerinden ilişkilendirilmiştir;
    // ekran okuyucu alana odaklanınca bu uyarıyı da okur.
    const pathInput = screen.getByLabelText("Controller'daki dizin yolu");
    const hintId = pathInput.getAttribute("aria-describedby");
    expect(hintId).toBeTruthy();

    const hint = document.getElementById(hintId ?? "");
    expect(hint).toHaveTextContent(/DORAnsible controller/i);
    expect(hint).toHaveTextContent(/tarayıcı cihazı aynı makineyse.*kendi bilgisayarınızdaki/i);
  });

  it("zorunlu alanları doğrular ve istek göndermez", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/projects/new");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    const nameError = await screen.findByText("Project adı zorunludur.");
    const pathError = screen.getByText("Controller'daki dizin yolu zorunludur.");
    expect(requests).toHaveLength(0);

    // Hata mesajları ilgili alanla ilişkilendirilir ve alan geçersiz işaretlenir.
    const nameInput = screen.getByLabelText("Project adı");
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(nameInput.getAttribute("aria-describedby")).toContain(nameError.id);
    expect(screen.getByLabelText("Controller'daki dizin yolu").getAttribute("aria-describedby")).toContain(
      pathError.id,
    );
  });

  it("boşluk dışında içerik taşımayan ad için de hata gösterir", async () => {
    const { requests } = installFetchMock(() => undefined);

    renderApp("/projects/new");
    const user = await fillForm({ name: "   ", path: "/srv/ansible/web" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    expect(await screen.findByText("Project adı zorunludur.")).toBeInTheDocument();
    expect(requests).toHaveLength(0);
  });

  it("doğru JSON gövdesiyle POST gönderir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return jsonResponse(activeProject, 201);
      }
      return detailResponder(request);
    });

    renderApp("/projects/new");
    const user = await fillForm({
      name: "  Web sunucuları  ",
      path: "  /srv/ansible/web  ",
      description: "  Nginx ve sertifika yönetimi  ",
    });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.url).toMatch(/\/api\/projects$/);
    expect(post?.body).toEqual({
      name: "Web sunucuları",
      path: "/srv/ansible/web",
      description: "Nginx ve sertifika yönetimi",
    });
  });

  it("açıklama boşken description alanını hiç göndermez", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return jsonResponse(activeProject, 201);
      }
      return detailResponder(request);
    });

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/ansible/web" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    expect(requests.find((r) => r.method === "POST")?.body).toEqual({
      name: "Web",
      path: "/srv/ansible/web",
    });
  });

  it("istek sürerken ikinci gönderimi engeller", async () => {
    const pendingPost = deferred<unknown>();
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return pendingPost.promise;
      }
      return detailResponder(request);
    });

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/ansible/web" });
    const submit = screen.getByRole("button", { name: "Project'i kaydet" });
    await user.click(submit);

    const pendingButton = await screen.findByRole("button", { name: "Kaydediliyor…" });
    expect(pendingButton).toBeDisabled();
    await user.click(pendingButton);

    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    pendingPost.resolve(jsonResponse(activeProject, 201));
  });

  it("buton devre dışı olmasa bile ikinci submit olayını yok sayar", async () => {
    // Devre dışı buton kullanıcı için yeterli korumadır; bu test alttaki
    // ikinci katmanı (submit handler'ının kendi kilidini) tek başına ölçer.
    const pendingPost = deferred<unknown>();
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return pendingPost.promise;
      }
      return detailResponder(request);
    });

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/ansible/web" });
    const form = document.querySelector("form") as HTMLFormElement;
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));
    await screen.findByRole("button", { name: "Kaydediliyor…" });

    // `act` içinde beklemek şart: mutation zinciri asenkrondur, hemen yapılan
    // bir doğrulama kilit kaldırılsa bile "1 istek" görürdü.
    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    pendingPost.resolve(jsonResponse(activeProject, 201));
  });

  it("başarılı kayıttan sonra detay sayfasına yönlendirir ve listeyi tazeler", async () => {
    let projects: unknown[] = [];
    installFetchMock((request) => {
      if (request.method === "POST") {
        projects = [activeProject];
        return jsonResponse(activeProject, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse(projects);
      }
      return detailResponder(request);
    });

    renderApp("/projects");
    await screen.findByText("Henüz project kaydı yok");

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Yeni project ekle" }));
    await fillForm({ name: "Web sunucuları", path: "/srv/ansible/web" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    // 1) Kullanıcı yeni kaydın detayına gider.
    expect(
      await screen.findByRole("heading", { level: 2, name: activeProject.name }),
    ).toBeInTheDocument();

    // 2) Liste cache'i geçersizleştiği için listeye dönünce yeni kayıt görünür.
    //    (Test client'ta `staleTime: Infinity`; invalidation olmasa eski boş
    //    liste gösterilirdi.)
    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    expect(
      await screen.findByRole("link", { name: activeProject.name }),
    ).toBeInTheDocument();
  });

  it("path_not_allowed hatasını anlaşılır biçimde açıklar", async () => {
    installFetchMock((request) =>
      request.method === "POST"
        ? errorResponse(
            403,
            "path_not_allowed",
            "Path, izin verilen project root'larının dışında.",
          )
        : undefined,
    );

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/etc" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Bu dizine izin verilmiyor");
    expect(alert).toHaveTextContent(/İzin verilen dizini controller yöneticisinden/);
  });

  it.each([
    ["path_not_found", 422, "Dizin bulunamadı"],
    ["path_not_a_directory", 422, "Yol bir dizin değil"],
    ["invalid_path", 422, "Dizin yolu geçersiz"],
  ])("%s hatasını kendi mesajıyla gösterir", async (code, status, expected) => {
    installFetchMock((request) =>
      request.method === "POST" ? errorResponse(status, code, "backend mesajı") : undefined,
    );

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/yok" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(expected);
  });

  it("aktif duplicate kaydı için mevcut kayda bağlantı verir", async () => {
    installFetchMock((request) =>
      request.method === "POST"
        ? errorResponse(409, "project_already_exists", "Bu dizin zaten kayıtlı.", {
            project_id: 7,
            is_active: true,
          })
        : undefined,
    );

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/ansible/web" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Bu dizin zaten kayıtlı");
    expect(alert).not.toHaveTextContent(/pasif/i);
    expect(screen.getByRole("link", { name: "Mevcut kaydı görüntüle" })).toHaveAttribute(
      "href",
      "/projects/7",
    );
  });

  it("pasif duplicate kaydını ayrı ve açık biçimde anlatır", async () => {
    installFetchMock((request) =>
      request.method === "POST"
        ? errorResponse(
            409,
            "project_already_exists",
            "Bu dizin daha önce kaydedilmiş ve şu anda pasif durumda.",
            { project_id: 8, is_active: false },
          )
        : undefined,
    );

    renderApp("/projects/new");
    const user = await fillForm({ name: "Web", path: "/srv/ansible/web" });
    await user.click(screen.getByRole("button", { name: "Project'i kaydet" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Bu dizin daha önce kaydedilmiş");
    expect(alert).toHaveTextContent(/pasif durumda/);
    expect(screen.getByRole("link", { name: "Mevcut kaydı görüntüle" })).toHaveAttribute(
      "href",
      "/projects/8",
    );
    // `details` nesnesi ham JSON olarak basılmaz.
    expect(alert).not.toHaveTextContent("project_id");
    expect(alert).not.toHaveTextContent("is_active");
  });
});

describe("Yeni project formu — Gözat dialogu (R1-V3J0C)", () => {
  it("Gözat → klasöre gir → seç akışı path alanını doldurur; alan hâlâ elle düzenlenebilir", async () => {
    const rootListing = {
      scope: "project",
      current_path: "/srv/ansible",
      target_kind: "directory",
      entries: [
        { name: "web", path: "/srv/ansible/web", kind: "directory", selectable: true },
      ],
      truncated: false,
    };
    const webListing = {
      scope: "project",
      current_path: "/srv/ansible/web",
      target_kind: "directory",
      entries: [],
      truncated: false,
    };

    const { requests } = installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return request.url.includes("path=")
          ? jsonResponse(webListing)
          : jsonResponse(rootListing);
      }
      return undefined;
    });

    renderApp("/projects/new");
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await user.click(await screen.findByRole("button", { name: "Aç" }));
    await user.click(await screen.findByRole("button", { name: "Seç" }));

    const pathInput = screen.getByLabelText("Controller'daki dizin yolu") as HTMLInputElement;
    expect(pathInput.value).toBe("/srv/ansible/web");
    expect(
      requests.some((r) => r.url.includes("/api/controller-paths") && r.url.includes("scope=project")),
    ).toBe(true);

    // Dialog kapandı, alan hâlâ elle düzenlenebilir — picker tek yetkili yol
    // değildir.
    await user.clear(pathInput);
    await user.type(pathInput, "/srv/ansible/elle-yazilan");
    expect(pathInput.value).toBe("/srv/ansible/elle-yazilan");
  });

  it("dialog İptal ile kapanınca path alanı değişmez", async () => {
    installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return jsonResponse({
          scope: "project",
          current_path: "/srv/ansible",
          target_kind: "directory",
          entries: [],
          truncated: false,
        });
      }
      return undefined;
    });

    renderApp("/projects/new");
    const user = await fillForm({ path: "/srv/elle-yazilmis" });

    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await screen.findByText("Bu dizin boş.");
    await user.click(screen.getByRole("button", { name: "İptal" }));

    expect(
      (screen.getByLabelText("Controller'daki dizin yolu") as HTMLInputElement).value,
    ).toBe("/srv/elle-yazilmis");
  });
});
