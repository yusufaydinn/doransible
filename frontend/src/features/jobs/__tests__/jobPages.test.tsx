/**
 * Job liste ve detay sayfaları (R1-V3D2B, R1-V3D3).
 *
 * Merkez iddialar:
 *
 * - `/jobs` bir sayfalık kaydı (en fazla 25) listeler, her satır detay
 *   sayfasına bağlanır ve sayfalar arası gezinme backend'in opaque
 *   `next_cursor` değeriyle yapılır.
 * - Job detayı pending/running iken 2 saniyede bir yenilenir; terminal durumda
 *   polling tamamen durur.
 * - Sonuç yalnızca terminal ve kayıtlı bir Job için istenir; aksi hâlde result
 *   endpoint'i **hiç** çağrılmaz.
 * - 404 ve result 503 ham hata ayrıntısı değil, güvenli bir mesaj gösterir.
 * - Başarısızlık sınıfı kullanıcı diline çevrilir (R1-V3G1): `playbook_failed`
 *   güvenilir bir playbook sonucundan, `runner_failed` ise sınıflandırmanın
 *   kesinleşmemesinden söz eder; ikisi de kök neden iddia etmez ve legacy
 *   `runner_failed` kayıtlarının recap taşıdığı gerçeği inkâr edilmez.
 * - Sanitize sözleşmesi boş bir iddia değildir: bilinmeyen payload alanlarına
 *   benzersiz sentinel değerler enjekte edilip render edilmedikleri ölçülür.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { activeProject } from "../../../test/fixtures";
import {
  deferred,
  errorResponse,
  installFetchMock,
  jsonResponse,
  renderApp,
  type RecordedRequest,
} from "../../../test/harness";
import {
  DISPLAY_OUTPUT_SENTINELS,
  displayOutput,
  displayOutputJobResult,
  failedJob,
  failedJobResult,
  jobList,
  jobResult,
  normalModeJob,
  pendingJob,
  playbookFailedJob,
  playbookFailedJobResult,
  playbookUnreachableJob,
  playbookUnreachableJobResult,
  runningJob,
  successfulJob,
  truncatedDisplayOutputJobResult,
  unstoredDisplayOutputJobResult,
} from "../../../test/jobFixtures";
import { JOB_ERROR_MESSAGES } from "../labels";
import { jobKeys } from "../queryKeys";
import type {
  PlaybookJobCursor,
  PlaybookJobList,
  PlaybookJobResult,
  PlaybookJobSummary,
  PublicErrorCode,
} from "../types";

/**
 * `PublicErrorCode` union'ında olup sözlükte karşılığı olmayan kod kümesi.
 *
 * `AssertNever` yalnız `never` kabul ettiği için, union'a yeni bir kod eklenip
 * `JOB_ERROR_MESSAGES` güncellenmezse bu satır `tsc --noEmit` sırasında hata
 * verir; yani exhaustiveness runtime'a bırakılmaz.
 */
type AssertNever<T extends never> = T;
type UncoveredErrorCode = AssertNever<
  Exclude<PublicErrorCode, keyof typeof JOB_ERROR_MESSAGES>
>;

/**
 * Ekranda görünmemesi gereken ham alan adları.
 *
 * `assert` ve `debug` bilinçli olarak listede **değildir**: meşru bir Ansible
 * task adı ("Assert sshd PermitRootLogin", "Debug facts") bu kelimeleri
 * taşıyabilir ve bu bir payload sızıntısı değildir. Sızıntının kendisi alan
 * adlarıyla değil, aşağıdaki sentinel testiyle ölçülür.
 */
const FORBIDDEN_PAYLOAD_PATTERNS = [
  /stdout/i,
  /stderr/i,
  /argv/i,
  /event_data/i,
  /\bres\b/i,
  /\bmsg\b/i,
  /task_args/i,
];

/**
 * Sanitize sözleşmesinin non-vacuous kanıtı için benzersiz sentinel değerler.
 *
 * Bunlar API'nin gerçek alanları değildir; testte bilinçle "kirletilmiş" bir
 * cevap üretmek için kullanılır. Değerler üründe hiçbir yerde geçmediği için
 * `document.body.textContent` içinde görülmeleri ancak bileşenin tanımadığı bir
 * alanı bastığı anlamına gelebilir.
 *
 * `ansible_output` bu kümeye **girmez** ve girmemelidir: o, sözleşmede yeri
 * olan, bilinçli olarak ham gösterilen bir display alanıdır (R1-V3J3A/J3B).
 * Buradaki `stdout`/`stderr` sentinel'leri ise sözleşmede hiç bulunmayan,
 * bilinmeyen payload alanlarıdır. İkisi karıştırılmamalıdır: aşağıdaki
 * fixture'lar `ansible_output` alanını `null` bırakır, yani bu test ham
 * çıktının gösterilmesini değil, tanınmayan alanların basılmamasını ölçer.
 */
const FORBIDDEN_SENTINELS = {
  stdout: "AOPS-G1C-FORBIDDEN-STDOUT",
  stderr: "AOPS-G1C-FORBIDDEN-STDERR",
  argv: "AOPS-G1C-FORBIDDEN-ARGV",
  eventData: "AOPS-G1C-FORBIDDEN-EVENT-DATA",
  res: "AOPS-G1C-FORBIDDEN-RES",
  msg: "AOPS-G1C-FORBIDDEN-MSG",
  taskArgs: "AOPS-G1C-FORBIDDEN-TASK-ARGS",
  debugPayload: "AOPS-G1C-FORBIDDEN-DEBUG-PAYLOAD",
  assertPayload: "AOPS-G1C-FORBIDDEN-ASSERT-PAYLOAD",
} as const;

/**
 * Sözleşmede olmayan payload alanlarını sonuç belgesine enjekte eder.
 *
 * `as unknown as` yalnız testin sınırında kullanılır: amaç tipi gevşetmek
 * değil, backend bir gün fazladan alan gönderse bile bileşenin onu basmadığını
 * ölçmektir.
 */
function withInjectedPayload(result: PlaybookJobResult): PlaybookJobResult {
  return {
    ...result,
    stdout: FORBIDDEN_SENTINELS.stdout,
    stderr: FORBIDDEN_SENTINELS.stderr,
    argv: ["ansible-playbook", FORBIDDEN_SENTINELS.argv],
    events: result.events.map((event) => ({
      ...event,
      event_data: { note: FORBIDDEN_SENTINELS.eventData },
      res: {
        msg: FORBIDDEN_SENTINELS.res,
        assertion: FORBIDDEN_SENTINELS.assertPayload,
        stdout: FORBIDDEN_SENTINELS.stdout,
      },
      msg: FORBIDDEN_SENTINELS.msg,
      task_args: { that: FORBIDDEN_SENTINELS.taskArgs },
      debug: FORBIDDEN_SENTINELS.debugPayload,
    })),
  } as unknown as PlaybookJobResult;
}

function expectNoRawPayload() {
  const text = document.body.textContent ?? "";
  for (const pattern of FORBIDDEN_PAYLOAD_PATTERNS) {
    expect(text).not.toMatch(pattern);
  }
}

/** Job detayını ve sonucunu sunan ortak fetch mock'u. */
function installJobDetailMock(job: PlaybookJobSummary, result: PlaybookJobResult) {
  return installFetchMock((request) => {
    if (request.url.endsWith(`/api/jobs/${job.job_id}`)) {
      return jsonResponse(job);
    }
    if (request.url.endsWith(`/api/jobs/${job.job_id}/result`)) {
      return jsonResponse(result);
    }
    return jsonResponse(activeProject);
  });
}

describe("Job listesi", () => {
  it("bir sayfalık işi, durum ve detay bağlantısıyla gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");

    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    // Başlık satırı + her Job için bir satır.
    expect(rows).toHaveLength(jobList.items.length + 1);
    expect(screen.getAllByRole("link", { name: "Detay" })).toHaveLength(jobList.items.length);
  });

  it("project/inventory sütunlarında yalnız #id değil gerçek adı gösterir, ID ikincil kalır (R1-V3J0B2)", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");

    const table = await screen.findByRole("table", { name: "Son çalıştırmalar" });
    const projectLinks = within(table).getAllByRole("link", { name: failedJob.project_name });
    expect(projectLinks[0]).toHaveAttribute("href", `/projects/${failedJob.project_id}`);
    const inventoryLinks = within(table).getAllByRole("link", {
      name: failedJob.inventory_name,
    });
    expect(inventoryLinks[0]).toHaveAttribute("href", `/inventories/${failedJob.inventory_id}`);
    // ID hâlâ görünür ama ikincil (teknik referans) olarak durur.
    expect(within(table).getAllByText(`#${failedJob.project_id}`).length).toBeGreaterThan(0);
  });

  it("boş listede bilgilendirici mesaj gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse({ items: [], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");

    expect(await screen.findByText("Henüz çalıştırma yok")).toBeInTheDocument();
  });

  it("Job bağlantısı klavyeyle kullanılabilir ve detay sayfasına götürür", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.endsWith(`/api/jobs/${failedJob.job_id}`)) {
        return jsonResponse(failedJob);
      }
      if (request.url.endsWith(`/api/jobs/${failedJob.job_id}/result`)) {
        return jsonResponse(failedJobResult);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    const [firstLink] = screen.getAllByRole("link", { name: "Detay" });
    firstLink?.focus();
    expect(firstLink).toHaveFocus();
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("heading", { level: 2, name: "Çalıştırma detayı" }),
    ).toBeInTheDocument();
  });
});

