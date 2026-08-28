import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { StatusMessage } from "../../../components/StatusMessage";
import type { ExecutionMode } from "../../../lib/executionMode";
import { formatDateTime } from "../../../lib/format";
import { ErrorNoticePanel } from "../../projects/components/ErrorNoticePanel";
import type { ErrorNotice } from "../../projects/errorMessages";
import type { PlaybookEntry, Project } from "../../projects/types";
import { describeExecutionPlanError } from "../errorMessages";
import {
  useCreateExecutionPlan,
  useLaunchExecution,
  usePrepareExecutionPlan,
  useProjectInventories,
} from "../hooks";
import type { ExecutionPlan } from "../types";
import { ExecutionPlanPanel } from "./ExecutionPlanPanel";

/**
 * Check mode onay metni (R1-V3D3'ten değişmeden korunur).
 *
 * Metin, check mode'un yan etkisizlik garantisi vermediğini açıkça söyler
 * (bkz. `ExecutionPlanPanel` docstring'i — R0 ölçümünde `check_mode: false`
 * taşıyan bir task'ın check altında gerçekten çalıştığı görülmüştür).
 */
const CHECK_APPROVAL_LABEL =
  "Yukarıdaki project, inventory, playbook ve hedef host'ları " +
  "inceledim. Check mode değişiklik yapılmayacağını garanti etmez; bu " +
  "çalıştırmayı açıkça onaylıyorum.";

/**
 * Normal mode onay metni (R1-V3H2B).
 *
 * Check'in onay metninden bilinçli olarak farklıdır: normal mode bir deneme
 * değildir, hedefte gerçekten değişiklik uygular ve otomatik geri alma yoktur.
 * Kullanıcının onayladığı şey check'teki "garanti vermez" ihtiyatı değil, bu
 * doğrudan sonuçtur.
 */
const NORMAL_APPROVAL_LABEL =
  "Yukarıdaki project, inventory, playbook ve hedef host'ları inceledim. " +
  "Bu normal mode çalıştırması hedefte gerçek değişiklik uygulayacak; " +
  "bağlantı kesilebilir ve hata ya da zaman aşımı sonrasında otomatik bir " +
  "geri alma olmadığını biliyorum. Bu çalıştırmayı açıkça onaylıyorum.";

/**
 * Plan akışının mantıksal durumları.
 *
 * Ayrık birleşim bilinçlidir: "yükleniyor" ile "hazır plan" aynı anda açık
 * olamaz ve bayat bir plan hata durumunun altında sessizce kalamaz.
 */
type PlanPhase =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; plan: ExecutionPlan }
  | { kind: "error"; notice: ErrorNotice };

/**
 * Hazırlama akışının durumları (R1-V2).
 *
 * ``ready`` bilinçli olarak **token taşımaz**: gösterilecek alanlar burada,
 * token ise yalnızca bir ref'te durur. Böylece token'ın ekrana düşmesi bir
 * dikkatsizlik meselesi olmaktan çıkar, yapısal olarak imkânsızlaşır.
 */
type PreparePhase =
  | { kind: "idle" }
  | { kind: "preparing" }
  | { kind: "ready"; plan: ExecutionPlan; expiresAt: string; digest: string }
  | { kind: "error"; notice: ErrorNotice };

/**
 * Çalıştırmaya alma akışının durumları (R1-V3D3).
 *
 * Hazırlanmış plan ile aynı sebeple `idle`/`launching`/`error` ayrıktır: bir
 * önceki launch hatası, yeni bir hazırlığın üstünde sessizce kalamaz.
 */
type LaunchPhase = { kind: "idle" } | { kind: "launching" } | { kind: "error"; notice: ErrorNotice };

/** Kullanıcıya gösterilen kısa manifest parmak izi. */
const DISPLAY_FINGERPRINT_LENGTH = 12;

interface ExecutionPlanSectionProps {
  project: Project;
  /** Aynı sayfada keşfedilmiş playbook'lar; ikinci bir keşif isteği atılmaz. */
  playbooks: PlaybookEntry[];
}

