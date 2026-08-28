import { Link } from "react-router-dom";

import { StatusMessage, type StatusTone } from "../../../components/StatusMessage";
import { formatDateTime } from "../../../lib/format";
import { JOB_ERROR_MESSAGES, JOB_MODE_LABELS } from "../labels";
import type { JobStatus, PlaybookJobSummary } from "../types";
import { JobStatusBadge } from "./JobStatusBadge";

interface JobSummaryPanelProps {
  job: PlaybookJobSummary;
}

/**
 * Durum bandı metinleri bilinçli olarak `JOB_STATUS_LABELS` değerlerinden
 * (ör. "Çalışıyor", "Başarılı") farklıdır: aynı metin iki kez basılırsa
 * `screen.findByText(...)` gibi tekil eşleşme bekleyen testler birden fazla
 * öğe bulup başarısız olur (bkz. `jobPages.test.tsx`).
 */
const STATUS_BANNER: Record<JobStatus, { tone: StatusTone; title: string }> = {
  pending: { tone: "info", title: "Çalıştırma kuyrukta bekliyor" },
  running: { tone: "info", title: "Çalıştırma sürüyor" },
  successful: { tone: "success", title: "Çalıştırma başarıyla tamamlandı" },
  failed: { tone: "error", title: "Çalıştırma başarısız oldu" },
  canceled: { tone: "warning", title: "Çalıştırma iptal edildi" },
};

/**
 * Job detayının özet paneli.
 *
 * Aktör, plan/workspace kimliği, manifest digest'i ve artifact yolu backend
 * tarafından zaten verilmez; burada da gösterilmeye çalışılmaz.
 */
export function JobSummaryPanel({ job }: JobSummaryPanelProps) {
  const banner = STATUS_BANNER[job.status];
  // Başarısız bir Job'ın kodu varsa generic "Çalıştırma başarısız oldu" başlığı
  // yerine kodun kendi kullanıcı dili gösterilir: `playbook_failed` ile
  // `runner_failed` aynı cümleyle anlatılırsa kullanıcı, güvenilir bir playbook
  // sonucu olup olmadığını ayırt edemez. Ham kod aşağıdaki `<code>` alanında
  // olduğu gibi durmaya devam eder.
  const errorNotice =
    job.status === "failed" && job.error_code !== null
      ? JOB_ERROR_MESSAGES[job.error_code]
      : null;

  /**
   * `playbook_failed`, çalıştırma altyapısının (runner) bozulduğu anlamına
   * gelmez: güvenilir bir Ansible terminal sonucu üretilmiş, yalnızca o sonuç
   * başarısızlık raporlamıştır (R1-V3I0). Bu yüzden banner tonu bilinçli
   * olarak diğer hata kodlarından ayrılır: turuncu/uyarı, kırmızı/hata değil.
   * `runner_failed`, `runner_timeout` vb. diğer kodlar `STATUS_BANNER.failed`
   * üzerinden mevcut kırmızı davranışını korur.
   */
  const isPlaybookOutcomeFailure = job.status === "failed" && job.error_code === "playbook_failed";
  const bannerTone: StatusTone = isPlaybookOutcomeFailure ? "warning" : banner.tone;

  return (
    <div className="panel">
      <StatusMessage
        tone={bannerTone}
        title={errorNotice === null ? banner.title : errorNotice.title}
        headingLevel={4}
      >
        {errorNotice !== null && <p>{errorNotice.description}</p>}
      </StatusMessage>

      <dl>
        {isPlaybookOutcomeFailure ? (
          <>
            {/*
             * Çalıştırma yaşam döngüsü (runner) ile playbook sonucu ayrı
             * satırlarda gösterilir. `job.status` alanı hâlâ "failed"'dir
             * (backend değişmez); yalnızca sunum, "runner bozuldu" iddiası
             * yerine "çalıştırma tamamlandı, playbook başarısız sonuç
             * bildirdi" ayrımını yapar. "Tamamlandı" nötr bir rozettir —
             * playbook'un başarılı olduğunu iddia etmez, yalnız çalıştırma
             * sürecinin sonlandığını söyler.
             */}
            <dt>Çalıştırma durumu</dt>
            <dd>
              <span className="badge badge--neutral">Tamamlandı</span>
            </dd>

            <dt>Playbook sonucu</dt>
            <dd>
              <span className="badge badge--failed">Başarısız</span>
            </dd>
          </>
        ) : (
          <>
            <dt>Durum</dt>
            <dd>
              <JobStatusBadge status={job.status} />
            </dd>
          </>
        )}

        <dt>Çalıştırma biçimi</dt>
        <dd>
          {/*
           * Kullanıcı dostu etiket ham koddan **önce** gelir (R1-V3H2B):
           * `normal` bir Job burada ham `check` varsayımıyla değil, kendi
           * `mode` alanından okunarak gösterilir. Tire ayracı (R1-V3H2B-
           * AUDIT-FIX1) etiketin kendi parantezini ("gerçek uygulama") ham
           * kodun ikinci bir parantezle iç içe binmesinden ayırır.
           */}
          {JOB_MODE_LABELS[job.mode]} — <code>{job.mode}</code>
        </dd>

        <dt>Project</dt>
        <dd>
          <Link to={`/projects/${job.project_id}`}>{job.project_name}</Link>{" "}
          <span className="muted">#{job.project_id}</span>
        </dd>

        <dt>Inventory</dt>
        <dd>
          <Link to={`/inventories/${job.inventory_id}`}>{job.inventory_name}</Link>{" "}
          <span className="muted">#{job.inventory_id}</span>
        </dd>

        <dt>Playbook</dt>
        <dd>
          <code>{job.playbook_path}</code>
        </dd>

        <dt>Oluşturulma</dt>
        <dd>{formatDateTime(job.created_at)}</dd>

        <dt>Başlama</dt>
        <dd>{job.started_at === null ? "Henüz başlamadı" : formatDateTime(job.started_at)}</dd>

        <dt>Bitiş</dt>
        <dd>{job.finished_at === null ? "Henüz tamamlanmadı" : formatDateTime(job.finished_at)}</dd>

        <dt>Dönüş kodu</dt>
        <dd>{job.return_code === null ? "Yok" : job.return_code}</dd>

        <dt>Hata kodu</dt>
        <dd>{job.error_code === null ? "Yok" : <code>{job.error_code}</code>}</dd>
      </dl>

      {job.result_truncated && (
        <StatusMessage tone="warning" title="Sonuç kırpıldı" headingLevel={4}>
          <p>
            Bu çalıştırmanın sonuç belgesi boyut sınırı nedeniyle kırpılmış olabilir.
          </p>
        </StatusMessage>
      )}
    </div>
  );
}
