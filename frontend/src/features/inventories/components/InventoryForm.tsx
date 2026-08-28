import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";

import { ControllerPathDialog } from "../../../components/ControllerPathDialog";
import type { Project } from "../../projects/types";
import { SOURCE_TYPE_LABELS } from "../labels";
import type { CreateInventoryRequest, InventorySourceType } from "../types";

/** Backend model sınırlarıyla aynı (`app/schemas/inventory.py`). */
const NAME_MAX_LENGTH = 200;
const PATH_MAX_LENGTH = 1024;

/** Bağımsız (project'e bağlı olmayan) kaydı temsil eden form değeri. */
const STANDALONE_VALUE = "";

type FieldName = "name" | "path";
type FieldErrors = Partial<Record<FieldName, string>>;

interface InventoryFormProps {
  /** Yalnızca aktif project'ler; pasif project bu forma seçenek olarak gelmez. */
  projects: Project[];
  /**
   * Formun açılışta seçili göstereceği project.
   *
   * Çağıran, bu değerin `projects` içinde gerçekten bulunduğunu önceden
   * doğrulamış olmalıdır (bkz. `NewInventoryPage`). Form bunu yeniden
   * doğrulamaz; yalnızca ilk render'da seçili değeri kurar.
   */
  initialProjectId: number | null;
  /**
   * Gönderim isteğini başlatır ve istek tamamlanana kadar (başarı ya da hata)
   * pending kalan bir `Promise` döner. Çift gönderim kilidi bu Promise'ın
   * settle olmasına bağlıdır; hata durumunu yorumlamak veya göstermek bu
   * bileşenin işi değildir — çağıran (`NewInventoryPage`) mevcut mutation
   * state'i ve `InventoryErrorPanel` üzerinden hatayı gösterir.
   */
  onSubmit: (request: CreateInventoryRequest) => Promise<void>;
  /** İstek sürerken alanlar ve buton kilitlenir. */
  isSubmitting: boolean;
}

/**
 * Sunucuda zaten var olan bir inventory dosyasının kayıt formu.
 *
 * Bu form dosya oluşturmaz, yüklemez veya kopyalamaz; yalnızca sunucudaki
 * mevcut bir dosyanın kaydını tutar. Doğrulama iki katmanlıdır: burada anında
 * geri bildirim verilir, nihai karar (allowlist, symlink, dosya varlığı,
 * project bağı) her zaman backend'e aittir.
 */
