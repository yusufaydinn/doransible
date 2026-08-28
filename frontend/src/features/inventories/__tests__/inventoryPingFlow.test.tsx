/**
 * T-204C ping akışı ve token güvenliği regresyonları (Aşama 2A).
 *
 * Testler gerçek `App`, gerçek router ve gerçek `QueryClient` ile çalışır;
 * yalnızca `fetch` sınırı sahtelenir. Böylece "hangi istek çıktı, gövdesinde ne
 * vardı" soruları uygulamanın kendi kodu üzerinden yanıtlanır.
 *
 * Token invariantı davranışla ölçülür: benzersiz bir kanarya değeri kullanılır
 * ve DOM, URL, query cache, mutation cache ile storage'ta **bulunmadığı**
 * doğrulanır. `MutationCache`'e doğrudan bakmak bilinçli bir istisnadır —
 * ADR-018 Karar 6'daki "token yalnızca istek gövdesinde taşınır" kuralı ancak
 * o cache'in boş kaldığı gösterilerek kanıtlanabilir.
 *
 * Assertion'lar token'ı **değere göre** değil, hesaplanmış boolean'lar üzerinden
 * karşılaştırır: başarısız bir `expect` çıktısı token'ı yazdırmamalıdır.
 */

import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { inventoryContents, linkedInventory, standaloneInventory } from "../../../test/fixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";
import type { PingPreviewResponse, PingRunResponse } from "../types";

/**
 * Kanarya token'ı.
 *
 * Gerçek token biçiminden (43 karakter base64url) bilinçli olarak farklıdır ve
 * eşsizdir: bir metnin içinde geçiyorsa oraya yalnızca bu akıştan gelmiş
 * olabilir.
 */
const FAKE_TOKEN = "PING-TOKEN-CANARY-3f9d2a7c5b1e4086af23d7c1";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;

const previewResponse: PingPreviewResponse = {
  preview_token: FAKE_TOKEN,
  expires_at: "2026-08-03T10:15:00Z",
  plan: {
    inventory: {
      id: linkedInventory.id,
      name: linkedInventory.name,
      binding: "project",
      project_id: linkedInventory.project_id,
      project_name: "Web sunucuları",
    },
    operation: "ansible.builtin.ping",
    operation_effect:
      "Hedef host'lara SSH bağlantısı kurulur; uzak hostta geçici modül dosyaları " +
      "ve süreç oluşabilir.",
    limit: null,
    host_count: 2,
    hosts: ["web01", "web02"],
    hosts_truncated: false,
    connection: "ssh",
    host_key_policy: "strict",
    become: false,
  },
};

const runResponse: PingRunResponse = {
  job_id: "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42",
  job_type: "ping",
  status: "successful",
  inventory_id: linkedInventory.id,
  project_id: linkedInventory.project_id,
  limit: null,
  return_code: 0,
  started_at: "2026-08-03T10:10:00Z",
  finished_at: "2026-08-03T10:10:04Z",
  summary: { total: 2, reachable: 2, unreachable: 0, failed: 0, no_result: 0 },
  hosts: [
    { name: "web01", status: "reachable", message: null },
    { name: "web02", status: "reachable", message: null },
  ],
};

/* --- Sahte cevap yönlendirmesi --------------------------------------------- */

interface PingRoutes {
  preview?: unknown;
  cancel?: unknown;
  confirm?: unknown;
  hosts?: unknown;
  pingRuns?: unknown;
}

/**
 * Ping geçmişi ucunun varsayılan cevabı.
 *
 * Bu dosya geçmişin **içeriğiyle** ilgilenmez; boş liste, geçmiş bölümünün
 * akış testlerine karışmasını önler.
 */
const emptyPingHistory = { inventory_id: linkedInventory.id, items: [] };

/**
 * Ping uçlarını ve inventory okumalarını karşılar.
 *
 * Sıra önemlidir: `/ping-runs` ayrı bir uçtur, ardından `/ping/preview/cancel`
 * en özgül, `/ping` en genel eşleşmedir.
 */