/**
 * Check/normal mode execution plan önizleme, onaya hazırlama ve çalıştırma
 * akışı (R1-V1, R1-V2, R1-V3D3, R1-V3H2B).
 *
 * Kip seçimi (varsayılan `check`) formun bir parçasıdır; "Onayla ve Çalıştır"
 * (normal'de "Onayla ve Gerçek Değişiklikleri Uygula") yalnızca **hazırlanmış**
 * bir plan, açık kullanıcı onayı (checkbox) ve uçuşta başka bir launch yokken
 * tıklanabilir. `window.confirm` kullanılmaz: onay, testlenebilir ve
 * erişilebilir bir form kontrolüdür. Onay metni ve risk uyarısı kipe göre
 * değişir — normal mode bir deneme değildir ve bunu gizlemez. Başarılı
 * çalıştırma isteği `/jobs/{job_id}` sayfasına yönlendirir; hata durumunda
 * token yeniden kullanılmaz ve kullanıcı planı yeniden hazırlamak zorundadır.
 *
 * Dört davranış kuralı vardır:
 *
 * 1. **Seçim değiştiğinde plan, hazırlık, onay ve launch durumu temizlenir.**
 *    Ekranda kalan bir plan, artık seçili olmayan girdilerin özeti olurdu;
 *    kullanıcı okuduğu şeyle seçtiği şeyin aynı olduğuna güvenebilmelidir. Kip
 *    değişimi de inventory/playbook değişimiyle **aynı** kuralı izler.
 * 2. **Çift tıklama koruması senkrondur.** `disabled` yalnızca bir sonraki
 *    render'da etkili olur; bu yüzden her handler ilk iş olarak senkron bir
 *    kilit alır.
 * 3. **Prepare gövdesi önizlenen planın, launch gövdesi hazırlanmış planın
 *    kendi alanlarından kurulur.** Form state'i seçimden sonra değişmiş
 *    olabilir; gövdedeki `mode`/`inventory_id`/`playbook_path` bu yüzden
 *    sırasıyla `phase.plan` ve `prepare.plan`'dan okunur, o an formda seçili
 *    olandan değil.
 * 4. **Preview gövdesi tek istisnadır.** Preview henüz dondurulmuş bir plan
 *    üretmediği için form state'inin kendisidir; `mode` burada güncel seçimden
 *    okunur.
 */
