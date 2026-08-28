import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { describePingError, type PingErrorNotice, type PingStage } from "../errorMessages";
import { usePingActions } from "../hooks";
import type { Inventory, PingPlan, PingRunResponse } from "../types";
import { PingErrorPanel } from "./PingErrorPanel";
import { PingPlanPanel } from "./PingPlanPanel";
import { PingResultPanel } from "./PingResultPanel";

/** Backend gövde sınırıyla aynı üst sınır; yalnızca payload korumasıdır. */
const MAX_LIMIT_LENGTH = 4096;

/**
 * Ping akışının mantıksal durumları.
 *
 * Ayrık birleşim (discriminated union) bilinçlidir: çakışan iki işlemin aynı
 * anda "açık" olması tip düzeyinde imkânsızdır. Örneğin `confirming` durumunda
 * plan okunabilir kalır ama "Vazgeç" eylemi yoktur, çünkü o eylem yalnızca
 * `preview_ready` dalında render edilir.
 */
type PingPhase =
  | { kind: "idle" }
  | { kind: "previewing" }
  | { kind: "preview_ready"; plan: PingPlan; expiresAt: string }
  | { kind: "confirming"; plan: PingPlan; expiresAt: string }
  | { kind: "canceling"; plan: PingPlan; expiresAt: string }
  | { kind: "result"; run: PingRunResponse }
  | { kind: "error"; stage: PingStage; notice: PingErrorNotice };

/**
 * Onay kaybolduğunda gösterilen bildirim.
 *
 * Normal akışta oluşmaz; buton yalnızca token varken render edilir. Yine de
 * sessizce token'sız istek göndermek yerine akış açıkça durdurulur.
 */
const MISSING_TOKEN_NOTICE: PingErrorNotice = {
  title: "Onay bulunamadı",
  message:
    "Bu plana ait onay artık bellekte değil, bu yüzden hiçbir istek gönderilmedi. " +
    "Yeni bir önizleme oluşturup planı tekrar onaylayın.",
  retryable: false,
  requiresNewPreview: true,
};

interface PingSectionProps {
  inventory: Inventory;
}

/**
 * Inventory erişilebilirlik testi (T-204C).
 *
 * Bölüm inventory metadata'sına bağlıdır ve `/hosts` sorgusundan **bağımsızdır**:
 * dosya ayrıştırılamasa bile ping denenebilir.
 *
 * İki güvenlik kararı bu bileşeni şekillendirir:
 *
 * 1. **Onay token'ı hiçbir cache'e ve render edilen state'e girmez.** İstekler
 *    TanStack mutation'ı değil, `usePingActions` üzerinden gelen durumsuz
 *    `Promise`'lerdir; token yalnızca yerel bir `Promise` değişkeninde ve
 *    private bir `useRef` içinde yaşar. Plan ve son kullanma zamanı token'sız
 *    UI state'ine kopyalanır.
 * 2. **Çift tıklama koruması senkrondur.** `disabled` yalnızca bir sonraki
 *    render'da etkili olur; hızlı iki tıklama arasındaki pencerede iki istek
 *    çıkabilirdi. Bu yüzden her handler ilk iş olarak senkron bir kilidi alır.
 */
