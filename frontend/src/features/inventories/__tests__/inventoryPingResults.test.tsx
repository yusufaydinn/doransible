/**
 * Terminal ping sonucu, plan varyantları ve jsdom düzeyinde erişilebilirlik
 * (Aşama 2B).
 *
 * En önemli ayrım burada ölçülür: `status: "failed"` bir **API hatası değildir**.
 * İş çalıştı, terminal duruma geçti ve sonucu kaydedildi (ADR-019 Karar 7); bu
 * yüzden hata kutusu değil uyarı kutusu kullanılır ve özet ile host tablosu her
 * iki durumda da gösterilir.
 */

import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { linkedInventory, standaloneInventory } from "../../../test/fixtures";
import { installFetchMock, jsonResponse, renderApp } from "../../../test/harness";
import {
  pingPreviewResponse,
  pingResponder,
  pingRunFailed,
  pingRunSuccessful,
  previewWith,
  type PingRoutes,
} from "../../../test/pingFixtures";
import type { PingPlan, PingRunResponse } from "../types";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;

function install(routes: PingRoutes) {
  return installFetchMock(pingResponder(routes));
}

async function openPlan(): Promise<void> {
  await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
  await userEvent.click(screen.getByRole("button", { name: "Önizle" }));
  await screen.findByRole("heading", { level: 4, name: "Onay bekleyen plan" });
}

/** Planı açar, onaylar ve sonucu bekler. */
async function runPing(run: PingRunResponse, plan?: Partial<PingPlan>) {
  const mock = install({
    preview: jsonResponse(plan === undefined ? pingPreviewResponse : previewWith(plan)),
    confirm: jsonResponse(run),
  });
  const app = renderApp(DETAIL_ROUTE);

  await openPlan();
  await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));
  await screen.findByRole("table", { name: "Host bazlı ping sonuçları" });

  return { ...mock, ...app };
}

/** Plan panelini açar; sonuç aşamasına geçmez. */
async function showPlan(plan: Partial<PingPlan> = {}) {
  const mock = install({ preview: jsonResponse(previewWith(plan)) });
  const app = renderApp(DETAIL_ROUTE);
  await openPlan();
  return { ...mock, ...app };
}

function resultTable(): HTMLElement {
  return screen.getByRole("table", { name: "Host bazlı ping sonuçları" });
}

