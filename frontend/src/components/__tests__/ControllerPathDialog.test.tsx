import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

// Gerçek `styles.css` yan etkili olarak yüklenir (vitest.config'teki `css:
// true` sayesinde jsdom'a gerçekten enjekte edilir — bkz.
// `styleContrast.test.ts`). Bu, aşağıdaki görünürlük testlerinin component
// prop'unu tekrar assert eden vacuous bir test olmasını değil, gerçek CSS
// kaskadının `<dialog>` görünürlüğünü doğru sonuçlandırdığını ölçmesini
// sağlar (LIVE-UI-FIX1).
import "../../styles.css";
import { deferred, errorResponse, installFetchMock, jsonResponse } from "../../test/harness";
import { ControllerPathDialog } from "../ControllerPathDialog";

/**
 * `ControllerPathDialog`'un kendi navigasyon/seçim mantığını, ProjectForm ve
 * InventoryForm'dan bağımsız olarak ölçer (`InventoryForm — güncel olmayan
 * (stale) project seçimi` testlerindeki `render`/`rerender` deseniyle aynı
 * yaklaşım). Formlara bağlı akışlar (`newProject.test.tsx`,
 * `newInventory.test.tsx`) bu bileşeni gerçek bir form içinde ayrıca kapsar.
 */

const projectResponse = {
  scope: "project",
  current_path: "/srv/ansible",
  target_kind: "directory",
  entries: [
    { name: "web", path: "/srv/ansible/web", kind: "directory", selectable: true },
    { name: "site.yml", path: "/srv/ansible/site.yml", kind: "file", selectable: false },
  ],
  truncated: false,
};

const webResponse = {
  scope: "project",
  current_path: "/srv/ansible/web",
  target_kind: "directory",
  entries: [],
  truncated: false,
};

/**
 * Ortak `deferred()` yardımcısı yalnızca `resolve` sunar; bu dosyadaki tek
 * bir test gerçek bir `AbortController.abort()`'un ürettiği türden bir
 * **reddi** simüle etmek için `reject`'e de ihtiyaç duyar. Paylaşılan
 * `test/harness.tsx` bu yamanın kapsamı dışında tutulduğu için yalnızca
 * burada, yerel bir varyant olarak tanımlanır.
 */
function deferredWithReject<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((innerResolve, innerReject) => {
    resolve = innerResolve;
    reject = innerReject;
  });
  return { promise, resolve, reject };
}

function openDialog(props: {
  scope?: "project" | "inventory" | "project_inventory";
  projectId?: number | null;
  onSelect?: (path: string) => void;
  onCancel?: () => void;
}) {
  const onSelect = props.onSelect ?? vi.fn();
  const onCancel = props.onCancel ?? vi.fn();
  const utils = render(
    <ControllerPathDialog
      open={false}
      scope={props.scope ?? "project"}
      projectId={props.projectId}
      onSelect={onSelect}
      onCancel={onCancel}
    />,
  );
  utils.rerender(
    <ControllerPathDialog
      open
      scope={props.scope ?? "project"}
      projectId={props.projectId}
      onSelect={onSelect}
      onCancel={onCancel}
    />,
  );
  return { ...utils, onSelect, onCancel };
}