function pingResponder(routes: PingRoutes): (request: RecordedRequest) => unknown {
  return (request) => {
    // `/ping-runs` adresi `/ping` ile başlar ama onunla bitmez; geçmiş ucu bu
    // yüzden ping eşleşmelerinden önce ayrılır.
    if (request.url.includes("/ping-runs")) {
      return routes.pingRuns ?? jsonResponse(emptyPingHistory);
    }
    if (request.url.endsWith("/ping/preview/cancel")) {
      return routes.cancel;
    }
    if (request.url.endsWith("/ping/preview")) {
      return routes.preview;
    }
    if (request.url.endsWith("/ping")) {
      return routes.confirm;
    }
    if (request.url.endsWith("/hosts")) {
      return routes.hosts ?? jsonResponse(inventoryContents);
    }
    if (request.url.endsWith("/api/inventories")) {
      return jsonResponse([linkedInventory, standaloneInventory]);
    }
    if (request.url.endsWith(`/api/inventories/${standaloneInventory.id}`)) {
      return jsonResponse(standaloneInventory);
    }
    return jsonResponse(linkedInventory);
  };
}

/**
 * `204 No Content` cevabı.
 *
 * `json` casusu bilinçlidir: gövdesiz cevapta `json()` çağrılırsa gerçek
 * tarayıcıda ayrıştırma hatası olurdu. Casus hem çağrılmadığını ölçer hem de
 * çağrılırsa testi yüksek sesle düşürür.
 */
function noContentResponse() {
  const json = vi.fn(async () => {
    throw new Error("204 cevabında json() çağrılmamalıdır.");
  });
  return { response: { ok: true, status: 204, json }, json };
}

/* --- Token sızıntısı ölçümleri --------------------------------------------- */

function includesToken(value: unknown): boolean {
  if (typeof value === "string") {
    return value.includes(FAKE_TOKEN);
  }
  if (value === undefined) {
    return false;
  }
  try {
    return JSON.stringify(value)?.includes(FAKE_TOKEN) ?? false;
  } catch {
    return false;
  }
}

function domIncludesToken(): boolean {
  return includesToken(document.body.innerHTML) || includesToken(document.title);
}

function urlIncludesToken(): boolean {
  return includesToken(window.location.href);
}

function storageIncludesToken(storage: Storage): boolean {
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key === null) {
      continue;
    }
    if (includesToken(key) || includesToken(storage.getItem(key))) {
      return true;
    }
  }
  return false;
}

function queryCacheIncludesToken(client: QueryClient): boolean {
  return client
    .getQueryCache()
    .getAll()
    .some((query) => includesToken(query.queryKey) || includesToken(query.state.data));
}

function mutationCacheSize(client: QueryClient): number {
  return client.getMutationCache().getAll().length;
}

/** Token'ın sızmadığı bütün yüzeyleri tek yerde ölçer. */
function leakSurfaces(client: QueryClient) {
  return {
    dom: domIncludesToken(),
    url: urlIncludesToken(),
    queryCache: queryCacheIncludesToken(client),
    localStorage: storageIncludesToken(window.localStorage),
    sessionStorage: storageIncludesToken(window.sessionStorage),
  };
}

const NO_LEAK = {
  dom: false,
  url: false,
  queryCache: false,
  localStorage: false,
  sessionStorage: false,
};

/**
 * Token'ın hangi isteklerde göründüğünü, token'ın kendisini basmadan özetler.
 */
function tokenSightings(requests: RecordedRequest[]) {
  return requests
    .filter((request) => includesToken(request.body) || includesToken(request.url))
    .map((request) => ({
      method: request.method,
      path: new URL(request.url).pathname,
      inBody: includesToken(request.body),
      inUrl: includesToken(request.url),
    }));
}

/**
 * Onay akışının ürettiği istekler.
 *
 * Salt okunur geçmiş ucu (`/ping-runs`) bilinçli olarak **dışarıda** bırakılır:
 * bu dosya preview/confirm/cancel sırasını ölçer ve geçmiş okuması o sıranın
 * parçası değildir.
 */
function pingRequests(requests: RecordedRequest[]): RecordedRequest[] {
  return requests.filter(
    (request) => request.url.includes("/ping") && !request.url.includes("/ping-runs"),
  );
}

function countRequests(requests: RecordedRequest[], suffix: string): number {
  return requests.filter((request) => request.url.endsWith(suffix)).length;
}