function hostRow(name: string): HTMLElement {
  const row = within(resultTable()).getByText(name).closest("tr");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

/**
 * Tanım listesindeki bir terimin değerini döndürür.
 *
 * Sorgu bilinçli olarak `<dt>` ile sınırlıdır: "Başarısız" ve "Erişilebilir"
 * gibi kelimeler hem özet teriminde hem host tablosunun durum hücresinde geçer.
 */
function valueFor(term: string): HTMLElement {
  const terms = screen.getAllByText(term).filter((element) => element.tagName === "DT");
  expect(terms).toHaveLength(1);

  const node = terms[0]?.nextElementSibling;
  expect(node).not.toBeNull();
  return node as HTMLElement;
}

/* --- Başarılı sonuç --------------------------------------------------------- */

describe("Ping sonucu — başarılı iş", () => {
  it("iş kimliği, çıkış kodu, limit, zamanlar ve özeti gösterir", async () => {
    await runPing(pingRunSuccessful);

    expect(screen.getByText(pingRunSuccessful.job_id)).toBeInTheDocument();
    expect(valueFor("Çıkış kodu")).toHaveTextContent("0");
    expect(valueFor("Limit")).toHaveTextContent("Tüm inventory");
    expect(valueFor("Başlangıç")).toHaveTextContent("2026");
    expect(valueFor("Bitiş")).toHaveTextContent("2026");
    expect(valueFor("Toplam host")).toHaveTextContent("2");
    expect(valueFor("Erişilebilir")).toHaveTextContent("2");
  });

  it("başarı kutusunu metin etiketiyle birlikte gösterir", async () => {
    await runPing(pingRunSuccessful);

    const box = screen
      .getByRole("heading", { level: 4, name: "Ping tamamlandı: tüm host'lar erişilebilir" })
      .closest(".status") as HTMLElement;

    // Anlam yalnızca renkle verilmez; kutunun görünür metin etiketi vardır.
    expect(within(box).getByText("Tamamlandı")).toBeInTheDocument();
    expect(box).toHaveAttribute("role", "status");
  });
});

/* --- Başarısız (fakat tamamlanmış) sonuç ------------------------------------ */

describe("Ping sonucu — tamamlanmış fakat başarısız iş", () => {
  it("API hatası gibi değil, uyarı olarak gösterilir", async () => {
    await runPing(pingRunFailed);

    const box = screen
      .getByRole("heading", { level: 4, name: "Ping tamamlandı: bazı host kontrolleri başarısız" })
      .closest(".status") as HTMLElement;

    expect(within(box).getByText("Uyarı")).toBeInTheDocument();
    expect(within(box).queryByText("Hata")).not.toBeInTheDocument();
    expect(box).toHaveClass("status--warning");
    expect(box).not.toHaveClass("status--error");
    // Kök nedenin bu sonuçtan tek başına sınıflandırılamadığı söylenir; "bu
    // bir sunucu arızası değildir" gibi kesin bir iddia yoktur.
    expect(within(box).getByText(/kök neden.*sınıflandırılamaz/i)).toBeInTheDocument();
    expect(within(box).queryByText(/sunucu arızası değildir/i)).not.toBeInTheDocument();
  });

  it("başarısız işte de özet ve host tablosu gösterilir", async () => {
    await runPing(pingRunFailed);

    expect(valueFor("Çıkış kodu")).toHaveTextContent("4");
    expect(valueFor("Limit")).toHaveTextContent("web");
    expect(valueFor("Toplam host")).toHaveTextContent("4");
    expect(valueFor("Erişilemiyor")).toHaveTextContent("1");
    expect(valueFor("Başarısız")).toHaveTextContent("1");
    expect(valueFor("Sonuç alınamadı")).toHaveTextContent("1");
    expect(within(resultTable()).getAllByRole("row")).toHaveLength(5);
  });

  it("dört host durumunun tamamını metinle etiketler", async () => {
    await runPing(pingRunFailed);

    expect(hostRow("web01")).toHaveTextContent("Erişilebilir");
    expect(hostRow("web02")).toHaveTextContent("Erişilemiyor");
    expect(hostRow("db01")).toHaveTextContent("Başarısız");
    expect(hostRow("app01")).toHaveTextContent("Sonuç alınamadı");
  });

  it("her durum görünür metinli bir rozet taşır", async () => {
    await runPing(pingRunFailed);

    // Anlam yalnızca renkle verilmez: her rozetin görünür metni durum
    // etiketiyle birebir aynıdır.
    expect(within(hostRow("web01")).getByText("Erişilebilir")).toHaveClass(
      "badge",
      "badge--reachable",
    );
    expect(within(hostRow("web02")).getByText("Erişilemiyor")).toHaveClass(
      "badge",
      "badge--unreachable",
    );
    expect(within(hostRow("db01")).getByText("Başarısız")).toHaveClass("badge", "badge--failed");
    expect(within(hostRow("app01")).getByText("Sonuç alınamadı")).toHaveClass(
      "badge",
      "badge--no_result",
    );
  });

  it("unreachable/failed/no_result satırları renkten bağımsız semantik sınıf taşır", async () => {
    await runPing(pingRunFailed);

    expect(hostRow("web01")).not.toHaveClass("table-row--problem", "table-row--unknown");
    expect(hostRow("web02")).toHaveClass("table-row--problem");
    expect(hostRow("db01")).toHaveClass("table-row--problem");
    expect(hostRow("app01")).toHaveClass("table-row--unknown");
  });

  it("karma bir sonuçta erişilebilir ve erişilemeyen host sayısını özet ve tablo birlikte gösterir", async () => {
    const mixed: PingRunResponse = {
      ...pingRunFailed,
      limit: null,
      summary: { total: 5, reachable: 4, unreachable: 1, failed: 0, no_result: 0 },
      hosts: [
        { name: "ubuntu-demo-2", status: "reachable", message: null },
        { name: "ubuntu-demo-3", status: "reachable", message: null },
        { name: "ubuntu-demo-4", status: "reachable", message: null },
        { name: "ubuntu-demo-5", status: "reachable", message: null },
        { name: "ubuntu-demo-6", status: "unreachable", message: "Bağlantı kurulamadı." },
      ],
    };

    await runPing(mixed);

    expect(valueFor("Toplam host")).toHaveTextContent("5");
    expect(valueFor("Erişilebilir")).toHaveTextContent("4");
    expect(valueFor("Erişilemiyor")).toHaveTextContent("1");
    expect(within(resultTable()).getAllByRole("row")).toHaveLength(6);
    for (const name of ["ubuntu-demo-2", "ubuntu-demo-3", "ubuntu-demo-4", "ubuntu-demo-5"]) {
      expect(hostRow(name)).toHaveTextContent("Erişilebilir");
    }
    expect(hostRow("ubuntu-demo-6")).toHaveTextContent("Erişilemiyor");
  });

  /*
   * AUDIT-FIX1 bulgu 1: `PingRun` `failed` olduğunda neden yalnız unreachable
   * olmayabilir — `failed` veya `no_result` da olabilir. Başlık, hiçbir
   * host'un `unreachable` olmadığı bu iki kombinasyonda da "erişilemedi"
   * iddiası kurmayan, nötr bir metin göstermelidir.
   */

  it("unreachable=0, failed=1 iken başlık erişilemedi iddiası kurmaz", async () => {
    const run: PingRunResponse = {
      ...pingRunFailed,
      limit: null,
      summary: { total: 1, reachable: 0, unreachable: 0, failed: 1, no_result: 0 },
      hosts: [{ name: "db01", status: "failed", message: "Modül çalıştırılamadı." }],
    };

    await runPing(run);

    // Başlığın kendisi hiçbir host'un erişilemediğini iddia etmez — yalnız
    // genel, nötr bir ifadedir. Gövde metni (aşağıda) üç olası durumu da
    // (unreachable/failed/no_result) tarafsızca sıralayabilir; bu ayrı bir
    // endişedir ve burada ölçülmez.
    expect(
      screen.getByRole("heading", { level: 4, name: "Ping tamamlandı: bazı host kontrolleri başarısız" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Ping tamamlandı: bazı host'lara erişilemedi" }),
    ).not.toBeInTheDocument();
    expect(valueFor("Erişilemiyor")).toHaveTextContent("0");
    expect(valueFor("Başarısız")).toHaveTextContent("1");
    expect(hostRow("db01")).toHaveTextContent("Başarısız");
  });

  it("unreachable=0, failed=0, no_result=1 iken de aynı nötr başlık gösterilir", async () => {
    const run: PingRunResponse = {
      ...pingRunFailed,
      limit: null,
      summary: { total: 1, reachable: 0, unreachable: 0, failed: 0, no_result: 1 },
      hosts: [{ name: "app01", status: "no_result", message: null }],
    };

    await runPing(run);

    expect(
      screen.getByRole("heading", { level: 4, name: "Ping tamamlandı: bazı host kontrolleri başarısız" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Ping tamamlandı: bazı host'lara erişilemedi" }),
    ).not.toBeInTheDocument();
    expect(valueFor("Erişilemiyor")).toHaveTextContent("0");
    expect(valueFor("Sonuç alınamadı")).toHaveTextContent("1");
    expect(hostRow("app01")).toHaveTextContent("Sonuç alınamadı");
  });

  it("4 reachable + 1 unreachable görünümünde de aynı nötr başlık kullanılır", async () => {
    const run: PingRunResponse = {
      ...pingRunFailed,
      limit: null,
      summary: { total: 5, reachable: 4, unreachable: 1, failed: 0, no_result: 0 },
      hosts: [
        { name: "ubuntu-demo-2", status: "reachable", message: null },
        { name: "ubuntu-demo-3", status: "reachable", message: null },
        { name: "ubuntu-demo-4", status: "reachable", message: null },
        { name: "ubuntu-demo-5", status: "reachable", message: null },
        { name: "ubuntu-demo-6", status: "unreachable", message: "Bağlantı kurulamadı." },
      ],
    };

    await runPing(run);

    expect(
      screen.getByRole("heading", { level: 4, name: "Ping tamamlandı: bazı host kontrolleri başarısız" }),
    ).toBeInTheDocument();
    expect(valueFor("Erişilebilir")).toHaveTextContent("4");
    expect(valueFor("Erişilemiyor")).toHaveTextContent("1");
    expect(hostRow("ubuntu-demo-6")).toHaveTextContent("Erişilemiyor");
  });

  it("null mesaj için yapay hata açıklaması üretmez", async () => {
    await runPing(pingRunFailed);

    // `no_result` host'unun mesajı yoktur; yerine uydurma bir açıklama konmaz.
    const row = hostRow("app01");
    expect(within(row).getByText("—")).toBeInTheDocument();
    expect(row).toHaveTextContent(/^app01Sonuç alınamadı—$/);
  });

  it("host mesajını yalnız metin olarak basar, HTML çalıştırmaz", async () => {
    const injected = '<img src="x" onerror="alert(1)"><b>kalın</b>';
    await runPing({
      ...pingRunFailed,
      hosts: [{ name: "web02", status: "unreachable", message: injected }],
      summary: { total: 1, reachable: 0, unreachable: 1, failed: 0, no_result: 0 },
    });

    const row = hostRow("web02");
    // Metin olduğu gibi görünür…
    expect(within(row).getByText(injected)).toBeInTheDocument();
    // …ama hiçbir öğe olarak yorumlanmaz.
    expect(row.querySelector("img")).toBeNull();
    expect(row.querySelector("b")).toBeNull();
  });

  it("boş host listesini kontrollü açıklar", async () => {
    install({
      preview: jsonResponse(pingPreviewResponse),
      confirm: jsonResponse({
        ...pingRunFailed,
        hosts: [],
        summary: { total: 0, reachable: 0, unreachable: 0, failed: 0, no_result: 0 },
      }),
    });
    renderApp(DETAIL_ROUTE);

    await openPlan();
    await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));

    expect(await screen.findByText("Sonuçta host kaydı bulunmuyor.")).toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "Host bazlı ping sonuçları" }),
    ).not.toBeInTheDocument();
  });

  it("bilinmeyen çıkış kodu null ise Bilinmiyor gösterir", async () => {
    await runPing({ ...pingRunSuccessful, return_code: null });

    expect(valueFor("Çıkış kodu")).toHaveTextContent("Bilinmiyor");
  });
});