export function ExecutionPlanSection({ project, playbooks }: ExecutionPlanSectionProps) {
  const [inventoryId, setInventoryId] = useState("");
  const [playbookPath, setPlaybookPath] = useState("");
  /**
   * Çalıştırma kipi (R1-V3H2B).
   *
   * Varsayılan `check`'tir. Bu yalnızca **form** state'idir: preview isteği
   * buradan okunur, ama prepare ve launch istekleri sırasıyla önizlenen ve
   * hazırlanmış planın kendi `mode` alanından kurulur (aşağıdaki
   * `handlePrepare`/`handleLaunch`) — form burada seçim değişse bile
   * gövdenin ikinci bir kaynaktan beslenmesini engeller.
   */
  const [mode, setMode] = useState<ExecutionMode>("check");
  const [phase, setPhase] = useState<PlanPhase>({ kind: "idle" });
  const [prepare, setPrepare] = useState<PreparePhase>({ kind: "idle" });
  const [launch, setLaunch] = useState<LaunchPhase>({ kind: "idle" });
  /**
   * Açık kullanıcı onayı (R1-V3D3).
   *
   * Varsayılan olarak işaretsizdir ve yalnızca hazırlanmış planın kendi
   * checkbox'ı ile açılır; `window.confirm` kullanılmaz.
   */
  const [approved, setApproved] = useState(false);

  const inFlightRef = useRef(false);
  const prepareInFlightRef = useRef(false);
  const launchInFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const navigate = useNavigate();
  /**
   * Tek kullanımlık plan token'ı.
   *
   * Yalnızca bileşen belleğindedir: state değildir (render'a giremez),
   * localStorage/sessionStorage/URL'ye yazılmaz ve query cache'ine konmaz.
   * Sayfa yenilenince kaybolur; sunucu TTL ile zaten temizler.
   */
  const planTokenRef = useRef<string | null>(null);
  /**
   * Hazırlama nesli.
   *
   * Seçim değiştiğinde, yeni bir önizleme başladığında ve unmount'ta senkron
   * olarak artar. Uçuştaki bir hazırlama isteği başladığı nesli hatırlar; cevap
   * geldiğinde nesil değişmişse **hiçbir** state veya ref'e dokunulmaz.
   *
   * Alanları `disabled` yapmak tek başına yeterli değildir: istek zaten
   * gönderilmiştir ve cevabı sonradan döner. Güncellik bu yüzden cevabın
   * kendisinde doğrulanır — aksi hâlde iptal edilmiş bir hazırlığın token'ı
   * `planTokenRef`'e geri yazılabilir ve ekranda artık seçili olmayan girdilerin
   * dondurulmuş planı belirebilirdi.
   */
  const prepareGenerationRef = useRef(0);

  const inventoryFieldId = useId();
  const playbookFieldId = useId();
  const planTitleId = useId();
  const preparedTitleId = useId();
  const approveFieldId = useId();
  const checkModeFieldId = useId();
  const checkModeHintId = useId();
  const normalModeFieldId = useId();
  const normalModeHintId = useId();

  const inventories = useProjectInventories(project.id, { enabled: project.is_active });
  const createPlan = useCreateExecutionPlan(project.id);
  const preparePlan = usePrepareExecutionPlan(project.id);
  const launchExecution = useLaunchExecution(project.id);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Uçuştaki hazırlama artık hiçbir şeye yazamaz.
      prepareGenerationRef.current += 1;
      planTokenRef.current = null;
    };
  }, []);

  /**
   * Hazırlanmış state'i ve token'ı bellekten düşürür.
   *
   * Nesil de burada artar: bu çağrıdan **önce** başlamış bir hazırlama isteği
   * artık bayattır ve cevabı yok sayılır.
   */
  function discardPrepared() {
    prepareGenerationRef.current += 1;
    planTokenRef.current = null;
    setPrepare({ kind: "idle" });
    // Bir önceki hazırlığa ait onay ve launch durumu da bu hazırlığın parçası
    // sayılır: yeni bir plan, eski onayı devralamaz.
    setLaunch({ kind: "idle" });
    setApproved(false);
  }

  /** Seçim değişti: eski plan artık bu seçimin özeti değil. */
  function selectInventory(value: string) {
    setInventoryId(value);
    setPhase({ kind: "idle" });
    discardPrepared();
  }

  function selectPlaybook(value: string) {
    setPlaybookPath(value);
    setPhase({ kind: "idle" });
    discardPrepared();
  }

  /**
   * Kip değişti: inventory/playbook değişimiyle aynı kural geçerlidir.
   * Ekrandaki önizleme, hazırlanmış plan, token ve onay artık bu seçimin
   * özeti değildir.
   */
  function selectMode(value: ExecutionMode) {
    setMode(value);
    setPhase({ kind: "idle" });
    discardPrepared();
  }

  const selectionComplete = inventoryId !== "" && playbookPath !== "";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Eksik seçimde istek **hiç** gönderilmez; sunucuya anlamsız bir çağrı
    // yapıp 422 beklemek yerine akış burada durur.
    if (inFlightRef.current || !selectionComplete) {
      return;
    }
    inFlightRef.current = true;
    setPhase({ kind: "loading" });
    // Yeni bir önizleme, eski hazırlığı geçersiz kılar.
    discardPrepared();

    try {
      // Preview gövdesi güncel form seçiminden kurulur: bu aşamada
      // "dondurulmuş" bir plan henüz yoktur.
      const plan = await createPlan({
        mode,
        inventory_id: Number(inventoryId),
        playbook_path: playbookPath,
      });
      if (!mountedRef.current) {
        return;
      }
      setPhase({ kind: "ready", plan });
    } catch (error) {
      if (!mountedRef.current) {
        return;
      }
      setPhase({ kind: "error", notice: describeExecutionPlanError(error) });
    } finally {
      inFlightRef.current = false;
    }
  }

  /**
   * Planı onaya hazırlar (R1-V2).
   *
   * Yalnızca geçerli bir önizleme varken çağrılabilir: hazırlama, kullanıcının
   * **gördüğü** planın dondurulmasıdır. Kilit senkrondur; `disabled` ancak bir
   * sonraki render'da etkili olur.
   *
   * Gövde (R1-V3H2B) form state'inden değil, ekranda gösterilen önizleme
   * planının (`phase.plan`) kendi `mode`/`inventory`/`playbook` alanlarından
   * kurulur. Form seçimi önizlemeden sonra teoride değişmiş olsa bile — ki bu
   * durumda zaten `selectMode`/`selectInventory`/`selectPlaybook` önizlemeyi
   * anında düşürür ve buton kaybolur — hazırlanan şey her zaman kullanıcının
   * okuduğu plandır.
   */
  async function handlePrepare() {
    if (prepareInFlightRef.current || phase.kind !== "ready" || !selectionComplete) {
      return;
    }
    prepareInFlightRef.current = true;
    // İsteğin ait olduğu nesil: cevap döndüğünde hâlâ geçerli mi diye buna
    // bakılır.
    const generation = prepareGenerationRef.current;
    const previewedPlan = phase.plan;
    setPrepare({ kind: "preparing" });

    try {
      const prepared = await preparePlan({
        mode: previewedPlan.mode,
        inventory_id: previewedPlan.inventory.id,
        playbook_path: previewedPlan.playbook.path,
      });
      if (!isCurrent(generation)) {
        // Bayat başarı: token belleğe **geri yazılmaz**, ekran değişmez.
        return;
      }
      // Token ekrana değil belleğe gider; state yalnızca gösterilecek alanları
      // taşır.
      planTokenRef.current = prepared.plan_token;
      setPrepare({
        kind: "ready",
        plan: prepared.plan,
        expiresAt: prepared.expires_at,
        digest: prepared.manifest_digest,
      });
    } catch (error) {
      if (!isCurrent(generation)) {
        // Bayat hata da yeni durumu bozmaz.
        return;
      }
      planTokenRef.current = null;
      setPrepare({ kind: "error", notice: describeExecutionPlanError(error) });
    } finally {
      prepareInFlightRef.current = false;
    }
  }

  /** Cevap, hâlâ geçerli olan hazırlama nesline mi ait. */
  function isCurrent(generation: number): boolean {
    return mountedRef.current && generation === prepareGenerationRef.current;
  }

  /**
   * Hazırlanmış planı çalıştırmaya alır (R1-V3D3).
   *
   * Yalnız hazırlanmış bir plan, açık onay ve uçuşta başka bir launch yokken
   * çağrılabilir. Token ref'ten **istekten önce** okunur ve ref hemen `null`
   * yapılır: başarı veya belirsiz bir ağ hatası sonrasında aynı token bir daha
   * denenmez. Gövdedeki `mode`/`inventory_id`/`playbook_path` hazırlanmış
   * planın kendi alanlarından gelir, o an formda seçili olandan değil.
   */
  async function handleLaunch() {
    if (launchInFlightRef.current || prepare.kind !== "ready" || !approved) {
      return;
    }
    const token = planTokenRef.current;
    if (token === null) {
      return;
    }
    launchInFlightRef.current = true;
    planTokenRef.current = null;
    setLaunch({ kind: "launching" });

    try {
      const launched = await launchExecution({
        plan_token: token,
        mode: prepare.plan.mode,
        inventory_id: prepare.plan.inventory.id,
        playbook_path: prepare.plan.playbook.path,
      });
      if (!mountedRef.current) {
        return;
      }
      navigate(`/jobs/${launched.job_id}`);
    } catch (error) {
      if (!mountedRef.current) {
        return;
      }
      // Token zaten tüketildi/geçersiz sayılıyor: kullanıcı planı yeniden
      // hazırlamalı. `discardPrepared` hazırlanmış paneli kaldırıp önizlemeyi
      // geri getirir; ardından launch hatası ayrıca gösterilir.
      const notice = describeExecutionPlanError(error);
      discardPrepared();
      setLaunch({ kind: "error", notice });
    } finally {
      launchInFlightRef.current = false;
    }
  }

  if (!project.is_active) {
    return (
      <StatusMessage tone="info" title="Pasif project'te plan üretilmez">
        <p>
          Bu project pasif durumda. Plan yalnızca aktif project'ler için üretilebilir.
        </p>
      </StatusMessage>
    );
  }

  return (
    <>
      <p>
        Seçilen kip ve playbook için bir plan üretilir. Bu adımda{" "}
        <strong>hiçbir şey çalıştırılmaz</strong>: hedef host'lara bağlanılmaz, Job
        kaydı ve çıktı oluşmaz. Plan yalnızca ne yapılacağını özetler.
      </p>

      <form onSubmit={(event) => void handleSubmit(event)}>
        <fieldset className="mode-fieldset" disabled={phase.kind === "loading"}>
          <legend>Çalıştırma kipi</legend>

          <div className="mode-option">
            <input
              id={checkModeFieldId}
              type="radio"
              name="execution-mode"
              value="check"
              checked={mode === "check"}
              aria-describedby={checkModeHintId}
              onChange={() => selectMode("check")}
            />
            <label htmlFor={checkModeFieldId}>Check</label>
          </div>
          <p className="field__hint" id={checkModeHintId}>
            Ansible <code>--check</code> kullanır. Bu, hedefte hiçbir değişiklik
            yapılmayacağının garantisi <strong>değildir</strong>.
          </p>

          <div className="mode-option">
            <input
              id={normalModeFieldId}
              type="radio"
              name="execution-mode"
              value="normal"
              checked={mode === "normal"}
              aria-describedby={normalModeHintId}
              onChange={() => selectMode("normal")}
            />
            <label htmlFor={normalModeFieldId}>Normal</label>
          </div>
          <p className="field__hint" id={normalModeHintId}>
            Değişiklikleri hedefte <strong>gerçekten uygular</strong>.
          </p>
        </fieldset>

        <div className="field">
          <label htmlFor={inventoryFieldId}>Inventory</label>
          <InventoryField
            fieldId={inventoryFieldId}
            value={inventoryId}
            disabled={phase.kind === "loading"}
            onChange={selectInventory}
            query={inventories}
          />
        </div>

        <div className="field">
          <label htmlFor={playbookFieldId}>Playbook</label>
          {playbooks.length === 0 ? (
            <p className="muted">
              Bu project'te keşfedilmiş playbook yok. Plan üretmek için project
              kökünde bir playbook bulunmalıdır.
            </p>
          ) : (
            <select
              id={playbookFieldId}
              name="playbook_path"
              value={playbookPath}
              disabled={phase.kind === "loading"}
              onChange={(event) => selectPlaybook(event.target.value)}
            >
              <option value="">Seçiniz…</option>
              {playbooks.map((playbook) => (
                <option key={playbook.path} value={playbook.path}>
                  {playbook.path}
                </option>
              ))}
            </select>
          )}
        </div>

        <button type="submit" disabled={!selectionComplete || phase.kind === "loading"}>
          {phase.kind === "loading" ? "Plan hazırlanıyor…" : "Planı Oluştur"}
        </button>

        {phase.kind === "loading" && (
          <p role="status" aria-live="polite">
            Plan hazırlanıyor… Bu adımda hiçbir host'a bağlanılmaz.
          </p>
        )}
      </form>

      {/*
       * Hazırlanmış plan varken bilgilendirici önizleme **gösterilmez**: iki
       * plan yan yana durursa hangisinin dondurulmuş olduğu belirsizleşir ve
       * kullanıcı, onayladığını sandığı içerikten başkasını okuyabilir.
       */}
      {phase.kind === "ready" && prepare.kind !== "ready" && (
        <div className="plan-preview" role="group" aria-labelledby={planTitleId}>
          <h4 id={planTitleId}>Execution planı</h4>

          <ExecutionPlanPanel plan={phase.plan} />

          <div className="plan-actions">
            <button
              type="button"
              onClick={() => void handlePrepare()}
              disabled={prepare.kind === "preparing"}
            >
              {prepare.kind === "preparing" ? "Hazırlanıyor…" : "Onaya Hazırla"}
            </button>
          </div>

          {prepare.kind === "preparing" && (
            <p role="status" aria-live="polite">
              Plan onaya hazırlanıyor… Bu adımda da hiçbir şey çalıştırılmaz.
            </p>
          )}
        </div>
      )}

      {prepare.kind === "ready" && (
        <div className="plan-prepared" role="group" aria-labelledby={preparedTitleId}>
          <h4 id={preparedTitleId}>Plan onaya hazır</h4>
          <p>
            Aşağıdaki plan, sunucuda dondurulmuş bir kopyadan üretildi. Project
            dosyaları bundan sonra değişse bile onaya hazırlanan içerik değişmez.
          </p>

          <dl>
            <dt>Geçerlilik</dt>
            <dd>{formatDateTime(prepare.expiresAt)} tarihine kadar</dd>

            <dt>İçerik parmak izi</dt>
            <dd>
              <code>{prepare.digest.slice(0, DISPLAY_FINGERPRINT_LENGTH)}</code>
            </dd>
          </dl>

          <ExecutionPlanPanel plan={prepare.plan} />

          {prepare.plan.mode === "normal" && (
            <StatusMessage tone="warning" title="Bu bir deneme değildir" headingLevel={4}>
              <p>
                Normal mode hedefte <strong>gerçek değişiklik</strong> uygular: dosya,
                paket veya servis durumu değişebilir ve bağlantı kesilebilir. Bir zaman
                aşımı veya hata sonrasında hedefte <strong>kısmi bir değişiklik</strong>{" "}
                kalmış olabilir; <strong>otomatik bir rollback yoktur</strong>.
              </p>
            </StatusMessage>
          )}

          <div className="confirm">
            <label htmlFor={approveFieldId}>
              <input
                id={approveFieldId}
                type="checkbox"
                checked={approved}
                disabled={launch.kind === "launching"}
                onChange={(event) => setApproved(event.target.checked)}
              />{" "}
              {prepare.plan.mode === "normal" ? NORMAL_APPROVAL_LABEL : CHECK_APPROVAL_LABEL}
            </label>
          </div>

          <div className="plan-actions">
            <button
              type="button"
              onClick={() => void handleLaunch()}
              disabled={!approved || launch.kind === "launching"}
            >
              {launch.kind === "launching"
                ? "Çalıştırılıyor…"
                : prepare.plan.mode === "normal"
                  ? "Onayla ve Gerçek Değişiklikleri Uygula"
                  : "Onayla ve Çalıştır"}
            </button>
          </div>

          {launch.kind === "launching" && (
            <p role="status" aria-live="polite">
              Çalıştırma kuyruğa alınıyor…
            </p>
          )}
        </div>
      )}

      {prepare.kind === "error" && (
        <StatusMessage tone="error" title={prepare.notice.title} headingLevel={4}>
          <p>{prepare.notice.message}</p>
        </StatusMessage>
      )}

      {launch.kind === "error" && (
        <StatusMessage tone="error" title={launch.notice.title} headingLevel={4}>
          <p>{launch.notice.message}</p>
          <p>Planı yeniden hazırlayıp tekrar deneyin.</p>
        </StatusMessage>
      )}

      {phase.kind === "error" && (
        <StatusMessage tone="error" title={phase.notice.title} headingLevel={4}>
          <p>{phase.notice.message}</p>
        </StatusMessage>
      )}
    </>
  );
}

interface InventoryFieldProps {
  fieldId: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  query: ReturnType<typeof useProjectInventories>;
}

/**
 * Inventory seçimi.
 *
 * Yalnızca kayıt **adı** gösterilir: inventory kaydının `path` alanı sunucudaki
 * mutlak yoldur ve arayüzde hiç basılmaz.
 */
function InventoryField({ fieldId, value, disabled, onChange, query }: InventoryFieldProps) {
  if (query.isPending) {
    return <p role="status">Inventory listesi yükleniyor…</p>;
  }

  if (query.isError) {
    return <ErrorNoticePanel error={query.error} onRetry={() => void query.refetch()} />;
  }

  if (query.data.length === 0) {
    return (
      <p className="muted">
        Bu project'e bağlı inventory yok. Plan üretmek için önce project'e bağlı bir
        inventory kaydedin; bağımsız inventory'ler bu akışta kullanılamaz.
      </p>
    );
  }

  return (
    <select
      id={fieldId}
      name="inventory_id"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">Seçiniz…</option>
      {query.data.map((inventory) => (
        <option key={inventory.id} value={String(inventory.id)}>
          {inventory.name}
        </option>
      ))}
    </select>
  );
}
