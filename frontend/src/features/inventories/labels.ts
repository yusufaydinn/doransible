/** Inventory alan değerlerinin kullanıcıya gösterilen karşılıkları. */

import type { InventorySourceType } from "./types";

/**
 * Dosya biçimi etiketleri.
 *
 * Backend `source_type` alanını enum olarak doğrular; yine de gelen değer
 * beklenmeyen bir şey olursa arayüz ham değeri gösterip devam eder.
 */
export const SOURCE_TYPE_LABELS: Record<InventorySourceType, string> = {
  ini: "INI",
  yaml: "YAML",
};
