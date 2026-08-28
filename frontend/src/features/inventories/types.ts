/**
 * Inventory API sözleşmesinin TypeScript karşılığı (T-201, T-202).
 *
 * Alan adları backend şemasıyla birebir aynıdır (`app/schemas/inventory.py`).
 * Ham `ansible-inventory` çıktısı API'de dönmez; buradaki gösterim backend'in
 * normalize ettiği, kararlı sıralı ve maskelenmiş hâlidir (MIMARI.md bölüm 7).
 */

/** Desteklenen inventory dosya biçimleri. Dinamik inventory yoktur (ADR-015). */
export type InventorySourceType = "ini" | "yaml";

/** `GET /api/inventories` ve `GET /api/inventories/{id}` cevabı. */
export interface Inventory {
  id: number;
  /** Bağlı project; standalone kayıtlarda `null`. */
  project_id: number | null;
  name: string;
  /** Sunucu üzerindeki normalize edilmiş mutlak dosya yolu. */
  path: string;
  source_type: InventorySourceType;
  created_at: string;
  updated_at: string;
}

/**
 * `POST /api/inventories` istek gövdesi (`app/schemas/inventory.py`).
 *
 * Backend gövdesi `extra="forbid"`tir. `path`, sunucuda **zaten var olan** bir
 * dosyayı göstermelidir; bu istek dosya oluşturmaz veya yüklemez.
 * `project_id` `null` ise kayıt standalone'dur; sayıysa dosyanın o project'in
 * kendi dizini altında olması sunucu tarafında zorunludur.
 */
export interface CreateInventoryRequest {
  name: string;
  path: string;
  source_type: InventorySourceType;
  project_id: number | null;
}

/** Bir inventory grubu ve etkin host listesi. */
export interface InventoryGroup {
  name: string;
  /** Alt gruplardan gelenler dâhil, ada göre sıralı. */
  hosts: string[];
}

/** Tek bir host; ait olduğu gruplar ve **maskelenmiş** değişkenleri. */
export interface InventoryHost {
  name: string;
  /** Üst gruplar dâhil, ada göre sıralı. */
  groups: string[];
  /**
   * Host değişkenleri.
   *
   * Değerler backend tarafından maskelenmiş olarak gelir: secret anahtarları ve
   * secret görünümlü değerler `***` olur (GUVENLIK.md bölüm 9). Maskeleme iç
   * içe yapılarda da uygulanır. Arayüz bu değerleri **geldiği gibi** gösterir;
   * açmaya veya yeniden üretmeye çalışmaz.
   */
  variables: Record<string, unknown>;
}

/** `GET /api/inventories/{id}/hosts` cevabı. */
export interface InventoryHostsResponse {
  inventory_id: number;
  groups: InventoryGroup[];
  hosts: InventoryHost[];
}

/* --- Ping: iki aşamalı onay sözleşmesi (T-204A, T-204B2) ------------------- */

/**
 * `POST /api/inventories/{id}/ping/preview` isteği.
 *
 * Gövde `extra="forbid"`tir: modül, modül argümanı, timeout, fork sayısı ve
 * inventory path'i **gönderilemez**. Çalıştırılacak iş kodda sabittir
 * (MIMARI.md bölüm 7).
 */
export interface PingPreviewRequest {
  /** Host pattern'i; tüm inventory hedeflenecekse `null`. */
  limit: string | null;
}

/** Inventory'nin bir project'e bağlı olup olmadığı. */
export type InventoryBinding = "project" | "standalone";

/** Onaylanan planın known_hosts politikası. */
export type PingHostKeyPolicy = "strict" | "accept_new";

/** Plandaki inventory tanıtımı. */
export interface PingPlanInventory {
  id: number;
  name: string;
  binding: InventoryBinding;
  project_id: number | null;
  project_name: string | null;
}

/**
 * Kullanıcının onaylayacağı ping planı.
 *
 * Yalnızca güvenli alanlar taşınır: host **adları** vardır; adres, port,
 * kullanıcı, private key yolu, diğer host değişkenleri ve sunucudaki dosya yolu
 * bilinçli olarak **yoktur** (GUVENLIK.md bölüm 3).
 */
export interface PingPlan {
  inventory: PingPlanInventory;
  /** Çalıştırılacak modül; kodda sabittir. */
  operation: string;
  /**
   * İşlemin etkisini anlatan metin.
   *
   * Mutlak güvence **vermez**: ping uzak hostta geçici modül dosyası ve süreç
   * oluşturur, yani gerçek execution'dır (ADR-018 Karar 1).
   */
  operation_effect: string;
  limit: string | null;
  /** Kesin hedef sayısı; liste kırpılsa bile doğrudur. */
  host_count: number;
  /** Hedef host adları, ada göre sıralı. */
  hosts: string[];
  /** Liste sunucudaki üst sınırla kırpıldıysa true. */
  hosts_truncated: boolean;
  connection: string;
  host_key_policy: PingHostKeyPolicy;
  become: boolean;
}

