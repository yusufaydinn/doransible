/**
 * Kalıcı ping geçmişi görünümü (R1-V3J1B).
 *
 * Bölümün iddiası dardır ve testler tam olarak o iddiayı ölçer: burada
 * **gerçek zamanlı izleme yoktur**. Ekran kendiliğinden tazelenmez, arka planda
 * yoklama kurmaz ve hiçbir ölçüm başlatmaz; yalnızca kullanıcının daha önce
 * başlattığı, sunucuda kalıcı hâle gelmiş ölçümleri okur.
 *
 * İkinci ölçüm ekseni sızıntıdır: hata metni backend'den hiçbir şey taşımaz,
 * cevap host adı/mesajı içermez ve onay token'ı hiçbir cache'e girmez.
 */

import type { QueryClient } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { linkedInventory, standaloneInventory } from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";
import {
  emptyPingHistory,
  pingHistoryFailed,
  pingHistorySuccessful,
  pingHistoryWith,
  pingPreviewResponse,
  pingResponder,
  pingRunSuccessful,
  PING_TOKEN,
  type PingRoutes,
} from "../../../test/pingFixtures";
import type { PingHistoryItem } from "../types";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;
const HISTORY_URL = `/api/inventories/${linkedInventory.id}/ping-runs?limit=10`;

const SECTION_TITLE = "Son ping ölçümü";
const TABLE_NAME = "Kaydedilmiş son ping ölçümleri, en yenisi başta";
const ERROR_TITLE = "Ping geçmişi şu anda yüklenemedi.";

function install(routes: PingRoutes) {
  return installFetchMock(pingResponder(routes));
}

/** Geçmiş bölümünü verilen cevapla kurar. */
function installHistory(response: unknown) {
  return install({ pingRuns: response });
}

async function waitForHistorySection(): Promise<HTMLElement> {
  return screen.findByRole("heading", { level: 3, name: SECTION_TITLE });
}

function historyTable(): HTMLElement {
  return screen.getByRole("table", { name: TABLE_NAME });
}

/** Geçmiş tablosunun veri satırları (başlık satırı hariç). */
function historyRows(): HTMLElement[] {
  const [, ...rows] = within(historyTable()).getAllByRole("row");
  return rows;
}

/** Bir özet kartının görünen değeri. */
function summaryValue(label: string): HTMLElement {
  const terms = screen.getAllByText(label).filter((element) => element.tagName === "DT");
  expect(terms).toHaveLength(1);
  const value = terms[0]?.nextElementSibling;
  expect(value).not.toBeNull();
  return value as HTMLElement;
}

function historyRequests(requests: RecordedRequest[]): RecordedRequest[] {
  return requests.filter((request) => request.url.includes("/ping-runs"));
}

/* --- 1. Yükleniyor ---------------------------------------------------------- */

describe("Ping geçmişi — yükleniyor", () => {
  it("veri gelene kadar duyurulan bir yükleniyor durumu gösterir", async () => {
    let release: (() => void) | undefined;
    const pending = new Promise<unknown>((resolve) => {
      release = () => resolve(jsonResponse(emptyPingHistory));
    });

    install({ pingRuns: () => pending });
    renderApp(DETAIL_ROUTE);

    await waitForHistorySection();
    expect(screen.getByText("Ping geçmişi yükleniyor…")).toHaveAttribute("role", "status");

    release?.();
    expect(
      await screen.findByText("Henüz kaydedilmiş bir ping ölçümü yok."),
    ).toBeInTheDocument();
  });
});

/* --- 2. Boş geçmiş ---------------------------------------------------------- */

describe("Ping geçmişi — kayıt yok", () => {
  it("boş listede açık bir metin gösterir ve tablo kurmaz", async () => {
    installHistory(jsonResponse(emptyPingHistory));
    renderApp(DETAIL_ROUTE);

    expect(
      await screen.findByText("Henüz kaydedilmiş bir ping ölçümü yok."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: TABLE_NAME })).not.toBeInTheDocument();
  });

  it("gerçek zamanlı izleme olmadığını görünür biçimde söyler", async () => {
    installHistory(jsonResponse(emptyPingHistory));
    renderApp(DETAIL_ROUTE);

    await waitForHistorySection();
    expect(
      screen.getByText(
        /gerçek zamanlı izleme değildir; kullanıcı tarafından başlatılmış kalıcı ping ölçümlerini gösterir/i,
      ),
    ).toBeInTheDocument();
  });
});

/* --- 3-6. Son ölçümün özeti -------------------------------------------------- */