describe("Status ve mode filtreleri (R1-V3J0B2)", () => {
  /** Native label/select erişilebilirliği: `getByLabelText` doğrudan çalışır. */
  function statusSelect() {
    return screen.getByLabelText("Durum") as HTMLSelectElement;
  }
  function modeSelect() {
    return screen.getByLabelText("Kip") as HTMLSelectElement;
  }

  it("filtre yokken istek tam olarak mevcut /api/jobs?limit=25 sözleşmesini korur", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    await screen.findByText(failedJob.playbook_path);

    const jobRequests = requests.filter((request) => request.url.includes("/api/jobs?"));
    expect(jobRequests).toHaveLength(1);
    expect(jobRequests[0]?.url).toMatch(/\/api\/jobs\?limit=25$/);
  });

  it("durum seçimi doğru query parametresini üretir ve filtrelenmiş sonucu gösterir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.includes("/api/jobs?") && request.url.includes("status=failed")) {
        return jsonResponse({ items: [failedJob], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(statusSelect(), "failed");

    await waitFor(() =>
      expect(
        requests.some(
          (request) => request.url.includes("/api/jobs?") && request.url.includes("status=failed"),
        ),
      ).toBe(true),
    );
    const filteredRequest = requests.find((request) => request.url.includes("status=failed"));
    expect(filteredRequest?.url).toMatch(/limit=25&status=failed$/);
    // mode hiç seçilmediği için query'de hiç yer almaz.
    expect(filteredRequest?.url).not.toContain("mode=");
  });

  it("kip seçimi doğru query parametresini üretir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.includes("/api/jobs?") && request.url.includes("mode=normal")) {
        return jsonResponse({ items: [normalModeJob], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(modeSelect(), "normal");

    await waitFor(() =>
      expect(
        requests.some(
          (request) => request.url.includes("/api/jobs?") && request.url.includes("mode=normal"),
        ),
      ).toBe(true),
    );
    const filteredRequest = requests.find((request) => request.url.includes("mode=normal"));
    expect(filteredRequest?.url).toMatch(/limit=25&mode=normal$/);
    expect(filteredRequest?.url).not.toContain("status=");
  });

  it("durum ve kip birlikte seçildiğinde ikisi de tek istekte query'ye taşınır", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      return jsonResponse({ items: [], has_more: false, next_cursor: null });
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(statusSelect(), "failed");
    await user.selectOptions(modeSelect(), "normal");

    await waitFor(() =>
      expect(
        requests.some(
          (request) =>
            request.url.includes("status=failed") && request.url.includes("mode=normal"),
        ),
      ).toBe(true),
    );
    const combined = requests.find(
      (request) => request.url.includes("status=failed") && request.url.includes("mode=normal"),
    );
    expect(combined?.url).toMatch(/limit=25&status=failed&mode=normal$/);
  });

  it("farklı filtre seçimleri arasında geçiş her zaman doğru sonucu gösterir, birbirini kirletmez", async () => {
    // Bu test yalnız **görünen sonucun doğruluğunu** ölçer; kaç ağ isteği
    // atıldığına veya bir seçimin cache'ten mi yoksa yeniden fetch'ten mi
    // geldiğine dair bir iddia kurmaz — `staleTime: 0` altında bu ayrım
    // garanti edilemez (R1-V3J0B2-AUDIT-FIX1, bulgu 2). Anahtarların gerçekten
    // ayrıştığının kanıtı aşağıdaki `jobKeys.list` birim testlerindedir.
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.includes("status=failed") && !request.url.includes("mode=")) {
        return jsonResponse({ items: [failedJob], has_more: false, next_cursor: null });
      }
      if (request.url.includes("status=successful")) {
        return jsonResponse({ items: [successfulJob], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(statusSelect(), "failed");
    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
    expect(screen.queryByText(successfulJob.playbook_path)).not.toBeInTheDocument();

    await user.selectOptions(statusSelect(), "successful");
    expect(await screen.findByText(successfulJob.playbook_path)).toBeInTheDocument();
    expect(screen.queryByText(failedJob.playbook_path)).not.toBeInTheDocument();

    // İlk filtreye geri dönmek de doğru sonucu gösterir; ikinci filtrenin
    // verisi ilkinin ekranında kalmaz.
    await user.selectOptions(statusSelect(), "failed");
    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
    expect(screen.queryByText(successfulJob.playbook_path)).not.toBeInTheDocument();

    // Non-vacuous kanıt: senaryo gerçekten en az iki farklı filtreli istek
    // gönderdi (yalnız ilk render'ın filtresiz isteği değil).
    const jobRequests = requests.filter((request) => request.url.includes("/api/jobs?"));
    expect(jobRequests.filter((request) => request.url.includes("status=")).length).toBeGreaterThanOrEqual(2);
  });

  it("filtrelerle eşleşen kayıt olmadığında boş liste değil, filtreye özgü mesaj gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.includes("status=canceled")) {
        return jsonResponse({ items: [], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(statusSelect(), "canceled");

    expect(await screen.findByText("Filtrelerle eşleşen çalıştırma yok")).toBeInTheDocument();
    // Filtresiz boş durumun genel mesajıyla karıştırılmaz.
    expect(screen.queryByText("Henüz çalıştırma yok")).not.toBeInTheDocument();
  });

  it("status rozetleri filtre etkinken de görünür metin taşımaya devam eder", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse(jobList);
      }
      if (request.url.includes("status=failed")) {
        return jsonResponse({ items: [failedJob], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(statusSelect(), "failed");

    const table = await screen.findByRole("table", { name: "Son çalıştırmalar" });
    // Anlam yalnız rengden değil, rozetin kendi görünür metninden gelir.
    expect(within(table).getByText("Başarısız")).toBeInTheDocument();
  });

  describe("jobKeys.list — filtre anahtarlarının ayrışması (R1-V3J0B2-AUDIT-FIX1, bulgu 2)", () => {
    it("farklı status değerleri farklı anahtar üretir", () => {
      expect(jobKeys.list({ status: "failed" })).not.toEqual(
        jobKeys.list({ status: "successful" }),
      );
    });

    it("farklı mode değerleri farklı anahtar üretir", () => {
      expect(jobKeys.list({ mode: "check" })).not.toEqual(jobKeys.list({ mode: "normal" }));
    });

    it("filtresiz anahtar, status veya mode filtreli anahtardan farklıdır", () => {
      expect(jobKeys.list({})).not.toEqual(jobKeys.list({ status: "failed" }));
      expect(jobKeys.list({})).not.toEqual(jobKeys.list({ mode: "normal" }));
      expect(jobKeys.list({})).not.toEqual(jobKeys.list({ status: "failed", mode: "normal" }));
    });

    it("{} ile {status: undefined, mode: undefined} aynı normalize anahtarı üretir", () => {
      expect(jobKeys.list({})).toEqual(jobKeys.list({ status: undefined, mode: undefined }));
      // Argüman hiç verilmediğinde de (varsayılan `{}`) aynı sonuç.
      expect(jobKeys.list()).toEqual(jobKeys.list({ status: undefined, mode: undefined }));
    });
  });
});

describe("Çalıştırma kipinin kullanıcı diline çevrilmesi (R1-V3H2B)", () => {
  it("normal mode Job'ı listede ham check varsayımıyla değil kendi kipiyle gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith("/api/jobs?limit=25")) {
        return jsonResponse({ items: [normalModeJob], has_more: false, next_cursor: null });
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");

    // Kip filtresi her zaman iki seçeneği de sunar (R1-V3J0B2); iddia bu
    // yüzden **tabloya** özeldir, sayfanın tamamına değil — aksi hâlde
    // filtre `<select>`'indeki "Check" seçeneği yanlışlıkla yakalanırdı.
    const table = await screen.findByRole("table", { name: "Son çalıştırmalar" });
    expect(within(table).getByText("Normal (gerçek uygulama)")).toBeInTheDocument();
    expect(within(table).queryByText("Check (Ansible --check)")).not.toBeInTheDocument();
  });

  it("normal mode Job'ı detayda kullanıcı dostu etiket ve ham kodla birlikte gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${normalModeJob.job_id}`)) {
        return jsonResponse(normalModeJob);
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${normalModeJob.job_id}`);

    const label = await screen.findByText(/Normal \(gerçek uygulama\)/);
    expect(label).toBeInTheDocument();
    expect(label.closest("dd")).toHaveTextContent("normal");
  });
});

describe("Job detayı", () => {
  it("bulunamayan Job için güvenli mesaj gösterir", async () => {
    installFetchMock(() => errorResponse(404, "job_not_found", "Böyle bir çalıştırma kaydı bulunamadı."));

    renderApp(`/jobs/${pendingJob.job_id}`);

    expect(
      await screen.findByRole("heading", { level: 2, name: "Çalıştırma kaydı bulunamadı" }),
    ).toBeInTheDocument();
  });

  it(
    "404 sonrasında otomatik ikinci detail isteği oluşmaz (regresyon)",
    async () => {
      // Regresyon: `refetchInterval` `data` henüz gelmediğinde (ilk render ya
      // da query hatası) `status`'u `undefined` okuyup terminal olmayan bir
      // durummuş gibi 2 saniyede bir yeniden istek atıyordu; `retry: false`
      // olmasına rağmen kullanıcı hata mesajını görürken arka planda sessizce
      // polling devam ediyordu.
      let callCount = 0;
      installFetchMock((request) => {
        if (request.url.endsWith(`/api/jobs/${pendingJob.job_id}`)) {
          callCount += 1;
          return errorResponse(404, "job_not_found", "Böyle bir çalıştırma kaydı bulunamadı.");
        }
        return jsonResponse(activeProject);
      });

      renderApp(`/jobs/${pendingJob.job_id}`);

      await screen.findByRole("heading", { level: 2, name: "Çalıştırma kaydı bulunamadı" });
      expect(callCount).toBe(1);

      await new Promise((resolve) => setTimeout(resolve, 2500));
      expect(callCount).toBe(1);
    },
    6_000,
  );

  it("pending/running/kayıtsız sonuçta result endpoint'i hiç çağrılmaz", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${pendingJob.job_id}`)) {
        return jsonResponse(pendingJob);
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${pendingJob.job_id}`);

    await screen.findByText("Çalıştırma tamamlanana kadar sonuç okunmaz.");
    expect(requests.some((request) => request.url.includes("/result"))).toBe(false);
  });

  it(
    "pending/running iken 2 saniyede bir yenilenir, terminal durumda polling durur",
    async () => {
      let callCount = 0;
      const detailPath = `/api/jobs/${runningJob.job_id}`;
      // Sonuç bilerek kayıtsız bırakılır (`has_recorded_result: false`): bu
      // test yalnızca polling'in başlama/durma koşulunu sınar, sonuç okumayı
      // değil (bkz. "successful sonuç için host recap..." testi).
      const terminalJob = { ...successfulJob, has_recorded_result: false };
      installFetchMock((request) => {
        if (request.url.endsWith(detailPath)) {
          callCount += 1;
          return jsonResponse(callCount === 1 ? runningJob : terminalJob);
        }
        return jsonResponse(activeProject);
      });

      renderApp(`/jobs/${runningJob.job_id}`);

      await screen.findByText("Çalışıyor");
      await waitFor(() => expect(callCount).toBeGreaterThanOrEqual(2), { timeout: 4000 });
      await screen.findByText("Başarılı");

      const countAfterTerminal = callCount;
      await new Promise((resolve) => setTimeout(resolve, 2500));
      expect(callCount).toBe(countAfterTerminal);
    },
    10_000,
  );

  it("successful sonuç için host recap ve event'leri gösterir", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}`)) {
        return jsonResponse(successfulJob);
      }
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}/result`)) {
        return jsonResponse(jobResult);
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${successfulJob.job_id}`);

    expect(await screen.findByText("Install nginx")).toBeInTheDocument();
    expect(screen.getAllByText("runner_on_ok")).not.toHaveLength(0);
    expect(screen.getByRole("rowheader", { name: "web01" })).toBeInTheDocument();
    // Yasak alanlar hiç basılmaz.
    expect(document.body.textContent).not.toContain("stdout");
    expect(document.body.textContent).not.toContain("argv");
  });

  it("durum bandı ve etiket metni aynı anda yalnız bir kez görünür (R1-V3F0)", async () => {
    // Banttaki metin `JOB_STATUS_LABELS` etiketinden bilinçli olarak farklıdır;
    // aynı olsaydı bu test (ve üstteki polling testi) birden fazla eşleşme
    // bulup başarısız olurdu.
    installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}`)) {
        return jsonResponse(successfulJob);
      }
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}/result`)) {
        return jsonResponse(jobResult);
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${successfulJob.job_id}`);

    expect(await screen.findByText("Çalıştırma başarıyla tamamlandı")).toBeInTheDocument();
    expect(screen.getByText("Başarılı")).toBeInTheDocument();
  });

  it("project ve inventory'e kayıt adıyla, ID'yi ikincil tutarak bağlantı verir (R1-V3J0B2)", async () => {
    installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${pendingJob.job_id}`)) {
        return jsonResponse(pendingJob);
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${pendingJob.job_id}`);

    await screen.findByText("Çalıştırma kuyrukta bekliyor");
    // Bağlantı metni artık `#id` değil, backend'in döndürdüğü gerçek addır;
    // ID'den tahmin edilmez ve ayrı bir kısımda ikincil olarak durur.
    expect(
      screen.getByRole("link", { name: pendingJob.project_name }),
    ).toHaveAttribute("href", `/projects/${pendingJob.project_id}`);
    expect(
      screen.getByRole("link", { name: pendingJob.inventory_name }),
    ).toHaveAttribute("href", `/inventories/${pendingJob.inventory_id}`);
    expect(screen.getByText(`#${pendingJob.project_id}`)).toBeInTheDocument();
    expect(screen.getByText(`#${pendingJob.inventory_id}`)).toBeInTheDocument();
  });

  it("result 503 döndüğünde güvenli mesaj ve tekrar dene düğmesi gösterir", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}`)) {
        return jsonResponse(successfulJob);
      }
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}/result`)) {
        return errorResponse(503, "job_result_unavailable", "Sonuç şu anda okunamıyor.", {
          reason: "artifact_missing",
        });
      }
      return jsonResponse(activeProject);
    });

    renderApp(`/jobs/${successfulJob.job_id}`);
    const user = userEvent.setup();

    expect(
      await screen.findByText("Çalıştırma sonucu şu anda okunamıyor."),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("artifact_missing");

    const before = requests.filter((request) => request.url.includes("/result")).length;
    await user.click(screen.getByRole("button", { name: "Tekrar dene" }));
    await waitFor(() =>
      expect(requests.filter((request) => request.url.includes("/result")).length).toBe(
        before + 1,
      ),
    );
  });
});

