/**
 * Execution query key hiyerarşisi.
 *
 * Plan **cache'lenmez** ve bu yüzden bir anahtarı yoktur: plan üretimi açık bir
 * kullanıcı eylemidir ve sonucu, seçim değiştiği anda geçersizdir. Burada
 * yalnızca plan formunun okuduğu, project'e bağlı inventory listesi bulunur.
 */
export const executionKeys = {
  all: ["executions"] as const,
  projectInventories: (projectId: number) =>
    [...executionKeys.all, "project", projectId, "inventories"] as const,
};