export function InventoryForm({
  projects,
  initialProjectId,
  onSubmit,
  isSubmitting,
}: InventoryFormProps) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [sourceType, setSourceType] = useState<InventorySourceType>("ini");
  const [projectSelection, setProjectSelection] = useState(
    initialProjectId === null ? STANDALONE_VALUE : String(initialProjectId),
  );
  const [errors, setErrors] = useState<FieldErrors>({});
  const [browserOpen, setBrowserOpen] = useState(false);

  /**
   * Gözat dialogunun hangi sınırda gezineceği.
   *
   * `projectSelection`'dan **her render'da yeniden türetilir** — ayrı bir
   * state değildir. Dropdown değiştiğinde dialog zaten kapatılır (aşağıdaki
   * `onChange`); açık kalsaydı bile bu değer bir sonraki açılışta güncel
   * seçimi yansıtırdı.
   */
  const activeProjectId = resolveProjectId(projectSelection, projects);
  const browseScope = activeProjectId === null ? "inventory" : "project_inventory";

  /**
   * Senkron çift gönderim kilidi.
   *
   * `isSubmitting` prop'u `mutation.isPending`'in zamanlanmış (React state)
   * yayılımına dayanır: ilk geçerli submit'in `onSubmit`'i çağırması ile bu
   * prop'un `true` olarak render edilmesi arasında kısa bir pencere vardır.
   * Aynı senkron olay çevriminde (ör. art arda iki `submit` event'i) art arda
   * gelen ikinci `handleSubmit` çağrısı bu pencerede `isSubmitting`'i hâlâ
   * `false` görebilir. `useRef` React render'ından bağımsız, anında
   * güncellenen bir değerdir; bu yüzden kilit burada tutulur, prop'ta değil.
   *
   * Kilit, React'in render/effect zamanlamasına göre değil doğrudan
   * `onSubmit`'in döndürdüğü `Promise`'ın gerçek settle anına göre açılır
   * (bkz. `handleSubmit` içindeki `finally`). Daha önce burada dependency
   * array'siz bir `useEffect` ile `isSubmitting` prop'u izlenip kilit
   * açılıyordu; bu, aradaki herhangi bir render (ör. `setErrors` sonrası
   * form-local render, TanStack'in `isPending` yayımı henüz gelmeden) kilidi
   * erken açabildiği için yanlıştı — gerçek isteğin hâlâ sürdüğü bir anda
   * ikinci submit üçüncü bir POST başlatabiliyordu.
   */
  const inFlightRef = useRef(false);

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // Çift gönderim koruması. `isSubmitting` prop'u henüz güncel olmasa bile
    // `inFlightRef` senkron olarak ikinci event'i durdurur.
    if (isSubmitting || inFlightRef.current) {
      return;
    }

    const found = validate({ name, path });
    setErrors(found);
    if (Object.keys(found).length > 0) {
      // Doğrulama başarısızsa kilit alınmaz: kullanıcı alanı düzeltip hemen
      // tekrar deneyebilmelidir.
      return;
    }

    inFlightRef.current = true;
    onSubmit({
      name: name.trim(),
      path: path.trim(),
      source_type: sourceType,
      project_id: activeProjectId,
    })
      .catch(() => {
        // Hata backend'in ne olduğuna göre burada yorumlanmaz; mevcut
        // mutation state + `InventoryErrorPanel` gösterimi zaten devam eder.
        // Bu `catch` yalnızca unhandled promise rejection oluşmasını önler.
      })
      .finally(() => {
        // Başarı ve hata yollarının ikisinde de kilit gerçek istek
        // tamamlandığında açılır; kullanıcı hatadan sonra hemen yeniden
        // gönderebilir.
        inFlightRef.current = false;
      });
  }

  // Seçili project artık güncel `projects` listesinde yoksa (ör. pasife
  // alındı veya kaldırıldı) seçim standalone'a düşürülür. State'in kendisi
  // değiştiği için bu, yalnızca görünümü değil gönderim anında okunacak
  // değeri de kalıcı olarak düzeltir; project daha sonra listeye yeniden
  // eklense bile kullanıcı yeniden seçmeden eski seçim geri gelmez.
  useEffect(() => {
    if (
      projectSelection !== STANDALONE_VALUE &&
      resolveProjectId(projectSelection, projects) === null
    ) {
      setProjectSelection(STANDALONE_VALUE);
      // Bu, kullanıcının dropdown'ı elle değiştirmesinin dışında sınırı
      // (scope) değiştiren tek yerdir (ör. arka planda `projects` listesi
      // tazelenip seçili project kaybolduğunda). Açık bir Gözat dialogu
      // burada da kapatılmazsa artık geçersiz bir project_inventory
      // sınırının sonucunu göstermeye devam ederdi.
      setBrowserOpen(false);
    }
  }, [projects, projectSelection]);

  const nameId = useId();
  const pathId = useId();
  const pathHintId = useId();
  const sourceTypeId = useId();
  const projectFieldId = useId();

  return (
    <form onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label htmlFor={nameId}>Inventory adı</label>
        <input
          id={nameId}
          name="name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          disabled={isSubmitting}
          aria-invalid={errors.name ? true : undefined}
          aria-describedby={errors.name ? `${nameId}-error` : undefined}
          maxLength={NAME_MAX_LENGTH}
        />
        {errors.name && (
          <p className="field__error" id={`${nameId}-error`} role="alert">
            {errors.name}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor={pathId}>Controller'daki inventory dosya yolu</label>
        <p className="field__hint" id={pathHintId}>
          Bu yol, DORAnsible controller'ında (backend'in ve Ansible süreçlerinin
          çalıştığı makinede) <strong>zaten var olan</strong> bir inventory dosyasını
          gösterir; dosya önceden mevcut olmalıdır. Controller ile bu sayfayı açtığınız
          tarayıcı cihazı aynı makineyse bu, kendi bilgisayarınızdaki bir dosya da
          olabilir. Tam (mutlak) yolunu yazın. Bu ekran dosya oluşturmaz, yüklemez veya
          kopyalamaz — yalnızca kaydını tutar. Bir project'e bağlıyorsanız dosya, seçilen
          project'in kendi dizini altında bulunmalıdır.
        </p>
        <div className="field__path-row">
          <input
            id={pathId}
            name="path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            disabled={isSubmitting}
            aria-invalid={errors.path ? true : undefined}
            aria-describedby={errors.path ? `${pathHintId} ${pathId}-error` : pathHintId}
            maxLength={PATH_MAX_LENGTH}
            spellCheck={false}
          />
          <button type="button" onClick={() => setBrowserOpen(true)} disabled={isSubmitting}>
            Gözat…
          </button>
        </div>
        {errors.path && (
          <p className="field__error" id={`${pathId}-error`} role="alert">
            {errors.path}
          </p>
        )}
      </div>

      <ControllerPathDialog
        open={browserOpen}
        scope={browseScope}
        projectId={activeProjectId}
        onSelect={(selectedPath) => {
          setPath(selectedPath);
          setBrowserOpen(false);
        }}
        onCancel={() => setBrowserOpen(false)}
      />

      <div className="field">
        <label htmlFor={sourceTypeId}>Dosya biçimi</label>
        <select
          id={sourceTypeId}
          name="source_type"
          value={sourceType}
          disabled={isSubmitting}
          onChange={(event) => setSourceType(event.target.value as InventorySourceType)}
        >
          <option value="ini">{SOURCE_TYPE_LABELS.ini}</option>
          <option value="yaml">{SOURCE_TYPE_LABELS.yaml}</option>
        </select>
      </div>

      <div className="field">
        <label htmlFor={projectFieldId}>Bağlı project</label>
        <p className="field__hint">
          Bağımsız (standalone) inventory hiçbir project'e ait değildir. Bir project
          seçerseniz, dosya yolunun o project'in dizini altında olması gerekir.
        </p>
        <select
          id={projectFieldId}
          name="project_id"
          value={projectSelection}
          disabled={isSubmitting}
          onChange={(event) => {
            setProjectSelection(event.target.value);
            // Sınır (scope) değişti: açık bir Gözat dialogu artık **eski**
            // project'in dizin ağacını gösteriyor olurdu. Kapatmak, eski
            // navigasyon durumunun ve sonucunun yeni seçime hiçbir şekilde
            // taşınmamasını garanti eder — dialog her açılışta sıfırdan başlar.
            setBrowserOpen(false);
          }}
        >
          <option value={STANDALONE_VALUE}>Standalone inventory</option>
          {projects.map((project) => (
            <option key={project.id} value={String(project.id)}>
              {project.name}
            </option>
          ))}
        </select>
      </div>

      <button type="submit" disabled={isSubmitting}>
        {isSubmitting ? "Kaydediliyor…" : "Inventory'yi kaydet"}
      </button>
    </form>
  );
}

