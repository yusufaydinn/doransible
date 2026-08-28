import { useEffect, useId, useRef, useState } from "react";

import { apiFetch, ApiError } from "../lib/apiClient";

/**
 * Controller path browse dialogu (R1-V3J0C).
 *
 * Bu bir upload veya tarayıcının native `input[type=file]` özelliği
 * **değildir**: tarayıcı cihazının dosya sistemi hiç okunmaz. Yalnızca
 * `GET /api/controller-paths` üzerinden controller'daki izinli yollar
 * listelenir; dosya oluşturulmaz, yüklenmez, kopyalanmaz, silinmez veya
 * düzenlenmez.
 *
 * Erişilebilirlik yeni bir modal sistemi kurularak değil, tarayıcının native
 * `<dialog>` elemanıyla sağlanır: `showModal()` odağı otomatik olarak
 * dialog içine hapseder ve Escape'i kendiliğinden `close` event'ine çevirir.
 * Bu yüzden ek bir focus-trap veya klavye kütüphanesi gerekmez (yeni
 * dependency eklenmez).
 *
 * Geri navigasyonu backend'den gelen bir `parent_path` alanına değil, bu
 * bileşenin kendi tuttuğu küçük bir yığına (`stack`) dayanır: backend
 * yalnızca "bu dizinin doğrudan çocukları" sorusuna cevap verir, breadcrumb
 * hesaplamaz (R1-V3J0C kapsam kararı).
 *
 * **Stale response koruması (AUDIT-FIX1 bulgu 2).** Her `load()` çağrısı bir
 * önceki isteği hem `AbortController` ile iptal eder hem de monoton bir
 * `requestId` üretir; yalnızca çağrı anında **güncel** olan `requestId`'ye
 * sahip cevap state'e yazılır. Bu, iki ayrı riski birlikte kapatır:
 *
 * 1. Kullanıcı hızlıca bir dizine girip geri dönerse veya `scope`/`projectId`
 *    değişirse, geç gelen eski bir cevap güncel görünümü **ezemez**.
 * 2. İptal edilmiş/stale bir istek (`fetch`'in `AbortError`'ı bile) hiçbir
 *    zaman kullanıcıya "network error" olarak gösterilmez — yalnızca
 *    sessizce yok sayılır.
 *
 * Seçim (`selected`) her `load()` başında **hemen** `null`'a döner; böylece
 * yükleme veya hata sırasında "Seç" eski bir path ile kullanılamaz.
 */

export type ControllerPathScope = "project" | "inventory" | "project_inventory";
type EntryKind = "directory" | "file";

interface ControllerPathEntry {
  name: string;
  path: string;
  kind: EntryKind;
  selectable: boolean;
}

interface ControllerPathBrowseResponse {
  scope: ControllerPathScope;
  current_path: string | null;
  target_kind: EntryKind;
  entries: ControllerPathEntry[];
  truncated: boolean;
}

/**
 * `GET /api/controller-paths` çağrısı.
 *
 * Bu dialog, uygulamadaki bu endpoint'in **tek** çağıranıdır; bu yüzden URL
 * kurgusu (`features/*'/api.ts` düzeninin aksine) burada, tek erişim
 * noktasında tutulur.
 *
 * `signal` çağıranın `AbortController`'ından gelir; eski bir navigasyonun
 * isteği burada gerçekten iptal edilir (yalnızca sonucu yok saymak değil).
 */
function fetchControllerPaths(
  params: {
    scope: ControllerPathScope;
    projectId?: number | null;
    path?: string | null;
  },
  signal: AbortSignal,
): Promise<ControllerPathBrowseResponse> {
  const query = new URLSearchParams({ scope: params.scope });
  if (params.projectId != null) {
    query.set("project_id", String(params.projectId));
  }
  if (params.path != null) {
    query.set("path", params.path);
  }
  return apiFetch<ControllerPathBrowseResponse>(`/api/controller-paths?${query.toString()}`, {
    signal,
  });
}

