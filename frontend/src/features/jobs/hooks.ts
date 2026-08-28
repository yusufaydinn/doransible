/**
 * Job ekranlarının kullandığı veri erişimi.
 *
 * `useJob`, Job terminal duruma ulaşana kadar bounded HTTP polling yapar;
 * SSE bu dilimde yoktur. Polling durması `status`'a bakılarak belirlenir,
 * ayrı bir zamanlayıcı elle kurulmaz — component unmount olduğunda TanStack
 * Query interval'i de kendiliğinden durdurur.
 */

import { useQuery } from "@tanstack/react-query";

import { fetchJob, fetchJobResult, fetchJobs, type JobListFilters } from "./api";
import { jobKeys } from "./queryKeys";
import type { PlaybookJobCursor, PlaybookJobSummary } from "./types";

const POLL_INTERVAL_MS = 2000;

/**
 * Job listesinin tek bir sayfası; sayfa başına en fazla 25 kayıt.
 *
 * Filtreleme sunucu tarafındadır: `filters` doğrudan `GET /api/jobs` query
 * parametrelerine dönüşür, istemci tarafında ek bir eleme yapılmaz. Filtre ve
 * cursor verilmediğinde istek mevcut `/api/jobs?limit=25` sözleşmesini korur.
 *
 * `cursor`, backend'in bir önceki cevabındaki opaque `next_cursor` değeridir
 * (R1-V3J2A). Query key ile query function **aynı** filtre/cursor çiftini
 * kullanır; aksi hâlde bir sayfanın verisi başka bir sayfanın anahtarına
 * yazılabilirdi.
 *
 * `placeholderData` bilinçli olarak yoktur: yeni sayfa yüklenirken eski
 * sayfanın satırlarını göstermek, kullanıcıya "Sayfa 2" yazarken sayfa 1'in
 * kayıtlarını göstermek anlamına gelirdi. Polling de yoktur — liste
 * kendiliğinden tazelenmez.
 */
export function useJobs(filters: JobListFilters = {}, cursor: PlaybookJobCursor | null = null) {
  return useQuery({
    queryKey: jobKeys.list(filters, cursor),
    queryFn: () => fetchJobs(filters, cursor),
    retry: false,
  });
}

/**
 * Tek bir Job'ın özeti; yalnız `pending`/`running` iken 2 saniyede bir
 * yenilenir.
 *
 * Allow-list bilinçlidir: `status`'un henüz hiç gelmediği (`undefined` —
 * ilk render veya query hatası) durum da, terminal durumlar kadar polling'i
 * durdurmalıdır. Bir deny-list (`TERMINAL_STATUSES`'a bakıp değilse devam et)
 * bu ayrımı kaçırırdı — 404/network hatası sonrasında `retry: false` sorguyu
 * "başarısız" sayar ama `data` hâlâ `undefined`'dır ve deny-list bunu
 * "durum bilinmiyor, o zaman devam et" diye yanlış yorumlardı; kullanıcı
 * ekranda hata mesajını görürken arka planda sessizce 2 saniyede bir yeniden
 * istek atılırdı.
 */
export function useJob(jobId: string) {
  return useQuery({
    queryKey: jobKeys.detail(jobId),
    queryFn: () => fetchJob(jobId),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? POLL_INTERVAL_MS : false;
    },
  });
}

/**
 * Bir Job'ın sonucu.
 *
 * Yalnızca terminal (`successful`/`failed`) ve `has_recorded_result === true`
 * bir Job için istek gönderir; pending/running/canceled veya kayıtsız sonuçta
 * result endpoint'i hiç çağrılmaz.
 */
export function useJobResult(job: PlaybookJobSummary | undefined) {
  const jobId = job?.job_id;
  const enabled =
    job !== undefined &&
    (job.status === "successful" || job.status === "failed") &&
    job.has_recorded_result;

  return useQuery({
    queryKey: jobKeys.result(jobId ?? "unknown"),
    queryFn: () => fetchJobResult(jobId as string),
    enabled,
    retry: false,
  });
}
