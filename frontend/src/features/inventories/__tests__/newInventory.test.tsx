import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { fetchProjectInventories } from "../../executions/api";
import { executionKeys } from "../../executions/queryKeys";
import { activeProject, emptyInventoryContents } from "../../../test/fixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";
import { fetchInventories } from "../api";
import { InventoryForm } from "../components/InventoryForm";
import { inventoryKeys } from "../queryKeys";
import type { Inventory } from "../types";

/** POST sonrası dönen yeni kayıt. */
const createdInventory: Inventory = {
  id: 42,
  project_id: null,
  name: "Yeni envanter",
  path: "/srv/ansible-data/inventories/new.ini",
  source_type: "ini",
  created_at: "2026-08-01T09:00:00Z",
  updated_at: "2026-08-01T09:00:00Z",
};

/** Yönlendirilen detay sayfasının ihtiyaç duyduğu `/hosts` isteğini karşılar. */
function detailResponder(request: RecordedRequest) {
  if (request.url.endsWith("/hosts")) {
    return jsonResponse(emptyInventoryContents);
  }
  return undefined;
}

async function fillForm(values?: { name?: string; path?: string }) {
  const user = userEvent.setup();
  // Form yalnızca project listesi (`useProjects()`) yüklendikten sonra render
  // edilir; ilk alan bu yüzden `findBy` ile beklenir.
  if (values?.name !== undefined) {
    await user.type(await screen.findByLabelText("Inventory adı"), values.name);
  }
  if (values?.path !== undefined) {
    await user.type(screen.getByLabelText("Controller'daki inventory dosya yolu"), values.path);
  }
  return user;
}

function submitButton() {
  return screen.getByRole("button", { name: "Inventory'yi kaydet" });
}

