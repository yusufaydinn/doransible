import type { JobListFilters } from "./api";
import type { PlaybookJobCursor } from "./types";

/**
 * Job query key hiyerarşisi.
 *
 * `list` filtreleri anahtara **dahil eder** (R1-V3J0B2): `status`/`mode`
 * verilmeyen bir çağrı `null` olarak normalize edilir, böylece "filtre hiç
 * verilmedi" ile "filtre `undefined` olarak verildi" aynı cache girdisine
 * düşer ve farklı filtre sonuçları asla karışmaz.
 *
 * Cursor da aynı nedenle anahtarın **ayrı** bir parçasıdır (R1-V3J2A): ilk
 * sayfa `null` olarak normalize edilir; aynı filtrenin farklı sayfaları ve
 * aynı sayfanın farklı filtreleri birbirinden ayrı cache girdileri üretir.
 * Token, artifact yolu veya Job sonucu gibi hiçbir hassas değer anahtara
 * girmez.
 */
export const jobKeys = {
  all: ["jobs"] as const,
  list: (filters: JobListFilters = {}, cursor: PlaybookJobCursor | null = null) =>
    [
      ...jobKeys.all,
      "list",
      { status: filters.status ?? null, mode: filters.mode ?? null },
      cursor === null ? null : { created_at: cursor.created_at, job_id: cursor.job_id },
    ] as const,
  detail: (jobId: string) => [...jobKeys.all, "detail", jobId] as const,
  result: (jobId: string) => [...jobKeys.all, "result", jobId] as const,
};
