import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activeProject, emptyPlaybookResult, inactiveProject } from "../../../test/fixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";

const DETAIL_ROUTE = `/projects/${activeProject.id}`;
const DEACTIVATE_LABEL = "Project kaydını pasife al";
const CONFIRM_LABEL = "Evet, kaydı pasife al";

function baseResponder(request: RecordedRequest) {
  if (request.url.endsWith("/playbooks")) {
    return jsonResponse(emptyPlaybookResult);
  }
  // Plan formunun okuduğu, project'e bağlı inventory listesi (R1-V1).
  if (request.url.includes("/api/inventories")) {
    return jsonResponse([]);
  }
  if (request.method === "GET") {
    return jsonResponse(activeProject);
  }
  return undefined;
}

describe("Project'i pasife alma", () => {
  it("işlemin dosyaları silmediğini açıkça yazar ve 'sil' ifadesini tek başına kullanmaz", async () => {
    installFetchMock(baseResponder);

    renderApp(DETAIL_ROUTE);

    const section = (await screen.findByRole("heading", { level: 3, name: DEACTIVATE_LABEL }))
      .parentElement as HTMLElement;
    expect(section).toHaveTextContent(/hiçbir dosyayı silmez/i);
    expect(section).toHaveTextContent(/Project dizini, playbook'lar ve roller diskte olduğu gibi kalır/i);
    expect(section).toHaveTextContent(/yalnızca uygulamadaki kayıt pasife alınır/i);

    // Butonun kendisi de "Sil" demez.
    expect(screen.queryByRole("button", { name: /^sil$/i })).not.toBeInTheDocument();
  });

  it("ilk tıklamada DELETE göndermez, önce onay ister", async () => {
    const { requests } = installFetchMock(baseResponder);

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));

    expect(requests.some((request) => request.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: CONFIRM_LABEL })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Vazgeç" })).toBeInTheDocument();
  });

  it("onay açıldığında odağı onay butonuna taşır", async () => {
    installFetchMock(baseResponder);

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: CONFIRM_LABEL })).toHaveFocus();
    });
  });

  it("vazgeçildiğinde istek gönderilmez ve başlangıç durumuna dönülür", async () => {
    const { requests } = installFetchMock(baseResponder);

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));
    await user.click(screen.getByRole("button", { name: "Vazgeç" }));

    expect(requests.some((request) => request.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: DEACTIVATE_LABEL })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: CONFIRM_LABEL })).not.toBeInTheDocument();
  });

  it("onaydan sonra DELETE çağrısı yapar ve sonucu gösterir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.method === "DELETE") {
        return jsonResponse({ ...activeProject, is_active: false });
      }
      return baseResponder(request);
    });

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));
    await user.click(screen.getByRole("button", { name: CONFIRM_LABEL }));

    expect(await screen.findByText("Project kaydı pasife alındı")).toBeInTheDocument();

    const deleteRequests = requests.filter((request) => request.method === "DELETE");
    expect(deleteRequests).toHaveLength(1);
    expect(deleteRequests[0]?.url).toMatch(
      new RegExp(`/api/projects/${activeProject.id}$`),
    );
  });

  it("işlem sürerken butonları kilitler", async () => {
    const pendingDelete = deferred<unknown>();
    installFetchMock((request) => {
      if (request.method === "DELETE") {
        return pendingDelete.promise;
      }
      return baseResponder(request);
    });

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));
    await user.click(screen.getByRole("button", { name: CONFIRM_LABEL }));

    const pending = await screen.findByRole("button", { name: "Pasife alınıyor…" });
    expect(pending).toBeDisabled();
    expect(screen.getByRole("button", { name: "Vazgeç" })).toBeDisabled();
    pendingDelete.resolve(jsonResponse({ ...activeProject, is_active: false }));
  });

  it("başarıdan sonra liste cache'ini tazeler", async () => {
    let projects: unknown[] = [activeProject];
    installFetchMock((request) => {
      if (request.method === "DELETE") {
        projects = [];
        return jsonResponse({ ...activeProject, is_active: false });
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse(projects);
      }
      return baseResponder(request);
    });

    renderApp("/projects");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: activeProject.name }));

    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));
    await user.click(screen.getByRole("button", { name: CONFIRM_LABEL }));
    await screen.findByText("Project kaydı pasife alındı");

    // Liste sorgusu geçersizleştiği için pasif kayıt varsayılan listeden düşer.
    // (Test client'ta `staleTime: Infinity`; invalidation olmasa eski liste kalırdı.)
    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    expect(await screen.findByText("Henüz project kaydı yok")).toBeInTheDocument();
  });

  it("DELETE hatasını kullanıcıya gösterir", async () => {
    installFetchMock((request) => {
      if (request.method === "DELETE") {
        return errorResponse(404, "not_found", "Project bulunamadı: 7");
      }
      return baseResponder(request);
    });

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: DEACTIVATE_LABEL }));
    await user.click(screen.getByRole("button", { name: CONFIRM_LABEL }));

    expect(await screen.findByText("Kayıt bulunamadı")).toBeInTheDocument();
  });

  it("pasif project detayında yeniden etkinleştirme butonu göstermez", async () => {
    installFetchMock(() => jsonResponse(inactiveProject));

    renderApp(`/projects/${inactiveProject.id}`);

    await screen.findByRole("heading", { level: 3, name: "Kaydın durumu" });
    expect(screen.queryByRole("button", { name: /etkinleştir/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: DEACTIVATE_LABEL })).not.toBeInTheDocument();
    expect(
      screen.getByText(/Kaydı arayüzden yeniden etkinleştirmek şu anda mümkün değil/i),
    ).toBeInTheDocument();
  });
});
