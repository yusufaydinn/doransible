/**
 * Inventory query key hiyerarşisi.
 *
 * Anahtarlar iç içe geçecek şekilde türetilir; böylece `inventoryKeys.all` ile
 * yapılan bir invalidation liste, detay ve host sorgularının hepsini kapsar.
 */
export const inventoryKeys = {
  all: ["inventories"] as const,
  list: () => [...inventoryKeys.all, "list"] as const,
  detail: (inventoryId: number) => [...inventoryKeys.all, "detail", inventoryId] as const,
  hosts: (inventoryId: number) =>
    [...inventoryKeys.all, "detail", inventoryId, "hosts"] as const,
  /**
   * Bir inventory'nin kalıcı ping ölçüm geçmişi.
   *
   * Anahtar detay dalının altındadır ve inventory kimliğini **taşır**: iki
   * farklı inventory'nin geçmişi hiçbir zaman aynı cache girdisini paylaşmaz.
   * Ayrı bir yaprak olması, ping sonrası yapılan invalidation'ın kayıt
   * metadata'sını veya dosya içeriğini gereksizce tazelememesini sağlar.
   */
  pingHistory: (inventoryId: number) =>
    [...inventoryKeys.all, "detail", inventoryId, "ping-history"] as const,
};
