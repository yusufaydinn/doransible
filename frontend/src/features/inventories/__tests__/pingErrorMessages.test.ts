/**
 * `describePingError` saf eşleme matrisi (Aşama 2B).
 *
 * Bu dosya React kullanmaz: eşleyici saf bir fonksiyondur ve tek başına
 * ölçülebilir. Testler metnin tamamına kilitlenmez; güvenlik açısından anlamlı
 * **olumlu** ifadeleri ("başlamış olabilir") ve **yasak** ifadeleri
 * ("ping gönderilmedi", ham details) ölçer.
 *
 * Kanarya değerleri hiçbir assertion çıktısına yazdırılmaz: karşılaştırmalar
 * hesaplanmış boolean'lar üzerinden yapılır.
 */

import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/apiClient";
import {
  describePingError,
  type PingErrorNotice,
  type PingStage,
} from "../errorMessages";

/** Hiçbir çıktıda görünmemesi gereken kanarya. */
const CANARY = "CANARY-SECRET-b7e41d9028fa3c65";

function apiError(
  code: string,
  options: { status?: number; details?: unknown; message?: string } = {},
): ApiError {
  return new ApiError(
    options.message ?? "Sunucudan gelen mesaj.",
    options.status ?? 500,
    code,
    options.details ?? null,
  );
}

/** Bildirimin tamamını, değeri basmadan arar. */
function noticeIncludes(notice: PingErrorNotice, needle: string): boolean {
  return JSON.stringify(notice).includes(needle);
}

/* --- 1. Kod matrisi --------------------------------------------------------- */

const CODE_MATRIX: ReadonlyArray<[code: string, stage: PingStage, title: string]> = [
  ["ping_invalid_limit", "preview", "Limit kabul edilmedi"],
  ["ping_no_hosts_matched", "preview", "Limit hiçbir host ile eşleşmedi"],
  ["ping_inventory_unsafe", "preview", "Inventory güvenli biçimde ping'lenemiyor"],
  ["ping_preview_unavailable", "preview", "Ping önizlemesi hazırlanamadı"],
  ["ping_preview_invalid", "confirm", "Onay geçerli değil"],
  ["job_already_running", "confirm", "Bu inventory için bir ping işi zaten çalışıyor"],
  ["ping_artifact_unavailable", "confirm", "Ping işi başlatılamadı"],
  ["ping_artifact_write_failed", "confirm", "Ping sonucu kaydedilemedi"],
  ["ping_snapshot_unavailable", "confirm", "Ping çalışma alanı arızası"],
  ["ping_known_hosts_unavailable", "confirm", "Host anahtarı dosyası hazırlanamadı"],
  ["ansible_unavailable", "confirm", "Ansible çalıştırılamıyor"],
  ["ping_timeout", "confirm", "Ping zaman aşımına uğradı"],
  ["ping_output_too_large", "confirm", "Ping çıktısı boyut sınırını aştı"],
  ["ping_invalid_output", "confirm", "Ping çıktısı doğrulanamadı"],
  ["request_validation_error", "preview", "İstek sunucu tarafından kabul edilmedi"],
  ["network_error", "confirm", "Sunucudan cevap alınamadı; ping başlamış olabilir"],
];

