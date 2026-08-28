import { useId, useState } from "react";

import { StatusMessage } from "../components/StatusMessage";
import { JobList } from "../features/jobs/components/JobList";
import { useJobs } from "../features/jobs/hooks";
import { JOB_MODE_LABELS, JOB_STATUS_LABELS } from "../features/jobs/labels";
import type { ExecutionMode, JobStatus, PlaybookJobCursor } from "../features/jobs/types";

/** Select'te "hiç filtre yok" durumunu temsil eden değer. */
const ALL_STATUSES = "all";
const ALL_MODES = "all";

const STATUS_OPTIONS: Array<{ value: JobStatus | typeof ALL_STATUSES; label: string }> = [
  { value: ALL_STATUSES, label: "Tümü" },
  { value: "pending", label: JOB_STATUS_LABELS.pending },
  { value: "running", label: JOB_STATUS_LABELS.running },
  { value: "successful", label: JOB_STATUS_LABELS.successful },
  { value: "failed", label: JOB_STATUS_LABELS.failed },
  { value: "canceled", label: JOB_STATUS_LABELS.canceled },
];

const MODE_OPTIONS: Array<{ value: ExecutionMode | typeof ALL_MODES; label: string }> = [
  { value: ALL_MODES, label: "Tümü" },
  { value: "check", label: JOB_MODE_LABELS.check },
  { value: "normal", label: JOB_MODE_LABELS.normal },
];

/**
 * Gezilen sayfaların cursor yığını.
 *
 * İlk eleman her zaman `null`'dır: ilk sayfa "cursor'sız istek" demektir.
 * Sonraki her eleman, sunucunun o sayfayı üretirken verdiği `next_cursor`
 * değeridir. Böylece "Önceki" için ayrı bir backend sözleşmesine (ters yön
 * cursor'ı, offset, toplam sayı) gerek kalmaz — geri gitmek yığından bir
 * eleman çıkarmaktır.
 */
type CursorStack = ReadonlyArray<PlaybookJobCursor | null>;

/** Sıfırlama için tek, paylaşılan ilk sayfa yığını; asla mutasyona uğramaz. */
const FIRST_PAGE_STACK: CursorStack = [null];

/**
 * Çalıştırmalar listesi; project/inventory adları, status/mode filtreleri
 * (R1-V3J0B2) ve keyset sayfalama gezinmesi (R1-V3J2A) taşır.
 *
 * Filtreleme **sunucu tarafındadır**: seçim `useJobs`'a geçilir ve doğrudan
 * `GET /api/jobs` query parametrelerine dönüşür (bkz. `features/jobs/api.ts`).
 * İstemci tarafında ayrıca eleme yapılmaz. Sayfalama da sunucu tarafındadır:
 * her sayfada en fazla 25 kayıt gösterilir ve ileri gezinme yalnız backend'in
 * döndürdüğü opaque `next_cursor` değeriyle yapılır. Cursor'ın iç yapısı
 * kullanıcıya hiçbir yerde gösterilmez.
 *
 * Toplam kayıt/sayfa sayısı bilinçli olarak **iddia edilmez**: keyset
 * sayfalama böyle bir sayı üretmez, dolayısıyla "1 / N" veya rastgele sayfaya
 * atlama da yoktur.
 */