/**
 * Backend hata zarfını sabit, sanitize edilmiş bir kullanıcı mesajına çevirir.
 *
 * Ham backend mesajı, exception metni, gönderilen path veya `details` nesnesi
 * **hiçbir koşulda** gösterilmez; yalnızca tip korumasından geçen `code`
 * alanına göre önceden yazılmış sabit metinler kullanılır.
 */
function describeControllerPathError(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "Beklenmeyen bir hata oluştu.";
  }

  switch (error.code) {
    case "network_error":
      return "Backend'e ulaşılamadı. Bağlantıyı kontrol edin.";
    case "path_not_allowed":
      return "Bu konuma izin verilmiyor.";
    case "path_not_found":
    case "path_not_a_directory":
      return "Bu yol artık bulunamıyor ya da bir dizin değil.";
    case "browse_directory_unreadable":
      return "Bu dizin okunamadı.";
    case "project_inactive":
      return "Seçili project pasif durumda.";
    case "not_found":
      return "Seçili project bulunamadı.";
    case "browse_invalid_scope":
      return "Bu tarama isteği geçersiz.";
    default:
      return "Bu konum listelenemedi.";
  }
}

/**
 * Yüklenmiş bir görünümün "nerede olduğumu ve neyi seçtiğimi" özetini tek
 * satırda üretir (AUDIT-FIX1 bulgu 3). Breadcrumb kurulmaz; yalnızca mevcut
 * `current_path` ve varsa `selected` düz metin olarak yazılır.
 *
 * `project` scope'unda açık dizinin kendisi otomatik seçilidir
 * (`selected === currentPath`); bu durum "Konum ve seçili klasör" ile ayrı
 * ayrı belirsiz iki path göstermek yerine tek, açık bir ifadeye indirgenir.
 */
function describeCurrentSelection(currentPath: string | null, selected: string | null): string {
  if (currentPath === null) {
    return "İzinli köklerden birini seçin.";
  }
  if (selected === null) {
    return `Konum: ${currentPath} — henüz seçim yok.`;
  }
  if (selected === currentPath) {
    return `Konum ve seçili klasör: ${currentPath}`;
  }
  return `Konum: ${currentPath} · Seçili: ${selected}`;
}

type LoadState =
  | { status: "loading" }
  | { status: "loaded"; response: ControllerPathBrowseResponse }
  | { status: "error"; message: string };

const SCOPE_COPY: Record<ControllerPathScope, { title: string; hint: string }> = {
  project: {
    title: "Bir klasör seçin",
    hint: "Yalnızca controller'daki izinli project dizinleri gösterilir.",
  },
  inventory: {
    title: "Bir dosya seçin",
    hint: "Yalnızca controller'daki izinli inventory dizinleri gösterilir.",
  },
  project_inventory: {
    title: "Bir dosya seçin",
    hint: "Yalnızca seçili project'in kendi dizini altında gezinilebilir.",
  },
};

interface ControllerPathDialogProps {
  /** Dialog açık mı. Açma/kapama kararı ve state'i çağırana aittir. */
  open: boolean;
  scope: ControllerPathScope;
  /** Yalnızca `scope === "project_inventory"` için anlamlıdır. */
  projectId?: number | null;
  /** Kullanıcı bir satırı onayladığında **tam olarak bir kez** çağrılır. */
  onSelect: (path: string) => void;
  /** İptal, Escape veya native kapanma. Idempotenttir. */
  onCancel: () => void;
}