/** Sıradaki ping isteğini döndürür; yoksa testi anlaşılır biçimde düşürür. */
function pingRequestAt(requests: RecordedRequest[], index: number): RecordedRequest {
  const matched = pingRequests(requests);
  const request = matched[index];
  if (request === undefined) {
    throw new Error(
      `Beklenen ${index}. ping isteği yok; toplam ${matched.length} istek kaydedildi.`,
    );
  }
  return request;
}

/* --- Ortak adımlar ---------------------------------------------------------- */

async function waitForPingSection(): Promise<void> {
  await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
}

async function waitForPlan(): Promise<HTMLElement> {
  return screen.findByRole("heading", { level: 4, name: "Onay bekleyen plan" });
}

function previewButton(): HTMLElement {
  return screen.getByRole("button", { name: "Önizle" });
}

function confirmButton(): HTMLElement {
  return screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" });
}

function limitInput(): HTMLElement {
  return screen.getByLabelText("Host limiti (isteğe bağlı)");
}

/** Aynı act içinde iki kez tıklar: React araya render giremez. */
async function doubleClick(element: HTMLElement): Promise<void> {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

/** Aynı act içinde iki farklı düğmeye basar. */
async function clickBoth(first: HTMLElement, second: HTMLElement): Promise<void> {
  await act(async () => {
    first.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    second.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

/* --- 1. Preview sözleşmesi -------------------------------------------------- */

describe("Ping — preview sözleşmesi", () => {
  it("doğru inventory adresine POST eder ve boş limiti null gönderir", async () => {
    const { requests } = installFetchMock(
      pingResponder({ preview: jsonResponse(previewResponse) }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    expect(pingRequests(requests)).toHaveLength(1);

    const preview = pingRequestAt(requests, 0);
    expect(preview.method).toBe("POST");
    expect(new URL(preview.url).pathname).toBe(
      `/api/inventories/${linkedInventory.id}/ping/preview`,
    );
    expect(preview.body).toEqual({ limit: null });
  });

  it("boş olmayan limiti trim veya normalize etmeden aynen gönderir", async () => {
    const { requests } = installFetchMock(
      pingResponder({ preview: jsonResponse(previewResponse) }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    // Baştaki/sondaki boşluk ve büyük harf bilinçlidir: istemci limiti
    // yorumlamaz, sunucu çözer.
    await user.type(limitInput(), "  Web01, web02  ");
    await user.click(previewButton());
    await waitForPlan();

    expect(pingRequestAt(requests, 0).body).toEqual({ limit: "  Web01, web02  " });
  });

  it("tek başına confirm veya cancel çağırmaz", async () => {
    const { requests } = installFetchMock(
      pingResponder({ preview: jsonResponse(previewResponse) }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    const paths = pingRequests(requests).map((request) => new URL(request.url).pathname);
    expect(paths).toEqual([`/api/inventories/${linkedInventory.id}/ping/preview`]);
  });

  it("plan geldiğinde token DOM'da veya cache'lerde görünmez", async () => {
    installFetchMock(pingResponder({ preview: jsonResponse(previewResponse) }));

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
    expect(mutationCacheSize(queryClient)).toBe(0);
  });

  it("inventory içeriği okunamasa bile ping bölümü kullanılabilir", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        hosts: errorResponse(
          422,
          "inventory_parse_failed",
          "Inventory dosyası ayrıştırılamadı.",
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "Inventory dosyası ayrıştırılamadı" });
    await waitForPingSection();
    await user.click(previewButton());

    await waitForPlan();
    expect(pingRequests(requests)).toHaveLength(1);
  });
});

/* --- 2. Plan ve açık onay --------------------------------------------------- */

describe("Ping — plan ve açık onay", () => {
  it("planın güvenli alanlarını gösterir", async () => {
    installFetchMock(pingResponder({ preview: jsonResponse(previewResponse) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    expect(screen.getByText("Inventory").nextElementSibling).toHaveTextContent(
      linkedInventory.name,
    );
    expect(screen.getByText("Bağlam").nextElementSibling).toHaveTextContent(
      "Bir project'e bağlı",
    );
    expect(screen.getByText("İşlem").nextElementSibling).toHaveTextContent(
      "ansible.builtin.ping",
    );
    expect(screen.getByText("Etkisi").nextElementSibling).toHaveTextContent(
      "geçici modül dosyaları",
    );
    expect(screen.getByText("Limit").nextElementSibling).toHaveTextContent(
      "Tüm inventory",
    );
    expect(screen.getByText("Hedef host sayısı").nextElementSibling).toHaveTextContent(
      "2",
    );
    expect(screen.getByText("Host anahtarı politikası").nextElementSibling).toHaveTextContent(
      "Katı (strict)",
    );
    expect(
      screen.getByText("Yetki yükseltme (become)").nextElementSibling,
    ).toHaveTextContent("Kullanılmıyor");

    const hostList = screen.getByRole("list", { name: "Hedeflenen host'lar" });
    expect(within(hostList).getByText("web01")).toBeInTheDocument();
    expect(within(hostList).getByText("web02")).toBeInTheDocument();
  });

  it("confirm yalnızca kullanıcı butona bastığında ve yalnız token gövdesiyle çağrılır", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: jsonResponse(runResponse),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    // Plan görünürken henüz hiçbir execution isteği çıkmamıştır.
    expect(pingRequests(requests)).toHaveLength(1);
    expect(confirmButton()).toBeInTheDocument();

    await user.click(confirmButton());
    await screen.findByRole("heading", {
      level: 4,
      name: "Ping tamamlandı: tüm host'lar erişilebilir",
    });

    const confirm = pingRequestAt(requests, 1);
    expect(confirm.method).toBe("POST");
    expect(new URL(confirm.url).pathname).toBe(
      `/api/inventories/${linkedInventory.id}/ping`,
    );
    // Gövdede token'dan başka alan yoktur: limit, timeout, modül gönderilmez.
    expect(Object.keys(confirm.body as object)).toEqual(["preview_token"]);
  });

  it("confirm sürerken plan okunabilir kalır ve eylemler gizlenir", async () => {
    const pending = deferred<unknown>();
    installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: pending.promise,
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(confirmButton());

    expect(await screen.findByRole("status")).toHaveTextContent("Ping çalıştırılıyor…");
    // Plan hâlâ okunabilir…
    expect(screen.getByRole("heading", { level: 4, name: "Onay bekleyen plan" })).toBeInTheDocument();
    // …ama çakışabilecek eylemlerin hiçbiri render edilmez.
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Vazgeç" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Önizle" })).not.toBeInTheDocument();

    await act(async () => {
      pending.resolve(jsonResponse(runResponse));
    });
  });
});

/* --- 3. Cancel ve 204 ------------------------------------------------------- */

describe("Ping — vazgeçme ve 204 No Content", () => {
  it("doğru adrese yalnız token gövdesiyle gider ve json() çağrılmaz", async () => {
    const noContent = noContentResponse();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        cancel: noContent.response,
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(screen.getByRole("button", { name: "Vazgeç" }));

    // Başarılı iptalden sonra boş forma dönülür.
    expect(await screen.findByRole("button", { name: "Önizle" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 4, name: "Onay bekleyen plan" }),
    ).not.toBeInTheDocument();

    const cancel = pingRequestAt(requests, 1);
    expect(cancel.method).toBe("POST");
    expect(new URL(cancel.url).pathname).toBe(
      `/api/inventories/${linkedInventory.id}/ping/preview/cancel`,
    );
    expect(Object.keys(cancel.body as object)).toEqual(["preview_token"]);
    expect(noContent.json).not.toHaveBeenCalled();
  });

  it("iptalden sonra confirm isteği üretilemez", async () => {
    const noContent = noContentResponse();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        cancel: noContent.response,
        confirm: jsonResponse(runResponse),
      }),
    );

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(screen.getByRole("button", { name: "Vazgeç" }));
    await screen.findByRole("button", { name: "Önizle" });

    // Onay ekranı kapandığı için basılacak bir confirm butonu kalmaz.
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();

    const paths = pingRequests(requests).map((request) => new URL(request.url).pathname);
    expect(paths).toEqual([
      `/api/inventories/${linkedInventory.id}/ping/preview`,
      `/api/inventories/${linkedInventory.id}/ping/preview/cancel`,
    ]);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
  });
});

/* --- 4. Senkron yarış koruması ---------------------------------------------- */

describe("Ping — senkron yarış koruması", () => {
  it("hızlı çift önizleme yalnız bir istek üretir", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(
      pingResponder({ preview: pending.promise }),
    );

    renderApp(DETAIL_ROUTE);
    await waitForPingSection();

    const form = previewButton().closest("form") as HTMLFormElement;
    await act(async () => {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(pingRequests(requests)).toHaveLength(1);
    // İstek sürerken kontroller kapalıdır. Buton bu sırada bekleme etiketini
    // taşır; "Önizle" adıyla basılabilecek bir kontrol kalmaz.
    expect(screen.queryByRole("button", { name: "Önizle" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan hazırlanıyor…" })).toBeDisabled();
    expect(limitInput()).toBeDisabled();

    await act(async () => {
      pending.resolve(jsonResponse(previewResponse));
    });
    await waitForPlan();
    expect(pingRequests(requests)).toHaveLength(1);
  });

  it("hızlı çift onay yalnız bir execution üretir", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: pending.promise,
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    await doubleClick(confirmButton());

    const confirmCalls = pingRequests(requests).filter((request) =>
      new URL(request.url).pathname.endsWith("/ping"),
    );
    expect(confirmCalls).toHaveLength(1);

    await act(async () => {
      pending.resolve(jsonResponse(runResponse));
    });
    await screen.findByRole("heading", {
      level: 4,
      name: "Ping tamamlandı: tüm host'lar erişilebilir",
    });
    expect(pingRequests(requests)).toHaveLength(2);
  });

  it("onay ve vazgeçme aynı token için birlikte gönderilemez", async () => {
    const pending = deferred<unknown>();
    const noContent = noContentResponse();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: pending.promise,
        cancel: noContent.response,
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    await clickBoth(confirmButton(), screen.getByRole("button", { name: "Vazgeç" }));

    // Kilidi ilk alan eylem kazanır; ikincisi hiç istek üretmez.
    const paths = pingRequests(requests).map((request) => new URL(request.url).pathname);
    expect(paths).toEqual([
      `/api/inventories/${linkedInventory.id}/ping/preview`,
      `/api/inventories/${linkedInventory.id}/ping`,
    ]);
    expect(noContent.json).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve(jsonResponse(runResponse));
    });
  });
});

/* --- 5. Token güvenliği ----------------------------------------------------- */

describe("Ping — token yaşam döngüsü", () => {
  it("preview ve confirm boyunca mutation cache boş kalır", async () => {
    const previewPending = deferred<unknown>();
    const confirmPending = deferred<unknown>();
    installFetchMock(
      pingResponder({
        preview: previewPending.promise,
        confirm: confirmPending.promise,
      }),
    );

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    expect(mutationCacheSize(queryClient)).toBe(0);

    // İstek sürerken.
    await user.click(previewButton());
    expect(mutationCacheSize(queryClient)).toBe(0);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);

    // Cevap geldikten sonra.
    await act(async () => {
      previewPending.resolve(jsonResponse(previewResponse));
    });
    await waitForPlan();
    expect(mutationCacheSize(queryClient)).toBe(0);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);

    // Confirm sürerken ve tamamlandıktan sonra.
    await user.click(confirmButton());
    expect(mutationCacheSize(queryClient)).toBe(0);

    await act(async () => {
      confirmPending.resolve(jsonResponse(runResponse));
    });
    await screen.findByRole("heading", {
      level: 4,
      name: "Ping tamamlandı: tüm host'lar erişilebilir",
    });
    expect(mutationCacheSize(queryClient)).toBe(0);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
  });

  it("token yalnızca beklenen iki istek gövdesinde görünür", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: jsonResponse(runResponse),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(confirmButton());
    await screen.findByRole("heading", {
      level: 4,
      name: "Ping tamamlandı: tüm host'lar erişilebilir",
    });

    // Token yalnızca confirm gövdesinde; hiçbir URL'de değil. Preview isteğinin
    // gövdesi token taşımaz, cevabı taşır.
    expect(tokenSightings(requests)).toEqual([
      {
        method: "POST",
        path: `/api/inventories/${linkedInventory.id}/ping`,
        inBody: true,
        inUrl: false,
      },
    ]);
  });

  it("iptal yolunda da token yalnız cancel gövdesindedir", async () => {
    const noContent = noContentResponse();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        cancel: noContent.response,
      }),
    );

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(screen.getByRole("button", { name: "Vazgeç" }));
    await screen.findByRole("button", { name: "Önizle" });

    expect(tokenSightings(requests)).toEqual([
      {
        method: "POST",
        path: `/api/inventories/${linkedInventory.id}/ping/preview/cancel`,
        inBody: true,
        inUrl: false,
      },
    ]);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
    expect(mutationCacheSize(queryClient)).toBe(0);
  });

  it("vazgeçme isteği sürerken de tamamlandıktan sonra da token sızmaz", async () => {
    // Cancel'ın **bekleyen** penceresi ayrı bir risk yüzeyidir: token o sırada
    // uçuşta olan bir isteğin gövdesindedir ve bir mutation cache kaydına
    // dönüşebilirdi. Bu yüzden 204 çözülmeden önce ve çözüldükten sonra ayrı
    // ayrı ölçülür.
    const pending = deferred<unknown>();
    const noContent = noContentResponse();
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        cancel: pending.promise,
        confirm: jsonResponse(runResponse),
      }),
    );

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    await user.click(screen.getByRole("button", { name: "Vazgeç" }));

    // --- İstek sürerken (204 henüz çözülmedi) ---
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Önizleme iptal ediliyor…",
    );
    expect(mutationCacheSize(queryClient)).toBe(0);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
    expect(tokenSightings(requests)).toEqual([
      {
        method: "POST",
        path: `/api/inventories/${linkedInventory.id}/ping/preview/cancel`,
        inBody: true,
        inUrl: false,
      },
    ]);
    // Onay kalıcı olarak kapanmıştır; bekleme sırasında geri gelmez.
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();

    // --- Gerçek 204 cevabıyla çözülür ---
    await act(async () => {
      pending.resolve(noContent.response);
    });

    // --- Tamamlandıktan sonra ---
    expect(await screen.findByRole("button", { name: "Önizle" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { level: 4, name: "Onay bekleyen plan" }),
    ).not.toBeInTheDocument();
    expect(mutationCacheSize(queryClient)).toBe(0);
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
    expect(noContent.json).not.toHaveBeenCalled();

    // Toplam ping trafiği yalnız preview + cancel; confirm hiç çıkmaz.
    expect(pingRequests(requests).map((request) => new URL(request.url).pathname)).toEqual([
      `/api/inventories/${linkedInventory.id}/ping/preview`,
      `/api/inventories/${linkedInventory.id}/ping/preview/cancel`,
    ]);
  });
});

