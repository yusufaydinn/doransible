/**
 * Execution plan API sözleşmesinin TypeScript karşılığı (R1-V1, R1-V3H2B).
 *
 * Alan adları backend şemasıyla birebir aynıdır (`app/schemas/execution.py`).
 * Sunucudaki mutlak yollar, hostvar'lar ve private key bilgileri cevapta
 * **bulunmaz**; bu yüzden burada da karşılıkları yoktur.
 */

import type { ExecutionMode } from "../../lib/executionMode";

export type { ExecutionMode };

/**
 * `POST /api/projects/{id}/execution-plan` istek gövdesi.
 *
 * `mode` zorunludur ve varsayılanı yoktur: backend'in
 * `ExecutionPlanCreate.mode`'u ile birebir aynı kuralı taşır (bkz. o alanın
 * docstring'i) — arayüz de kipi her istekte açıkça göndermek zorundadır,
 * sessizce `check`'e düşemez.
 */
export interface ExecutionPlanRequest {
  mode: ExecutionMode;
  inventory_id: number;
  /** Project köküne göreli, keşifte listelenmiş yol. */
  playbook_path: string;
}

export interface ExecutionPlanProject {
  id: number;
  name: string;
}

export interface ExecutionPlanInventory {
  id: number;
  name: string;
  /** Bu dilimde daima `project`; backend'de de `Literal` ile bağlıdır. */
  binding: "project";
}

export interface ExecutionPlanPlaybook {
  /** Project köküne **göreli** POSIX yol. Mutlak yol asla dönmez. */
  path: string;
  name: string;
  size_bytes: number;
  modified_at: string;
}

/**
 * `POST /api/projects/{id}/execution-plan` cevabı.
 *
 * `executable` bağlayıcıdır ve bu dilimde daima `false`'tur: plan
 * çalıştırılabilir bir onay **değildir** ve arayüz onu öyle kullanamaz.
 *
 * Değişmez alanlar literal type ile yazılır (backend'deki `Literal` alanların
 * karşılığı). Böylece arayüzde "ya bir gün `executable` true gelirse" biçiminde
 * bir çalıştırma dalı yazmak type check'te hata verir.
 *
 * `mode` R1-V3H2B'den itibaren sabit değildir: istekte seçilen kipi aynen
 * taşır (backend'deki `ExecutionPlanResponse.mode` ile aynı kural).
 */
export interface ExecutionPlan {
  project: ExecutionPlanProject;
  inventory: ExecutionPlanInventory;
  playbook: ExecutionPlanPlaybook;
  mode: ExecutionMode;
  limit: null;
  tags: null;
  skip_tags: null;
  host_count: number;
  hosts: string[];
  hosts_truncated: boolean;
  connection: "ssh";
  /** Sunucu politikası; merkezî bir type yok, bu turda string kalır. */
  host_key_policy: string;
  become: false;
  executable: false;
  not_executable_reason: "execution_not_enabled";
  generated_at: string;
}

/**
 * `POST /api/projects/{id}/execution-plans` cevabı (R1-V2).
 *
 * `plan_token` **tek kullanımlıktır ve yalnızca bu cevapta döner**. Arayüz onu
 * ekrana basmaz, storage'a veya URL'ye yazmaz ve query cache'ine koymaz;
 * yalnızca bileşen belleğinde tutar.
 *
 * `prepared: true` hazırlanmışlığı anlatır, çalıştırılabilirliği değil: içteki
 * plan `executable: false` olmaya devam eder ve token'ı tüketen bir public
 * endpoint bu dilimde yoktur.
 */
export interface PreparedExecutionPlan {
  plan_token: string;
  expires_at: string;
  manifest_digest: string;
  prepared: true;
  plan: ExecutionPlan;
}

/**
 * `POST /api/projects/{id}/executions` istek gövdesi (R1-V3D1, R1-V3H2B).
 *
 * Yalnızca bu dört alan gönderilir; backend `extra="forbid"` ile başka hiçbir
 * alanı kabul etmez (`requested_by`, `host_key_policy` dahil).
 *
 * `mode` istisnadır (bkz. backend'deki `ExecutionLaunchCreate.mode`
 * docstring'i): Job'a yazılan değerin kaynağı değildir — o hep claim edilen
 * plan satırıdır — yalnızca *beklenen* kiptir. Arayüz burayı hazırlanmış
 * planın kendi `mode` alanından kurar, o an formda seçili olandan değil.
 */
export interface ExecutionLaunchRequest {
  plan_token: string;
  mode: ExecutionMode;
  inventory_id: number;
  playbook_path: string;
}

/**
 * `POST /api/projects/{id}/executions` cevabı.
 *
 * `initial_status` bilinçli olarak `status` değildir: arka plan worker'ı Job'ı
 * bu cevap istemciye ulaşmadan alabilir; alan yalnızca "kayıt bu durumda
 * oluşturuldu" der, güncel durum garantisi vermez.
 */
export interface ExecutionLaunchResponse {
  job_id: string;
  job_type: "playbook";
  initial_status: "pending";
  mode: ExecutionMode;
  project_id: number;
  inventory_id: number;
  playbook_path: string;
  accepted_at: string;
}
