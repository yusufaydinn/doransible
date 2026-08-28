/**
 * Backend HTTP client.
 *
 * API kök adresi build sırasında `VITE_API_BASE_URL` ile değiştirilebilir.
 * Frontend hiçbir zaman komut üretmez; yalnızca yapılandırılmış istek gönderir
 * (MIMARI.md bölüm 2).
 */

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL
).replace(/\/+$/, "");

/** Backend'in standart hata zarfı. */
interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /**
   * Hata zarfının makine tarafından okunabilir ek bilgisi.
   *
   * Bu alan kullanıcıya **ham JSON olarak gösterilmez**; yalnızca tip
   * korumalarından geçirilerek anlamlı bir metne çevrilir
   * (bkz. `features/projects/errorMessages.ts`).
   */
  readonly details: unknown;

  constructor(message: string, status: number, code: string, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/**
 * İsteği gönderir, taşıma ve HTTP hatalarını `ApiError`'a çevirir.
 *
 * Gövdeyi **okumaz**: cevabın nasıl yorumlanacağı çağıranın sözleşmesidir.
 * Böylece JSON dönen ve gövdesiz (`204`) dönen endpoint'ler aynı hata
 * dönüşümünü paylaşır, ama hiçbiri diğerinin gövde varsayımını taşımaz.
 */
async function requestOk(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(
      `Backend'e ulaşılamadı (${API_BASE_URL}). Servisin çalıştığını doğrulayın.`,
      0,
      "network_error",
    );
  }

  if (!response.ok) {
    throw await toApiError(response);
  }

  return response;
}

/** JSON gövdeli POST isteğinin `RequestInit` karşılığı. */
function jsonBodyInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** Backend'e JSON isteği gönderir ve hataları `ApiError`'a çevirir. */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await requestOk(path, init);
  return (await response.json()) as T;
}

/** JSON gövdeli POST isteği gönderir. */
export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, jsonBodyInit("POST", body));
}

/**
 * Gövdesiz cevap dönen POST isteği gönderir (`204 No Content`).
 *
 * Cevap gövdesi **hiç okunmaz**: `204` üzerinde `response.json()` çağırmak
 * ayrıştırma hatası üretir ve başarılı bir işlemi başarısız gösterirdi. Dönüş
 * tipi bu yüzden `void`'dir; `undefined as T` gibi bir kestirme, JSON dönen
 * çağrıların tip güvenliğini de zayıflatırdı.
 *
 * HTTP hataları `apiPost` ile aynı `ApiError` dönüşümünden geçer.
 */
export async function apiPostNoContent(path: string, body: unknown): Promise<void> {
  await requestOk(path, jsonBodyInit("POST", body));
}

/** DELETE isteği gönderir; backend güncel kaydı gövdede döndürür. */
export function apiDelete<T>(path: string): Promise<T> {
  return apiFetch<T>(path, { method: "DELETE" });
}

async function toApiError(response: Response): Promise<ApiError> {
  const fallback = `İstek başarısız oldu (HTTP ${response.status}).`;

  try {
    const body = (await response.json()) as Partial<ApiErrorEnvelope>;
    const error = body.error;
    if (error && typeof error.message === "string") {
      return new ApiError(
        error.message,
        response.status,
        error.code ?? "http_error",
        error.details ?? null,
      );
    }
  } catch {
    // Gövde JSON değilse fallback mesaj kullanılır.
  }

  return new ApiError(fallback, response.status, "http_error");
}