describe("describePingError — kod matrisi", () => {
  CODE_MATRIX.forEach(([code, stage, title]) => {
    it(`${code} kodunu uygulanabilir bir bildirime çevirir`, () => {
      const notice = describePingError(apiError(code), stage);

      expect(notice.title).toBe(title);
      // Mesaj boş bırakılmaz; kullanıcı ne yapacağını okuyabilmelidir.
      expect(notice.message.length).toBeGreaterThan(40);
    });
  });

  it("her kod için ayırt edici bir başlık üretir", () => {
    const titles = CODE_MATRIX.map(([code, stage]) =>
      describePingError(apiError(code), stage).title,
    );

    expect(new Set(titles).size).toBe(CODE_MATRIX.length);
  });

  it("hiçbir kod ve aşamada kullanıcı metni markdown işareti taşımaz", () => {
    // Panel `message` alanını düz metin olarak basar; kaçmış bir `**vurgu**`
    // kullanıcıya yıldızlarıyla görünürdü. Aşama duyarlı dalların hepsi
    // gezilir, çünkü metin adıma göre değişir.
    const stages: PingStage[] = ["preview", "confirm", "cancel"];
    const offenders: string[] = [];

    for (const [code] of CODE_MATRIX) {
      for (const stage of stages) {
        const notice = describePingError(apiError(code), stage);
        if (/\*\*|__/.test(`${notice.title} ${notice.message}`)) {
          offenders.push(`${code}/${stage}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });
});

/* --- Devretme ve fallback --------------------------------------------------- */

describe("describePingError — ping dışı kodlar", () => {
  it("parser hatasını inventory eşleyicisine devreder", () => {
    const notice = describePingError(
      apiError("inventory_parse_failed", {
        status: 422,
        details: { parser_message: "satır 3: beklenmeyen girinti", inventory_id: 3 },
      }),
      "preview",
    );

    expect(notice.title).toBe("Inventory dosyası ayrıştırılamadı");
    expect(notice.parserMessage).toBe("satır 3: beklenmeyen girinti");
    expect(notice.relatedInventoryId).toBe(3);
  });

  it("path hatasını inventory eşleyicisine devreder", () => {
    const notice = describePingError(
      apiError("inventory_path_unavailable", { status: 409, details: { reason: "missing" } }),
      "preview",
    );

    expect(notice.title).toBe("Inventory dosyası controller'da bulunamadı");
  });

  it("not_found kodunu inventory eşleyicisine devreder", () => {
    const notice = describePingError(apiError("not_found", { status: 404 }), "confirm");

    expect(notice.title).toBe("Inventory bulunamadı");
  });

  it("bilinmeyen kodu kontrollü fallback'e düşürür", () => {
    const notice = describePingError(
      apiError("beklenmeyen_yeni_kod", { status: 500, message: "Sunucu mesajı." }),
      "preview",
    );

    expect(notice.title).toBe("İşlem tamamlanamadı");
    expect(notice.message).toBe("Sunucu mesajı.");
  });

  it("ApiError olmayan confirm arızasında ham exception metnini göstermez", () => {
    const raw = new SyntaxError(`Unexpected token in ${CANARY}`);

    const notice = describePingError(raw, "confirm");

    expect(notice.title).toBe("Ping isteği beklenmedik biçimde sonuçlandı");
    expect(noticeIncludes(notice, CANARY)).toBe(false);
    expect(noticeIncludes(notice, "SyntaxError")).toBe(false);
    expect(notice.retryable).toBe(false);
    expect(notice.requiresNewPreview).toBe(true);
  });

  it("ApiError olmayan preview/cancel arızasında da ham metin göstermez", () => {
    for (const stage of ["preview", "cancel"] as const) {
      const notice = describePingError(new Error(CANARY), stage);

      expect(noticeIncludes(notice, CANARY)).toBe(false);
      expect(notice.title).toBe("Beklenmeyen bir hata oluştu");
    }
  });
});

/* --- 2. Aşama duyarlı belirsizlikler ---------------------------------------- */

describe("describePingError — aşama duyarlı belirsizlik", () => {
  it("preview taşıma hatasında planın oluştuğunu ya da oluşmadığını iddia etmez", () => {
    const notice = describePingError(apiError("network_error", { status: 0 }), "preview");

    // Yapısal garanti verilebilir: bu adım ping çalıştırmaz.
    expect(notice.message).toContain("ping göndermez");
    // Ama sunucu tarafındaki sonuç hakkında kesinlik yoktur.
    expect(notice.message).toContain("bilinemez");
    expect(notice.message).not.toContain("plan oluşturulmadı");
    expect(notice.retryable).toBe(true);
    expect(notice.requiresNewPreview).toBeUndefined();
  });

  it("preview store arızasında da ping çalışmadığı kesin, plan durumu belirsizdir", () => {
    const notice = describePingError(apiError("ping_preview_unavailable"), "preview");

    expect(notice.message).toContain("ping çalıştırmaz");
    expect(notice.message).toContain("bilinemez");
    expect(notice.requiresNewPreview).toBeUndefined();
  });

  it("cancel arızasında temizliğin tamamlandığını iddia etmez ve token'ı yeniden kullandırmaz", () => {
    for (const code of ["network_error", "ping_preview_unavailable"]) {
      const notice = describePingError(apiError(code), "cancel");

      expect(notice.message).toContain("doğrulan");
      expect(notice.message).toContain("yeniden kullanmayın");
      expect(notice.message).not.toContain("iptal edildi");
    }
  });

  it("confirm taşıma/store/snapshot arızasında ping'in çalışmış olabileceğini söyler", () => {
    const codes = ["network_error", "ping_preview_unavailable", "ping_snapshot_unavailable"];

    for (const code of codes) {
      const notice = describePingError(apiError(code), "confirm");

      expect(notice.message).toContain("olabilir");
      // Otomatik tekrar önerilmez ve yeni onay gerekir.
      expect(notice.retryable).toBe(false);
      expect(notice.requiresNewPreview).toBe(true);
      // "Hiç çalışmadı" güvencesi verilmez.
      expect(notice.message).not.toContain("ping gönderilmedi");
      expect(notice.message).not.toContain("ping başlatılmadı");
    }
  });

  it("ping_invalid_output çıktının doğrulanamadığını söyler, ansible kurulumu için kesinlik iddia etmez", () => {
    const notice = describePingError(apiError("ping_invalid_output"), "confirm");

    expect(notice.title).toBe("Ping çıktısı doğrulanamadı");
    expect(notice.message).toContain("tek başına belirlenemez");
    expect(notice.message).not.toContain("sürümü desteklenmiyor veya kurulum bozuk");
  });

  it("ping_invalid_output Ansible sürecini controller'a bağlar, hedef host'la karıştırmaz (AUDIT-FIX1)", () => {
    const notice = describePingError(apiError("ping_invalid_output"), "confirm");

    expect(notice.message).toContain("controller");
    // "Sunucudaki Ansible sürümü" ifadesi hedef host'un sunucusuyla karışırdı;
    // Ansible süreci controller üzerinde çalışır, hedef host üzerinde değil.
    expect(notice.message).not.toContain("sunucudaki Ansible sürümü");
  });

  it("confirm request_validation_error'da token tüketildi demez", () => {
    const notice = describePingError(
      apiError("request_validation_error", { status: 422 }),
      "confirm",
    );

    // Gövde şema doğrulamasında elendi; claim hiç yapılmadı.
    expect(notice.requiresNewPreview).toBeUndefined();
  });

  it("diğer bütün confirm hataları onayı tükenmiş sayar", () => {
    const codes = CODE_MATRIX.map(([code]) => code).filter(
      (code) => code !== "request_validation_error",
    );

    for (const code of codes) {
      expect(describePingError(apiError(code), "confirm").requiresNewPreview).toBe(true);
    }
  });

  it("preview ve cancel adımlarında onay tükenmiş sayılmaz", () => {
    for (const stage of ["preview", "cancel"] as const) {
      for (const [code] of CODE_MATRIX) {
        expect(describePingError(apiError(code), stage).requiresNewPreview).toBeUndefined();
      }
    }
  });
});

/* --- 3. Details type guard'ları --------------------------------------------- */

/** Guard'ların reddetmesi gereken değerler. */
const BAD_VALUES: ReadonlyArray<[label: string, value: unknown]> = [
  ["null", null],
  ["sayı", 42],
  ["boolean", true],
  ["dizi", ["expired"]],
  ["nesne", { value: "expired" }],
  ["iç içe nesne", { nested: { reason: "expired" } }],
  ["aşırı uzun metin", "x".repeat(5000)],
  ["kanarya taşıyan metin", CANARY],
];

describe("describePingError — details type guard'ları", () => {
  it("reason yalnızca üç bilinen değeri kabul eder", () => {
    const titles = {
      expired: "Onay süresi doldu",
      mismatch: "Plan artık geçerli değil",
      invalid: "Onay geçerli değil",
    } as const;

    for (const [reason, title] of Object.entries(titles)) {
      const notice = describePingError(
        apiError("ping_preview_invalid", { status: 409, details: { reason } }),
        "confirm",
      );
      expect(notice.title).toBe(title);
    }
  });

  BAD_VALUES.forEach(([label, value]) => {
    it(`reason alanındaki ${label} değeri genel mesaja düşer`, () => {
      const notice = describePingError(
        apiError("ping_preview_invalid", { status: 409, details: { reason: value } }),
        "confirm",
      );

      expect(notice.title).toBe("Onay geçerli değil");
      expect(noticeIncludes(notice, CANARY)).toBe(false);
    });
  });

  it("bilinmeyen reason değeri ('already_used') genel mesaja düşer", () => {
    // Backend böyle bir garanti vermez (ADR-018 Karar 10); istemci de uydurmaz.
    const notice = describePingError(
      apiError("ping_preview_invalid", { status: 409, details: { reason: "already_used" } }),
      "confirm",
    );

    expect(notice.title).toBe("Onay geçerli değil");
    expect(notice.message).not.toContain("already_used");
  });

  it("stream yalnızca stdout ve stderr değerlerini ayırır", () => {
    const stdout = describePingError(
      apiError("ping_output_too_large", { status: 502, details: { stream: "stdout" } }),
      "confirm",
    );
    const stderr = describePingError(
      apiError("ping_output_too_large", { status: 502, details: { stream: "stderr" } }),
      "confirm",
    );

    expect(stdout.message).toContain("sonuç");
    expect(stderr.message).toContain("hata metni");
    expect(stdout.message).not.toBe(stderr.message);
  });

  BAD_VALUES.forEach(([label, value]) => {
    it(`stream alanındaki ${label} değeri genel mesaja düşer`, () => {
      const notice = describePingError(
        apiError("ping_output_too_large", { status: 502, details: { stream: value } }),
        "confirm",
      );

      expect(notice.message).toContain("Daha dar bir limit");
      expect(noticeIncludes(notice, CANARY)).toBe(false);
    });
  });

  it("job_id yalnızca canonical küçük harfli UUID ise okunur", () => {
    const jobId = "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42";

    const notice = describePingError(
      apiError("job_already_running", { status: 409, details: { job_id: jobId } }),
      "confirm",
    );

    expect(notice.jobId).toBe(jobId);
  });

  const BAD_JOB_IDS: ReadonlyArray<[label: string, value: unknown]> = [
    ["büyük harfli UUID", "6B1F0C74-8A2E-4D35-9C11-5F7AB0E39D42"],
    ["eksik bölüm", "6b1f0c74-8a2e-4d35-9c11"],
    ["fazladan ek", "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42-extra"],
    ["tire içermeyen", "6b1f0c748a2e4d359c115f7ab0e39d42"],
    ["hex olmayan", "zzzzzzzz-8a2e-4d35-9c11-5f7ab0e39d42"],
    ...BAD_VALUES,
  ];

  BAD_JOB_IDS.forEach(([label, value]) => {
    it(`job_id alanındaki ${label} değeri gösterilmez`, () => {
      const notice = describePingError(
        apiError("job_already_running", { status: 409, details: { job_id: value } }),
        "confirm",
      );

      expect(notice.jobId).toBeUndefined();
      expect(noticeIncludes(notice, CANARY)).toBe(false);
    });
  });

  it("details'in kendisi dizi veya ilkel olduğunda çökmeden genel mesaja düşer", () => {
    const shapes: unknown[] = [["reason", "expired"], "expired", 7, true, null, undefined];

    for (const details of shapes) {
      const notice = describePingError(
        apiError("ping_preview_invalid", { status: 409, details }),
        "confirm",
      );
      expect(notice.title).toBe("Onay geçerli değil");
    }
  });

  it("bilinmeyen details alanları çıktıya sızmaz", () => {
    const notice = describePingError(
      apiError("job_already_running", {
        status: 409,
        details: {
          job_id: "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42",
          preview_token: CANARY,
          argv: ["ansible", "all", "-m", "ping"],
          traceback: `Traceback ... ${CANARY}`,
          snapshot_path: "/srv/app-data/ping-previews/abc/inventory-targets.yml",
        },
      }),
      "confirm",
    );

    expect(noticeIncludes(notice, CANARY)).toBe(false);
    expect(noticeIncludes(notice, "Traceback")).toBe(false);
    expect(noticeIncludes(notice, "app-data")).toBe(false);
    expect(noticeIncludes(notice, "ansible")).toBe(false);
    // Yalnızca doğrulanmış alan geçer.
    expect(notice.jobId).toBe("6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42");
  });
});