/* --- Sonuçtan sonra yeni önizleme ------------------------------------------- */

describe("Ping sonucu — yeni önizleme", () => {
  it("Yeni önizleme oluştur yalnız idle forma döner ve istek başlatmaz", async () => {
    const { requests } = await runPing(pingRunSuccessful);

    const before = requests.length;
    await userEvent.click(screen.getByRole("button", { name: "Yeni önizleme oluştur" }));

    expect(screen.getByRole("button", { name: "Önizle" })).toBeInTheDocument();
    expect(
      screen.queryByRole("table", { name: "Host bazlı ping sonuçları" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
    ).not.toBeInTheDocument();
    expect(requests).toHaveLength(before);
  });
});

/* --- Plan varyantları ------------------------------------------------------- */

describe("Ping planı — varyantlar", () => {
  it("accept_new politikası için görünür TOFU uyarısı gösterir", async () => {
    await showPlan({ host_key_policy: "accept_new" });

    const warning = screen.getByRole("heading", {
      level: 4,
      name: "İlk görülen host anahtarı sorgulanmadan kabul edilecek",
    });
    const box = warning.closest(".status") as HTMLElement;

    expect(within(box).getByText("Uyarı")).toBeInTheDocument();
    expect(box).toHaveTextContent(/trust on first use/i);
    expect(valueFor("Host anahtarı politikası")).toHaveTextContent("accept_new");
  });

  it("strict politikada TOFU uyarısı gösterilmez", async () => {
    await showPlan({ host_key_policy: "strict" });

    expect(valueFor("Host anahtarı politikası")).toHaveTextContent("Katı (strict)");
    expect(
      screen.queryByRole("heading", {
        name: "İlk görülen host anahtarı sorgulanmadan kabul edilecek",
      }),
    ).not.toBeInTheDocument();
  });

  it("become=true için görünür uyarı gösterir", async () => {
    await showPlan({ become: true });

    expect(
      screen.getByRole("heading", { level: 4, name: "Bu plan yetki yükseltme içeriyor" }),
    ).toBeInTheDocument();
    expect(valueFor("Yetki yükseltme (become)")).toHaveTextContent("Kullanılıyor");
  });

  it("become=false için uyarı yoktur ve kullanılmadığı yazar", async () => {
    await showPlan({ become: false });

    expect(
      screen.queryByRole("heading", { name: "Bu plan yetki yükseltme içeriyor" }),
    ).not.toBeInTheDocument();
    expect(valueFor("Yetki yükseltme (become)")).toHaveTextContent("Kullanılmıyor");
  });

  it("kırpılmış listeyi kesin hedef sayısından ayırır", async () => {
    await showPlan({
      hosts: ["web01", "web02"],
      host_count: 57,
      hosts_truncated: true,
    });

    // Sayı listeden bağımsız olarak kesindir.
    expect(valueFor("Hedef host sayısı")).toHaveTextContent("57");
    const note = screen.getByText(/Liste kısaltıldı/);
    expect(note).toHaveTextContent("2 host adı görünüyor");
    expect(note).toHaveTextContent("57 host üzerinde çalışacak");
  });

  it("kırpılmamış listede kısaltma açıklaması yoktur", async () => {
    await showPlan({ hosts_truncated: false });

    expect(screen.queryByText(/Liste kısaltıldı/)).not.toBeInTheDocument();
  });

  it("project bağlamını project adıyla gösterir", async () => {
    await showPlan();

    expect(valueFor("Bağlam")).toHaveTextContent("Bir project'e bağlı (Web sunucuları)");
  });

  it("standalone bağlamı bağımsız olarak gösterir", async () => {
    await showPlan({
      inventory: {
        id: standaloneInventory.id,
        name: standaloneInventory.name,
        binding: "standalone",
        project_id: null,
        project_name: null,
      },
    });

    expect(valueFor("Bağlam")).toHaveTextContent("Bağımsız (bir project'e bağlı değil)");
  });
});

/* --- Erişilebilirlik -------------------------------------------------------- */

describe("Ping — erişilebilirlik", () => {
  it("plan hazır olduğunda odak onay butonuna taşınır", async () => {
    await showPlan();

    expect(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" })).toHaveFocus();
  });

  it("onay bölümünün erişilebilir adı vardır", async () => {
    await showPlan();

    const group = screen.getByRole("group", { name: "Onay bekleyen plan" });
    expect(within(group).getByRole("button", { name: "Vazgeç" })).toBeInTheDocument();
  });

  it("yükleniyor durumları role=status ve aria-live taşır", async () => {
    // Plan hazırlanırken.
    install({ preview: new Promise(() => {}) });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
    await userEvent.click(screen.getByRole("button", { name: "Önizle" }));

    const status = await screen.findByText(/Onay planı hazırlanıyor/);
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("ping çalışırken durum canlı bölgede duyurulur", async () => {
    install({
      preview: jsonResponse(pingPreviewResponse),
      confirm: new Promise(() => {}),
    });
    renderApp(DETAIL_ROUTE);

    await openPlan();
    await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));

    const status = await screen.findByText(/Ping çalıştırılıyor/);
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("limit alanı etiket ve açıklamayla bağlıdır", async () => {
    install({});
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });

    const input = screen.getByLabelText("Host limiti (isteğe bağlı)");
    const hintId = input.getAttribute("aria-describedby");
    expect(hintId).not.toBeNull();

    const hint = document.getElementById(hintId as string);
    expect(hint).not.toBeNull();
    expect(hint).toHaveTextContent(/Boş bırakırsanız inventory'nin tamamı hedeflenir/);
  });

  it("hedeflenen host listesinin erişilebilir adı vardır", async () => {
    await showPlan();

    const list = screen.getByRole("list", { name: "Hedeflenen host'lar" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });

  it("sonuç tablosunun başlıkları ve caption'ı vardır", async () => {
    await runPing(pingRunFailed);

    const table = resultTable();
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    expect(headers).toEqual(["Host", "Durum", "Açıklama"]);
    // Host adları satır başlığıdır; ekran okuyucu hücreleri onlarla ilişkilendirir.
    expect(within(table).getAllByRole("rowheader")).toHaveLength(4);
  });

  it("başlık hiyerarşisi h3 bölüm ve h4 alt başlıklar biçimindedir", async () => {
    await runPing(pingRunSuccessful);

    expect(
      screen.getByRole("heading", { level: 3, name: "Erişilebilirlik testi" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 4, name: "Ping tamamlandı: tüm host'lar erişilebilir" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 4, name: "Özet" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 4, name: "Host sonuçları" }),
    ).toBeInTheDocument();
    // Bölüm içinde ikinci bir h3 veya atlanmış bir h5 yoktur.
    expect(screen.queryByRole("heading", { level: 5 })).not.toBeInTheDocument();
  });
});
