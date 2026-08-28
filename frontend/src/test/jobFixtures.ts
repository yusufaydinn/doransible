/** Job testlerinde kullanılan örnek API cevapları (R1-V3D2B, R1-V3D3). */

import type {
  PlaybookJobList,
  PlaybookJobResult,
  PlaybookJobSummary,
} from "../features/jobs/types";

import { activeProject, linkedInventory } from "./fixtures";

export const pendingJob: PlaybookJobSummary = {
  job_id: "018f1e0a-9b1a-7c3a-9e2a-1a2b3c4d5e6f",
  job_type: "playbook",
  status: "pending",
  mode: "check",
  project_id: activeProject.id,
  project_name: activeProject.name,
  inventory_id: linkedInventory.id,
  inventory_name: linkedInventory.name,
  playbook_path: "site.yml",
  return_code: null,
  error_code: null,
  result_truncated: false,
  has_recorded_result: false,
  created_at: "2026-07-28T10:07:00Z",
  started_at: null,
  finished_at: null,
};

export const runningJob: PlaybookJobSummary = {
  ...pendingJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-2b3c4d5e6f70",
  playbook_path: "playbooks/health-check.yml",
  status: "running",
  started_at: "2026-07-28T10:07:05Z",
};

export const successfulJob: PlaybookJobSummary = {
  ...pendingJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-3c4d5e6f7081",
  playbook_path: "playbooks/tasks/deploy.yml",
  status: "successful",
  return_code: 0,
  has_recorded_result: true,
  started_at: "2026-07-28T10:07:05Z",
  finished_at: "2026-07-28T10:08:00Z",
};

/**
 * Legacy `runner_failed` kaydı.
 *
 * R1-V3G1'den önce üretilmiş belgeler bu kodu taşır ve geriye dönük uyumluluk
 * için bilerek korunur: frontend bunları recap'e bakıp `playbook_failed` gibi
 * yeniden sınıflandırmamalıdır.
 */
export const failedJob: PlaybookJobSummary = {
  ...pendingJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-4d5e6f708192",
  playbook_path: "playbooks/backup.yml",
  status: "failed",
  return_code: 2,
  error_code: "runner_failed",
  has_recorded_result: true,
  started_at: "2026-07-28T10:09:00Z",
  finished_at: "2026-07-28T10:09:30Z",
};

/** Güvenilir terminal sonucu başarısızlık raporlayan Job (R1-V3G1). */
export const playbookFailedJob: PlaybookJobSummary = {
  ...pendingJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-5e6f70819203",
  playbook_path: "playbooks/compliance/ssh.yml",
  status: "failed",
  return_code: 2,
  error_code: "playbook_failed",
  has_recorded_result: true,
  started_at: "2026-07-28T10:11:00Z",
  finished_at: "2026-07-28T10:11:40Z",
};

/** Aynı sınıfın erişilemeyen host içeren varyantı (R1-V3G1). */
export const playbookUnreachableJob: PlaybookJobSummary = {
  ...playbookFailedJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-6f7081920314",
  playbook_path: "playbooks/compliance/audit.yml",
};

/** Normal mode'da çalıştırılmış Job (R1-V3H2B). */
export const normalModeJob: PlaybookJobSummary = {
  ...successfulJob,
  job_id: "018f1e0a-9b1a-7c3a-9e2a-7081920314a5",
  playbook_path: "playbooks/apply/site.yml",
  mode: "normal",
};

export const jobList: PlaybookJobList = {
  items: [failedJob, successfulJob, runningJob, pendingJob],
  has_more: false,
  next_cursor: null,
};