export function JobListPage() {
  const [status, setStatus] = useState<JobStatus | typeof ALL_STATUSES>(ALL_STATUSES);
  const [mode, setMode] = useState<ExecutionMode | typeof ALL_MODES>(ALL_MODES);
  const [cursorStack, setCursorStack] = useState<CursorStack>(FIRST_PAGE_STACK);
  const statusId = useId();
  const modeId = useId();

  const currentCursor = cursorStack[cursorStack.length - 1] ?? null;
  const pageNumber = cursorStack.length;
  const isFirstPage = pageNumber === 1;

  const hasActiveFilter = status !== ALL_STATUSES || mode !== ALL_MODES;
  const jobs = useJobs(
    {
      status: status === ALL_STATUSES ? undefined : status,
      mode: mode === ALL_MODES ? undefined : mode,
    },
    currentCursor,
  );

  /**
   * Bir istek sürerken gezinme kilitlenir.
   *
   * Aksi hâlde "Sonraki"ye iki kez basmak, ikinci basışta hâlâ bir önceki
   * sayfanın `next_cursor`'ı elde olduğu için yığını **aynı** cursor'la iki kez
   * ilerletirdi ve "Sayfa 3" aslında sayfa 2'yi gösterirdi.
   */
  const isBusy = jobs.isPending || jobs.isFetching;

  /**
   * İleri gezinme fail-closed'dır.
   *
   * `has_more === true` fakat `next_cursor === null` sözleşme dışı bir cevaptır.
   * Böyle bir cevapta cursor'ı son satırdan uydurmak yerine "Sonraki" kapalı
   * kalır: eksik veri, yanlış veriden iyidir.
   */
  const nextCursor = jobs.isSuccess ? jobs.data.next_cursor : null;
  const canGoNext = jobs.isSuccess && jobs.data.has_more && nextCursor !== null && !isBusy;
  const canGoPrev = !isFirstPage && !isBusy;

  /** Updater saf kalır: yığını değiştirir, yan etki (fetch) tetiklemez. */
  function goToNextPage() {
    if (!canGoNext || nextCursor === null) {
      return;
    }
    setCursorStack((stack) => [...stack, nextCursor]);
  }

  function goToPreviousPage() {
    if (!canGoPrev) {
      return;
    }
    setCursorStack((stack) => (stack.length > 1 ? stack.slice(0, -1) : stack));
  }

  /**
   * Filtre değişimi sayfayı daima 1'e sıfırlar.
   *
   * Sıfırlama filtre state'iyle **aynı** olayda yapılır; React ikisini tek
   * render'da toplar, dolayısıyla yeni filtreli istek eski cursor'ı hiç
   * taşımaz. Ara bir "yeni filtre + eski cursor" isteği oluşmaz.
   */
  function changeStatus(next: JobStatus | typeof ALL_STATUSES) {
    setStatus(next);
    setCursorStack(FIRST_PAGE_STACK);
  }

  function changeMode(next: ExecutionMode | typeof ALL_MODES) {
    setMode(next);
    setCursorStack(FIRST_PAGE_STACK);
  }

  // İlk sayfada henüz hiç kayıt yokken gezinme alanı gereksizdir; ikinci ve
  // sonraki sayfalarda ise (boş sonuç veya hata dâhil) her zaman görünür kalır,
  // aksi hâlde kullanıcı geri dönemezdi.
  const showPager = !isFirstPage || (jobs.isSuccess && jobs.data.items.length > 0);

  return (
    <section>
      <div className="page-header">
        <h2>Çalıştırmalar</h2>
      </div>

      <p className="muted">
        Onaylanmış planlardan oluşturulan çalıştırmalar en yeni önce listelenir.
        Kayıtlar sunucu tarafında filtrelenir ve her sayfada en fazla 25 kayıt
        gösterilir.
      </p>

      <div className="filter-bar" role="group" aria-label="Çalıştırma filtreleri">
        <div className="field">
          <label htmlFor={statusId}>Durum</label>
          <select
            id={statusId}
            value={status}
            onChange={(event) => changeStatus(event.target.value as JobStatus | typeof ALL_STATUSES)}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor={modeId}>Kip</label>
          <select
            id={modeId}
            value={mode}
            onChange={(event) => changeMode(event.target.value as ExecutionMode | typeof ALL_MODES)}
          >
            {MODE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {jobs.isPending && <p role="status">Çalıştırmalar yükleniyor…</p>}

      {jobs.isError && <JobListError onRetry={() => void jobs.refetch()} />}

      {jobs.isSuccess &&
        (jobs.data.items.length === 0 ? (
          <EmptyState hasActiveFilter={hasActiveFilter} isFirstPage={isFirstPage} />
        ) : (
          <JobList jobs={jobs.data.items} />
        ))}

      {showPager && (
        <nav className="pager" aria-label="Çalıştırma sayfaları">
          <button type="button" onClick={goToPreviousPage} disabled={!canGoPrev}>
            Önceki
          </button>
          <span className="pager__page">Sayfa {pageNumber}</span>
          <button type="button" onClick={goToNextPage} disabled={!canGoNext}>
            Sonraki
          </button>
        </nav>
      )}
    </section>
  );
}

/**
 * Boş sonuç mesajı, filtre ve sayfa durumuna göre ayrışır (R1-V3J0B2,
 * R1-V3J2A).
 *
 * Filtre yokken "Henüz çalıştırma yok" doğrudur; ama filtre seçiliyken aynı
 * cümle yanlış bir izlenim verirdi — kayıt var, yalnızca seçilen durum/kiple
 * eşleşen yok. İkinci ve sonraki sayfalarda ise ikisi de yanlış olurdu: kayıt
 * da var, filtreyle eşleşen de olabilir; boş olan yalnızca **bu** sayfadır
 * (kayıtlar cursor alındıktan sonra silinmiş olabilir). Kullanıcı burada
 * kilitlenmesin diye otomatik geri atlama yapılmaz; "Önceki" açık kalır.
 */
function EmptyState({
  hasActiveFilter,
  isFirstPage,
}: {
  hasActiveFilter: boolean;
  isFirstPage: boolean;
}) {
  if (!isFirstPage) {
    return (
      <StatusMessage tone="info" title="Bu sayfada çalıştırma bulunamadı">
        <p>Bu sayfada gösterilecek çalıştırma yok. Önceki sayfaya dönebilirsiniz.</p>
      </StatusMessage>
    );
  }

  if (hasActiveFilter) {
    return (
      <StatusMessage tone="info" title="Filtrelerle eşleşen çalıştırma yok">
        <p>Seçili durum/kip kombinasyonuyla eşleşen bir çalıştırma bulunamadı. Filtreleri değiştirip tekrar deneyin.</p>
      </StatusMessage>
    );
  }

  return (
    <StatusMessage tone="info" title="Henüz çalıştırma yok">
      <p>
        Bir project detayında plan hazırlayıp onayladığınızda çalıştırma burada
        listelenecek.
      </p>
    </StatusMessage>
  );
}

/**
 * Liste hatasının **sabit** paneli (R1-V3J2AF).
 *
 * Metin bilinçli olarak hata nesnesinden türetilmez. `describeJobError`
 * bilinmeyen bir kodda `describeApiError`'ın default dalına düşer ve orası
 * backend'in `message` alanını doğrudan basar; bu, liste ekranı için fazla
 * geniş bir yüzeydir — Job listesi tek bir endpoint'ten okur ve kullanıcının
 * yapabileceği tek şey zaten tekrar denemektir. Bu yüzden panel `error`
 * değerini hiç almaz: `message`, `details`, `code` ve cursor değerlerinin
 * ekrana sızması tip düzeyinde imkânsızdır.
 *
 * Bu daraltma yalnız bu panele özgüdür; `describeApiError`'ın ortak davranışı
 * ve Job detay/sonuç ekranlarının hata metinleri değişmez.
 */
function JobListError({ onRetry }: { onRetry: () => void }) {
  return (
    <StatusMessage tone="error" title="Çalıştırmalar yüklenemedi" headingLevel={3}>
      <p>Çalıştırma listesi şu anda yüklenemedi. Tekrar deneyin.</p>
      <button type="button" onClick={onRetry}>
        Tekrar dene
      </button>
    </StatusMessage>
  );
}