/* --- 6. Unmount ve geç cevap ------------------------------------------------ */

describe("Ping — unmount ve geç cevap", () => {
  it("unmount sonrası çözülen preview cevabı state veya token oluşturmaz", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const pending = deferred<unknown>();
    const { requests } = installFetchMock(pingResponder({ preview: pending.promise }));

    const app = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());

    const requestsBeforeUnmount = requests.length;
    app.unmount();

    // Cevap unmount'tan **sonra** çözülür.
    await act(async () => {
      pending.resolve(jsonResponse(previewResponse));
    });

    // Geç cevap ne yeni istek doğurur (fire-and-forget cancel yok) ne de
    // herhangi bir yüzeye token yazar.
    expect(requests).toHaveLength(requestsBeforeUnmount);
    expect(pingRequests(requests)).toHaveLength(1);
    expect(mutationCacheSize(app.queryClient)).toBe(0);
    expect(leakSurfaces(app.queryClient)).toEqual(NO_LEAK);

    // React unmount/state-update uyarısı veya başka bir hata çıkmaz.
    expect(consoleError.mock.calls).toHaveLength(0);
    expect(consoleWarn.mock.calls).toHaveLength(0);
  });
});

/* --- 7. Inventory izolasyonu ------------------------------------------------ */

describe("Ping — inventory izolasyonu", () => {
  it("başka bir inventory'ye geçildiğinde plan taşınmaz ve ekran idle başlar", async () => {
    installFetchMock(pingResponder({ preview: jsonResponse(previewResponse) }));

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();

    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    await user.click(await screen.findByRole("link", { name: standaloneInventory.name }));

    await screen.findByRole("heading", { level: 2, name: standaloneInventory.name });
    await waitForPingSection();

    // Yeni ekran temiz/idle: plan yok, sonuç yok, form var.
    expect(
      screen.queryByRole("heading", { level: 4, name: "Onay bekleyen plan" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();
    expect(previewButton()).toBeInTheDocument();
    expect(limitInput()).toHaveValue("");
  });

  it("eski inventory'nin geç preview cevabı yeni ekrana yazılamaz", async () => {
    const pending = deferred<unknown>();
    const { requests } = installFetchMock(pingResponder({ preview: pending.promise }));

    const { queryClient } = renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());

    // Plan gelmeden başka bir inventory'ye geçilir.
    await user.click(screen.getByRole("link", { name: "Listeye dön" }));
    await user.click(await screen.findByRole("link", { name: standaloneInventory.name }));
    await screen.findByRole("heading", { level: 2, name: standaloneInventory.name });

    await act(async () => {
      pending.resolve(jsonResponse(previewResponse));
    });

    // Geç cevap yeni ekranda plan açmaz ve token hiçbir yüzeye girmez.
    expect(
      screen.queryByRole("heading", { level: 4, name: "Onay bekleyen plan" }),
    ).not.toBeInTheDocument();
    expect(leakSurfaces(queryClient)).toEqual(NO_LEAK);
    expect(mutationCacheSize(queryClient)).toBe(0);

    // Eski token yeni inventory'nin ucuna gönderilmez. Salt okunur geçmiş
    // okuması (`/ping-runs`) bu ölçümün dışındadır: gövdesizdir ve token
    // taşımaz.
    const otherPingRequests = requests.filter(
      (request) =>
        request.url.includes(`/api/inventories/${standaloneInventory.id}/ping`) &&
        !request.url.includes("/ping-runs"),
    );
    expect(otherPingRequests).toHaveLength(0);
    expect(tokenSightings(requests)).toEqual([]);
  });
});

/* --- 8. Retry ve cache davranışı -------------------------------------------- */

describe("Ping — retry ve cache davranışı", () => {
  it("preview hatasında kendiliğinden ikinci istek çıkmaz", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: errorResponse(
          422,
          "ping_invalid_limit",
          "Limit çözümlenemedi.",
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());

    await screen.findByRole("heading", { level: 4, name: "Limit kabul edilmedi" });
    expect(pingRequests(requests)).toHaveLength(1);
  });

  it("confirm hatasında kendiliğinden ikinci istek çıkmaz", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: errorResponse(
          409,
          "job_already_running",
          "Bu inventory için hâlâ çalışan bir ping işi var.",
          { job_id: runResponse.job_id },
        ),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    await user.click(previewButton());
    await waitForPlan();
    await user.click(confirmButton());

    await screen.findByRole("heading", {
      level: 4,
      name: "Bu inventory için bir ping işi zaten çalışıyor",
    });
    expect(pingRequests(requests)).toHaveLength(2);
    // Aynı onayla yeniden denemek için buton sunulmaz.
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Tekrar dene" })).not.toBeInTheDocument();
  });

  it("ping, inventory metadata ve hosts sorgularını tazelemez", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(previewResponse),
        confirm: jsonResponse(runResponse),
      }),
    );

    renderApp(DETAIL_ROUTE);
    const user = userEvent.setup();

    await waitForPingSection();
    const detailBefore = countRequests(requests, `/api/inventories/${linkedInventory.id}`);
    const hostsBefore = countRequests(requests, "/hosts");

    await user.click(previewButton());
    await waitForPlan();
    await user.click(confirmButton());
    await screen.findByRole("heading", {
      level: 4,
      name: "Ping tamamlandı: tüm host'lar erişilebilir",
    });

    expect(countRequests(requests, `/api/inventories/${linkedInventory.id}`)).toBe(
      detailBefore,
    );
    expect(countRequests(requests, "/hosts")).toBe(hostsBefore);
  });
});