describe("Başarısızlık sınıfının kullanıcı dili (R1-V3G1)", () => {
  const playbookNotice = JOB_ERROR_MESSAGES.playbook_failed;
  const runnerNotice = JOB_ERROR_MESSAGES.runner_failed;

  it("playbook_failed için kendi başlığını ve açıklamasını gösterir", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    // Sonuç paneli yüklenene kadar beklenir; aksi hâlde aşağıdaki sayım özet
    // bandını tek başına ölçerdi.
    await screen.findByRole("rowheader", { name: "web01" });

    expect(screen.getByText(playbookNotice.title)).toBeInTheDocument();
    expect(playbookNotice.title).toBe("Çalıştırma tamamlandı; playbook başarısız sonuç bildirdi");
    // Açıklama hem özet bandında hem sonuç panelinde durur; ikisi de aynı
    // sözlükten okur.
    expect(screen.getAllByText(playbookNotice.description)).toHaveLength(2);

    // Generic durum başlığı bu Job için artık kullanılmaz.
    expect(screen.queryByText("Çalıştırma başarısız oldu")).not.toBeInTheDocument();

    // Metin tek başına anlaşılır: renk/tone tek taşıyıcı değil. Fazla kesin
    // bir altyapı iddiası ("runner/altyapı arızası değildir") kurulmaz —
    // unreachable bir host için kök neden yine runner/ağ/SSH olabilir
    // (unreachable görünürlüğü regresyon sözleşmesi).
    expect(playbookNotice.description).toContain("kök nedenini sınıflandırmaz");
    expect(playbookNotice.description).not.toMatch(/runner veya altyapı arızası değildir/);
  });

  it("playbook_failed ham hata kodunu <code> olarak göstermeye devam eder", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    const codes = await screen.findAllByText("playbook_failed");
    expect(codes.length).toBeGreaterThan(0);
    expect(codes.some((node) => node.tagName === "CODE")).toBe(true);
  });

  it("playbook_failed sonucunda toplam failures kullanıcıya yazılır", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    // Fixture'da failures iki host'a dağıtılmıştır (2 + 1); sunum recap
    // toplamını okur, tek host'u değil.
    expect(
      await screen.findByText(/3 task sonucu başarısız olarak raporlandı/),
    ).toBeInTheDocument();
    // Unreachable yokken erişim cümlesi hiç kurulmaz.
    expect(screen.queryByText(/erişilemedi; bunun kök nedeni/)).not.toBeInTheDocument();
    expectNoRawPayload();
  });

  it("unreachable varyantında SSH/ağ/hedef yapılandırması ihtimali dürüstçe söylenir", async () => {
    installJobDetailMock(playbookUnreachableJob, playbookUnreachableJobResult);

    renderApp(`/jobs/${playbookUnreachableJob.job_id}`);

    expect(
      await screen.findByText(
        /2 host'a erişilemedi; bunun kök nedeni SSH, ağ veya hedef yapılandırması olabilir/,
      ),
    ).toBeInTheDocument();
    // İkisi birlikteyken iki cümle de görünür.
    expect(screen.getByText(/1 task sonucu başarısız olarak raporlandı/)).toBeInTheDocument();
    expectNoRawPayload();
  });

  it("legacy runner_failed Job'ı 'kesin sınıflandırılamadı' diliyle gösterir", async () => {
    installJobDetailMock(failedJob, failedJobResult);

    renderApp(`/jobs/${failedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });

    expect(screen.getByText(runnerNotice.title)).toBeInTheDocument();
    expect(runnerNotice.title).toBe("Çalıştırma sonucu kesin sınıflandırılamadı");
    expect(screen.getAllByText(runnerNotice.description)).toHaveLength(2);
    expect(screen.queryByText("Çalıştırma başarısız oldu")).not.toBeInTheDocument();

    // Legacy ihtimali açıkça anılır; kodun genel/toplayıcı olduğu saklanmaz.
    expect(runnerNotice.description).toContain("legacy");
    expect(runnerNotice.description).toContain("önceki sürümden kalan");
    expect(runnerNotice.description).toContain("olabilir");

    const codes = screen.getAllByText("runner_failed");
    expect(codes.some((node) => node.tagName === "CODE")).toBe(true);
  });

  it("runner_failed için uygunsuzluk, altyapı veya kesin kök neden iddiası kurulmaz", async () => {
    installJobDetailMock(failedJob, failedJobResult);

    renderApp(`/jobs/${failedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/uygunsuzluk/i);
    expect(text).not.toMatch(/altyapı sorunsuz/i);
    expect(text).not.toMatch(/playbook sonucu başarısızlık içeriyor/i);
    // Geri çekilen legacy iddialar: belge dolu bir recap taşıyor olabilir,
    // dolayısıyla "sonuç üretilemedi" veya "denetim sonucu çıkarılamaz"
    // denemez.
    expect(text).not.toMatch(/güvenilir sonuç üretilemedi/i);
    expect(text).not.toMatch(/denetim sonucu çıkarılamaz/i);
    expect(text).not.toMatch(/hiç üretilemedi/i);
    // Neden aday olarak sayılır, tek bir kök neden olarak ilan edilmez.
    expect(runnerNotice.description).toContain("olabilir");
    expectNoRawPayload();
  });

  it("runner_failed sonucu recap'e bakılarak yeniden sınıflandırılmaz", async () => {
    // `failedJobResult` recap'inde failures = 1 vardır; buna rağmen
    // `playbook_failed` sunumu (task/host sayaç cümleleri) kurulmamalıdır.
    expect(failedJobResult.recap.web01?.failures).toBe(1);
    installJobDetailMock(failedJob, failedJobResult);

    renderApp(`/jobs/${failedJob.job_id}`);

    // Ham recap tablosu olduğu gibi durur; olumsuz iddialar ancak sonuç paneli
    // gerçekten render olduktan sonra anlamlıdır.
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();
    expect(screen.getByText(runnerNotice.title)).toBeInTheDocument();
    expect(screen.queryByText(/task sonucu başarısız olarak raporlandı/)).not.toBeInTheDocument();
    expect(screen.queryByText(/erişilemedi; bunun kök nedeni/)).not.toBeInTheDocument();
  });

  it("başarılı Job'da hiçbir başarısızlık dili görünmez", async () => {
    installJobDetailMock(successfulJob, jobResult);

    renderApp(`/jobs/${successfulJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });
    expect(screen.getByText("Çalıştırma başarıyla tamamlandı")).toBeInTheDocument();
    expect(screen.queryByText(playbookNotice.title)).not.toBeInTheDocument();
    expect(screen.queryByText(runnerNotice.title)).not.toBeInTheDocument();
    expect(screen.queryByText("Sonuç değerlendirmesi")).not.toBeInTheDocument();
    // R1-V3I0 regresyonu: başarılı Job'da ne "Başarısız veya erişilemeyen
    // task/eventler" kısa özeti ne de ignored açıklaması görünür —
    // `jobResult` fixture'ında hiçbir event `failed: true` ya da
    // `runner_on_unreachable` değildir ve tüm `ignored` sayaçları 0'dır.
    expect(
      screen.queryByText("Başarısız veya erişilemeyen task/eventler"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/task hatasından sonra playbook çalışmaya devam etti/)).not
      .toBeInTheDocument();
    expect(screen.queryByText("Çalıştırma durumu")).not.toBeInTheDocument();
    expect(screen.queryByText("Playbook sonucu")).not.toBeInTheDocument();
    expectNoRawPayload();
  });

  it("bilinmeyen payload alanları basılmaz, meşru task adı görünür kalır", async () => {
    const injected = withInjectedPayload(playbookFailedJobResult);
    // Testin non-vacuous olduğunun kanıtı: sentinel'lerin hepsi gerçekten
    // bileşene verilen girdinin içindedir. Enjeksiyon bozulursa bu blok
    // patlar, sessizce "hiçbir şey sızmadı" demez.
    const injectedJson = JSON.stringify(injected);
    for (const [field, sentinel] of Object.entries(FORBIDDEN_SENTINELS)) {
      expect(injectedJson, field).toContain(sentinel);
    }

    installJobDetailMock(playbookFailedJob, injected);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    // Pozitif kanıt: sonuç paneli gerçekten render edildi, yani aşağıdaki
    // olumsuz iddialar boş bir ekranı ölçmüyor.
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();
    expect(screen.getByText(/3 task sonucu başarısız olarak raporlandı/)).toBeInTheDocument();

    // Meşru task adı "Assert" kelimesini taşır ve görünür kalır; yasak olan
    // task adı değil, assert/debug payload'ıdır.
    expect(screen.getAllByText("Assert sshd PermitRootLogin").length).toBeGreaterThan(0);

    const text = document.body.textContent ?? "";
    for (const [field, sentinel] of Object.entries(FORBIDDEN_SENTINELS)) {
      expect(text, field).not.toContain(sentinel);
    }
    expectNoRawPayload();
  });

  it("her PublicErrorCode için sabit bir başlık ve açıklama vardır", () => {
    // Derleme zamanı tanık: `UncoveredErrorCode` ancak sözlük union'ı tamamen
    // kapsıyorsa `never` olur, aksi hâlde bu dosya hiç derlenmez.
    const uncoveredErrorCodes: UncoveredErrorCode[] = [];
    expect(uncoveredErrorCodes).toHaveLength(0);

    const entries = Object.entries(JOB_ERROR_MESSAGES);
    expect(entries.length).toBeGreaterThan(0);
    for (const [code, notice] of entries) {
      expect(notice.title.length, code).toBeGreaterThan(0);
      expect(notice.description.length, code).toBeGreaterThan(0);
    }
    expect(Object.keys(JOB_ERROR_MESSAGES)).toEqual(
      expect.arrayContaining(["playbook_failed", "runner_failed"]),
    );
  });
});

describe("Execution tamamlandı ile playbook sonucunun ayrılması (R1-V3I0)", () => {
  it("playbook_failed banner'ı kırmızı/hata değil turuncu/uyarı tonunda gösterir", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });

    // Yeni başlık: güvenilir bir çalıştırmanın playbook başarısızlığı
    // bildirdiğini söyler; kök nedenin runner/altyapı dışında olduğunu iddia
    // etmez (kök neden sınıflandırması regresyon sözleşmesi).
    expect(
      screen.getByText("Çalıştırma tamamlandı; playbook başarısız sonuç bildirdi"),
    ).toBeInTheDocument();
    // Açıklama, kök nedeni sınıflandırmadığını söyler; ne kesinlikte
    // "runner/altyapı arızası değildir" iddiası kurar.
    expect(
      screen.getAllByText(/kök nedenini sınıflandırmaz/).length,
    ).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/runner veya altyapı arızası değildir/);

    // `StatusMessage` tonu görünür etiketle taşınır (`TONE_LABELS`): banner ve
    // sonuç paneli için "Uyarı" görünür, "Hata" hiç görünmez — renk tek başına
    // taşıyıcı değildir.
    expect(screen.getAllByText("Uyarı").length).toBeGreaterThan(0);
    expect(screen.queryByText("Hata")).not.toBeInTheDocument();

    // Eski/generic "çalıştırma başarısız oldu" iddiası artık kurulmaz.
    expect(screen.queryByText("Çalıştırma başarısız oldu")).not.toBeInTheDocument();
  });

  it("execution durumunu 'Tamamlandı', playbook sonucunu ayrı satırda 'Başarısız' gösterir", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });

    const executionStatusLabel = screen.getByText("Çalıştırma durumu");
    expect(executionStatusLabel.nextElementSibling).toHaveTextContent("Tamamlandı");

    const playbookOutcomeLabel = screen.getByText("Playbook sonucu");
    expect(playbookOutcomeLabel.nextElementSibling).toHaveTextContent("Başarısız");

    // Generic "Durum" satırı bu Job için artık gösterilmez; ayrım yerini alır.
    expect(screen.queryByText("Durum")).not.toBeInTheDocument();

    // Ham kod teknik alanda durmaya devam eder.
    const codes = await screen.findAllByText("playbook_failed");
    expect(codes.some((node) => node.tagName === "CODE")).toBe(true);
  });

  it("runner_failed hâlâ kırmızı/hata tonundadır ve 'çalıştırma tamamlandı' iddiası kurmaz", async () => {
    installJobDetailMock(failedJob, failedJobResult);

    renderApp(`/jobs/${failedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });

    // Mevcut generic "Durum" satırı korunur; yeni ayrım yalnız playbook_failed
    // için devreye girer.
    expect(screen.getByText("Durum")).toBeInTheDocument();
    expect(screen.queryByText("Çalıştırma durumu")).not.toBeInTheDocument();
    expect(screen.queryByText("Playbook sonucu")).not.toBeInTheDocument();

    // Tone hâlâ "Hata" (kırmızı); "tamamlandı" iddiası hiçbir başlıkta yoktur.
    expect(screen.getAllByText("Hata").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/Çalıştırma tamamlandı/);
  });

  it("başarısız event'lerin kısa listesini tablonun üstünde gösterir ve yalnız problem satırlarına semantik class ekler", async () => {
    // Fixture dosyasına dokunulmaz: karışık (başarılı + başarısız) bir event
    // listesi yalnızca bu test için, mevcut fixture'ların events dizileri
    // birleştirilerek türetilir. Böylece "başarısız olmayan satırlar mevcut
    // davranışı korusun" iddiası boş bir ölçüm olmaz.
    const mixedResult: PlaybookJobResult = {
      ...playbookFailedJobResult,
      events: [...jobResult.events, ...playbookFailedJobResult.events],
    };
    expect(mixedResult.events.filter((event) => event.failed)).toHaveLength(2);
    expect(mixedResult.events.filter((event) => !event.failed)).toHaveLength(3);

    installJobDetailMock(playbookFailedJob, mixedResult);

    const { container } = renderApp(`/jobs/${playbookFailedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });

    expect(
      screen.getByRole("heading", { level: 5, name: "Başarısız veya erişilemeyen task/eventler" }),
    ).toBeInTheDocument();

    // Kısa liste yalnız problem event'lerini gösterir (web01, db01); kök
    // neden uydurulmaz, yalnız host, task adı ve görünür "Başarısız" etiketi
    // gösterilir (bu fixture'da unreachable event yoktur).
    const list = screen.getByRole("heading", {
      level: 5,
      name: "Başarısız veya erişilemeyen task/eventler",
    }).nextElementSibling as HTMLElement;
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(list).toHaveTextContent("web01");
    expect(list).toHaveTextContent("db01");
    expect(list).toHaveTextContent("Assert sshd PermitRootLogin");
    expect(list.querySelectorAll("li")).toHaveLength(2);
    list.querySelectorAll("li").forEach((item) => expect(item).toHaveTextContent("Başarısız"));

    // Ana event tablosunda yalnız gerçekten problem satırları semantik class
    // taşır; başarılı satırlar mevcut (class'sız) davranışını korur. Renk tek
    // anlam kaynağı değildir — "Event" sütunundaki ham event kodu durur.
    const problemRows = container.querySelectorAll("tr.table-row--problem");
    expect(problemRows.length).toBe(2);
    problemRows.forEach((row) => {
      expect(row).toHaveTextContent("Evet");
    });

    const eventsTable = screen.getByText("Sanitize edilmiş event listesi").closest("table");
    const allRows = eventsTable?.querySelectorAll("tbody tr") ?? [];
    expect(allRows).toHaveLength(5);
  });

  it("olmayan başarısız event'ler için kısa liste hiç render edilmez (successful Job)", async () => {
    installJobDetailMock(successfulJob, jobResult);

    renderApp(`/jobs/${successfulJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });
    expect(
      screen.queryByRole("heading", { level: 5, name: "Başarısız veya erişilemeyen task/eventler" }),
    ).not.toBeInTheDocument();
  });

  it("unreachable-only event (failed=false, event=runner_on_unreachable) ile playbook_failed regresyonu", async () => {
    // `failures=0, unreachable=1` ve tek event `runner_on_unreachable` +
    // `failed: false`: yalnız `event.failed` bayrağına bakan bir filtre bu
    // event'i kaçırırdı. Fixture dosyasına dokunulmaz; sonuç bu test içinde
    // sıfırdan, sözleşmedeki tiplerle kurulur.
    const unreachableOnlyResult: PlaybookJobResult = {
      schema_version: 1,
      job_id: playbookFailedJob.job_id,
      return_code: 2,
      outcome: "failed",
      error_code: "playbook_failed",
      recap: {
        web01: {
          ok: 2,
          changed: 0,
          failures: 0,
          unreachable: 1,
          skipped: 0,
          rescued: 0,
          ignored: 0,
        },
      },
      events: [
        {
          event: "runner_on_unreachable",
          host: "web01",
          task: "Gather facts",
          changed: false,
          failed: false,
        },
      ],
      events_truncated: false,
      result_truncated: false,
      // Birleşik cevap shape'i (R1-V3J3A): v1 belgesi display çıktısı taşımaz.
      ansible_output: null,
      ansible_output_truncated: false,
    };
    // Testin non-vacuous olduğunun kanıtı: senaryo gerçekten "failed=false"
    // bir unreachable event içeriyor.
    expect(unreachableOnlyResult.events[0]?.failed).toBe(false);
    expect(unreachableOnlyResult.events[0]?.event).toBe("runner_on_unreachable");
    expect(unreachableOnlyResult.events.filter((event) => event.failed)).toHaveLength(0);

    installFetchMock((request) => {
      if (request.url.endsWith(`/api/jobs/${playbookFailedJob.job_id}`)) {
        return jsonResponse(playbookFailedJob);
      }
      if (request.url.endsWith(`/api/jobs/${playbookFailedJob.job_id}/result`)) {
        return jsonResponse(unreachableOnlyResult);
      }
      return jsonResponse(activeProject);
    });

    const { container } = renderApp(`/jobs/${playbookFailedJob.job_id}`);

    // Warning sunumu korunuyor.
    await screen.findByText("Çalıştırma tamamlandı; playbook başarısız sonuç bildirdi");
    expect(screen.getAllByText("Uyarı").length).toBeGreaterThan(0);
    expect(screen.queryByText("Hata")).not.toBeInTheDocument();

    // Problem kısa listesi render ediliyor ve "Erişilemedi" görünür.
    const heading = await screen.findByRole("heading", {
      level: 5,
      name: "Başarısız veya erişilemeyen task/eventler",
    });
    const list = heading.nextElementSibling as HTMLElement;
    expect(list.querySelectorAll("li")).toHaveLength(1);
    expect(list).toHaveTextContent("web01");
    expect(list).toHaveTextContent("Erişilemedi");
    expect(list).not.toHaveTextContent("Başarısız");

    // unreachable tablo satırı problem class'ı taşıyor (failed=false olmasına
    // rağmen).
    const problemRows = container.querySelectorAll("tr.table-row--problem");
    expect(problemRows).toHaveLength(1);
    expect(problemRows[0]).toHaveTextContent("runner_on_unreachable");

    // Metin, kök nedenin altyapı dışında olduğunu iddia etmiyor.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/runner veya altyapı arızası değildir/);
    expect(text).toMatch(/kök nedenini sınıflandırmaz/);
  });

  it("ignored toplamı > 0 ise açık bir devam etti metni gösterir; 'güvenlik bulgusu' demez", async () => {
    // Fixture dosyasına dokunulmaz: mevcut `playbookFailedJobResult` recap'i
    // burada yalnızca test içi bir kopya üzerinde `ignored` alanıyla genişletilir.
    const resultWithIgnored: PlaybookJobResult = {
      ...playbookFailedJobResult,
      recap: {
        ...playbookFailedJobResult.recap,
        web01: { ...playbookFailedJobResult.recap.web01!, ignored: 2 },
      },
    };
    installJobDetailMock(playbookFailedJob, resultWithIgnored);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });
    expect(
      await screen.findByText("2 task hatasından sonra playbook çalışmaya devam etti."),
    ).toBeInTheDocument();

    // Generic playbook: "ignored" ne bir güvenlik bulgusu ne de bir "bulgu
    // sayısı" olarak adlandırılır.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/güvenlik bulgusu/i);
    expect(text).not.toMatch(/bulgu sayısı/i);
  });

  it("ignored toplamı 0 ise devam etti metni hiç gösterilmez (regresyon)", async () => {
    installJobDetailMock(playbookFailedJob, playbookFailedJobResult);

    renderApp(`/jobs/${playbookFailedJob.job_id}`);

    await screen.findByRole("rowheader", { name: "web01" });
    expect(
      screen.queryByText(/task hatasından sonra playbook çalışmaya devam etti/),
    ).not.toBeInTheDocument();
  });
});