/**
 * Preview cevabı.
 *
 * `preview_token` yalnızca burada, bir kez döner ve tek kullanımlıktır. Token
 * yalnızca istek gövdesinde taşınır; URL veya query string'e konmaz.
 */
export interface PingPreviewResponse {
  preview_token: string;
  expires_at: string;
  plan: PingPlan;
}

/**
 * Cancel ve confirm isteklerinin gövdesi.
 *
 * Gövde `extra="forbid"`tir ve **yalnızca** token taşır: limit dâhil hiçbir
 * parametre ikinci bir kanaldan geçirilemez, çünkü çalıştırılan iş onaylanan
 * planın kendisidir (ADR-019 Karar 6).
 */
export interface PingTokenRequest {
  preview_token: string;
}

/** Tek bir host'un ping sonucu. */
export type PingHostStatus = "reachable" | "unreachable" | "failed" | "no_result";

/**
 * Confirm sonrasında Job'un terminal durumu.
 *
 * `successful` yalnızca return code 0 **ve** beklenen bütün host'lar reachable
 * olduğunda döner (ADR-019 Karar 7).
 */
export type PingJobStatus = "successful" | "failed";

export interface PingRunHost {
  name: string;
  status: PingHostStatus;
  /**
   * Yalnızca `unreachable` ve `failed` durumlarında doludur.
   *
   * Backend mesajı redaction, path maskeleme ve snapshot bağlantı değerlerinin
   * (adres, port, kullanıcı, anahtar yolu) maskelenmesinden geçirir; ham
   * stdout/stderr taşınmaz (ADR-019 Karar 8).
   */
  message: string | null;
}

/** Host durumlarının sayımı. */
export interface PingRunSummary {
  total: number;
  reachable: number;
  unreachable: number;
  failed: number;
  no_result: number;
}

/**
 * `POST /api/inventories/{id}/ping` cevabı.
 *
 * Token, snapshot içeriği, artifact path'i, argv ve ham çıktı **yer almaz**.
 * Geçerli bir Ansible sonucu (rc 2/4) altyapı hatası değildir: HTTP 200 döner
 * ve `status` `failed` olur.
 */
export interface PingRunResponse {
  job_id: string;
  job_type: "ping";
  status: PingJobStatus;
  inventory_id: number;
  project_id: number | null;
  limit: string | null;
  return_code: number | null;
  started_at: string;
  finished_at: string;
  summary: PingRunSummary;
  /** Ada göre deterministik sıralı. */
  hosts: PingRunHost[];
}

/* --- Ping geçmişi: kalıcı son ölçümler (R1-V3J1A) -------------------------- */

/**
 * Bir geçmiş kaydındaki host durum sayımları.
 *
 * Alan kümesi backend'in `PingHistorySummaryResponse` şemasıyla **birebir**
 * aynıdır. Host **adları** ve mesajları burada yoktur: geçmiş yalnızca kaç
 * host'un hangi durumda olduğunu bildirir.
 */
export interface PingHistorySummary {
  total: number;
  reachable: number;
  unreachable: number;
  failed: number;
  no_result: number;
}

/**
 * Geçmişte görünen tek bir tamamlanmış ölçüm.
 *
 * Alan kümesi backend'in `PingHistoryItemResponse` şemasıyla **birebir**
 * aynıdır ve o şema `extra="forbid"`tir: `requested_by`, `artifact_path`,
 * `limit`, `project_id`, host adı, host mesajı ve ham çıktı bu yüzeyde hiç yer
 * almaz (GUVENLIK.md bölüm 3).
 *
 * `status` yalnız terminal iki değeri alır; geçmiş sonucu yayımlanmış
 * ölçümlerden oluşur. `return_code` gerçekten null olabilir: `ansible` ad-hoc
 * komutu hiç başlatılamadığında bir çıkış kodu oluşmaz.
 */
export interface PingHistoryItem {
  job_id: string;
  status: PingJobStatus;
  return_code: number | null;
  started_at: string;
  finished_at: string;
  summary: PingHistorySummary;
}

/**
 * `GET /api/inventories/{id}/ping-runs` cevabı.
 *
 * `items` en yeni ölçüm başta olacak biçimde **sunucuda** sıralanır; istemci
 * yeniden sıralamaz. Cursor/pagination sözleşmede yoktur.
 */
export interface PingHistoryResponse {
  inventory_id: number;
  items: PingHistoryItem[];
}
