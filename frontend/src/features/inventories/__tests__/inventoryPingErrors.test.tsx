/**
 * Ping hatalarının gerçek `App` üzerinde nasıl göründüğü (Aşama 2B).
 *
 * Saf eşleme matrisi `pingErrorMessages.test.ts` içindedir; burada yalnızca
 * yüksek riskli yolların **kullanıcıya ne gösterdiği** ölçülür: hangi eylemler
 * sunuluyor, hangileri kalıcı olarak kapanıyor ve ham backend `details` alanının
 * hiçbir parçası ekrana geliyor mu.
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { linkedInventory } from "../../../test/fixtures";
import {
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
} from "../../../test/harness";
import {
  networkFailure,
  pingPreviewResponse,
  pingResponder,
  type PingRoutes,
} from "../../../test/pingFixtures";

const DETAIL_ROUTE = `/inventories/${linkedInventory.id}`;

/** Hiçbir hata panelinde görünmemesi gereken kanarya. */
const CANARY = "CANARY-DETAIL-9f24c7be015a38d6";

function install(routes: PingRoutes) {
  return installFetchMock(pingResponder(routes));
}

async function openPlan(): Promise<void> {
  await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
  await userEvent.click(screen.getByRole("button", { name: "Önizle" }));
  await screen.findByRole("heading", { level: 4, name: "Onay bekleyen plan" });
}

/** Onay planı açıp confirm eder; hata panelinin başlığını bekler. */
async function confirmAndFail(routes: PingRoutes, title: string): Promise<void> {
  install({ preview: jsonResponse(pingPreviewResponse), ...routes });
  renderApp(DETAIL_ROUTE);

  await openPlan();
  await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));
  await screen.findByRole("heading", { level: 4, name: title });
}

function bodyIncludes(needle: string): boolean {
  return document.body.innerHTML.includes(needle);
}

/** Aynı onayla tekrar denemeye yarayacak hiçbir eylem kalmamalıdır. */
function expectNoRetryAffordance(): void {
  expect(
    screen.queryByRole("button", { name: "Onayla ve Ping Çalıştır" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Tekrar dene" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vazgeç" })).not.toBeInTheDocument();
}

/* --- Confirm: belirsiz sonuçlar --------------------------------------------- */

describe("Ping hataları — confirm belirsizliği", () => {
  it("taşıma hatasında ping'in çalışmış olabileceğini söyler ve retry sunmaz", async () => {
    await confirmAndFail(
      { confirm: networkFailure },
      "Sunucudan cevap alınamadı; ping başlamış olabilir",
    );

    // Uyarı hem eşleyici mesajında hem panelin `retryable: false` notunda geçer.
    expect(screen.getAllByText(/otomatik olarak tekrar etmeyin/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Onaylar tek kullanımlıktır/i)).toBeInTheDocument();
    expectNoRetryAffordance();
    // Tek çıkış yolu boş forma dönmektir.
    expect(
      screen.getByRole("button", { name: "Yeni önizleme oluştur" }),
    ).toBeInTheDocument();
  });

  it("ApiError olmayan arızada ham exception metnini basmaz", async () => {
    // Gövde `ok` görünür ama JSON ayrıştırması patlar: `apiClient` bunu
    // `ApiError`'a çevirmez, ham istisna yukarı çıkar.
    await confirmAndFail(
      {
        confirm: {
          ok: true,
          status: 200,
          json: async () => {
            throw new SyntaxError(`Unexpected token < in ${CANARY}`);
          },
        },
      },
      "Ping isteği beklenmedik biçimde sonuçlandı",
    );

    expect(bodyIncludes(CANARY)).toBe(false);
    expect(bodyIncludes("SyntaxError")).toBe(false);
    expect(screen.getByText(/tamamlanmış olabilir/i)).toBeInTheDocument();
    expectNoRetryAffordance();
  });

  it("snapshot arızasında ping gönderilmedi güvencesi vermez", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(
          500,
          "ping_snapshot_unavailable",
          "Ping çalışma alanı hazırlanamadı.",
        ),
      },
      "Ping çalışma alanı arızası",
    );

    expect(bodyIncludes("ping gönderilmedi")).toBe(false);
    expect(screen.getByText(/çalışmış ve sonucu kaydedilmiş de olabilir/i)).toBeInTheDocument();
    expectNoRetryAffordance();
  });
});

/* --- Onayın kalıcı olarak kapanması ----------------------------------------- */

describe("Ping hataları — onayın kapanması", () => {
  it("ping_preview_invalid planı kapatır ve eski confirm geri gelmez", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(
          409,
          "ping_preview_invalid",
          "Ping önizlemesinin süresi doldu.",
          { reason: "expired" },
        ),
      },
      "Onay süresi doldu",
    );

    // Plan görünümü kapanır…
    expect(
      screen.queryByRole("heading", { level: 4, name: "Onay bekleyen plan" }),
    ).not.toBeInTheDocument();
    // …ve onay eylemi bir daha sunulmaz.
    expectNoRetryAffordance();
  });

  it("iptal arızasında eski confirm butonu geri gelmez", async () => {
    install({
      preview: jsonResponse(pingPreviewResponse),
      cancel: errorResponse(
        500,
        "ping_preview_unavailable",
        "Önizleme durumu temizlenemedi.",
      ),
    });
    renderApp(DETAIL_ROUTE);

    await openPlan();
    await userEvent.click(screen.getByRole("button", { name: "Vazgeç" }));

    await screen.findByRole("heading", { level: 4, name: "Önizleme iptali doğrulanamadı" });
    expectNoRetryAffordance();
    expect(screen.getByText(/yeniden kullanmayın/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Yeni önizleme oluştur" }),
    ).toBeInTheDocument();
  });

  it("Yeni önizleme oluştur yalnız boş forma döner, istek başlatmaz", async () => {
    const { requests } = installFetchMock(
      pingResponder({
        preview: jsonResponse(pingPreviewResponse),
        confirm: errorResponse(504, "ping_timeout", "Ping zaman aşımına uğradı."),
      }),
    );
    renderApp(DETAIL_ROUTE);

    await openPlan();
    await userEvent.click(screen.getByRole("button", { name: "Onayla ve Ping Çalıştır" }));
    await screen.findByRole("heading", { level: 4, name: "Ping zaman aşımına uğradı" });

    const before = requests.length;
    await userEvent.click(screen.getByRole("button", { name: "Yeni önizleme oluştur" }));

    expect(screen.getByRole("button", { name: "Önizle" })).toBeInTheDocument();
    expect(screen.getByLabelText("Host limiti (isteğe bağlı)")).toHaveValue("");
    // Hiçbir HTTP isteği kendiliğinden çıkmaz.
    expect(requests).toHaveLength(before);
  });
});