/**
 * Keyset cursor sayfalama gezinmesi (R1-V3J2A).
 *
 * Merkez iddialar:
 *
 * - İlk sayfa isteği hâlâ **tam olarak** `/api/jobs?limit=25`'tir; cursor
 *   parametreleri hiç görünmez.
 * - İleri gezinme yalnız sunucunun `next_cursor` cevabını kullanır. Bu
 *   testlerde cursor bilinçli olarak **hiçbir satırın alanlarıyla eşleşmez**
 *   (`SERVER_CURSOR`), böylece "cursor son satırdan türetilmiyor" iddiası
 *   boş bir iddia olmaz.
 * - `before_created_at` ve `before_job_id` ya birlikte gider ya hiç gitmez;
 *   backend yarım cursor'ı zaten reddeder, frontend onu hiç kuramaz.
 * - Sözleşme dışı bir cevapta (`has_more: true` ama `next_cursor: null`)
 *   gezinme fail-closed olur.
 * - Toplam kayıt/sayfa sayısı ve rastgele sayfaya atlama **yoktur**; keyset
 *   sayfalama böyle bir sayı üretmez.
 */
describe("Keyset cursor sayfalama (R1-V3J2A)", () => {
  /**
   * Sunucunun döndürdüğü opaque cursor.
   *
   * Değerleri hiçbir fixture Job'ın `created_at`/`job_id` alanıyla eşleşmez:
   * URL'de bu değerleri görmek, cursor'ın gerçekten cevaptan alındığının
   * (son satırdan yeniden türetilmediğinin) kanıtıdır.
   */
  const SERVER_CURSOR: PlaybookJobCursor = {
    created_at: "2026-07-28T09:00:00Z",
    job_id: "018f1e0a-9b1a-7c3a-9e2a-99887766aabb",
  };

  const firstPage: PlaybookJobList = {
    items: [failedJob, successfulJob],
    has_more: true,
    next_cursor: SERVER_CURSOR,
  };

  const secondPage: PlaybookJobList = {
    items: [runningJob, pendingJob],
    has_more: false,
    next_cursor: null,
  };

  const emptyLastPage: PlaybookJobList = { items: [], has_more: false, next_cursor: null };

  /** Query string'i ham metin karşılaştırmasına bırakmadan okur. */
  function queryOf(url: string): URLSearchParams {
    return new URL(url, "http://localhost").searchParams;
  }

  function jobListRequests(requests: RecordedRequest[]): RecordedRequest[] {
    return requests.filter((request) => request.url.includes("/api/jobs?"));
  }

  function hasCursor(request: RecordedRequest): boolean {
    const query = queryOf(request.url);
    return query.has("before_created_at") || query.has("before_job_id");
  }

  /** Liste isteklerini query'sine göre yanıtlayan ortak mock. */
  function installJobListMock(respond: (query: URLSearchParams) => unknown) {
    return installFetchMock((request) => {
      if (request.url.includes("/api/jobs?")) {
        return respond(queryOf(request.url));
      }
      return jsonResponse(activeProject);
    });
  }

  function pager() {
    return screen.getByRole("navigation", { name: "Çalıştırma sayfaları" });
  }
  function nextButton() {
    return within(pager()).getByRole("button", { name: "Sonraki" });
  }
  function previousButton() {
    return within(pager()).getByRole("button", { name: "Önceki" });
  }

  it("ilk sayfa isteği tam olarak /api/jobs?limit=25'tir, cursor parametresi taşımaz", async () => {
    const { requests } = installJobListMock(() => jsonResponse(firstPage));

    renderApp("/jobs");
    await screen.findByText(failedJob.playbook_path);

    const listRequests = jobListRequests(requests);
    expect(listRequests).toHaveLength(1);
    expect(listRequests[0]?.url).toMatch(/\/api\/jobs\?limit=25$/);
    expect(listRequests[0]?.url).not.toContain("before_");
  });

  it("\"Sonraki\", sunucunun next_cursor değerinin iki alanını da URL'ye taşır", async () => {
    const { requests } = installJobListMock((query) =>
      query.has("before_job_id") ? jsonResponse(secondPage) : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    expect(await screen.findByText(runningJob.playbook_path)).toBeInTheDocument();

    const listRequests = jobListRequests(requests);
    expect(listRequests).toHaveLength(2);
    const query = queryOf(listRequests[1]!.url);
    expect(query.get("limit")).toBe("25");
    // Değerler cevaptan aynen gelir; hiçbir satırın alanından türetilmez.
    expect(query.get("before_created_at")).toBe(SERVER_CURSOR.created_at);
    expect(query.get("before_job_id")).toBe(SERVER_CURSOR.job_id);
    expect(query.get("before_job_id")).not.toBe(successfulJob.job_id);
  });

  it("cursor alanları hiçbir istekte tek başına gitmez (yarım cursor kurulamaz)", async () => {
    const { requests } = installJobListMock((query) =>
      query.has("before_job_id") ? jsonResponse(secondPage) : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    await screen.findByText(runningJob.playbook_path);

    const listRequests = jobListRequests(requests);
    // Non-vacuous: senaryo gerçekten en az bir cursor'lı istek üretti.
    expect(listRequests.some(hasCursor)).toBe(true);
    for (const request of listRequests) {
      const query = queryOf(request.url);
      expect(query.has("before_created_at")).toBe(query.has("before_job_id"));
    }
  });

  it("status/mode filtreleri ikinci sayfa isteğinde de korunur", async () => {
    const { requests } = installJobListMock((query) => {
      if (query.get("status") === "failed" && query.has("before_job_id")) {
        return jsonResponse({ items: [runningJob], has_more: false, next_cursor: null });
      }
      if (query.get("status") === "failed") {
        return jsonResponse({ items: [failedJob], has_more: true, next_cursor: SERVER_CURSOR });
      }
      return jsonResponse(firstPage);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.selectOptions(screen.getByLabelText("Durum"), "failed");
    await waitFor(() => expect(nextButton()).toBeEnabled());

    await user.click(nextButton());
    expect(await screen.findByText(runningJob.playbook_path)).toBeInTheDocument();

    const pagedFiltered = jobListRequests(requests).filter(hasCursor);
    expect(pagedFiltered).toHaveLength(1);
    const query = queryOf(pagedFiltered[0]!.url);
    expect(query.get("status")).toBe("failed");
    expect(query.get("before_created_at")).toBe(SERVER_CURSOR.created_at);
    expect(query.get("before_job_id")).toBe(SERVER_CURSOR.job_id);
  });

  it("\"Önceki\" ilk sayfaya döner ve o istekte cursor parametreleri bulunmaz", async () => {
    const { requests } = installJobListMock((query) =>
      query.has("before_job_id") ? jsonResponse(secondPage) : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    await screen.findByText(runningJob.playbook_path);
    expect(screen.getByText("Sayfa 2")).toBeInTheDocument();

    const beforeBack = requests.length;
    await user.click(previousButton());

    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
    expect(screen.getByText("Sayfa 1")).toBeInTheDocument();
    expect(screen.queryByText(runningJob.playbook_path)).not.toBeInTheDocument();
    // Geri dönüş ister cache'ten ister yeni istekle gelsin, cursor'lı bir
    // ilk sayfa isteği asla kurulmaz.
    for (const request of jobListRequests(requests.slice(beforeBack))) {
      expect(hasCursor(request)).toBe(false);
    }
  });

  it("ilk sayfada \"Önceki\" devre dışıdır", async () => {
    installJobListMock(() => jsonResponse(firstPage));

    renderApp("/jobs");
    await screen.findByText(failedJob.playbook_path);

    expect(previousButton()).toBeDisabled();
    expect(screen.getByText("Sayfa 1")).toBeInTheDocument();
  });

  it("has_more false iken \"Sonraki\" devre dışıdır", async () => {
    installJobListMock(() => jsonResponse(secondPage));

    renderApp("/jobs");
    await screen.findByText(runningJob.playbook_path);

    expect(nextButton()).toBeDisabled();
  });

  it("has_more true ama next_cursor null olan sözleşme dışı cevapta \"Sonraki\" fail-closed devre dışı kalır", async () => {
    installJobListMock(() =>
      jsonResponse({ items: [failedJob], has_more: true, next_cursor: null }),
    );

    renderApp("/jobs");
    await screen.findByText(failedJob.playbook_path);

    // Cursor uydurmak yerine ileri gezinme kapatılır.
    expect(nextButton()).toBeDisabled();
  });

  it("filtre değişimi sayfayı 1'e sıfırlar ve eski cursor yeni isteğe taşınmaz", async () => {
    const { requests } = installJobListMock((query) => {
      if (query.get("mode") === "normal") {
        return jsonResponse({ items: [normalModeJob], has_more: false, next_cursor: null });
      }
      if (query.has("before_job_id")) {
        return jsonResponse(secondPage);
      }
      return jsonResponse(firstPage);
    });

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    await screen.findByText(runningJob.playbook_path);
    expect(screen.getByText("Sayfa 2")).toBeInTheDocument();

    const beforeFilter = requests.length;
    await user.selectOptions(screen.getByLabelText("Kip"), "normal");

    await waitFor(() => expect(screen.getByText("Sayfa 1")).toBeInTheDocument());
    expect(previousButton()).toBeDisabled();

    const afterFilter = jobListRequests(requests.slice(beforeFilter));
    // Non-vacuous: filtre değişimi gerçekten yeni bir istek üretti…
    expect(afterFilter).toHaveLength(1);
    const query = queryOf(afterFilter[0]!.url);
    expect(query.get("mode")).toBe("normal");
    // …ve o istek eski sayfanın cursor'ını hiç taşımadı.
    expect(query.has("before_created_at")).toBe(false);
    expect(query.has("before_job_id")).toBe(false);
  });

  it("ikinci sayfa boş dönerse kullanıcı kilitlenmez; \"Önceki\" kullanılabilir kalır", async () => {
    installJobListMock((query) =>
      query.has("before_job_id") ? jsonResponse(emptyLastPage) : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());

    expect(await screen.findByText("Bu sayfada çalıştırma bulunamadı")).toBeInTheDocument();
    // İlk sayfanın "Henüz çalıştırma yok" mesajıyla karıştırılmaz.
    expect(screen.queryByText("Henüz çalıştırma yok")).not.toBeInTheDocument();
    expect(screen.getByText("Sayfa 2")).toBeInTheDocument();
    // Otomatik geri atlama yapılmaz; kullanıcı kendisi döner.
    expect(previousButton()).toBeEnabled();

    await user.click(previousButton());
    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
  });

  /**
   * Liste hatasının sabit metin sözleşmesi (R1-V3J2AF).
   *
   * Bu değerler `JobListError`'da sabit yazılıdır; hiçbiri backend hata
   * zarfından türetilmez.
   */
  const LIST_ERROR_TITLE = "Çalıştırmalar yüklenemedi";
  const LIST_ERROR_MESSAGE = "Çalıştırma listesi şu anda yüklenemedi. Tekrar deneyin.";

  /**
   * `describeApiError`'ın **default** dalını gerçekten çalıştıran hata zarfı.
   *
   * Kod bilinçli olarak allowlist'te değildir: eski uygulamada bu yol
   * `error.message`'ı doğrudan panele basıyordu. Sentinel'ler o sızıntının
   * kırmızı/yeşil kanıtıdır — allowlist edilmiş bir kod (`request_validation_error`
   * gibi) kullanılsaydı test default yolu hiç ölçmezdi.
   */
  const UNKNOWN_ERROR_CODE = "job_list_backend_failure";
  const FORBIDDEN_ERROR_MESSAGE = "AOPS-J2AF-FORBIDDEN-MESSAGE";
  const FORBIDDEN_ERROR_DETAILS = "AOPS-J2AF-FORBIDDEN-DETAILS";

  function listFailureResponse() {
    return errorResponse(503, UNKNOWN_ERROR_CODE, FORBIDDEN_ERROR_MESSAGE, {
      reason: FORBIDDEN_ERROR_DETAILS,
      before_created_at: SERVER_CURSOR.created_at,
      before_job_id: SERVER_CURSOR.job_id,
    });
  }

  /** Hata panelinin backend zarfından hiçbir metin sızdırmadığını ölçer. */
  function expectFixedListErrorText() {
    const text = document.body.textContent ?? "";
    expect(text).toContain(LIST_ERROR_TITLE);
    expect(text).toContain(LIST_ERROR_MESSAGE);
    // Backend'in message/details/code alanlarının hiçbiri ekrana basılmaz.
    expect(text).not.toContain(FORBIDDEN_ERROR_MESSAGE);
    expect(text).not.toContain(FORBIDDEN_ERROR_DETAILS);
    expect(text).not.toContain(UNKNOWN_ERROR_CODE);
    // Cursor'ın alan adları ve değeri de sızmaz.
    expect(text).not.toContain("before_created_at");
    expect(text).not.toContain("before_job_id");
    expect(text).not.toContain(SERVER_CURSOR.job_id);
    expect(text).not.toContain(SERVER_CURSOR.created_at);
  }

  it("ilk sayfadaki liste hatası backend metnini değil sabit metni gösterir", async () => {
    installJobListMock(() => listFailureResponse());

    renderApp("/jobs");

    expect(await screen.findByText(LIST_ERROR_TITLE)).toBeInTheDocument();
    expectFixedListErrorText();
    expect(screen.getByRole("button", { name: "Tekrar dene" })).toBeInTheDocument();
  });

  it("ikinci sayfa hatasında sabit metin gösterilir, \"Tekrar dene\" aynı cursor'ı kullanır ve \"Önceki\" çalışır", async () => {
    const { requests } = installJobListMock((query) =>
      query.has("before_job_id") ? listFailureResponse() : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    expect(await screen.findByText(LIST_ERROR_TITLE)).toBeInTheDocument();
    expectFixedListErrorText();

    const beforeRetry = requests.length;
    await user.click(screen.getByRole("button", { name: "Tekrar dene" }));

    await waitFor(() => expect(jobListRequests(requests.slice(beforeRetry))).toHaveLength(1));
    const retryQuery = queryOf(jobListRequests(requests.slice(beforeRetry))[0]!.url);
    // Retry aynı query key'i, yani aynı cursor'ın **iki** alanını tekrar gönderir.
    expect(retryQuery.get("before_created_at")).toBe(SERVER_CURSOR.created_at);
    expect(retryQuery.get("before_job_id")).toBe(SERVER_CURSOR.job_id);

    // Hata panelinde takılı kalınmaz: önceki sayfaya dönüş hâlâ mümkündür.
    await waitFor(() => expect(previousButton()).toBeEnabled());
    await user.click(previousButton());
    expect(await screen.findByText(failedJob.playbook_path)).toBeInTheDocument();
    expect(screen.getByText("Sayfa 1")).toBeInTheDocument();
  });

  it("istek sürerken yinelenen \"Sonraki\" tıklaması cursor yığınını iki kez ilerletmez", async () => {
    const pendingSecondPage = deferred<unknown>();
    const { requests } = installJobListMock((query) =>
      query.has("before_job_id") ? pendingSecondPage.promise : jsonResponse(firstPage),
    );

    renderApp("/jobs");
    const user = userEvent.setup();
    await screen.findByText(failedJob.playbook_path);

    await user.click(nextButton());
    // İstek sürerken gezinme kilitli olmalı.
    expect(nextButton()).toBeDisabled();
    await user.click(nextButton());
    await user.click(nextButton());

    pendingSecondPage.resolve(jsonResponse(secondPage));

    expect(await screen.findByText(runningJob.playbook_path)).toBeInTheDocument();
    // Sayfa 3'e "atlanmadı" ve tek bir cursor'lı istek gönderildi.
    expect(screen.getByText("Sayfa 2")).toBeInTheDocument();
    expect(jobListRequests(requests).filter(hasCursor)).toHaveLength(1);
  });

  it("toplam kayıt/sayfa sayısı iddia etmez ve rastgele sayfaya atlama sunmaz", async () => {
    installJobListMock(() => jsonResponse(firstPage));

    renderApp("/jobs");
    await screen.findByText(failedJob.playbook_path);

    const navigation = pager();
    // Yalnız iki gezinme düğmesi; numaralı sayfa listesi veya sayfa seçici yok.
    expect(within(navigation).getAllByRole("button")).toHaveLength(2);
    expect(within(navigation).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(navigation).queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(within(navigation).queryByRole("textbox")).not.toBeInTheDocument();

    const navText = navigation.textContent ?? "";
    // "1 / N" ya da "Sayfa 1 / 4" gibi bir toplam iddiası yok.
    expect(navText).not.toMatch(/\d\s*\/\s*\d/);

    const pageText = document.body.textContent ?? "";
    expect(pageText).not.toMatch(/toplam/i);
    // Cursor'ın iç yapısı kullanıcıya hiçbir yerde sızmaz.
    expect(pageText).not.toContain(SERVER_CURSOR.job_id);
    expect(pageText).not.toContain("before_created_at");
    expect(pageText).not.toContain("next_cursor");
  });

  describe("jobKeys.list — cursor anahtarının ayrışması", () => {
    const cursorA: PlaybookJobCursor = { created_at: "2026-07-28T09:00:00Z", job_id: "aaa" };
    const cursorB: PlaybookJobCursor = { created_at: "2026-07-28T08:00:00Z", job_id: "bbb" };

    it("aynı filtrede farklı cursor'lar farklı anahtar üretir", () => {
      expect(jobKeys.list({ status: "failed" }, cursorA)).not.toEqual(
        jobKeys.list({ status: "failed" }, cursorB),
      );
      expect(jobKeys.list({ status: "failed" }, null)).not.toEqual(
        jobKeys.list({ status: "failed" }, cursorA),
      );
    });

    it("aynı cursor'da farklı filtreler farklı anahtar üretir", () => {
      expect(jobKeys.list({ status: "failed" }, cursorA)).not.toEqual(
        jobKeys.list({ status: "successful" }, cursorA),
      );
      expect(jobKeys.list({ mode: "check" }, cursorA)).not.toEqual(
        jobKeys.list({ mode: "normal" }, cursorA),
      );
    });

    it("ilk sayfa cursor'ı null olarak normalize edilir", () => {
      expect(jobKeys.list({})).toEqual(jobKeys.list({}, null));
    });
  });
});

/**
 * Ham Ansible display çıktısı (R1-V3J3B).
 *
 * Sözleşme dürüsttür: bu metin sanitize edilmiş sayılmaz, arayüz onu
 * dönüştürmez ve "güvenli/temizlendi" iddiası kurmaz. Ölçülen şeyler:
 *
 * - Bölüm varsayılan olarak **kapalı** bir native `<details>`tir.
 * - İçerideki HTML literal'leri element'e dönüşmez, düz metin kalır.
 * - v1 belgeleri (çıktı yok) hata gibi değil, nötr bir boş durumla sunulur.
 * - Kırpılma ve "hiç saklanamadı" hâlleri birbirinden ayrı cümlelerdir.
 *
 * Kapalı bir `<details>` erişim kontrolü değildir: içeriği DOM'da bulunur.
 * Bu yüzden "tıklamadan önce metin DOM'da yok" gibi bir iddia kurulmaz;
 * ölçülen tek şey `open` durumudur.
 */
describe("Ham Ansible çıktısı bölümü (R1-V3J3B)", () => {
  const SUMMARY_TEXT = "Ham Ansible çıktısı";
  const WARNING_PATTERN = /Bu çıktı hassas bilgiler, credential değerleri veya controller yolları içerebilir/;
  const TRUNCATED_PATTERN = /yalnız kaydedilen başlangıç bölümü/;
  const UNSTORED_TEXT = "Ansible çıktısı sonuç boyutu bütçesi nedeniyle saklanamadı.";
  const EMPTY_TEXT = "Bu sonuç için görüntülenecek Ansible çıktısı kaydedilmemiş.";

  /** Sonuç panelindeki `<details>` öğesini döndürür. */
  function rawOutputDetails(container: HTMLElement): HTMLDetailsElement {
    const details = container.querySelector<HTMLDetailsElement>("details.job-raw-output");
    expect(details).not.toBeNull();
    return details as HTMLDetailsElement;
  }

  it("v2 çıktısını varsayılan olarak kapalı bir details içinde sunar ve açılınca birebir gösterir", async () => {
    installJobDetailMock(successfulJob, displayOutputJobResult);

    const { container } = renderApp(`/jobs/${successfulJob.job_id}`);

    // Pozitif kanıt: yapılandırılmış görünüm birincil kalmaya devam ediyor.
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();

    const summary = screen.getByText(SUMMARY_TEXT);
    expect(summary.tagName).toBe("SUMMARY");

    const details = rawOutputDetails(container);
    // Varsayılan kapalı: `open` attribute'u hiç verilmez.
    expect(details.open).toBe(false);
    expect(details.hasAttribute("open")).toBe(false);

    // Uyarı, açılmadan önce de bölümün parçasıdır ve garanti dili taşımaz.
    expect(within(details).getByText(WARNING_PATTERN)).toBeInTheDocument();
    expect(details.textContent).not.toMatch(/secret-free|temizlendi|redakte/i);

    await userEvent.click(summary);
    expect(details.open).toBe(true);

    // Metin birebir: trim, split/join veya ANSI temizleme uygulanmaz.
    const code = details.querySelector("pre > code");
    expect(code).not.toBeNull();
    expect(code?.textContent).toBe(displayOutput);
  });

  it("çıktıdaki HTML/XSS literal'leri element'e dönüşmez, düz metin kalır", async () => {
    // Non-vacuity: sentinel'ler gerçekten API cevabının içindedir ve JSON
    // sınırından geçtikten sonra da aynen kalırlar. (Ham JSON metninde
    // aranmaz: `JSON.stringify` tırnakları escape'ler; ölçülmesi gereken şey
    // bileşenin gördüğü çözülmüş değerdir.)
    const decoded = JSON.parse(JSON.stringify(displayOutputJobResult)) as PlaybookJobResult;
    for (const [name, sentinel] of Object.entries(DISPLAY_OUTPUT_SENTINELS)) {
      expect(displayOutputJobResult.ansible_output, name).toContain(sentinel);
      expect(decoded.ansible_output, name).toContain(sentinel);
    }

    installJobDetailMock(successfulJob, displayOutputJobResult);

    const { container } = renderApp(`/jobs/${successfulJob.job_id}`);
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();

    const details = rawOutputDetails(container);
    const code = details.querySelector("pre > code");
    expect(code).not.toBeNull();

    // Literal'ler **metin** olarak görünür.
    for (const [name, sentinel] of Object.entries(DISPLAY_OUTPUT_SENTINELS)) {
      expect(code?.textContent, name).toContain(sentinel);
    }

    // `dangerouslySetInnerHTML` kullanılsaydı bu iddialar düşerdi: sentinel'den
    // gelen `script`/`img` element'e dönüşür ve `innerHTML` escape'siz olurdu.
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(details.querySelector("[onerror]")).toBeNull();
    expect(code?.innerHTML).toContain("&lt;script&gt;");
    expect(code?.innerHTML).not.toContain("<script>");
    expect(code?.childElementCount).toBe(0);
  });

  it("schema v1 sonucunda bölüm vardır ama nötr bir boş durum gösterir, pre render edilmez", async () => {
    // `jobResult` v1'dir: backend birleşik cevapta null/false döndürür.
    expect(jobResult.schema_version).toBe(1);
    expect(jobResult.ansible_output).toBeNull();
    expect(jobResult.ansible_output_truncated).toBe(false);

    installJobDetailMock(successfulJob, jobResult);

    const { container } = renderApp(`/jobs/${successfulJob.job_id}`);

    // Mevcut recap/event görünümü aynen çalışmaya devam eder.
    expect(await screen.findByText("Install nginx")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "web01" })).toBeInTheDocument();

    const details = rawOutputDetails(container);
    expect(within(details).getByText(SUMMARY_TEXT)).toBeInTheDocument();
    expect(within(details).getByText(EMPTY_TEXT)).toBeInTheDocument();
    // Eksik çıktı bir hata değildir: kırpılma/saklanamadı dili kurulmaz.
    expect(details.textContent).not.toMatch(TRUNCATED_PATTERN);
    expect(details.textContent).not.toContain(UNSTORED_TEXT);
    expect(details.querySelector("pre")).toBeNull();
  });

  it("kırpılmış çıktı hem gösterilir hem de açıkça kırpıldığı söylenir", async () => {
    expect(truncatedDisplayOutputJobResult.ansible_output).not.toBeNull();
    expect(truncatedDisplayOutputJobResult.ansible_output_truncated).toBe(true);

    installJobDetailMock(successfulJob, truncatedDisplayOutputJobResult);

    const { container } = renderApp(`/jobs/${successfulJob.job_id}`);
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();

    const details = rawOutputDetails(container);
    expect(within(details).getByText(TRUNCATED_PATTERN)).toBeInTheDocument();
    // Çıktı yine de tam olarak gösterilir; uyarı onun yerine geçmez.
    expect(details.querySelector("pre > code")?.textContent).toBe(displayOutput);
    // Ayrı sözleşme: sonuç/event kırpılma uyarıları bundan etkilenmez.
    expect(screen.queryByText("Sonuç belgesi kırpıldı")).not.toBeInTheDocument();
    expect(screen.queryByText("Event listesi kırpıldı")).not.toBeInTheDocument();
  });

  it("hiç saklanamamış çıktı için pre değil, saklanamadı mesajı gösterilir", async () => {
    expect(unstoredDisplayOutputJobResult.ansible_output).toBeNull();
    expect(unstoredDisplayOutputJobResult.ansible_output_truncated).toBe(true);

    installJobDetailMock(successfulJob, unstoredDisplayOutputJobResult);

    const { container } = renderApp(`/jobs/${successfulJob.job_id}`);
    expect(await screen.findByRole("rowheader", { name: "web01" })).toBeInTheDocument();

    const details = rawOutputDetails(container);
    expect(within(details).getByText(UNSTORED_TEXT)).toBeInTheDocument();
    expect(details.querySelector("pre")).toBeNull();
    // "Kaydedilmemiş" ile "saklanamadı" farklı gerçeklerdir, karıştırılmaz.
    expect(details.textContent).not.toContain(EMPTY_TEXT);
  });

  it("display yüzeyi Job listesine sızmaz ve yeni bir istek doğurmaz", async () => {
    const { requests } = installFetchMock((request) => {
      if (request.url.includes("/api/jobs?")) {
        return jsonResponse(jobList);
      }
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}`)) {
        return jsonResponse(successfulJob);
      }
      if (request.url.endsWith(`/api/jobs/${successfulJob.job_id}/result`)) {
        return jsonResponse(displayOutputJobResult);
      }
      return jsonResponse(activeProject);
    });

    renderApp("/jobs");

    // Liste yüzeyi: summary tipinde output alanı yoktur ve çıktı görünmez.
    expect(await screen.findByText(successfulJob.playbook_path)).toBeInTheDocument();
    expect(screen.queryByText(SUMMARY_TEXT)).not.toBeInTheDocument();
    for (const sentinel of Object.values(DISPLAY_OUTPUT_SENTINELS)) {
      expect(document.body.textContent).not.toContain(sentinel);
    }
    expect(Object.keys(successfulJob)).not.toContain("ansible_output");
    for (const item of jobList.items) {
      expect(Object.keys(item)).not.toContain("ansible_output");
      expect(Object.keys(item)).not.toContain("ansible_output_truncated");
    }

    const detailLink = screen
      .getAllByRole("link", { name: "Detay" })
      .find((link) => link.getAttribute("href")?.endsWith(successfulJob.job_id));
    expect(detailLink).toBeDefined();
    await userEvent.click(detailLink as HTMLElement);
    expect(await screen.findByText(SUMMARY_TEXT)).toBeInTheDocument();

    // Ham çıktı için ayrı bir endpoint, query parametresi veya istek yoktur:
    // veri zaten mevcut result cevabından gelir.
    const resultRequests = requests.filter((request) => request.url.includes("/result"));
    expect(resultRequests).toHaveLength(1);
    expect(resultRequests[0]?.method).toBe("GET");
    expect(resultRequests[0]?.url).toMatch(
      new RegExp(`/api/jobs/${successfulJob.job_id}/result$`),
    );
    expect(requests.every((request) => request.method === "GET")).toBe(true);
  });
});
