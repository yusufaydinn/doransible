/**
 * Job okuma endpoint'lerinin tek erişim noktası.
 *
 * Sayfa bileşenleri doğrudan `fetch` çağırmaz; URL kurgusu yalnızca burada
 * bulunur (route/service katman ayrımı sözleşmesi).
 */

import { apiFetch } from "../../lib/apiClient";

import type {
  ExecutionMode,
  JobStatus,
  PlaybookJobCursor,
  PlaybookJobList,
  PlaybookJobResult,
  PlaybookJobSummary,
} from "./types";

/** Her sayfada en fazla bu kadar Job gösterilir; sınır sunucuda uygulanır. */
const DEFAULT_LIST_LIMIT = 25;

/**
 * Liste isteğine uygulanabilecek filtreler (R1-V3J0B2).
 *
 * İkisi de opsiyoneldir ve **sunucu tarafında** uygulanır: filtreleme
 * client-side yapılmaz, seçim doğrudan `GET /api/jobs` query parametresine
 * dönüşür.
 *
 * Cursor bilinçli olarak bu tipin **dışındadır** (R1-V3J2A): filtre "hangi
 * kayıtlar" sorusunu, cursor ise "hangi sayfa" sorusunu yanıtlar. İkisi tek
 * nesnede toplansaydı bir filtre değişimi eski cursor'ı kazara taşıyabilirdi.
 */
export interface JobListFilters {
  status?: JobStatus;
  mode?: ExecutionMode;
}

/**
 * Aktörün Job'larını en yeni önce döner.
 *
 * Filtre ve cursor verilmediğinde istek **tam olarak** `/api/jobs?limit=25`'tir;
 * bu sözleşme mevcut testlerle sabitlenir ve değişmez. `status`/`mode` yalnız
 * gerçekten seçiliyken query'ye eklenir.
 *
 * `cursor` tek bir nesne olarak alınır — iki ayrı opsiyonel string yerine —
 * çünkü backend `before_created_at` ve `before_job_id` alanlarını yalnız
 * **birlikte** kabul eder. Tek nesne, tip düzeyinde yarım cursor kurulmasını
 * imkânsız kılar. Değer her zaman sunucunun `next_cursor` cevabından aynen
 * alınmalıdır; son satırın alanlarından yeniden türetilmez.
 */
export function fetchJobs(
  filters: JobListFilters = {},
  cursor: PlaybookJobCursor | null = null,
): Promise<PlaybookJobList> {
  const params = new URLSearchParams({ limit: String(DEFAULT_LIST_LIMIT) });
  if (filters.status !== undefined) {
    params.set("status", filters.status);
  }
  if (filters.mode !== undefined) {
    params.set("mode", filters.mode);
  }
  if (cursor !== null) {
    params.set("before_created_at", cursor.created_at);
    params.set("before_job_id", cursor.job_id);
  }
  return apiFetch<PlaybookJobList>(`/api/jobs?${params.toString()}`);
}

/** Tek bir Job'ın özetini okur. */
export function fetchJob(jobId: string): Promise<PlaybookJobSummary> {
  return apiFetch<PlaybookJobSummary>(`/api/jobs/${jobId}`);
}

/**
 * Bir Job'ın doğrulanmış çalıştırma sonucunu okur.
 *
 * Yalnızca terminal ve kayıtlı bir sonuç için anlamlıdır; çağıran bunu
 * `status` ve `has_recorded_result` alanlarına bakarak kendisi denetler.
 */
export function fetchJobResult(jobId: string): Promise<PlaybookJobResult> {
  return apiFetch<PlaybookJobResult>(`/api/jobs/${jobId}/result`);
}
