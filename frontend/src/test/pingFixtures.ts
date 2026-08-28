/**
 * T-204C ping akışının UI testleri için ortak sahte cevaplar.
 *
 * 2A'daki akış testi kendi yerel kanarya token'ıyla çalışır ve bu dosyaya
 * bağlanmaz; buradaki fixture'lar 2B'nin hata ve sonuç testlerini besler.
 * Hiçbiri gerçek bir secret taşımaz (GUVENLIK.md bölüm 12).
 */

import type {
  PingHistoryItem,
  PingHistoryResponse,
  PingPlan,
  PingPreviewResponse,
  PingRunResponse,
} from "../features/inventories/types";
import { inventoryContents, linkedInventory, standaloneInventory } from "./fixtures";
import { jsonResponse, type RecordedRequest } from "./harness";

/** Bu dosyanın ürettiği planların taşıdığı sahte onay değeri. */
export const PING_TOKEN = "PING-TOKEN-FIXTURE-4c8e17b9a6d20f35";

export const basePingPlan: PingPlan = {
  inventory: {
    id: linkedInventory.id,
    name: linkedInventory.name,
    binding: "project",
    project_id: linkedInventory.project_id,
    project_name: "Web sunucuları",
  },
  operation: "ansible.builtin.ping",
  operation_effect:
    "Hedef host'lara SSH bağlantısı kurulur; uzak hostta geçici modül dosyaları ve " +
    "süreç oluşabilir. Kalıcı yapılandırma değişikliği amaçlanmaz.",
  limit: null,
  host_count: 2,
  hosts: ["web01", "web02"],
  hosts_truncated: false,
  connection: "ssh",
  host_key_policy: "strict",
  become: false,
};

/** Plan alanlarını değiştirerek preview cevabı üretir. */
export function previewWith(plan: Partial<PingPlan> = {}): PingPreviewResponse {
  return {
    preview_token: PING_TOKEN,
    expires_at: "2026-08-03T10:15:00Z",
    plan: { ...basePingPlan, ...plan },
  };
}

export const pingPreviewResponse = previewWith();

/** Bütün host'ları erişilebilir olan başarılı iş. */
export const pingRunSuccessful: PingRunResponse = {
  job_id: "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42",
  job_type: "ping",
  status: "successful",
  inventory_id: linkedInventory.id,
  project_id: linkedInventory.project_id,
  limit: null,
  return_code: 0,
  started_at: "2026-08-03T10:10:00Z",
  finished_at: "2026-08-03T10:10:04Z",
  summary: { total: 2, reachable: 2, unreachable: 0, failed: 0, no_result: 0 },
  hosts: [
    { name: "web01", status: "reachable", message: null },
    { name: "web02", status: "reachable", message: null },
  ],
};

/**
 * Dört durumun tamamını taşıyan, tamamlanmış fakat başarısız iş.
 *
 * `rc=4` geçerli bir Ansible sonucudur ve altyapı hatası değildir
 * (ADR-019 Karar 7): HTTP 200 döner, Job `failed` olur.
 */
export const pingRunFailed: PingRunResponse = {
  job_id: "9a3c5e21-7d40-4b18-8f62-1c0de4a7b935",
  job_type: "ping",
  status: "failed",
  inventory_id: linkedInventory.id,
  project_id: linkedInventory.project_id,
  limit: "web",
  return_code: 4,
  started_at: "2026-08-03T11:00:00Z",
  finished_at: "2026-08-03T11:00:09Z",
  summary: { total: 4, reachable: 1, unreachable: 1, failed: 1, no_result: 1 },
  hosts: [
    { name: "app01", status: "no_result", message: null },
    { name: "db01", status: "failed", message: "Modül çalıştırılamadı." },
    { name: "web01", status: "reachable", message: null },
    {
      name: "web02",
      status: "unreachable",
      message: "SSH bağlantısı kurulamadı: bağlantı reddedildi.",
    },
  ],
};

