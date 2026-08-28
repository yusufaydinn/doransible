import { StatusMessage, type StatusTone } from "../../../components/StatusMessage";
import { describeJobError } from "../errorMessages";
import type { useJobResult } from "../hooks";
import { JOB_ERROR_MESSAGES } from "../labels";
import type { PlaybookHostRecap, PlaybookJobResult, PlaybookResultEvent } from "../types";

interface JobResultPanelProps {
  result: ReturnType<typeof useJobResult>;
}

/**
 * Doğrulanmış çalıştırma sonucu.
 *
 * Yalnızca çağıranın zaten `status`/`has_recorded_result` denetiminden geçirip
 * etkinleştirdiği bir sorgu ile anlamlıdır; bu bileşen kendisi o kararı vermez.
 * 503 (`job_result_unavailable`) ham hata ayrıntısı göstermeden, manuel
 * "Tekrar dene" düğmesiyle sunulur.
 */
export function JobResultPanel({ result }: JobResultPanelProps) {
  if (result.isPending) {
    return <p role="status">Sonuç yükleniyor…</p>;
  }

  if (result.isError) {
    const notice = describeJobError(result.error);
    return (
      <StatusMessage tone="error" title={notice.title} headingLevel={4}>
        <p>{notice.message}</p>
        <button type="button" onClick={() => void result.refetch()}>
          Tekrar dene
        </button>
      </StatusMessage>
    );
  }

  return <JobResultContent result={result.data} />;
}

/**
 * Recap toplamları (R1-V3G1, R1-V3I0).
 *
 * Yalnız zaten sanitize edilmiş sayaçlar toplanır; yeni bir backend alanı
 * istenmez, assert/debug mesajı, stdout/stderr veya task argümanı okunmaz.
 * `ignored` de aynı toplamda taşınır: bir playbook `ignore_errors` ile devam
 * etmiş olabilir; bu, outcome'dan (başarılı/başarısız) bağımsız bir gerçektir.
 */
function totalRecapCounters(recap: Record<string, PlaybookHostRecap>) {
  return Object.values(recap).reduce(
    (totals, hostRecap) => ({
      failures: totals.failures + hostRecap.failures,
      unreachable: totals.unreachable + hostRecap.unreachable,
      ignored: totals.ignored + hostRecap.ignored,
    }),
    { failures: 0, unreachable: 0, ignored: 0 },
  );
}

/**
 * Bir event'in kullanıcıya "problem" olarak gösterilip gösterilmeyeceği.
 *
 * Yalnız `failed === true` yeterli değildir: `runner_on_unreachable` bir host
 * `failed` alanını `false` bırakarak üretilmiş olabilir; erişilemeyen bir host
 * yine de kullanıcının görmesi gereken bir problemdir.
 */
function isProblemEvent(event: PlaybookResultEvent): boolean {
  return event.failed || event.event === "runner_on_unreachable";
}

/**
 * Problem event'inin görünür etiketi.
 *
 * Anlam yalnız renkle taşınmaz: `runner_on_unreachable` için "Erişilemedi",
 * diğer başarısız event'ler için "Başarısız" metni her zaman görünür kalır.
 */
function problemEventLabel(event: PlaybookResultEvent): string {
  return event.event === "runner_on_unreachable" ? "Erişilemedi" : "Başarısız";
}

