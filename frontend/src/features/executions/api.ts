/**
 * Execution plan endpoint'inin tek erişim noktası.
 *
 * Sayfa bileşenleri doğrudan `fetch` çağırmaz; URL kurgusu yalnızca burada
 * bulunur (route/service katman ayrımı sözleşmesi).
 */

import { apiFetch, apiPost } from "../../lib/apiClient";

import type { Inventory } from "../inventories/types";
import type {
  ExecutionLaunchRequest,
  ExecutionLaunchResponse,
  ExecutionPlan,
  ExecutionPlanRequest,
  PreparedExecutionPlan,
} from "./types";

/**
 * Bir project'e **bağlı** inventory kayıtlarını listeler.
 *
 * Filtre sunucuda uygulanır: standalone kayıtlar hiç dönmesin diye istemcide
 * ikinci bir eleme yapılmaz. Plan üretimi zaten yalnızca project'e bağlı
 * inventory kabul eder (`inventory_not_linked_to_project`).
 */
export function fetchProjectInventories(projectId: number): Promise<Inventory[]> {
  return apiFetch<Inventory[]>(`/api/inventories?project_id=${projectId}`);
}

/**
 * Seçilen kipte (`check` veya `normal`) execution planı üretir (R1-V1, R1-V3H2B).
 *
 * **Hiçbir playbook çalıştırmaz**: Job kaydı, artifact ve onay token'ı
 * oluşmaz. Gövde yalnızca kip, inventory kimliği ve keşfedilmiş playbook
 * yolunu taşır; limit, tags ve benzeri çalıştırma parametreleri bu dilimde
 * kapsam dışıdır ve backend tarafından reddedilir (`extra="forbid"`).
 */
export function createExecutionPlan(
  projectId: number,
  request: ExecutionPlanRequest,
): Promise<ExecutionPlan> {
  return apiPost<ExecutionPlan>(`/api/projects/${projectId}/execution-plan`, request);
}

/**
 * Planı onaya hazırlar (R1-V2).
 *
 * Sunucu project ağacını ve normalize inventory snapshot'ını dondurur, planı
 * **yalnızca dondurulmuş içerikten** yeniden hesaplar ve tek kullanımlık bir
 * token döndürür. Bu çağrı da hiçbir playbook çalıştırmaz.
 *
 * Gövde önizleme ile aynıdır: ikinci bir kanaldan çalıştırma parametresi
 * geçirilemez.
 */
export function prepareExecutionPlan(
  projectId: number,
  request: ExecutionPlanRequest,
): Promise<PreparedExecutionPlan> {
  return apiPost<PreparedExecutionPlan>(
    `/api/projects/${projectId}/execution-plans`,
    request,
  );
}

/**
 * Hazırlanmış planı çalıştırmaya alır (R1-V3D1).
 *
 * Yalnızca `pending` bir Job rezerve eder: `ansible-runner` çağrılmaz, SSH
 * bağlantısı kurulmaz. Job'ın gerçekten çalışması ayrı bir arka plan
 * worker'ına bağlıdır (`ANSIBLEOPS_PLAYBOOK_WORKER_ENABLED`).
 */
export function launchPreparedExecution(
  projectId: number,
  request: ExecutionLaunchRequest,
): Promise<ExecutionLaunchResponse> {
  return apiPost<ExecutionLaunchResponse>(`/api/projects/${projectId}/executions`, request);
}