export function PingSection({ inventory }: PingSectionProps) {
  const [limit, setLimit] = useState("");
  const [phase, setPhase] = useState<PingPhase>({ kind: "idle" });

  // Senkron kilit: React'in `disabled` güncellemesini beklemez.
  const inFlightRef = useRef(false);
  // Token yalnızca burada durur; DOM'a, query key'e veya depolamaya girmez.
  const tokenRef = useRef<string | null>(null);
  // Unmount'tan sonra tamamlanan istekler için canlılık bayrağı.
  const mountedRef = useRef(true);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  const limitFieldId = useId();
  const limitHintId = useId();
  const approvalTitleId = useId();

  const ping = usePingActions(inventory.id);

  useEffect(() => {
    // StrictMode, geliştirme ortamında effect'i mount → unmount → mount olarak
    // iki kez çalıştırır; bayrak bu yüzden cleanup'ta kapatılıp effect
    // gövdesinde yeniden açılır.
    mountedRef.current = true;

    // Unmount'ta token bellekten düşer. Burada bilinçli olarak fire-and-forget
    // bir iptal isteği başlatılmaz: unmount sırasında başlatılan isteğin
    // tamamlandığı doğrulanamaz ve kullanılmayan server state zaten TTL ile
    // temizlenir.
    return () => {
      mountedRef.current = false;
      tokenRef.current = null;
    };
  }, []);

  useEffect(() => {
    // Onay görünür olduğunda odak onay butonuna taşınır; kullanıcı planı
    // klavyeyle okuyup aynı yerden onaylayabilir.
    if (phase.kind === "preview_ready") {
      confirmButtonRef.current?.focus();
    }
  }, [phase.kind]);

  /** Boş forma döner. Hiçbir istek başlatmaz; yeni ping ayrı bir karardır. */
  function startOver() {
    if (inFlightRef.current) {
      return;
    }
    tokenRef.current = null;
    setPhase({ kind: "idle" });
  }

  async function handlePreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setPhase({ kind: "previewing" });

    try {
      // Gönderim kuralı: yalnızca tam olarak boş metin `null` olur. Trim,
      // normalize veya pattern doğrulaması yapılmaz — limitin anlamı sunucuda
      // çözülür, ikinci bir istemci kuralı onaylanan planı saptırırdı.
      const response = await ping.preview({ limit: limit === "" ? null : limit });

      // Bileşen bu arada unmount olduysa token **ref'e yazılmaz** ve state
      // güncellenmez: geride hiçbir yerde tutulmayan cevap nesnesi çöpe gider.
      // Onaylanmamış plan sunucuda TTL ile temizlenir.
      if (!mountedRef.current) {
        return;
      }

      tokenRef.current = response.preview_token;
      setPhase({
        kind: "preview_ready",
        plan: response.plan,
        expiresAt: response.expires_at,
      });
    } catch (error) {
      if (!mountedRef.current) {
        return;
      }
      setPhase({
        kind: "error",
        stage: "preview",
        notice: describePingError(error, "preview"),
      });
    } finally {
      // Kilit başarı ve hata yollarının ikisinde de bırakılır.
      inFlightRef.current = false;
    }
  }

  async function handleConfirm() {
    if (inFlightRef.current || phase.kind !== "preview_ready") {
      return;
    }
    inFlightRef.current = true;

    const token = tokenRef.current;
    // Ref istek gönderilmeden **önce** temizlenir: kilidi aşan ikinci bir
    // handler aynı token'ı bir daha okuyamaz.
    tokenRef.current = null;

    if (token === null) {
      inFlightRef.current = false;
      setPhase({ kind: "error", stage: "confirm", notice: MISSING_TOKEN_NOTICE });
      return;
    }

    setPhase({ kind: "confirming", plan: phase.plan, expiresAt: phase.expiresAt });

    try {
      const run = await ping.confirm({ preview_token: token });
      if (!mountedRef.current) {
        return;
      }
      setPhase({ kind: "result", run });
    } catch (error) {
      // Belirsiz sonuç otomatik olarak tekrar edilmez; token zaten tüketildi ve
      // kullanıcı yalnızca yeni bir önizleme akışına dönebilir.
      if (!mountedRef.current) {
        return;
      }
      setPhase({
        kind: "error",
        stage: "confirm",
        notice: describePingError(error, "confirm"),
      });
    } finally {
      inFlightRef.current = false;
    }
  }

  async function handleCancel() {
    if (inFlightRef.current || phase.kind !== "preview_ready") {
      return;
    }
    inFlightRef.current = true;

    const token = tokenRef.current;
    tokenRef.current = null;

    if (token === null) {
      // Onay zaten yok; sunucuya gidecek bir şey kalmadı.
      inFlightRef.current = false;
      setPhase({ kind: "idle" });
      return;
    }

    setPhase({ kind: "canceling", plan: phase.plan, expiresAt: phase.expiresAt });

    try {
      await ping.cancel({ preview_token: token });
      if (!mountedRef.current) {
        return;
      }
      setPhase({ kind: "idle" });
    } catch (error) {
      if (!mountedRef.current) {
        return;
      }
      setPhase({
        kind: "error",
        stage: "cancel",
        notice: describePingError(error, "cancel"),
      });
    } finally {
      inFlightRef.current = false;
    }
  }

  const showForm = phase.kind === "idle" || phase.kind === "previewing";
  const showPlan =
    phase.kind === "preview_ready" ||
    phase.kind === "confirming" ||
    phase.kind === "canceling";

  return (
    <section className="section">
      <h3>Erişilebilirlik testi</h3>
      <p>
        Seçilen host'lara Ansible <code>ping</code> modülü çalıştırılır. Bu{" "}
        <strong>gerçek bir execution'dır</strong>: hedeflere SSH ile bağlanılır ve
        uzak hostta geçici modül dosyaları oluşabilir. Bu yüzden önce plan
        gösterilir, çalıştırma ayrı bir onay ister.
      </p>

      {showForm && (
        <form onSubmit={(event) => void handlePreview(event)}>
          <div className="field">
            <label htmlFor={limitFieldId}>Host limiti (isteğe bağlı)</label>
            <p className="field__hint" id={limitHintId}>
              Boş bırakırsanız inventory'nin tamamı hedeflenir. Bir host ya da grup
              adı yazarsanız yalnızca eşleşen host'lar ping'lenir. Değer olduğu gibi
              sunucuya gönderilir ve orada çözümlenir.
            </p>
            <input
              id={limitFieldId}
              name="limit"
              type="text"
              value={limit}
              maxLength={MAX_LIMIT_LENGTH}
              aria-describedby={limitHintId}
              autoComplete="off"
              disabled={phase.kind === "previewing"}
              onChange={(event) => setLimit(event.target.value)}
            />
          </div>

          <button type="submit" disabled={phase.kind === "previewing"}>
            {phase.kind === "previewing" ? "Plan hazırlanıyor…" : "Önizle"}
          </button>

          {phase.kind === "previewing" && (
            <p role="status" aria-live="polite">
              Onay planı hazırlanıyor… Bu adımda hiçbir host'a bağlanılmaz.
            </p>
          )}
        </form>
      )}

      {showPlan && (
        <div className="ping-approval" role="group" aria-labelledby={approvalTitleId}>
          <h4 id={approvalTitleId}>Onay bekleyen plan</h4>

          <PingPlanPanel plan={phase.plan} expiresAt={phase.expiresAt} />

          {phase.kind === "preview_ready" && (
            <div className="ping-actions">
              <button
                type="button"
                ref={confirmButtonRef}
                onClick={() => void handleConfirm()}
              >
                Onayla ve Ping Çalıştır
              </button>
              <button type="button" onClick={() => void handleCancel()}>
                Vazgeç
              </button>
            </div>
          )}

          {phase.kind === "confirming" && (
            <p role="status" aria-live="polite">
              Ping çalıştırılıyor… Sonuç gelene kadar bu işlemi tekrarlamayın.
            </p>
          )}

          {phase.kind === "canceling" && (
            <p role="status" aria-live="polite">
              Önizleme iptal ediliyor…
            </p>
          )}
        </div>
      )}

      {phase.kind === "result" && (
        <PingResultPanel run={phase.run} onStartOver={startOver} />
      )}

      {phase.kind === "error" && (
        <PingErrorPanel notice={phase.notice} onStartOver={startOver} headingLevel={4} />
      )}
    </section>
  );
}
