/**
 * Job okuma API sözleşmesinin TypeScript karşılığı (R1-V3D2A1/R1-V3D2B, R1-V3H2B).
 *
 * Alan adları backend şemasıyla birebir aynıdır (`app/schemas/job.py`). Aktör,
 * plan/workspace kimliği, manifest digest'i, artifact yolu, absolute path,
 * raw stdout/stderr ve argv backend tarafından bilinçli olarak **verilmez**;
 * bu yüzden burada da karşılıkları yoktur.
 */

import type { ExecutionMode } from "../../lib/executionMode";

export type { ExecutionMode };

export type JobStatus = "pending" | "running" | "successful" | "failed" | "canceled";

/**
 * `PlaybookJobSummaryResponse.error_code` alanının bütün değerleri.
 *
 * `playbook_failed` ile `runner_failed` ayrımı bağlayıcıdır:
 *
 * - `playbook_failed`: güvenilir bir Ansible terminal sonucu üretildi ve o sonuç
 *   başarısızlık raporluyor.
 * - `runner_failed`: genel/legacy toplayıcı kod. R1-V3G1 öncesinde üretilmiş
 *   kayıtlar bu kodu taşır ve **dolu, güvenilir bir recap içerebilir**; kod
 *   yalnızca sonucun kesin sınıflandırılamadığını söyler, sonucun ya da
 *   task/host verisinin yokluğunu değil. Geriye dönük uyumluluk için korunur.
 *
 * Hiçbiri bir kök neden sınıflandırması değildir.
 */
export type PublicErrorCode =
  | "runner_start_failed"
  | "runner_timeout"
  | "playbook_failed"
  | "runner_failed"
  | "runner_output_invalid"
  | "runner_no_hosts"
  | "workspace_unavailable"
  | "workspace_integrity_failed"
  | "result_limit_exceeded"
  | "execution_binding_invalid"
  | "interrupted_by_restart"
  | "unknown_failure";

/** `GET /api/jobs` ve `GET /api/jobs/{jobId}` cevabındaki Job özeti. */
export interface PlaybookJobSummary {
  job_id: string;
  job_type: "playbook";
  status: JobStatus;
  mode: ExecutionMode;
  project_id: number;
  /** Project'in kayıtlı adı; backend'de join ile okunur (R1-V3J0B2). */
  project_name: string;
  inventory_id: number;
  /** Inventory'nin kayıtlı adı; backend'de join ile okunur (R1-V3J0B2). */
  inventory_name: string;
  playbook_path: string;
  return_code: number | null;
  error_code: PublicErrorCode | null;
  result_truncated: boolean;
  /**
   * Kayıt bu Job'a ait bir sonuç dosyası gösteriyor mu; dosyanın gerçekten
   * mevcut veya okunabilir olduğunu iddia etmez.
   */
  has_recorded_result: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** Sonraki sayfanın başlangıç noktası; yalnız `has_more` doğruyken doludur. */
export interface PlaybookJobCursor {
  created_at: string;
  job_id: string;
}

export interface PlaybookJobList {
  items: PlaybookJobSummary[];
  has_more: boolean;
  next_cursor: PlaybookJobCursor | null;
}

/**
 * `PlaybookJobResultResponse.error_code` alanının bütün değerleri.
 *
 * Her değeri aynı zamanda bir `PublicErrorCode`'dur; kullanıcı metni bu yüzden
 * tek bir sözlükten (`JOB_ERROR_MESSAGES`) okunabilir.
 */
export type ResultErrorCode =
  | "playbook_failed"
  | "runner_failed"
  | "runner_timeout"
  | "runner_output_invalid"
  | "result_limit_exceeded"
  | "runner_no_hosts";

/** Sonuca girebilen tek event türleri. */
export type ResultEventType =
  | "playbook_on_task_start"
  | "runner_on_ok"
  | "runner_on_failed"
  | "runner_on_skipped"
  | "runner_on_unreachable";

/**
 * Sonuçtaki tek bir event'in tam alan kümesi.
 *
 * `event_data`, `res`, `stdout`, `stderr`, `task_args`, `command` ve `argv`
 * bilinçli olarak burada **yoktur**.
 */
export interface PlaybookResultEvent {
  event: ResultEventType;
  host: string | null;
  task: string | null;
  changed: boolean;
  failed: boolean;
}

/** Tek bir host için yalnız sayısal özet. */
export interface PlaybookHostRecap {
  ok: number;
  changed: number;
  failures: number;
  unreachable: number;
  skipped: number;
  rescued: number;
  ignored: number;
}

/**
 * `GET /api/jobs/{jobId}/result` cevabı.
 *
 * `artifact_path`, `workspace_id`, `manifest_digest` ve `requested_by` burada
 * **yoktur**. Yapılandırılmış yüzey (recap/events) sanitize edilmiş kalır;
 * bunun tek istisnası aşağıdaki `ansible_output` display yüzeyidir ve o alan
 * bilinçli olarak ham verilir (R1-V3J3A/R1-V3J3B).
 */
export interface PlaybookJobResult {
  /**
   * Okunan artifact'ın gerçek şema sürümü; backend bunu 2'ye normalize etmez.
   *
   * `1`: R1-V3J3A öncesi yazılmış belge; display çıktısı hiç saklanmamıştır.
   * `2`: display çıktısı sözleşmesini taşıyan belge.
   *
   * Cevabın şekli iki sürümde de aynıdır: v1 belgeleri `ansible_output=null`
   * ve `ansible_output_truncated=false` olarak yüzeye çıkar, bu yüzden
   * frontend tek bir tip ile çalışır.
   */
  schema_version: 1 | 2;
  job_id: string;
  return_code: number;
  outcome: "successful" | "failed";
  error_code: ResultErrorCode | null;
  recap: Record<string, PlaybookHostRecap>;
  events: PlaybookResultEvent[];
  events_truncated: boolean;
  result_truncated: boolean;
  /**
   * Ansible'ın insanın terminalde gördüğü display metni.
   *
   * Backend bunu **yalnız** runner event object'lerinin üst düzey `stdout`
   * alanlarından, event sırasıyla birleştirerek üretir. Bu alan nested
   * `event_data.res.stdout`, `res.stderr`, `msg`, task argümanları, process
   * stderr'i veya JSON event belgesinin kendisi **değildir**.
   *
   * Bu metin secret-free **değildir**: credential değerleri, playbook kaynak
   * satırları, controller üzerindeki absolute path'ler veya başka hassas
   * bilgiler içerebilir. Ansible'ın `no_log` davranışı korunur ama platform
   * bunun eksiksiz bir gizlilik garantisi olduğunu iddia etmez.
   *
   * Frontend metni dönüştürmez: trim, split/join, ANSI temizleme, HTML decode
   * veya redaction uygulanmaz ve "sanitize edildi" iddiası kurulmaz. Değer
   * düz metin olarak render edilir.
   *
   * Bu alan yalnız bu cevapta bulunur; `PlaybookJobSummary`,
   * `PlaybookJobList` ve `PlaybookResultEvent` tiplerinde karşılığı yoktur.
   */
  ansible_output: string | null;
  /**
   * Display çıktısı byte sınırı veya sonuç bütçesi nedeniyle kırpıldı mı.
   *
   * `result_truncated` ve `events_truncated` alanlarından **ayrı** bir
   * sözleşmedir; onların anlamını değiştirmez. `ansible_output` null iken de
   * `true` olabilir: bu, çıktının hiç saklanamadığı anlamına gelir.
   */
  ansible_output_truncated: boolean;
}