export const jobResult: PlaybookJobResult = {
  schema_version: 1,
  job_id: successfulJob.job_id,
  return_code: 0,
  outcome: "successful",
  error_code: null,
  recap: {
    web01: {
      ok: 3,
      changed: 1,
      failures: 0,
      unreachable: 0,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
    db01: {
      ok: 2,
      changed: 0,
      failures: 0,
      unreachable: 0,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
  },
  events: [
    {
      event: "playbook_on_task_start",
      host: null,
      task: "Gather facts",
      changed: false,
      failed: false,
    },
    { event: "runner_on_ok", host: "web01", task: "Gather facts", changed: false, failed: false },
    { event: "runner_on_ok", host: "web01", task: "Install nginx", changed: true, failed: false },
  ],
  events_truncated: false,
  result_truncated: false,
  // Birleşik cevap shape'i: v1 artifact'ları display çıktısı taşımaz ve
  // backend bunları null/false olarak yüzeye çıkarır (R1-V3J3A).
  ansible_output: null,
  ansible_output_truncated: false,
};

export const failedJobResult: PlaybookJobResult = {
  schema_version: 1,
  job_id: failedJob.job_id,
  return_code: 2,
  outcome: "failed",
  error_code: "runner_failed",
  recap: {
    web01: {
      ok: 1,
      changed: 0,
      failures: 1,
      unreachable: 0,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
  },
  events: [
    {
      event: "runner_on_failed",
      host: "web01",
      task: "Install nginx",
      changed: false,
      failed: true,
    },
  ],
  events_truncated: false,
  result_truncated: false,
  // Birleşik cevap shape'i: v1 artifact'ları display çıktısı taşımaz ve
  // backend bunları null/false olarak yüzeye çıkarır (R1-V3J3A).
  ansible_output: null,
  ansible_output_truncated: false,
};

/**
 * `playbook_failed` sonucu: toplam failures > 0, unreachable yok.
 *
 * Sayaçlar iki host'a dağıtılır; böylece sunumun tek host'u değil recap
 * toplamını okuduğu ölçülebilir. API'nin alan kümesi genişletilmez.
 */
export const playbookFailedJobResult: PlaybookJobResult = {
  schema_version: 1,
  job_id: playbookFailedJob.job_id,
  return_code: 2,
  outcome: "failed",
  error_code: "playbook_failed",
  recap: {
    web01: {
      ok: 4,
      changed: 0,
      failures: 2,
      unreachable: 0,
      skipped: 1,
      rescued: 0,
      ignored: 0,
    },
    db01: {
      ok: 3,
      changed: 0,
      failures: 1,
      unreachable: 0,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
  },
  // Task adı bilinçle "Assert ..." ile başlar: meşru bir task adı bu kelimeyi
  // taşıyabilir ve payload sızıntısı değildir; testler ikisini ayırt etmelidir.
  events: [
    {
      event: "runner_on_failed",
      host: "web01",
      task: "Assert sshd PermitRootLogin",
      changed: false,
      failed: true,
    },
    {
      event: "runner_on_failed",
      host: "db01",
      task: "Assert sshd PermitRootLogin",
      changed: false,
      failed: true,
    },
  ],
  events_truncated: false,
  result_truncated: false,
  // Birleşik cevap shape'i: v1 artifact'ları display çıktısı taşımaz ve
  // backend bunları null/false olarak yüzeye çıkarır (R1-V3J3A).
  ansible_output: null,
  ansible_output_truncated: false,
};

/**
 * Şema v2 display çıktısı sentinel'leri.
 *
 * Bu değerler üründe hiçbir yerde geçmez; testler önce sentinel'in gerçekten
 * girdi string'inde bulunduğunu doğrular, böylece "XSS çalışmadı" iddiası boş
 * bir ekranı ölçmez. HTML literal'leri bilinçlidir: çıktı düz metin olarak
 * render edilmelidir, HTML olarak yorumlanmamalıdır.
 */
export const DISPLAY_OUTPUT_SENTINELS = {
  script: "<script>AOPS-J3B-SCRIPT</script>",
  img: '<img src=x onerror="AOPS-J3B-XSS">',
} as const;

/**
 * `GET /api/jobs/{jobId}/result` cevabının v2 display çıktısı.
 *
 * Gerçek satır sonları, ANSI-benzeri bir escape dizisi ve HTML literal'leri
 * içerir. Metin backend'in ürettiği hâliyle bırakılır; frontend onu
 * dönüştürmez, sanitize etmez ve "temizlendi" iddiası kurmaz.
 */
export const displayOutput = [
  "PLAY [Deploy nginx] ************************************************************",
  "",
  "TASK [Gather facts] ************************************************************",
  "\u001b[0;32mok: [web01]\u001b[0m",
  `TASK [${DISPLAY_OUTPUT_SENTINELS.script}] ****************************************`,
  `changed: [web01] => ${DISPLAY_OUTPUT_SENTINELS.img}`,
  "",
  "PLAY RECAP *********************************************************************",
  "web01                      : ok=3    changed=1    unreachable=0    failed=0",
].join("\n");

/**
 * Şema v2 sonucu: display çıktısı tam olarak saklanmış.
 *
 * Yapılandırılmış yüzey (`recap`, `events`) `jobResult` ile aynıdır; tek fark
 * display yüzeyidir. Böylece testler farkın kaynağını karıştırmaz.
 */
export const displayOutputJobResult: PlaybookJobResult = {
  ...jobResult,
  schema_version: 2,
  ansible_output: displayOutput,
  ansible_output_truncated: false,
};

/** Şema v2 sonucu: çıktı byte sınırı nedeniyle kırpılmış ama saklanmış. */
export const truncatedDisplayOutputJobResult: PlaybookJobResult = {
  ...displayOutputJobResult,
  ansible_output_truncated: true,
};

/**
 * Şema v2 sonucu: çıktı sonuç bütçesi nedeniyle hiç saklanamamış.
 *
 * `ansible_output === null` ve `ansible_output_truncated === true` birlikte
 * bulunur; bu, "çıktı yoktu" değil "çıktı vardı ama saklanamadı" demektir.
 */
export const unstoredDisplayOutputJobResult: PlaybookJobResult = {
  ...displayOutputJobResult,
  ansible_output: null,
  ansible_output_truncated: true,
};

/** `playbook_failed` varyantı: hem failures hem unreachable > 0. */
export const playbookUnreachableJobResult: PlaybookJobResult = {
  ...playbookFailedJobResult,
  job_id: playbookUnreachableJob.job_id,
  recap: {
    web01: {
      ok: 2,
      changed: 0,
      failures: 1,
      unreachable: 0,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
    db01: {
      ok: 0,
      changed: 0,
      failures: 0,
      unreachable: 1,
      skipped: 0,
      rescued: 0,
      ignored: 0,
    },
    cache01: {
      ok: 0,
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
      event: "runner_on_failed",
      host: "web01",
      task: "Check audit rules",
      changed: false,
      failed: true,
    },
    {
      event: "runner_on_unreachable",
      host: "db01",
      task: "Check audit rules",
      changed: false,
      failed: true,
    },
    {
      event: "runner_on_unreachable",
      host: "cache01",
      task: "Check audit rules",
      changed: false,
      failed: true,
    },
  ],
};