describe("Ping geçmişi — son ölçümün özeti", () => {
  it("5/5 başarılı ölçümde beş sayacı da etiketleriyle gösterir", async () => {
    installHistory(jsonResponse(pingHistoryWith([pingHistorySuccessful])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    expect(summaryValue("Toplam")).toHaveTextContent("5");
    expect(summaryValue("Erişilebilir")).toHaveTextContent("5");
    expect(summaryValue("Erişilemiyor")).toHaveTextContent("0");
    expect(summaryValue("Başarısız")).toHaveTextContent("0");
    expect(summaryValue("Sonuç alınamadı")).toHaveTextContent("0");
  });

  it("4 erişilebilir + 1 erişilemeyen başarısız ölçümü sayaçlarla ayırır", async () => {
    installHistory(jsonResponse(pingHistoryWith([pingHistoryFailed])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    expect(summaryValue("Toplam")).toHaveTextContent("5");
    expect(summaryValue("Erişilebilir")).toHaveTextContent("4");
    expect(summaryValue("Erişilemiyor")).toHaveTextContent("1");
    expect(summaryValue("Başarısız")).toHaveTextContent("0");
    expect(summaryValue("Sonuç alınamadı")).toHaveTextContent("0");
  });

  it("bütün sayaçlar sıfır olsa bile hepsi görünür kalır", async () => {
    const zeroed: PingHistoryItem = {
      ...pingHistoryFailed,
      summary: { total: 0, reachable: 0, unreachable: 0, failed: 0, no_result: 0 },
    };
    installHistory(jsonResponse(pingHistoryWith([zeroed])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    for (const label of [
      "Toplam",
      "Erişilebilir",
      "Erişilemiyor",
      "Başarısız",
      "Sonuç alınamadı",
    ]) {
      expect(summaryValue(label)).toHaveTextContent("0");
    }
  });

  it("ölçüm zamanını, görünür durum rozetini ve çıkış kodunu gösterir", async () => {
    installHistory(jsonResponse(pingHistoryWith([pingHistoryFailed])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    const meta = screen.getByText(/^Son ölçüm:/).closest("li") as HTMLElement;
    // Makine okunur zaman ISO değeridir; görünen metin yerel biçimdedir.
    expect(within(meta).getByText(/2026/)).toHaveAttribute(
      "datetime",
      pingHistoryFailed.finished_at,
    );

    // Durum anlamı yalnızca renkle verilmez: rozet görünür metin taşır.
    const statusItem = screen.getByText(/^Durum:/).closest("li") as HTMLElement;
    expect(within(statusItem).getByText("Başarısız")).toBeInTheDocument();

    expect(screen.getByText("Çıkış kodu: 4")).toBeInTheDocument();
  });

  it("çıkış kodu null ise Bilinmiyor yazar", async () => {
    installHistory(
      jsonResponse(pingHistoryWith([{ ...pingHistorySuccessful, return_code: null }])),
    );
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });
    expect(screen.getByText("Çıkış kodu: Bilinmiyor")).toBeInTheDocument();
  });
});

/* --- 7. Tablo --------------------------------------------------------------- */

describe("Ping geçmişi — son ölçümler tablosu", () => {
  it("birden fazla kaydı sunucunun döndürdüğü sırayla gösterir", async () => {
    const older: PingHistoryItem = {
      ...pingHistorySuccessful,
      job_id: "1d2b3c4e-5f60-4a71-8b92-0c3d4e5f6a7b",
      finished_at: "2026-08-01T08:00:04Z",
    };
    installHistory(jsonResponse(pingHistoryWith([pingHistoryFailed, older])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    const rows = historyRows();
    expect(rows).toHaveLength(2);
    // Sıra sunucudan gelir; istemci yeniden sıralamaz.
    expect(within(rows[0] as HTMLElement).getByText(/2026/)).toHaveAttribute(
      "datetime",
      pingHistoryFailed.finished_at,
    );
    expect(within(rows[1] as HTMLElement).getByText(/2026/)).toHaveAttribute(
      "datetime",
      older.finished_at,
    );
  });

  it("her satırda beş sayaç, durum ve çıkış kodu bulunur", async () => {
    installHistory(jsonResponse(pingHistoryWith([pingHistoryFailed])));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    const table = historyTable();
    for (const header of [
      "Bitiş zamanı",
      "Durum",
      "Toplam",
      "Erişilebilir",
      "Erişilemiyor",
      "Başarısız",
      "Sonuç alınamadı",
      "Çıkış kodu",
    ]) {
      expect(within(table).getByRole("columnheader", { name: header })).toBeInTheDocument();
    }

    const cells = within(historyRows()[0] as HTMLElement).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toEqual([
      "Başarısız",
      "5",
      "4",
      "1",
      "0",
      "0",
      "4",
    ]);
  });

  it("tablo erişilebilir bir caption taşır", async () => {
    installHistory(jsonResponse(pingHistoryWith([pingHistorySuccessful])));
    renderApp(DETAIL_ROUTE);

    const table = await screen.findByRole("table", { name: TABLE_NAME });
    expect(table.querySelector("caption")).toHaveTextContent(TABLE_NAME);
  });
});

/* --- 8-9. Adres ve query key ------------------------------------------------ */

describe("Ping geçmişi — istek adresi", () => {
  it("tam olarak limit=10 ile tek bir GET isteği yapar", async () => {
    const { requests } = installHistory(jsonResponse(emptyPingHistory));
    renderApp(DETAIL_ROUTE);

    await screen.findByText("Henüz kaydedilmiş bir ping ölçümü yok.");

    const history = historyRequests(requests);
    expect(history).toHaveLength(1);
    expect(history[0]?.method).toBe("GET");
    expect(history[0]?.url.endsWith(HISTORY_URL)).toBe(true);
  });

  it("başka bir inventory ayrı bir query key ve ayrı bir adres kullanır", async () => {
    const { requests } = installHistory(jsonResponse(emptyPingHistory));
    const { queryClient } = renderApp(`/inventories/${standaloneInventory.id}`);

    await screen.findByText("Henüz kaydedilmiş bir ping ölçümü yok.");

    const history = historyRequests(requests);
    expect(history).toHaveLength(1);
    expect(
      history[0]?.url.endsWith(
        `/api/inventories/${standaloneInventory.id}/ping-runs?limit=10`,
      ),
    ).toBe(true);

    // Diğer inventory'nin geçmişi bu ekranda hiç oluşmamıştır.
    expect(historyCacheIds(queryClient)).toEqual([standaloneInventory.id]);
  });
});

/** Cache'te geçmiş sorgusu bulunan inventory kimlikleri. */
function historyCacheIds(client: QueryClient): unknown[] {
  return client
    .getQueryCache()
    .getAll()
    .filter((query) => query.queryKey[3] === "ping-history")
    .map((query) => query.queryKey[2]);
}

/* --- 10-11. Hata ------------------------------------------------------------ */

describe("Ping geçmişi — hata", () => {
  const LEAKY_MESSAGE = "app-data/jobs/9a3c5e21/result.json okunamadı";
  const LEAKY_DETAIL = "CANARY-HISTORY-4b1d7e0a";

  it("503 cevabında sabit metin gösterir; backend message ve details görünmez", async () => {
    installHistory(
      errorResponse(503, "ping_history_unavailable", LEAKY_MESSAGE, {
        artifact_path: LEAKY_DETAIL,
      }),
    );
    renderApp(DETAIL_ROUTE);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(ERROR_TITLE);

    const body = document.body.innerHTML;
    expect(body.includes(LEAKY_MESSAGE)).toBe(false);
    expect(body.includes(LEAKY_DETAIL)).toBe(false);
    expect(body.includes("result.json")).toBe(false);
    expect(body.includes("ping_history_unavailable")).toBe(false);
  });

  it("hata bölümü sayfanın geri kalanını düşürmez", async () => {
    installHistory(errorResponse(503, "ping_history_unavailable", LEAKY_MESSAGE));
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("alert");
    // Kayıt metadata'sı ve ping formu görünmeye devam eder.
    expect(
      screen.getByRole("heading", { level: 2, name: linkedInventory.name }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: "Erişilebilirlik testi" }),
    ).toBeInTheDocument();
  });

  it("Yeniden dene aynı adrese yeni bir okuma yapar", async () => {
    let attempt = 0;
    const { requests } = install({
      pingRuns: () => {
        attempt += 1;
        return attempt === 1
          ? errorResponse(503, "ping_history_unavailable", LEAKY_MESSAGE)
          : jsonResponse(pingHistoryWith([pingHistorySuccessful]));
      },
    });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("alert");
    expect(historyRequests(requests)).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Yeniden dene" }));

    await screen.findByRole("table", { name: TABLE_NAME });
    const history = historyRequests(requests);
    expect(history).toHaveLength(2);
    expect(history[1]?.url.endsWith(HISTORY_URL)).toBe(true);
  });
});

/* --- 12. Otomatik yoklama yok ------------------------------------------------ */

describe("Ping geçmişi — gerçek zamanlı değildir", () => {
  it("zaman ilerlese bile kendiliğinden yeni istek atmaz", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { requests } = installHistory(jsonResponse(pingHistoryWith([pingHistorySuccessful])));
      renderApp(DETAIL_ROUTE);

      await screen.findByRole("table", { name: TABLE_NAME });
      expect(historyRequests(requests)).toHaveLength(1);

      await vi.advanceTimersByTimeAsync(120_000);

      expect(historyRequests(requests)).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

/* --- 13-15. Ping sonrası davranış ve token ----------------------------------- */

/** Planı açıp onaylar. */
async function confirmPing(routes: PingRoutes) {
  const mock = install({ preview: jsonResponse(pingPreviewResponse), ...routes });
  const app = renderApp(DETAIL_ROUTE);

  await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
  await userEvent.click(screen.getByRole("button", { name: "Önizle" }));
  await screen.findByRole("heading", { level: 4, name: "Onay bekleyen plan" });
  await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));

  return { ...mock, ...app };
}

describe("Ping geçmişi — confirm sonrası tazeleme", () => {
  it("başarılı confirm sonrasında geçmiş yeniden okunur", async () => {
    let read = 0;
    const { requests } = await confirmPing({
      confirm: jsonResponse(pingRunSuccessful),
      pingRuns: () => {
        read += 1;
        return jsonResponse(
          read === 1 ? emptyPingHistory : pingHistoryWith([pingHistorySuccessful]),
        );
      },
    });

    // İlk okuma boştu; confirm başarıyla döndükten sonra kayıt görünür oldu.
    await screen.findByRole("table", { name: TABLE_NAME });
    expect(historyRequests(requests)).toHaveLength(2);
    expect(historyRequests(requests)[1]?.url.endsWith(HISTORY_URL)).toBe(true);
  });

  it("başarısız confirm sonrasında geçmiş tazelenmez ve eski liste korunur", async () => {
    const { requests } = await confirmPing({
      confirm: errorResponse(503, "ping_execution_failed", "Ansible çalıştırılamadı."),
      pingRuns: jsonResponse(pingHistoryWith([pingHistorySuccessful])),
    });

    await screen.findByRole("heading", { level: 4, name: "İşlem tamamlanamadı" });

    // Hata yolunda invalidation hiç yapılmaz: tek bir geçmiş okuması vardır.
    await waitFor(() => expect(historyRequests(requests)).toHaveLength(1));
    // Önceden okunmuş geçmiş ekranda olduğu gibi durur.
    expect(screen.getByRole("table", { name: TABLE_NAME })).toBeInTheDocument();
    expect(summaryValue("Erişilebilir")).toHaveTextContent("5");
  });

  it("onay token'ı geçmiş sorgusunun key veya verisine girmez", async () => {
    const { queryClient } = await confirmPing({
      confirm: jsonResponse(pingRunSuccessful),
      pingRuns: jsonResponse(pingHistoryWith([pingHistorySuccessful])),
    });

    await screen.findByRole("table", { name: TABLE_NAME });

    // Token hiçbir query cache girdisinde bulunmaz ve mutation cache boştur.
    const leaked = queryClient
      .getQueryCache()
      .getAll()
      .some(
        (query) =>
          JSON.stringify(query.queryKey).includes(PING_TOKEN) ||
          (JSON.stringify(query.state.data) ?? "").includes(PING_TOKEN),
      );
    expect(leaked).toBe(false);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    expect(document.body.innerHTML.includes(PING_TOKEN)).toBe(false);
  });
});

/* --- 16. Yüzeyin darlığı ----------------------------------------------------- */

describe("Ping geçmişi — dar yüzey", () => {
  it("host adı, mesaj, artifact yolu veya aktör istemez ve göstermez", async () => {
    const { requests } = installHistory(
      jsonResponse(pingHistoryWith([pingHistorySuccessful, pingHistoryFailed])),
    );
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });

    // İstek gövdesizdir ve hiçbir ek alan talep etmez.
    const request = historyRequests(requests)[0];
    expect(request?.body).toBeUndefined();
    expect(request?.url.includes("hosts")).toBe(false);

    // Cevapta zaten bulunmayan alanlar ekranda da üretilmez.
    const section = (await waitForHistorySection()).closest("section") as HTMLElement;
    const text = section.innerHTML;
    expect(text.includes("web01")).toBe(false);
    expect(text.includes("artifact")).toBe(false);
    expect(text.includes(pingHistorySuccessful.job_id)).toBe(false);
  });
});

/* --- 18. Sayfa bağımsızlıkları ------------------------------------------------ */

describe("Ping geçmişi — sayfa bağımsızlıkları", () => {
  it("inventory içeriği okunamasa bile geçmiş görünür kalır", async () => {
    install({
      hosts: errorResponse(
        503,
        "inventory_parser_unavailable",
        "Inventory parser çalıştırılamadı.",
      ),
      pingRuns: jsonResponse(pingHistoryWith([pingHistorySuccessful])),
    });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("table", { name: TABLE_NAME });
    expect(summaryValue("Toplam")).toHaveTextContent("5");
    // İçerik hatası kendi bölümünde kalır.
    expect(screen.getByText("Inventory parser kullanılamıyor")).toBeInTheDocument();
  });

  it("geçmiş okunamasa bile inventory içeriği görünür kalır", async () => {
    install({
      pingRuns: errorResponse(503, "ping_history_unavailable", "Geçmiş okunamadı."),
    });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("alert");
    expect(
      await screen.findByRole("heading", { level: 3, name: "Host'lar" }),
    ).toBeInTheDocument();
  });
});