describe("Yeni inventory formu", () => {
  it("inventory listesindeki bağlantı yeni kayıt sayfasını açar", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/inventories")) {
        return jsonResponse([]);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Yeni inventory kaydet" }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Yeni inventory kaydet" }),
    ).toBeInTheDocument();
  });

  it("boş inventory listesindeki boş durum mesajından da açılabilir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/inventories")) {
        return jsonResponse([]);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories");
    const emptyState = (await screen.findByText("Henüz inventory kaydı yok")).closest(
      ".status",
    ) as HTMLElement;

    const user = userEvent.setup();
    await user.click(within(emptyState).getByRole("link", { name: "Yeni inventory kaydet" }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Yeni inventory kaydet" }),
    ).toBeInTheDocument();
  });

  it("standalone kayıt exact body gönderir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return jsonResponse(createdInventory, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    const user = await fillForm({
      name: "  Yeni envanter  ",
      path: "  /srv/ansible-data/inventories/new.ini  ",
    });
    await user.click(submitButton());

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.url).toMatch(/\/api\/inventories$/);
    expect(post?.body).toEqual({
      name: "Yeni envanter",
      path: "/srv/ansible-data/inventories/new.ini",
      source_type: "ini",
      project_id: null,
    });
  });

  it("project'e bağlı kayıt exact body gönderir ve doğru project_id'yi taşır", async () => {
    const linkedCreated: Inventory = { ...createdInventory, project_id: activeProject.id };
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return jsonResponse(linkedCreated, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([activeProject]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    await screen.findByLabelText("Bağlı project");
    const user = await fillForm({
      name: "Prod envanteri",
      path: "/srv/ansible/web/inventories/production.ini",
    });
    await user.selectOptions(screen.getByLabelText("Bağlı project"), activeProject.name);
    await user.selectOptions(screen.getByLabelText("Dosya biçimi"), "YAML");
    await user.click(submitButton());

    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.body).toEqual({
      name: "Prod envanteri",
      path: "/srv/ansible/web/inventories/production.ini",
      source_type: "yaml",
      project_id: activeProject.id,
    });
  });

  it("geçerli ?project_id= aktif project'i önceden seçer", async () => {
    installFetchMock((request) =>
      request.url.endsWith("/api/projects") ? jsonResponse([activeProject]) : undefined,
    );

    renderApp(`/inventories/new?project_id=${activeProject.id}`);

    const select = (await screen.findByLabelText("Bağlı project")) as HTMLSelectElement;
    expect(select.value).toBe(String(activeProject.id));
  });

  it.each([["abc"], ["-1"], ["0"], ["999"]])(
    "geçersiz ?project_id=%s ön seçim yapmaz ve POST'a taşınmaz",
    async (rawProjectId) => {
      const { requests } = installFetchMock((request) => {
        if (request.method === "POST") {
          return jsonResponse(createdInventory, 201);
        }
        if (request.url.endsWith("/api/projects")) {
          return jsonResponse([activeProject]);
        }
        return detailResponder(request);
      });

      renderApp(`/inventories/new?project_id=${rawProjectId}`);

      const select = (await screen.findByLabelText("Bağlı project")) as HTMLSelectElement;
      expect(select.value).toBe("");

      const user = await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
      await user.click(submitButton());

      await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
      expect(requests.find((r) => r.method === "POST")?.body).toMatchObject({
        project_id: null,
      });
    },
  );

  it("pasif (aktif listede olmayan) project query parametresi POST'a taşınmadan standalone açılır", async () => {
    // useProjects() yalnızca aktif project'leri döndürür; pasif bir project_id
    // bu yüzden listede hiç yer almaz ve doğal olarak elenir.
    installFetchMock((request) =>
      request.url.endsWith("/api/projects") ? jsonResponse([activeProject]) : undefined,
    );

    const inactiveProjectId = activeProject.id + 100;
    renderApp(`/inventories/new?project_id=${inactiveProjectId}`);

    const select = (await screen.findByLabelText("Bağlı project")) as HTMLSelectElement;
    expect(select.value).toBe("");
  });

  it("boş ad veya boş path ile POST yapmaz", async () => {
    const { requests } = installFetchMock((request) =>
      request.url.endsWith("/api/projects") ? jsonResponse([]) : undefined,
    );

    renderApp("/inventories/new");
    await screen.findByLabelText("Bağlı project");
    const user = userEvent.setup();
    await user.click(submitButton());

    expect(await screen.findByText("Inventory adı zorunludur.")).toBeInTheDocument();
    expect(screen.getByText("Controller'daki inventory dosya yolu zorunludur.")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "POST")).toHaveLength(0);
  });

  it("istek sürerken ikinci tıklama ikinci POST üretmez", async () => {
    const pendingPost = deferred<unknown>();
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return pendingPost.promise;
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
    const submit = submitButton();
    await user.click(submit);

    const pendingButton = await screen.findByRole("button", { name: "Kaydediliyor…" });
    expect(pendingButton).toBeDisabled();
    await user.click(pendingButton);

    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    pendingPost.resolve(jsonResponse(createdInventory, 201));
  });

  it("pending render'ı beklemeden, aynı senkron çevrimde art arda iki submit yalnızca bir POST üretir", async () => {
    // `mutation.isPending`'in `isSubmitting` prop'una yansıması zamanlanmıştır.
    // Bu test, önceki testin aksine, ilk `onSubmit` çağrısı ile bu prop'un
    // `true` olarak render edildiği an arasındaki pencereyi bilerek hedefler:
    // pending buton render'ı hiç beklenmeden, aynı senkron olay çevriminde art
    // arda iki `submit` event'i gönderilir. Yalnızca `isSubmitting` prop'una
    // dayanan bir koruma bu pencerede ikinci event'i durduramaz; senkron bir
    // ref kilidi gerekir. Bu test düzeltme öncesinde kırmızıydı.
    const pendingPost = deferred<unknown>();
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return pendingPost.promise;
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
    const form = document.querySelector("form") as HTMLFormElement;

    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);

    pendingPost.resolve(jsonResponse(createdInventory, 201));
    await screen.findByRole("heading", { level: 2, name: createdInventory.name });
  });

  it("hata sonrası kilit açılır ve kullanıcı gerçekten yeniden gönderebilir", async () => {
    let postCount = 0;
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        postCount += 1;
        return postCount === 1
          ? errorResponse(422, "path_not_found", "Dosya bulunamadı.")
          : jsonResponse(createdInventory, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
    await user.click(submitButton());

    await screen.findByRole("alert");
    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);

    // Buton yeniden etkin: kilit mutation tamamlandığında (bu durumda hatayla)
    // açılmış olmalı. İkinci tıklama gerçek, ikinci bir POST üretmelidir.
    expect(submitButton()).not.toBeDisabled();
    await user.click(submitButton());

    await waitFor(() =>
      expect(requests.filter((r) => r.method === "POST")).toHaveLength(2),
    );
    await screen.findByRole("heading", { level: 2, name: createdInventory.name });
  });

  it("buton devre dışı olmasa bile ikinci submit olayını yok sayar", async () => {
    const pendingPost = deferred<unknown>();
    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        return pendingPost.promise;
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
    const form = document.querySelector("form") as HTMLFormElement;
    await user.click(submitButton());
    await screen.findByRole("button", { name: "Kaydediliyor…" });

    await act(async () => {
      fireEvent.submit(form);
      fireEvent.submit(form);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    pendingPost.resolve(jsonResponse(createdInventory, 201));
  });

  it("başarıda /inventories/{id} sayfasına yönlendirir", async () => {
    installFetchMock((request) => {
      if (request.method === "POST") {
        return jsonResponse(createdInventory, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return detailResponder(request);
    });

    renderApp("/inventories/new");
    const user = await fillForm({
      name: createdInventory.name,
      path: createdInventory.path,
    });
    await user.click(submitButton());

    expect(
      await screen.findByRole("heading", { level: 2, name: createdInventory.name }),
    ).toBeInTheDocument();
  });

  it("başarıdan sonra inventory listesi ve project'e bağlı inventory sorgusu tazelenir", async () => {
    // Bu test, sorguları mutation'dan ÖNCE gerçekten cache'e alır
    // (`prefetchQuery`). Test client'ta `staleTime: Infinity` olduğundan, bir
    // sorgu daha önce hiç gözlemlenmediyse her mount **zaten** ağa gider —
    // invalidation olmasa da geçerdi. Cache'i önceden doldurarak, aşağıdaki
    // "yeni kayıt görünür" doğrulaması yalnızca gerçek bir invalidation/refetch
    // olduğunda geçer; `useCreateInventory`'deki ilgili `invalidateQueries`
    // çağrılarından biri kaldırılırsa bu test kırmızı olur.
    const linkedCreated: Inventory = { ...createdInventory, project_id: activeProject.id };
    let inventories: Inventory[] = [];
    let projectInventories: Inventory[] = [];

    const { requests } = installFetchMock((request) => {
      if (request.method === "POST") {
        inventories = [linkedCreated];
        projectInventories = [linkedCreated];
        return jsonResponse(linkedCreated, 201);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([activeProject]);
      }
      // Project detayındaki plan formunun okuduğu, project'e bağlı liste.
      if (request.url.includes("/api/inventories?")) {
        return jsonResponse(projectInventories);
      }
      if (request.url.endsWith("/api/inventories")) {
        return jsonResponse(inventories);
      }
      if (request.url.endsWith("/playbooks")) {
        return jsonResponse({
          project_id: activeProject.id,
          playbooks: [],
          skipped_unreadable_files: 0,
          skipped_unreadable_directories: 0,
          truncated: false,
          scanned_at: "2026-07-28T10:00:00Z",
        });
      }
      if (request.url.endsWith(`/api/projects/${activeProject.id}`)) {
        return jsonResponse(activeProject);
      }
      return detailResponder(request);
    });

    const { queryClient } = renderApp(`/inventories/new?project_id=${activeProject.id}`);

    // Her iki sorguyu da (boş) sonuçlarıyla önceden cache'e al.
    await act(async () => {
      await queryClient.prefetchQuery({
        queryKey: inventoryKeys.list(),
        queryFn: fetchInventories,
      });
      await queryClient.prefetchQuery({
        queryKey: executionKeys.projectInventories(activeProject.id),
        queryFn: () => fetchProjectInventories(activeProject.id),
      });
    });

    expect(requests.filter((r) => r.method === "GET" && r.url.endsWith("/api/inventories")))
      .toHaveLength(1);
    expect(
      requests.filter((r) => r.method === "GET" && r.url.includes("/api/inventories?")),
    ).toHaveLength(1);

    const user = await fillForm({
      name: linkedCreated.name,
      path: linkedCreated.path,
    });
    await user.click(submitButton());

    await screen.findByRole("heading", { level: 2, name: linkedCreated.name });

    // 1) Inventory listesi tazelenmiş: yeni kayıt orada görünür — bu yalnızca
    //    cache önceden dolu olduğu hâlde ikinci bir GET yapıldıysa mümkündür.
    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    expect(
      await screen.findByRole("link", { name: linkedCreated.name }),
    ).toBeInTheDocument();
    expect(
      requests.filter((r) => r.method === "GET" && r.url.endsWith("/api/inventories")).length,
    ).toBeGreaterThan(1);

    // 2) Bağlı project'in çalıştırma planı formu da tazelenmiş liste görür.
    await user.click(screen.getByRole("link", { name: `Project #${activeProject.id}` }));
    await screen.findByRole("heading", { level: 2, name: activeProject.name });
    expect(
      await screen.findByRole("option", { name: linkedCreated.name }),
    ).toBeInTheDocument();
    expect(
      requests.filter((r) => r.method === "GET" && r.url.includes("/api/inventories?")).length,
    ).toBeGreaterThan(1);
  });

  it.each([
    ["path_not_allowed", 403, "Bu yola izin verilmiyor"],
    ["path_not_found", 422, "Dosya controller'da bulunamadı"],
    ["path_not_a_file", 422, "Yol bir dosya değil"],
    ["invalid_path", 422, "Dosya yolu geçersiz"],
    ["inventory_path_outside_project", 403, "Dosya, seçilen project'in dışında"],
    ["project_inactive", 409, "Project pasif durumda"],
    ["request_validation_error", 422, "Gönderilen bilgiler geçersiz"],
  ])("%s hatasını kullanıcı dostu metne çevirir", async (code, status, expected) => {
    installFetchMock((request) => {
      if (request.method === "POST") {
        return errorResponse(status, code, "ham backend mesajı: /etc/secret/traceback.py");
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/gizli/yol" });
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expected);
    expect(alert).not.toHaveTextContent("/etc/secret/traceback.py");
  });

  it("ağ hatasını anlaşılır biçimde gösterir", async () => {
    installFetchMock((request) => {
      if (request.method === "POST") {
        throw new TypeError("Failed to fetch");
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/srv/ansible-data/inv.ini" });
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend'e ulaşılamadı");
  });

  it("ham error details veya mutlak backend path bilgisi gösterilmez", async () => {
    installFetchMock((request) => {
      if (request.method === "POST") {
        return errorResponse(403, "path_not_allowed", "backend mesajı", {
          allowed_roots: ["/srv/ansible-data"],
          attempted_path: "/etc/shadow-like-secret",
        });
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = await fillForm({ name: "Envanter", path: "/etc/shadow-like-secret" });
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("allowed_roots");
    expect(alert).not.toHaveTextContent("attempted_path");
    expect(alert).not.toHaveTextContent("/etc/shadow-like-secret");
  });
});

describe("InventoryForm — güncel olmayan (stale) project seçimi", () => {
  it("seçili project listeden kaldırılınca standalone'a döner, eski id POST'a taşınmaz ve project yeniden eklense de seçim kendiliğinden dirilmez", async () => {
    const onSubmit = vi.fn(() => Promise.resolve());

    const { rerender } = render(
      <InventoryForm
        projects={[activeProject]}
        initialProjectId={activeProject.id}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );

    const select = screen.getByLabelText("Bağlı project") as HTMLSelectElement;
    expect(select.value).toBe(String(activeProject.id));

    // 1) Project artık güncel listede yok (ör. pasife alındı ya da silindi).
    rerender(
      <InventoryForm
        projects={[]}
        initialProjectId={activeProject.id}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );

    // 2) Görünen seçim standalone'a düşer.
    await waitFor(() => expect(select.value).toBe(""));

    // 3) Formu gönder.
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Inventory adı"), "Envanter");
    await user.type(
      screen.getByLabelText("Controller'daki inventory dosya yolu"),
      "/srv/ansible-data/inv.ini",
    );
    await user.click(screen.getByRole("button", { name: "Inventory'yi kaydet" }));

    // 4) Gövdede project_id tam olarak null — eski id state'te dursa bile
    //    DOM'un ötesinde de taşınmamış olmalı.
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      name: "Envanter",
      path: "/srv/ansible-data/inv.ini",
      source_type: "ini",
      project_id: null,
    });

    // 5) Project listeye yeniden eklenir; kullanıcı yeniden seçmediği sürece
    //    eski seçim kendiliğinden geri gelmemeli.
    rerender(
      <InventoryForm
        projects={[activeProject]}
        initialProjectId={activeProject.id}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );
    expect(select.value).toBe("");
  });
});

describe("Yeni inventory formu — Gözat dialogu (R1-V3J0C)", () => {
  it("standalone: Gözat → dosya seçilir → path alanı doldurulur", async () => {
    const listing = {
      scope: "inventory",
      current_path: "/srv/ansible-data/inventories",
      target_kind: "file",
      entries: [
        {
          name: "lab.ini",
          path: "/srv/ansible-data/inventories/lab.ini",
          kind: "file",
          selectable: true,
        },
      ],
      truncated: false,
    };
    const { requests } = installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return jsonResponse(listing);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = userEvent.setup();
    await screen.findByLabelText("Bağlı project");

    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await user.click(await screen.findByRole("button", { name: /lab\.ini/ }));
    await user.click(screen.getByRole("button", { name: "Seç" }));

    const pathInput = screen.getByLabelText(
      "Controller'daki inventory dosya yolu",
    ) as HTMLInputElement;
    expect(pathInput.value).toBe("/srv/ansible-data/inventories/lab.ini");

    const browseRequest = requests.find((r) => r.url.includes("/api/controller-paths"));
    expect(browseRequest?.url).toContain("scope=inventory");
    expect(browseRequest?.url).not.toContain("project_id=");
  });

  it("project'e bağlı: dialog project_inventory scope'u ve doğru project_id'yi kullanır", async () => {
    const listing = {
      scope: "project_inventory",
      current_path: activeProject.path,
      target_kind: "file",
      entries: [
        {
          name: "production.ini",
          path: `${activeProject.path}/production.ini`,
          kind: "file",
          selectable: true,
        },
      ],
      truncated: false,
    };
    const { requests } = installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return jsonResponse(listing);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([activeProject]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Bağlı project"), activeProject.name);

    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await user.click(await screen.findByRole("button", { name: /production\.ini/ }));
    await user.click(screen.getByRole("button", { name: "Seç" }));

    const pathInput = screen.getByLabelText(
      "Controller'daki inventory dosya yolu",
    ) as HTMLInputElement;
    expect(pathInput.value).toBe(`${activeProject.path}/production.ini`);

    const browseRequest = requests.find((r) => r.url.includes("/api/controller-paths"));
    expect(browseRequest?.url).toContain("scope=project_inventory");
    expect(browseRequest?.url).toContain(`project_id=${activeProject.id}`);
  });

  it("project seçimi değişince açık dialog kapanır ve eski sonuç yeni seçime taşınmaz", async () => {
    const standaloneListing = {
      scope: "inventory",
      current_path: "/srv/ansible-data/inventories",
      target_kind: "file",
      entries: [
        {
          name: "standalone.ini",
          path: "/srv/ansible-data/inventories/standalone.ini",
          kind: "file",
          selectable: true,
        },
      ],
      truncated: false,
    };
    const projectListing = {
      scope: "project_inventory",
      current_path: activeProject.path,
      target_kind: "file",
      entries: [
        {
          name: "production.ini",
          path: `${activeProject.path}/production.ini`,
          kind: "file",
          selectable: true,
        },
      ],
      truncated: false,
    };
    installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return request.url.includes("project_id=")
          ? jsonResponse(projectListing)
          : jsonResponse(standaloneListing);
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([activeProject]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = userEvent.setup();
    await screen.findByLabelText("Bağlı project");

    // 1) Standalone hâlde dialogu aç; içerik yüklensin.
    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await screen.findByRole("button", { name: /standalone\.ini/ });

    // 2) Dialog açıkken project seçimini değiştir: form dialogu kapatır
    //    (`InventoryForm`'daki proje `<select>` `onChange`'i). Kapanışın
    //    görsel etkisi native `<dialog>`'a aittir ve jsdom'da ölçülemez; asıl
    //    garanti aşağıdaki 3-4. adımlardır: yeniden açılan dialog sıfırdan
    //    yüklenir ve eski (standalone) sonuçtan hiçbir iz taşımaz.
    await user.selectOptions(screen.getByLabelText("Bağlı project"), activeProject.name);

    // 3) Dialogu tekrar aç: artık project_inventory scope'unda, sıfırdan
    //    yüklenmiş — standalone'dan kalan hiçbir satır görünmez.
    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await screen.findByRole("button", { name: /production\.ini/ });
    expect(screen.queryByRole("button", { name: /standalone\.ini/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /production\.ini/ }));
    await user.click(screen.getByRole("button", { name: "Seç" }));

    // 4) Eski (standalone) yol asla forma yazılmadı; yalnız yeni seçim var.
    const pathInput = screen.getByLabelText(
      "Controller'daki inventory dosya yolu",
    ) as HTMLInputElement;
    expect(pathInput.value).toBe(`${activeProject.path}/production.ini`);
    expect(pathInput.value).not.toContain("standalone.ini");
  });

  it("dialog kapanınca (İptal) source type dropdown'u ve manuel giriş değişmeden kalır", async () => {
    installFetchMock((request) => {
      if (request.url.includes("/api/controller-paths")) {
        return jsonResponse({
          scope: "inventory",
          current_path: "/srv/ansible-data/inventories",
          target_kind: "file",
          entries: [],
          truncated: false,
        });
      }
      if (request.url.endsWith("/api/projects")) {
        return jsonResponse([]);
      }
      return undefined;
    });

    renderApp("/inventories/new");
    const user = userEvent.setup();
    await screen.findByLabelText("Bağlı project");
    await fillForm({ path: "/srv/elle-yazilmis.ini" });
    await user.selectOptions(screen.getByLabelText("Dosya biçimi"), "YAML");

    await user.click(screen.getByRole("button", { name: "Gözat…" }));
    await screen.findByText("Bu dizin boş.");
    await user.click(screen.getByRole("button", { name: "İptal" }));

    expect(
      (screen.getByLabelText("Controller'daki inventory dosya yolu") as HTMLInputElement).value,
    ).toBe("/srv/elle-yazilmis.ini");
    expect((screen.getByLabelText("Dosya biçimi") as HTMLSelectElement).value).toBe("yaml");
  });
});

describe("InventoryForm — Promise tabanlı submit kilidi", () => {
  it("pending onSubmit Promise'ı sürerken araya giren rerender kilidi erken açmaz; Promise settle olunca form yeniden kullanılabilir", async () => {
    // Bu test, kilidin `isSubmitting` prop'unun render'a yansımasına değil
    // doğrudan `onSubmit`'in döndürdüğü Promise'ın settle anına bağlı
    // olduğunu kanıtlar. `isSubmitting` testin sonuna kadar bilinçli olarak
    // hep `false` verilir: TanStack'in `isPending` yayımı henüz gelmeden
    // gerçekleşebilecek herhangi bir form-local veya parent rerender'ı temsil
    // eder. Eski (dependency array'siz effect'e dayanan) uygulamada bu
    // senaryoda kilit her rerender'da erken açılırdı; bu test o durumda
    // kırmızıydı.
    const pending = deferred<void>();
    const onSubmit = vi.fn(() => pending.promise);

    const { rerender } = render(
      <InventoryForm
        projects={[]}
        initialProjectId={null}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Inventory adı"), "Envanter");
    await user.type(
      screen.getByLabelText("Controller'daki inventory dosya yolu"),
      "/srv/ansible-data/inv.ini",
    );

    const form = document.querySelector("form") as HTMLFormElement;

    // 1) İlk (geçerli) submit — kilit alınır, Promise pending kalır.
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(1);

    // 2) TanStack'in `isPending` yayımından önce gerçekleşebilecek bir
    //    rerender'ı temsil eder — `isSubmitting` hâlâ `false`.
    rerender(
      <InventoryForm
        projects={[]}
        initialProjectId={null}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );

    // 3) Rerender sonrası ikinci submit; kilit hâlâ kapalı olmalı.
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(1);

    // 4) Pending sırasında birden fazla rerender daha — kilit yine açılmamalı.
    rerender(
      <InventoryForm
        projects={[]}
        initialProjectId={null}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );
    rerender(
      <InventoryForm
        projects={[]}
        initialProjectId={null}
        isSubmitting={false}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(1);

    // 5) Gerçek istek şimdi tamamlanır; `handleSubmit`'in `finally`'si de
    //    tamamlanmasını beklemek için `act` içinde `await` edilir.
    await act(async () => {
      pending.resolve(undefined);
      await pending.promise;
    });

    // 6) Kilit gerçekten açıldı: üçüncü submit yeni bir `onSubmit` çağrısı
    //    üretmeli — form tamamlanma sonrası gerçekten yeniden kullanılabilir.
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(2);
  });
});
