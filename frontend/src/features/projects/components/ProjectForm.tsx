import { useId, useState } from "react";
import type { FormEvent } from "react";

import { ControllerPathDialog } from "../../../components/ControllerPathDialog";
import type { CreateProjectRequest } from "../types";

/** Backend model sınırlarıyla aynı (`app/models/project.py`). */
const NAME_MAX_LENGTH = 200;
const PATH_MAX_LENGTH = 1024;
const DESCRIPTION_MAX_LENGTH = 2000;

type FieldName = "name" | "path" | "description";
type FieldErrors = Partial<Record<FieldName, string>>;

interface ProjectFormProps {
  onSubmit: (request: CreateProjectRequest) => void;
  /** İstek sürerken alanlar ve buton kilitlenir. */
  isSubmitting: boolean;
}

/**
 * Yeni project kaydı formu.
 *
 * Doğrulama iki katmanlıdır: burada anında geri bildirim verilir, nihai karar
 * her zaman backend'e aittir. İstemci kontrolü bir güvenlik sınırı değildir.
 */
export function ProjectForm({ onSubmit, isSubmitting }: ProjectFormProps) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [description, setDescription] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [browserOpen, setBrowserOpen] = useState(false);

  const nameId = useId();
  const pathId = useId();
  const descriptionId = useId();
  const pathHintId = useId();

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    // Çift gönderim koruması: mutation sürerken yeni istek başlatılmaz.
    // Buton zaten `disabled`; bu, submit olayının doğrudan tetiklendiği
    // durumlar için ikinci katmandır.
    if (isSubmitting) {
      return;
    }

    const found = validate({ name, path, description });
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }

    const trimmedDescription = description.trim();
    onSubmit({
      name: name.trim(),
      path: path.trim(),
      ...(trimmedDescription === "" ? {} : { description: trimmedDescription }),
    });
  }

  return (
    <form onSubmit={handleSubmit} noValidate>
      <div className="field">
        <label htmlFor={nameId}>Project adı</label>
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
        <label htmlFor={pathId}>Controller'daki dizin yolu</label>
        <p className="field__hint" id={pathHintId}>
          Bu yol, DORAnsible controller'ında (backend servisinin çalıştığı makinede)
          bulunan, Ansible dosyalarını içeren dizinin tam (mutlak) yoludur. Controller
          ile bu sayfayı açtığınız tarayıcı cihazı aynı makineyse bu, kendi
          bilgisayarınızdaki bir klasör de olabilir. Örnek: <code>/srv/ansible/web</code>{" "}
          veya <code>C:\ansible\projeler\web</code>. Dizin oluşturulmaz veya
          kopyalanmaz; yalnızca kaydı tutulur.
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
        scope="project"
        onSelect={(selectedPath) => {
          setPath(selectedPath);
          setBrowserOpen(false);
        }}
        onCancel={() => setBrowserOpen(false)}
      />

      <div className="field">
        <label htmlFor={descriptionId}>Açıklama (isteğe bağlı)</label>
        <textarea
          id={descriptionId}
          name="description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          disabled={isSubmitting}
          aria-invalid={errors.description ? true : undefined}
          aria-describedby={errors.description ? `${descriptionId}-error` : undefined}
          rows={3}
          maxLength={DESCRIPTION_MAX_LENGTH}
        />
        {errors.description && (
          <p className="field__error" id={`${descriptionId}-error`} role="alert">
            {errors.description}
          </p>
        )}
      </div>

      <button type="submit" disabled={isSubmitting}>
        {isSubmitting ? "Kaydediliyor…" : "Project'i kaydet"}
      </button>
    </form>
  );
}

function validate(values: { name: string; path: string; description: string }): FieldErrors {
  const errors: FieldErrors = {};

  if (values.name.trim() === "") {
    errors.name = "Project adı zorunludur.";
  } else if (values.name.trim().length > NAME_MAX_LENGTH) {
    errors.name = `Project adı en fazla ${NAME_MAX_LENGTH} karakter olabilir.`;
  }

  const trimmedPath = values.path.trim();
  if (trimmedPath === "") {
    errors.path = "Controller'daki dizin yolu zorunludur.";
  } else if (trimmedPath.length > PATH_MAX_LENGTH) {
    errors.path = `Dizin yolu en fazla ${PATH_MAX_LENGTH} karakter olabilir.`;
  }

  if (values.description.trim().length > DESCRIPTION_MAX_LENGTH) {
    errors.description = `Açıklama en fazla ${DESCRIPTION_MAX_LENGTH} karakter olabilir.`;
  }

  return errors;
}