describe("ControllerPathDialog", () => {
  it("açıldığında path olmadan ve doğru scope ile çağrı yapar", async () => {
    const { requests } = installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });

    await screen.findByRole("button", { name: /web/ });
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toContain("scope=project");
    expect(requests[0]?.url).not.toContain("path=");
  });

  it("project_inventory scope'unda project_id sorguya eklenir", async () => {
    const { requests } = installFetchMock(() => jsonResponse(webResponse));

    openDialog({ scope: "project_inventory", projectId: 7 });

    await screen.findByText("Bu dizin boş.");
    expect(requests[0]?.url).toContain("scope=project_inventory");
    expect(requests[0]?.url).toContain("project_id=7");
  });

  it("dialog uygun bir başlığa `aria-labelledby` ile bağlanır", async () => {
    installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });
    await screen.findByRole("button", { name: /web/ });

    const dialog = document.querySelector("dialog") as HTMLDialogElement;
    const labelledBy = dialog.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy ?? "")).toHaveTextContent("Bir klasör seçin");
  });

  it("Aç ile alt dizine girer, Geri ile bir önceki listeye döner", async () => {
    const { requests } = installFetchMock((request) => {
      if (!request.url.includes("path=")) {
        return jsonResponse(projectResponse);
      }
      return jsonResponse(webResponse);
    });

    openDialog({ scope: "project" });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Aç" }));
    await screen.findByText("Bu dizin boş.");
    expect(requests[1]?.url).toContain(encodeURIComponent("/srv/ansible/web"));

    const backButton = screen.getByRole("button", { name: "Geri" });
    expect(backButton).not.toBeDisabled();
    await user.click(backButton);

    await screen.findByRole("button", { name: /web/ });
    expect(requests).toHaveLength(3);
  });

  /**
   * Geri navigasyonu `<StrictMode>` altında **tam olarak bir** browse isteği
   * üretmelidir (R1-V3J0CF2 regresyon testi).
   *
   * Uygulama `main.tsx`'te gerçekten `<StrictMode>` kullanır ve React
   * development modunda `setState` updater fonksiyonlarını bilinçli olarak iki
   * kez çağırır. Geri'nin yan etkisi (`load`) bir `setStack` updater'ının
   * içinde durduğu sürece tek bir tıklama iki istek, iki abort ve iki
   * `requestId` sıçraması üretiyordu.
   *
   * StrictMode ilk mount effect'lerini de tekrarladığı için **toplam** istek
   * sayısı sabit varsayılamaz; ölçüm Geri tıklamasından önceki sayı baz
   * alınarak yapılır.
   */
  it("StrictMode altında tek Geri tıklaması tam olarak bir browse isteği üretir", async () => {
    const webWithChild = {
      scope: "project",
      current_path: "/srv/ansible/web",
      target_kind: "directory",
      entries: [
        { name: "deep", path: "/srv/ansible/web/deep", kind: "directory", selectable: true },
      ],
      truncated: false,
    };
    const deepResponse = {
      scope: "project",
      current_path: "/srv/ansible/web/deep",
      target_kind: "directory",
      entries: [],
      truncated: false,
    };
    const { requests } = installFetchMock((request) => {
      // Sıra önemlidir: derin yolun sorgusu, üst dizinin kodlanmış hâlini de
      // içerir.
      if (request.url.includes(encodeURIComponent("/srv/ansible/web/deep"))) {
        return jsonResponse(deepResponse);
      }
      if (request.url.includes(encodeURIComponent("/srv/ansible/web"))) {
        return jsonResponse(webWithChild);
      }
      if (!request.url.includes("path=")) {
        return jsonResponse(projectResponse);
      }
      return undefined;
    });

    render(
      <StrictMode>
        <ControllerPathDialog open scope="project" onSelect={vi.fn()} onCancel={vi.fn()} />
      </StrictMode>,
    );
    const user = userEvent.setup();

    // Kök → /srv/ansible/web → /srv/ansible/web/deep (her adımda tek "Aç").
    await user.click(await screen.findByRole("button", { name: "Aç" }));
    await screen.findByRole("button", { name: /deep/ });
    await user.click(screen.getByRole("button", { name: "Aç" }));
    await screen.findByText("Bu dizin boş.");

    const requestsBeforeBack = requests.length;

    await user.click(screen.getByRole("button", { name: "Geri" }));

    // Görünüm bir önceki dizine döner…
    expect(
      await screen.findByText("Konum ve seçili klasör: /srv/ansible/web"),
    ).toBeInTheDocument();

    // …ve bunun için tam olarak bir yeni istek yapılmıştır.
    expect(requests).toHaveLength(requestsBeforeBack + 1);
    const backRequest = requests[requestsBeforeBack];
    expect(backRequest?.url).toContain(encodeURIComponent("/srv/ansible/web"));
    expect(backRequest?.url).not.toContain(encodeURIComponent("/srv/ansible/web/deep"));
  });

  it("kökte Geri devre dışıdır", async () => {
    installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });

    expect(await screen.findByRole("button", { name: "Geri" })).toBeDisabled();
  });

  it("seçilebilir olmayan bir satır tıklansa da seçim değişmez", async () => {
    installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });
    const user = userEvent.setup();

    const fileRow = await screen.findByRole("button", { name: /site\.yml/ });
    expect(fileRow).toBeDisabled();
    await user.click(fileRow);
    expect(fileRow).toHaveAttribute("aria-pressed", "false");
  });

  it("Seç, seçili path ile onSelect'i tam bir kez çağırır", async () => {
    installFetchMock(() => jsonResponse(projectResponse));
    const onSelect = vi.fn();

    openDialog({ scope: "project", onSelect });
    const user = userEvent.setup();

    // `project` scope'unda açık dizinin kendisi otomatik seçilidir (bkz.
    // bileşen dokümantasyonu); ekstra bir satır tıklaması gerekmez.
    await user.click(await screen.findByRole("button", { name: "Seç" }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("/srv/ansible");
  });

  it("dosya scope'unda bir satır seçilmeden Seç devre dışıdır, satır seçilince etkinleşir", async () => {
    const fileListing = {
      scope: "inventory",
      current_path: "/srv/data",
      target_kind: "file",
      entries: [{ name: "hosts.ini", path: "/srv/data/hosts.ini", kind: "file", selectable: true }],
      truncated: false,
    };
    installFetchMock(() => jsonResponse(fileListing));
    const onSelect = vi.fn();

    openDialog({ scope: "inventory", onSelect });
    const user = userEvent.setup();

    expect(screen.getByRole("button", { name: "Seç" })).toBeDisabled();

    const row = await screen.findByRole("button", { name: /hosts\.ini/ });
    await user.click(row);
    expect(row).toHaveAttribute("aria-pressed", "true");

    const selectButton = screen.getByRole("button", { name: "Seç" });
    expect(selectButton).not.toBeDisabled();
    await user.click(selectButton);

    expect(onSelect).toHaveBeenCalledWith("/srv/data/hosts.ini");
  });

  it("İptal onCancel'ı çağırır ve onSelect'i hiç çağırmaz", async () => {
    installFetchMock(() => jsonResponse(projectResponse));
    const onCancel = vi.fn();
    const onSelect = vi.fn();

    openDialog({ scope: "project", onCancel, onSelect });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "İptal" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("native `close` event (Escape'in tarayıcıda ürettiği sinyal) onCancel'ı çağırır", async () => {
    installFetchMock(() => jsonResponse(projectResponse));
    const onCancel = vi.fn();

    render(
      <ControllerPathDialog open scope="project" onSelect={vi.fn()} onCancel={onCancel} />,
    );
    await screen.findByRole("button", { name: /web/ });

    const dialog = document.querySelector("dialog") as HTMLDialogElement;
    dialog.dispatchEvent(new Event("close"));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("truncated=true için yalnız kısa bir bilgi mesajı gösterir", async () => {
    installFetchMock(() => jsonResponse({ ...projectResponse, truncated: true }));

    openDialog({ scope: "project" });

    expect(
      await screen.findByText(/Liste kısaltıldı; aradığınız burada değilse tam yolu elle girin\./),
    ).toBeInTheDocument();
  });

  it("boş dizin için boş durum mesajı gösterir", async () => {
    installFetchMock(() => jsonResponse(webResponse));

    openDialog({ scope: "project" });

    expect(await screen.findByText("Bu dizin boş.")).toBeInTheDocument();
  });

  it.each([
    ["path_not_allowed", 403, "Bu konuma izin verilmiyor."],
    ["path_not_found", 422, "Bu yol artık bulunamıyor ya da bir dizin değil."],
    ["browse_directory_unreadable", 500, "Bu dizin okunamadı."],
    ["project_inactive", 409, "Seçili project pasif durumda."],
    ["not_found", 404, "Seçili project bulunamadı."],
  ])(
    "%s hatasını sabit ve sanitize edilmiş bir mesajla gösterir",
    async (code, status, expectedMessage) => {
      installFetchMock(() =>
        errorResponse(status, code, "ham backend mesajı: /etc/secret/traceback.py", {
          allowed_roots: ["/gizli"],
        }),
      );

      openDialog({ scope: "project" });

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(expectedMessage);
      expect(alert).not.toHaveTextContent("/etc/secret/traceback.py");
      expect(alert).not.toHaveTextContent("allowed_roots");
    },
  );

  it("ağ hatasını sabit bir mesajla gösterir", async () => {
    installFetchMock(() => {
      throw new TypeError("Failed to fetch");
    });

    openDialog({ scope: "project" });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Backend'e ulaşılamadı");
  });

  it("hata durumunda dialog çökmez; İptal ve Geri hâlâ çalışır", async () => {
    installFetchMock(() => errorResponse(500, "browse_directory_unreadable", "ham mesaj"));
    const onCancel = vi.fn();

    openDialog({ scope: "project", onCancel });
    const user = userEvent.setup();

    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "İptal" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("satırlar gerçek button elemanlarıdır", async () => {
    installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });

    const list = await screen.findByRole("list");
    for (const button of within(list).getAllByRole("button")) {
      expect(button.tagName).toBe("BUTTON");
    }
  });

  // --- Küçük UX düzeltmesi: breadcrumb yok, tek satırlık konum/seçim özeti ---
  // (AUDIT-FIX1 bulgu 3) --------------------------------------------------

  it("project scope'unda otomatik seçilen mevcut klasörü tek satırda açıkça gösterir", async () => {
    installFetchMock(() => jsonResponse(projectResponse));

    openDialog({ scope: "project" });

    expect(
      await screen.findByText("Konum ve seçili klasör: /srv/ansible"),
    ).toBeInTheDocument();
  });

  it("dosya scope'unda henüz seçim yokken ve seçim yapıldıktan sonra konum satırını günceller", async () => {
    const fileListing = {
      scope: "inventory",
      current_path: "/srv/data",
      target_kind: "file",
      entries: [{ name: "hosts.ini", path: "/srv/data/hosts.ini", kind: "file", selectable: true }],
      truncated: false,
    };
    installFetchMock(() => jsonResponse(fileListing));

    openDialog({ scope: "inventory" });
    await screen.findByText("Konum: /srv/data — henüz seçim yok.");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /hosts\.ini/ }));

    expect(
      await screen.findByText("Konum: /srv/data · Seçili: /srv/data/hosts.ini"),
    ).toBeInTheDocument();
  });

  it("sentetik kök görünümünde (current_path=null) güvenli, kısa bir metin gösterir", async () => {
    const rootChooser = {
      scope: "project",
      current_path: null,
      target_kind: "directory",
      entries: [
        { name: "/srv/bir", path: "/srv/bir", kind: "directory", selectable: true },
        { name: "/srv/iki", path: "/srv/iki", kind: "directory", selectable: true },
      ],
      truncated: false,
    };
    installFetchMock(() => jsonResponse(rootChooser));

    openDialog({ scope: "project" });

    expect(await screen.findByText("İzinli köklerden birini seçin.")).toBeInTheDocument();
  });

  // --- Stale async response ve stale selection (AUDIT-FIX1 bulgu 2) ------

  it("yeni bir yükleme başladığı an 'Seç' hemen devre dışı kalır (eski seçim kullanılamaz)", async () => {
    const pendingChild = deferred<unknown>();
    installFetchMock((request) => {
      if (!request.url.includes("path=")) {
        return jsonResponse(projectResponse);
      }
      return pendingChild.promise;
    });

    openDialog({ scope: "project" });
    await screen.findByRole("button", { name: /web/ });
    expect(screen.getByRole("button", { name: "Seç" })).not.toBeDisabled();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Aç" }));

    // İkinci istek hâlâ pending; eski seçim ("/srv/ansible") artık geçersiz.
    expect(screen.getByRole("button", { name: "Seç" })).toBeDisabled();

    await act(async () => {
      pendingChild.resolve(jsonResponse(webResponse));
      await pendingChild.promise;
    });
  });

  it("başarısız bir navigasyon sonrası eski seçim kullanılabilir bırakılmaz", async () => {
    installFetchMock((request) => {
      if (!request.url.includes("path=")) {
        return jsonResponse(projectResponse);
      }
      return errorResponse(500, "browse_directory_unreadable", "ham mesaj");
    });

    openDialog({ scope: "project" });
    await screen.findByRole("button", { name: /web/ });
    expect(screen.getByRole("button", { name: "Seç" })).not.toBeDisabled();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Aç" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Seç" })).toBeDisabled();
  });

  it(
    "Project A beklenirken dialog kapanıp Project B ile açılır; A geç başarıyla " +
      "resolve olsa bile B'nin görünümü/seçimi değişmez",
    async () => {
      const pendingA = deferred<unknown>();
      const responseA = {
        scope: "project_inventory",
        current_path: "/srv/a",
        target_kind: "file",
        entries: [{ name: "a.ini", path: "/srv/a/a.ini", kind: "file", selectable: true }],
        truncated: false,
      };
      const responseB = {
        scope: "project_inventory",
        current_path: "/srv/b",
        target_kind: "file",
        entries: [{ name: "b.ini", path: "/srv/b/b.ini", kind: "file", selectable: true }],
        truncated: false,
      };
      installFetchMock((request) => {
        if (request.url.includes("project_id=1")) {
          return pendingA.promise;
        }
        if (request.url.includes("project_id=2")) {
          return jsonResponse(responseB);
        }
        return undefined;
      });

      const onSelect = vi.fn();
      const utils = render(
        <ControllerPathDialog
          open={false}
          scope="project_inventory"
          projectId={1}
          onSelect={onSelect}
          onCancel={vi.fn()}
        />,
      );
      // Dialog A ile açılır; A isteği pending kalır.
      utils.rerender(
        <ControllerPathDialog
          open
          scope="project_inventory"
          projectId={1}
          onSelect={onSelect}
          onCancel={vi.fn()}
        />,
      );
      // Dialog kapanmadan doğrudan B'ye geçer (InventoryForm'un project
      // değişince dialogu kapatıp yeniden açmasıyla aynı net etki: scope aynı
      // kalır, yalnızca projectId değişir — bileşen bunu **kendi başına**
      // (`[open, scope, projectId]` bağımlılığıyla) yakalamalı).
      utils.rerender(
        <ControllerPathDialog
          open
          scope="project_inventory"
          projectId={2}
          onSelect={onSelect}
          onCancel={vi.fn()}
        />,
      );

      await screen.findByRole("button", { name: /b\.ini/ });

      // A şimdi geç ve **başarıyla** resolve olur.
      await act(async () => {
        pendingA.resolve(jsonResponse(responseA));
        await pendingA.promise;
      });

      // B'nin görünümü hâlâ aynı; A'nın satırı hiç görünmedi.
      expect(screen.queryByRole("button", { name: /a\.ini/ })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /b\.ini/ })).toBeInTheDocument();

      // B'nin seçimiyle onSelect çağrılabilir olmalı; A'nınkiyle değil.
      await userEvent.setup().click(screen.getByRole("button", { name: /b\.ini/ }));
      await userEvent.setup().click(screen.getByRole("button", { name: "Seç" }));
      expect(onSelect).toHaveBeenCalledWith("/srv/b/b.ini");
    },
  );

  it("stale isteğin reddi (abort) güncel görünümü bozmaz ve network error göstermez", async () => {
    const pendingFirst = deferredWithReject<unknown>();
    const secondResponse = {
      scope: "project_inventory",
      current_path: "/srv/b",
      target_kind: "file",
      entries: [{ name: "b.ini", path: "/srv/b/b.ini", kind: "file", selectable: true }],
      truncated: false,
    };
    installFetchMock((request) => {
      if (request.url.includes("project_id=1")) {
        return pendingFirst.promise;
      }
      if (request.url.includes("project_id=2")) {
        return jsonResponse(secondResponse);
      }
      return undefined;
    });

    const utils = render(
      <ControllerPathDialog
        open={false}
        scope="project_inventory"
        projectId={1}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    utils.rerender(
      <ControllerPathDialog
        open
        scope="project_inventory"
        projectId={1}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    utils.rerender(
      <ControllerPathDialog
        open
        scope="project_inventory"
        projectId={2}
        onSelect={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await screen.findByRole("button", { name: /b\.ini/ });

    // İlk (artık stale) istek, gerçek bir `AbortController.abort()`'un
    // üreteceği türden bir reddedilmeyle sonuçlanır.
    await act(async () => {
      pendingFirst.reject(new DOMException("The operation was aborted.", "AbortError"));
      await pendingFirst.promise.catch(() => undefined);
    });

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /b\.ini/ })).toBeInTheDocument();
  });

  it("yeni bir load, önceki isteğin AbortController'ını gerçekten iptal eder", async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, "abort");
    try {
      installFetchMock((request) => {
        if (!request.url.includes("path=")) {
          return jsonResponse(projectResponse);
        }
        return jsonResponse(webResponse);
      });

      openDialog({ scope: "project" });
      await screen.findByRole("button", { name: /web/ });
      expect(abortSpy).not.toHaveBeenCalled();

      const user = userEvent.setup();
      await user.click(screen.getByRole("button", { name: "Aç" }));
      await screen.findByText("Bu dizin boş.");

      expect(abortSpy).toHaveBeenCalledTimes(1);
    } finally {
      abortSpy.mockRestore();
    }
  });

  // --- Kapalı dialog gerçekten görünmez olmalı (LIVE-UI-FIX1) -------------
  //
  // `.path-dialog { display: flex }` kuralı koşulsuzca uygulanırsa, native
  // `<dialog>`'un kapalıyken kendiliğinden `display: none` uygulayan
  // davranışını ezer ve `open=false` render'da dahi "Bir klasör seçin"
  // dialogu ekranda görünür kalır. Bu testler component prop'unu değil,
  // yukarıda yan etkili olarak import edilen gerçek `styles.css`'in jsdom
  // üzerinde hesaplattığı `getComputedStyle(...).display` değerini ölçer.
  describe("kapalı dialogun gerçek CSS görünürlüğü", () => {
    it("open=false render edildiğinde dialog DOM'da bulunur ama görünmez ve `open` attribute'u taşımaz", () => {
      render(
        <ControllerPathDialog
          open={false}
          scope="project"
          onSelect={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

      const dialog = document.querySelector("dialog");
      expect(dialog).not.toBeNull();
      expect(dialog).not.toHaveAttribute("open");
      expect(getComputedStyle(dialog as HTMLDialogElement).display).toBe("none");
    });

    it("open=true yapıldığında görünür olur ve mevcut browse isteğini başlatır; tekrar open=false yapıldığında yeniden görünmez olur", async () => {
      installFetchMock(() => jsonResponse(projectResponse));
      const utils = render(
        <ControllerPathDialog
          open={false}
          scope="project"
          onSelect={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

      utils.rerender(
        <ControllerPathDialog open scope="project" onSelect={vi.fn()} onCancel={vi.fn()} />,
      );

      // Mevcut browse isteği (R1-V3J0C davranışı) korunur.
      await screen.findByRole("button", { name: /web/ });

      const dialog = document.querySelector("dialog") as HTMLDialogElement;
      expect(dialog).toHaveAttribute("open");
      expect(getComputedStyle(dialog).display).not.toBe("none");

      utils.rerender(
        <ControllerPathDialog
          open={false}
          scope="project"
          onSelect={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

      expect(dialog).not.toHaveAttribute("open");
      expect(getComputedStyle(dialog).display).toBe("none");
    });
  });
});