/**
 * Form seçimini gönderilecek `project_id`'ye çevirir.
 *
 * Seçim yalnızca **güncel** `projects` listesinde gerçekten bulunan bir
 * kayda karşılık geliyorsa sayıya çevrilir; aksi hâlde (standalone seçili
 * ya da seçili project listeden kaldırılmışsa) `null` döner. Böylece eski
 * bir seçim, state'te dursa bile POST gövdesine hiçbir zaman taşınmaz.
 */
function resolveProjectId(selection: string, projects: Project[]): number | null {
  if (selection === STANDALONE_VALUE) {
    return null;
  }
  const parsed = Number(selection);
  return projects.some((project) => project.id === parsed) ? parsed : null;
}

function validate(values: { name: string; path: string }): FieldErrors {
  const errors: FieldErrors = {};

  if (values.name.trim() === "") {
    errors.name = "Inventory adı zorunludur.";
  } else if (values.name.trim().length > NAME_MAX_LENGTH) {
    errors.name = `Inventory adı en fazla ${NAME_MAX_LENGTH} karakter olabilir.`;
  }

  const trimmedPath = values.path.trim();
  if (trimmedPath === "") {
    errors.path = "Controller'daki inventory dosya yolu zorunludur.";
  } else if (trimmedPath.length > PATH_MAX_LENGTH) {
    errors.path = `Dosya yolu en fazla ${PATH_MAX_LENGTH} karakter olabilir.`;
  }

  return errors;
}