/* --- Doğrulanmış details alanları ------------------------------------------- */

describe("Ping hataları — details alanlarının gösterimi", () => {
  const JOB_ID = "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42";

  it("job_already_running doğrulanmış job_id'yi gösterir", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(
          409,
          "job_already_running",
          "Bu inventory için hâlâ çalışan bir ping işi var.",
          { job_id: JOB_ID },
        ),
      },
      "Bu inventory için bir ping işi zaten çalışıyor",
    );

    expect(screen.getByText(JOB_ID)).toBeInTheDocument();
  });

  it("canonical olmayan job_id gösterilmez", async () => {
    const badJobId = "6B1F0C74-8A2E-4D35-9C11-5F7AB0E39D42";

    await confirmAndFail(
      {
        confirm: errorResponse(
          409,
          "job_already_running",
          "Bu inventory için hâlâ çalışan bir ping işi var.",
          { job_id: badJobId },
        ),
      },
      "Bu inventory için bir ping işi zaten çalışıyor",
    );

    expect(bodyIncludes(badJobId)).toBe(false);
    expect(screen.queryByText(/İlgili iş kaydı/)).not.toBeInTheDocument();
  });

  it("ping_output_too_large stdout ile stderr'ı ayırır", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(502, "ping_output_too_large", "Çıktı sınırı aşıldı.", {
          stream: "stderr",
        }),
      },
      "Ping çıktısı boyut sınırını aştı",
    );

    expect(screen.getByText(/hata metni ürettiği için/i)).toBeInTheDocument();
    expect(screen.queryByText(/fazla sonuç ürettiği için/i)).not.toBeInTheDocument();
  });

  it("geçersiz stream değeri basılmaz ve genel mesaja düşer", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(502, "ping_output_too_large", "Çıktı sınırı aşıldı.", {
          stream: CANARY,
        }),
      },
      "Ping çıktısı boyut sınırını aştı",
    );

    expect(bodyIncludes(CANARY)).toBe(false);
    expect(screen.getByText(/Daha dar bir limit ile tekrar deneyin/i)).toBeInTheDocument();
  });

  it("ham details nesnesinin hiçbir parçası panelde render edilmez", async () => {
    await confirmAndFail(
      {
        confirm: errorResponse(
          409,
          "job_already_running",
          "Bu inventory için hâlâ çalışan bir ping işi var.",
          {
            job_id: JOB_ID,
            preview_token: CANARY,
            argv: ["ansible", "all", "-i", "/srv/app-data/snapshot.yml", "-m", "ping"],
            traceback: `Traceback (most recent call last): ${CANARY}`,
            snapshot_path: "/srv/app-data/ping-previews/abc/inventory-targets.yml",
          },
        ),
      },
      "Bu inventory için bir ping işi zaten çalışıyor",
    );

    expect(bodyIncludes(CANARY)).toBe(false);
    expect(bodyIncludes("Traceback")).toBe(false);
    expect(bodyIncludes("app-data")).toBe(false);
    expect(bodyIncludes("preview_token")).toBe(false);
    // Yalnızca type guard'dan geçen alan görünür.
    expect(screen.getByText(JOB_ID)).toBeInTheDocument();
  });
});

/* --- Preview aşaması hataları ----------------------------------------------- */

describe("Ping hataları — preview aşaması", () => {
  it("taşıma hatasında ping çalışmadığını söyler ama plan durumu için kesinlik iddia etmez", async () => {
    install({ preview: networkFailure });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
    await userEvent.click(screen.getByRole("button", { name: "Önizle" }));

    await screen.findByRole("heading", { level: 4, name: "Sunucudan cevap alınamadı" });
    expect(screen.getByText(/ping göndermez/i)).toBeInTheDocument();
    expect(bodyIncludes("plan oluşturulmadı")).toBe(false);
    // Preview aşamasında tükenmiş bir onay yoktur.
    expect(bodyIncludes("bu onay tükendi")).toBe(false);
  });

  it("hata paneli erişilebilir alert olarak duyurulur", async () => {
    install({
      preview: errorResponse(422, "ping_no_hosts_matched", "Limit hiçbir host ile eşleşmedi."),
    });
    renderApp(DETAIL_ROUTE);

    await screen.findByRole("heading", { level: 3, name: "Erişilebilirlik testi" });
    await userEvent.click(screen.getByRole("button", { name: "Önizle" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Limit hiçbir host ile eşleşmedi");
    // Anlam yalnızca renkle verilmez: kutunun görünür metin etiketi vardır.
    expect(alert).toHaveTextContent("Hata");
  });
});