export function ControllerPathDialog({
  open,
  scope,
  projectId = null,
  onSelect,
  onCancel,
}: ControllerPathDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [stack, setStack] = useState<Array<string | null>>([null]);
  const [selected, setSelected] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const titleId = useId();
  const hintId = useId();

  /**
   * Aktif isteğin kimliği. `abortControllerRef`'in kendisi değil, **hangi**
   * isteğin hâlâ geçerli sayıldığı bu sayaçla belirlenir: `AbortController`
   * gerçek ağ isteğini iptal eder, `requestIdRef` ise geç gelen bir cevabı
   * (abort her zaman anında etkili olmayabilir; test ortamında hiç
   * etkili değildir) state'e yazılmaktan **ayrıca** korur.
   */
  const requestIdRef = useRef(0);
  const abortControllerRef = useRef<AbortController | null>(null);

  /**
   * Aktif isteği geçersiz kılar: gerçek ağ isteğini iptal eder **ve**
   * `requestIdRef`'i ilerletir. İkincisi olmadan yalnızca `abort()` yeterli
   * değildir — abort edilmiş bir isteğin `catch` bloğu yine de çalışabilir
   * (ör. test ortamında hiç çalışmaz) ve o zaman eski isteği "network error"
   * olarak göstermiş olurduk; hâlbuki bu bizim **kendi** iptalimizdir, gerçek
   * bir ağ arızası değil.
   */
  function invalidateInFlightRequest() {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    requestIdRef.current += 1;
  }

  function load(path: string | null) {
    invalidateInFlightRequest();
    const controller = new AbortController();
    abortControllerRef.current = controller;
    const requestId = requestIdRef.current;

    // Önceki seçim **hemen** geçersiz kılınır — yükleme veya sonraki hata
    // sırasında "Seç" hiçbir zaman eski bir path ile kullanılamaz.
    setSelected(null);
    setState({ status: "loading" });

    fetchControllerPaths({ scope, projectId, path }, controller.signal)
      .then((response) => {
        if (requestIdRef.current !== requestId) {
          // Bu cevap artık en güncel istek değil (ör. kullanıcı bu arada
          // başka bir dizine girdi, dialogu kapattı veya scope/project
          // değişti); sessizce yok sayılır.
          return;
        }
        setState({ status: "loaded", response });
        // Dizin scope'unda az önce açılan dizinin kendisi kullanılabilir bir
        // seçimdir; dosya scope'unda ise navigasyon bir dosyayı "seçmez",
        // önceki seçim yeni dizinle ilgisiz kalır.
        setSelected(response.target_kind === "directory" ? response.current_path : null);
      })
      .catch((error: unknown) => {
        if (requestIdRef.current !== requestId) {
          // Stale veya bizim kendi iptalimiz — kullanıcıya asla "network
          // error" olarak gösterilmez.
          return;
        }
        setState({ status: "error", message: describeControllerPathError(error) });
      });
  }

  // Dialog açıldığında ve açıkken scope/project değişirse yığın ve önceki sonuç sıfırlanır.
  useEffect(() => {
    if (!open) {
      // Kapanışta aktif istek geçersizleştirilir; geç gelen bir cevap kapalı
      // bir dialogun state'ini güncelleyemez.
      invalidateInFlightRequest();
      return;
    }
    setStack([null]);
    load(null);
    // `load` bağımlılık listesinde bilinçli olarak yoktur: her render'da
    // yeniden üretilen düz bir fonksiyondur ve yalnızca prop'lardan
    // (`scope`/`projectId`, ikisi de zaten burada izleniyor) ve ref'lerden
    // (render'dan bağımsız, stabil) okur; ayrıca bir state değişkeni değildir.
  }, [open, scope, projectId]);

  // Bileşen gerçekten unmount olursa (bugün ProjectForm/InventoryForm
  // dialogu her zaman `open` prop'uyla kontrollü tutar, hiç kaldırmaz) aktif
  // istek yine de geçersiz kılınır; savunma amaçlıdır.
  useEffect(() => {
    return () => {
      invalidateInFlightRequest();
    };
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    // `showModal`/`close` yalnızca gerçek tarayıcılarda **modal** davranışı
    // (backdrop, focus-trap, Escape'in native `close` event'ine çevrilmesi)
    // sağlar. jsdom bu ikisini bu yazıda hiç uygulamıyor; `open` attribute'unu
    // elle basmak yalnızca test ortamında içeriğin sorgulanabilir kalmasını
    // sağlayan bir düşüş (fallback) dalıdır ve üretimde hiç çalışmaz.
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }
  }, [open]);

  function enter(path: string) {
    setStack((previous) => [...previous, path]);
    load(path);
  }

  /**
   * Tek bir "Geri" tıklaması **tam olarak bir** browse isteği üretir
   * (R1-V3J0CF2).
   *
   * Önceki görünüm burada, olay işleyicisinin kendisinde hesaplanır; `load`
   * bir `setStack` updater'ının **içinden** çağrılmaz. State updater'ları saf
   * olmak zorundadır: uygulama `main.tsx`'te `<StrictMode>` kullanır ve React
   * development modunda updater'ı bilinçli olarak iki kez çağırır — yan etki
   * orada dururken tek tıklama iki istek, iki abort ve iki `requestId`
   * sıçraması üretiyordu.
   *
   * `stack`'i closure'dan okumak güvenlidir: `goBack` yalnızca güncel
   * render'ın onClick'inden çağrılır ve her tıklama arasında commit edilmiş
   * yeni bir render vardır.
   */
  function goBack() {
    if (stack.length <= 1) {
      // Kökteyiz: buton zaten `disabled`, Geri bir no-op'tur.
      return;
    }
    const next = stack.slice(0, -1);
    // `stack.length > 1` az önce doğrulandı, yani `next` en az bir eleman
    // taşır; `?? null` yalnızca `noUncheckedIndexedAccess`'i tatmin eder,
    // gerçek bir `undefined` durumu temsil etmez.
    const previousPath = next[next.length - 1] ?? null;
    setStack(next);
    load(previousPath);
  }

  function confirmSelection() {
    if (selected !== null) {
      onSelect(selected);
    }
  }

  const copy = SCOPE_COPY[scope];

  return (
    <dialog
      ref={dialogRef}
      className="path-dialog"
      aria-labelledby={titleId}
      aria-describedby={hintId}
      onClose={onCancel}
    >
      <h2 id={titleId} className="path-dialog__title">
        {copy.title}
      </h2>
      <p id={hintId} className="path-dialog__hint">
        {copy.hint} İhtiyacınız olan yol burada yoksa dialogu kapatıp alana elle yazabilirsiniz.
      </p>

      {state.status === "loading" && <p role="status">Yükleniyor…</p>}

      {state.status === "error" && (
        <p role="alert" className="path-dialog__error">
          {state.message}
        </p>
      )}

      {state.status === "loaded" && (
        <>
          <p className="path-dialog__location" aria-live="polite">
            {describeCurrentSelection(state.response.current_path, selected)}
          </p>

          {state.response.entries.length === 0 ? (
            <p className="path-dialog__empty">Bu dizin boş.</p>
          ) : (
            <ul className="path-dialog__list">
              {state.response.entries.map((entry) => (
                <li key={entry.path} className="path-dialog__row">
                  <button
                    type="button"
                    className="path-row"
                    disabled={!entry.selectable}
                    aria-pressed={selected === entry.path}
                    onClick={() => entry.selectable && setSelected(entry.path)}
                  >
                    {entry.kind === "directory" ? "📁" : "📄"} {entry.name}
                  </button>
                  {entry.kind === "directory" && (
                    <button type="button" onClick={() => enter(entry.path)}>
                      Aç
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}

          {state.response.truncated && (
            <p className="path-dialog__truncated">
              Liste kısaltıldı; aradığınız burada değilse tam yolu elle girin.
            </p>
          )}
        </>
      )}

      <div className="path-dialog__footer">
        <button type="button" onClick={goBack} disabled={stack.length <= 1}>
          Geri
        </button>
        <button type="button" onClick={confirmSelection} disabled={selected === null}>
          Seç
        </button>
        <button type="button" onClick={onCancel}>
          İptal
        </button>
      </div>
    </dialog>
  );
}