/**
 * Boş ping geçmişi.
 *
 * Geçmiş uçunun **varsayılan** cevabıdır: preview/confirm testleri geçmişin
 * içeriğiyle ilgilenmez ve dolu bir liste onların özet/tablo sorgularına
 * karışırdı. Geçmişin kendi davranışı `inventoryPingHistory.test.tsx` içinde
 * ölçülür.
 */
export const emptyPingHistory: PingHistoryResponse = {
  inventory_id: linkedInventory.id,
  items: [],
};

/** Bütün host'ları erişilebilir olan geçmiş kaydı. */
export const pingHistorySuccessful: PingHistoryItem = {
  job_id: "6b1f0c74-8a2e-4d35-9c11-5f7ab0e39d42",
  status: "successful",
  return_code: 0,
  started_at: "2026-08-03T10:10:00Z",
  finished_at: "2026-08-03T10:10:04Z",
  summary: { total: 5, reachable: 5, unreachable: 0, failed: 0, no_result: 0 },
};

/** Karışık sonuçlu, tamamlanmış fakat başarısız geçmiş kaydı. */
export const pingHistoryFailed: PingHistoryItem = {
  job_id: "9a3c5e21-7d40-4b18-8f62-1c0de4a7b935",
  status: "failed",
  return_code: 4,
  started_at: "2026-08-03T11:00:00Z",
  finished_at: "2026-08-03T11:00:09Z",
  summary: { total: 5, reachable: 4, unreachable: 1, failed: 0, no_result: 0 },
};

/** Verilen kayıtlardan bir geçmiş cevabı üretir. */
export function pingHistoryWith(
  items: PingHistoryItem[],
  inventoryId: number = linkedInventory.id,
): PingHistoryResponse {
  return { inventory_id: inventoryId, items };
}

export interface PingRoutes {
  preview?: unknown;
  cancel?: unknown;
  confirm?: unknown;
  hosts?: unknown;
  pingRuns?: unknown;
}

/**
 * Ping uçlarını ve inventory okumalarını karşılar.
 *
 * Eşleşme sırası özgülden genele doğrudur: `/ping-runs` →
 * `/ping/preview/cancel` → `/ping/preview` → `/ping`.
 */
export function pingResponder(routes: PingRoutes): (request: RecordedRequest) => unknown {
  return (request) => {
    // Geçmiş ucu `/ping` ile **başlayan** ama onunla bitmeyen bir adrestir;
    // bu yüzden ping eşleşmelerinden önce ayrılır.
    if (request.url.includes("/ping-runs")) {
      return resolveRoute(routes.pingRuns ?? jsonResponse(emptyPingHistory));
    }
    if (request.url.endsWith("/ping/preview/cancel")) {
      return resolveRoute(routes.cancel);
    }
    if (request.url.endsWith("/ping/preview")) {
      return resolveRoute(routes.preview ?? jsonResponse(pingPreviewResponse));
    }
    if (request.url.endsWith("/ping")) {
      return resolveRoute(routes.confirm);
    }
    if (request.url.endsWith("/hosts")) {
      return resolveRoute(routes.hosts ?? jsonResponse(inventoryContents));
    }
    if (request.url.endsWith("/api/inventories")) {
      return jsonResponse([linkedInventory, standaloneInventory]);
    }
    if (request.url.endsWith(`/api/inventories/${standaloneInventory.id}`)) {
      return jsonResponse(standaloneInventory);
    }
    return jsonResponse(linkedInventory);
  };
}

/**
 * Route değeri bir fonksiyonsa çağırır.
 *
 * Böylece bir uç, sabit bir sahte cevap yerine **atan** bir davranış da
 * tanımlayabilir (taşıma katmanı arızası).
 */
function resolveRoute(value: unknown): unknown {
  return typeof value === "function" ? (value as () => unknown)() : value;
}

/** Taşıma katmanı arızasını taklit eder: `fetch` çağrısının kendisi atar. */
export function networkFailure(): never {
  throw new TypeError("Failed to fetch");
}