function JobResultContent({ result }: { result: PlaybookJobResult }) {
  const hostEntries = Object.entries(result.recap).sort(([a], [b]) => a.localeCompare(b));
  const errorNotice =
    result.outcome === "failed" && result.error_code !== null
      ? JOB_ERROR_MESSAGES[result.error_code]
      : null;
  // Sayaç özeti bilinçli olarak yalnız `playbook_failed` için üretilir: başka
  // bir kodun recap'ine bakıp "aslında playbook başarısız olmuş" demek,
  // backend'in vermediği bir sınıflandırmayı frontend'de uydurmak olurdu.
  // Legacy `runner_failed` belgeleri bu yüzden kendi diliyle gösterilir.
  const recapTotals = totalRecapCounters(result.recap);
  const playbookCounters = result.error_code === "playbook_failed" ? recapTotals : null;
  // `playbook_failed`, yapısal olarak doğrulanmış bir terminal sonuç
  // sınıfıdır (bkz. `types.ts` — `ResultErrorCode` dokümantasyonu); bu yüzden
  // diğer kodlardan farklı olarak turuncu/uyarı tonuyla gösterilir (R1-V3I0).
  // Bu, legacy `runner_failed` belgelerinin güvenilir bir recap taşıyamayacağı
  // anlamına gelmez — yalnızca `playbook_failed`'in sınıflandırması kesindir,
  // `runner_failed`'inki değildir. Ayrıca bu, kök nedenin runner/ağ/SSH/
  // altyapı dışında olduğu iddiası **değildir** — unreachable bir host için
  // kök neden yine altyapı olabilir; kod bunu sınıflandırmaz. Diğer kodlar
  // (`runner_failed` dahil) kırmızı/hata tonunu korur.
  const errorTone: StatusTone = result.error_code === "playbook_failed" ? "warning" : "error";
  // Problem event'lerin kısa özeti: `failed === true` VEYA
  // `event === "runner_on_unreachable"`. İkinci koşul ayrıca gereklidir: bir
  // host unreachable olduğunda üretilen event
  // `failed` alanını `false` bırakabilir, ama bu yine de kullanıcının görmesi
  // gereken bir problemdir. Outcome'dan bağımsız, kök neden uydurulmaz.
  const problemEvents = result.events.filter(isProblemEvent);

  return (
    <div className="panel">
      {errorNotice !== null && (
        <StatusMessage tone={errorTone} title="Sonuç değerlendirmesi" headingLevel={4}>
          <p>{errorNotice.description}</p>
          {playbookCounters !== null && playbookCounters.failures > 0 && (
            <p>
              {playbookCounters.failures} task sonucu başarısız olarak raporlandı.
            </p>
          )}
          {playbookCounters !== null && playbookCounters.unreachable > 0 && (
            <p>
              {playbookCounters.unreachable} host&apos;a erişilemedi; bunun kök nedeni SSH, ağ
              veya hedef yapılandırması olabilir.
            </p>
          )}
        </StatusMessage>
      )}

      {/*
       * `ignored` outcome'dan bağımsız bir gerçektir: bir playbook
       * `ignore_errors` ile devam etmiş olabilir, bu ne bir "güvenlik bulgusu"
       * ne de otomatik bir kök neden iddiasıdır — yalnız task hatasına rağmen
       * çalışmanın sürdüğünü söyler.
       */}
      {recapTotals.ignored > 0 && (
        <p className="job-result__ignored-note">
          {recapTotals.ignored} task hatasından sonra playbook çalışmaya devam etti.
        </p>
      )}

      <dl>
        <dt>Sonuç</dt>
        <dd>{result.outcome === "successful" ? "Başarılı" : "Başarısız"}</dd>

        <dt>Dönüş kodu</dt>
        <dd>{result.return_code}</dd>

        <dt>Hata kodu</dt>
        <dd>{result.error_code === null ? "Yok" : <code>{result.error_code}</code>}</dd>
      </dl>

      {result.events_truncated && (
        <StatusMessage tone="warning" title="Event listesi kırpıldı" headingLevel={4}>
          <p>Bu çalıştırmanın event listesi boyut sınırı nedeniyle kırpılmış.</p>
        </StatusMessage>
      )}

      {result.result_truncated && (
        <StatusMessage tone="warning" title="Sonuç belgesi kırpıldı" headingLevel={4}>
          <p>Bu çalıştırmanın sonuç belgesi boyut sınırı nedeniyle kırpılmış.</p>
        </StatusMessage>
      )}

      <h4>Host özeti</h4>
      {hostEntries.length === 0 ? (
        <p className="muted">Özete girecek host yok.</p>
      ) : (
        <div className="table-wrapper">
          <table className="table">
            <caption className="visually-hidden">Host bazlı özet</caption>
            <thead>
              <tr>
                <th scope="col">Host</th>
                <th scope="col">OK</th>
                <th scope="col">Değişti</th>
                <th scope="col">Başarısız</th>
                <th scope="col">Erişilemedi</th>
                <th scope="col">Atlandı</th>
                <th scope="col">Kurtarıldı</th>
                <th scope="col">Yok sayıldı</th>
              </tr>
            </thead>
            <tbody>
              {hostEntries.map(([host, recap]) => (
                <HostRecapRow key={host} host={host} recap={recap} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4>Event&apos;ler</h4>
      {problemEvents.length > 0 && (
        <div className="job-problem-events">
          <h5 className="job-problem-events__title">Başarısız veya erişilemeyen task/eventler</h5>
          <ul className="job-problem-events__list">
            {problemEvents.map((event, index) => (
              <ProblemEventItem key={index} event={event} />
            ))}
          </ul>
        </div>
      )}
      {result.events.length === 0 ? (
        <p className="muted">Listelenecek event yok.</p>
      ) : (
        <div className="table-wrapper">
          <table className="table">
            <caption className="visually-hidden">Sanitize edilmiş event listesi</caption>
            <thead>
              <tr>
                <th scope="col">Event</th>
                <th scope="col">Host</th>
                <th scope="col">Task</th>
                <th scope="col">Değişti</th>
                <th scope="col">Başarısız</th>
              </tr>
            </thead>
            <tbody>
              {result.events.map((event, index) => (
                // Renk (`.table-row--problem`) tek anlam kaynağı değildir: bu
                // sınıf hem `failed` hem `runner_on_unreachable` satırlarını
                // kapsar ve "Event" sütunundaki görünür ham event kodu (ör.
                // `runner_on_unreachable`) her
                // zaman satırla birlikte durur; sınıf yalnız görsel destektir.
                <tr key={index} className={isProblemEvent(event) ? "table-row--problem" : undefined}>
                  <td>
                    <code>{event.event}</code>
                  </td>
                  <td>{event.host ?? "—"}</td>
                  <td>{event.task ?? "—"}</td>
                  <td>{event.changed ? "Evet" : "Hayır"}</td>
                  <td>{event.failed ? "Evet" : "Hayır"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <AnsibleOutputSection result={result} />
    </div>
  );
}

/**
 * Ham Ansible display çıktısı (R1-V3J3B).
 *
 * Trusted-operator/CLI-equivalent model: tek bir güvenilir operatör, arayüzde
 * CLI'da göreceği metni görür. Bu metin **sanitize edilmiş sayılmaz**;
 * credential değerleri, playbook kaynak satırları veya controller yolları
 * içerebilir. Bu yüzden bileşen bir uyarı gösterir ve hiçbir "güvenli",
 * "temizlendi" ya da "redakte edildi" iddiası kurmaz.
 *
 * Metin hiçbir dönüşümden geçmez: trim, split/join, ANSI temizleme, HTML
 * decode veya markdown/HTML render yoktur. Değer bir `<pre><code>` içinde düz
 * metin olarak basılır; `dangerouslySetInnerHTML` bilinçli olarak
 * kullanılmaz, böylece çıktıdaki HTML literal'leri element'e dönüşmez.
 *
 * Native `<details>` varsayılan olarak kapalıdır. Bu **yalnız görsel bir
 * sunum tercihidir**: kapalı bir `<details>` erişim kontrolü değildir ve
 * içeriği DOM'da bulunur. "Secret DOM'a girmez" gibi bir iddia kurulmaz.
 */
function AnsibleOutputSection({ result }: { result: PlaybookJobResult }) {
  return (
    <details className="job-raw-output">
      <summary className="job-raw-output__summary">Ham Ansible çıktısı</summary>

      <p className="job-raw-output__warning">
        Bu çıktı hassas bilgiler, credential değerleri veya controller yolları içerebilir. Yalnız
        güvenilir operatör tarafından incelenmelidir.
      </p>

      {result.ansible_output === null ? (
        // İki farklı gerçek, iki farklı cümle: v1 belgesinde çıktı hiç
        // kaydedilmemiştir (hata değildir), v2'de `truncated` ile birlikte
        // gelen null ise çıktının saklanamadığı anlamına gelir.
        <p className="job-raw-output__empty">
          {result.ansible_output_truncated
            ? "Ansible çıktısı sonuç boyutu bütçesi nedeniyle saklanamadı."
            : "Bu sonuç için görüntülenecek Ansible çıktısı kaydedilmemiş."}
        </p>
      ) : (
        <>
          {/*
           * `ansible_output_truncated`, `result_truncated` ve
           * `events_truncated` uyarılarından ayrı bir sözleşmedir; onların
           * yerine geçmez, onları da değiştirmez.
           */}
          {result.ansible_output_truncated && (
            <p className="job-raw-output__truncated">
              Ansible çıktısı boyut sınırı nedeniyle kırpıldı; yalnız kaydedilen başlangıç bölümü
              gösteriliyor.
            </p>
          )}
          <pre className="job-raw-output__pre">
            <code>{result.ansible_output}</code>
          </pre>
        </>
      )}
    </details>
  );
}

/**
 * "Başarısız veya erişilemeyen task/eventler" kısa özetinde tek bir satır.
 *
 * Host, task adı ve görünür bir problem etiketi ("Başarısız" / "Erişilemedi")
 * gösterir; kök neden uydurulmaz (`event_data`, `res`, `msg` gibi ham alanlar
 * sözleşmede zaten yoktur, bkz. `types.ts`).
 */
function ProblemEventItem({ event }: { event: PlaybookResultEvent }) {
  return (
    <li className="job-problem-events__item">
      <strong>{event.host ?? "—"}</strong> — {event.task ?? "—"}{" "}
      <span className="badge badge--failed">{problemEventLabel(event)}</span>
    </li>
  );
}

function HostRecapRow({ host, recap }: { host: string; recap: PlaybookHostRecap }) {
  return (
    <tr>
      <th scope="row">
        <code>{host}</code>
      </th>
      <td>{recap.ok}</td>
      <td>{recap.changed}</td>
      <td>{recap.failures}</td>
      <td>{recap.unreachable}</td>
      <td>{recap.skipped}</td>
      <td>{recap.rescued}</td>
      <td>{recap.ignored}</td>
    </tr>
  );
}
