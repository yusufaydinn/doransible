/**
 * Inventory endpoint'lerinin tek erişim noktası.
 *
 * Sayfa bileşenleri doğrudan `fetch` çağırmaz; URL kurgusu yalnızca burada
 * bulunur (route/service katman ayrımı sözleşmesi, MIMARI.md bölüm 2).
 */

import { apiFetch, apiPost, apiPostNoContent } from "../../lib/apiClient";

import type {
  CreateInventoryRequest,
  Inventory,
  InventoryHostsResponse,
  PingHistoryResponse,
  PingPreviewRequest,
  PingPreviewResponse,
  PingRunResponse,
  PingTokenRequest,
} from "./types";

/**
 * Geçmiş görünümünün istediği azami ölçüm sayısı.
 *
 * Sabit bilinçlidir ve **parametre değildir**: bu ekran "son ölçümler"i
 * gösterir, sayfalanabilir bir arşiv değildir. İstemciden ayarlanabilir bir
 * limit, kullanıcıya görünür bir karşılığı olmayan bir düğme yaratır ve
 * sunucudaki üst sınırın anlamını bulanıklaştırırdı.
 */
const PING_HISTORY_LIMIT = 10;

/** Kayıtlı inventory'leri listeler; standalone kayıtlar da döner. */
export function fetchInventories(): Promise<Inventory[]> {
  return apiFetch<Inventory[]>("/api/inventories");
}

/**
 * Sunucuda zaten var olan bir inventory dosyasının kaydını oluşturur.
 *
 * Bu çağrı dosya oluşturmaz, yüklemez veya kopyalamaz. Allowlist, symlink,
 * dosya varlığı ve project bağı kontrollerinin tek yetkili kaynağı backend'dir.
 */
export function createInventory(request: CreateInventoryRequest): Promise<Inventory> {
  return apiPost<Inventory>("/api/inventories", request);
}

/** Tek bir inventory kaydını okur. Dosya içeriği okunmaz. */
export function fetchInventory(inventoryId: number): Promise<Inventory> {
  return apiFetch<Inventory>(`/api/inventories/${inventoryId}`);
}

/**
 * Inventory'nin host ve grup içeriğini okur (T-202).
 *
 * Endpoint path veya komut parametresi **almaz**: okunacak dosya yalnızca
 * sunucudaki kayıttan belirlenir. İstemci yalnızca kayıt kimliğini gönderir.
 */
export function fetchInventoryHosts(inventoryId: number): Promise<InventoryHostsResponse> {
  return apiFetch<InventoryHostsResponse>(`/api/inventories/${inventoryId}/hosts`);
}

/**
 * Ping onay planını ve tek kullanımlık token'ı üretir (T-204A).
 *
 * **Hiçbir SSH bağlantısı kurmaz, ping çalıştırmaz, Job kaydı veya artifact
 * dizini oluşturmaz.** Gerçek execution ayrı bir istektir (`confirmPing`).
 *
 * Gövde olduğu gibi gönderilir: limit burada kırpılmaz, normalize edilmez ve
 * doğrulanmaz. Limit'in anlam doğrulaması sunucudadır (`ping_invalid_limit`);
 * istemcide ikinci bir kural, onaylanan plan ile gönderilen isteği ayırırdı.
 */
export function createPingPreview(
  inventoryId: number,
  request: PingPreviewRequest,
): Promise<PingPreviewResponse> {
  return apiPost<PingPreviewResponse>(
    `/api/inventories/${inventoryId}/ping/preview`,
    request,
  );
}

/**
 * Onaylanmamış bir preview'ı iptal eder (T-204A).
 *
 * Backend token doğrulaması açısından **her durumda** `204 No Content` döner —
 * bilinmeyen, biçimsiz, süresi geçmiş, eşleşmeyen veya kullanılmış token da.
 * Aksi hâlde cevap farkı bir token'ın var olup olmadığını sızdırırdı. Yalnızca
 * altyapı arızası hata üretir (`ping_preview_unavailable`).
 */
export function cancelPingPreview(
  inventoryId: number,
  request: PingTokenRequest,
): Promise<void> {
  return apiPostNoContent(
    `/api/inventories/${inventoryId}/ping/preview/cancel`,
    request,
  );
}

/**
 * Onaylanmış planı çalıştırır (T-204B2).
 *
 * Gövde **yalnızca** token taşır; token dönüştürülmeden gönderilir. Limit,
 * timeout, forks, modül ve inventory path'i istemciden alınmaz: çalıştırılan
 * iş, onaylanan planın kendisidir.
 */
export function confirmPing(
  inventoryId: number,
  request: PingTokenRequest,
): Promise<PingRunResponse> {
  return apiPost<PingRunResponse>(`/api/inventories/${inventoryId}/ping`, request);
}

/**
 * Tamamlanmış ping ölçümlerinin kalıcı geçmişini okur (R1-V3J1A).
 *
 * **Salt okunurdur:** yeni bir ölçüm başlatmaz, hiçbir host'a bağlanmaz, Job
 * kaydı veya artifact üretmez. Yalnızca zaten kalıcı olan ölçümleri okur; bu
 * gerçek zamanlı bir izleme kanalı **değildir**.
 */
export function fetchPingHistory(inventoryId: number): Promise<PingHistoryResponse> {
  return apiFetch<PingHistoryResponse>(
    `/api/inventories/${inventoryId}/ping-runs?limit=${PING_HISTORY_LIMIT}`,
  );
}
