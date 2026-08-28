/** Execution plan testlerinde kullanılan örnek API cevapları (R1-V1, R1-V2, R1-V3D3). */

import type {
  ExecutionLaunchResponse,
  ExecutionPlan,
  PreparedExecutionPlan,
} from "../features/executions/types";

import { activeProject, linkedInventory } from "./fixtures";

export const executionPlan: ExecutionPlan = {
  project: { id: activeProject.id, name: activeProject.name },
  inventory: {
    id: linkedInventory.id,
    name: linkedInventory.name,
    binding: "project",
  },
  playbook: {
    path: "site.yml",
    name: "site.yml",
    size_bytes: 240,
    modified_at: "2026-07-18T17:45:00Z",
  },
  mode: "check",
  limit: null,
  tags: null,
  skip_tags: null,
  host_count: 3,
  hosts: ["db01", "web01", "web02"],
  hosts_truncated: false,
  connection: "ssh",
  host_key_policy: "strict",
  become: false,
  executable: false,
  not_executable_reason: "execution_not_enabled",
  generated_at: "2026-07-28T10:05:00Z",
};

/**
 * Onaya hazırlanmış plan (R1-V2).
 *
 * `plan`, önizlemeden **farklı** bir host kümesi taşır: ekranda dondurulmuş
 * planın gösterildiği, eski önizlemenin hazırlanmış gibi sunulmadığı ancak
 * ayırt edilebilir veriyle ölçülebilir.
 */
export const preparedExecutionPlan: PreparedExecutionPlan = {
  plan_token: "TESTTOKENtesttokenTESTTOKENtesttokenTESTTOK",
  expires_at: "2026-07-28T10:15:00Z",
  manifest_digest: "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
  prepared: true,
  plan: {
    ...executionPlan,
    host_count: 2,
    hosts: ["db01", "web01"],
    generated_at: "2026-07-28T10:06:00Z",
  },
};

/** Hazırlanmış planı çalıştırmaya alma cevabı (R1-V3D3). */
export const executionLaunchResponse: ExecutionLaunchResponse = {
  job_id: "018f1e0a-9b1a-7c3a-9e2a-1a2b3c4d5e6f",
  job_type: "playbook",
  initial_status: "pending",
  mode: "check",
  project_id: activeProject.id,
  inventory_id: linkedInventory.id,
  playbook_path: "site.yml",
  accepted_at: "2026-07-28T10:07:00Z",
};

/** Host listesi kırpılmış plan. */
export const truncatedExecutionPlan: ExecutionPlan = {
  ...executionPlan,
  host_count: 125,
  hosts: Array.from({ length: 100 }, (_, index) => `host${String(index).padStart(3, "0")}`),
  hosts_truncated: true,
};

/** Normal mode önizleme planı (R1-V3H2B). */
export const normalExecutionPlan: ExecutionPlan = {
  ...executionPlan,
  mode: "normal",
};

/** Normal mode onaya hazırlanmış plan (R1-V3H2B). */
export const preparedNormalExecutionPlan: PreparedExecutionPlan = {
  ...preparedExecutionPlan,
  plan_token: "NORMALTOKENnormaltokenNORMALTOKENnormaltokenNOR",
  plan: {
    ...preparedExecutionPlan.plan,
    mode: "normal",
  },
};

/** Normal mode çalıştırmaya alma cevabı (R1-V3H2B). */
export const executionLaunchNormalResponse: ExecutionLaunchResponse = {
  ...executionLaunchResponse,
  mode: "normal",
};
