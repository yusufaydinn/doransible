/**
 * Project query key hiyerarşisi.
 *
 * Anahtarlar iç içe geçecek şekilde türetilir; böylece `projectKeys.all` ile
 * yapılan bir invalidation liste, detay ve playbook sorgularının hepsini
 * kapsar.
 */
export const projectKeys = {
  all: ["projects"] as const,
  list: () => [...projectKeys.all, "list"] as const,
  detail: (projectId: number) => [...projectKeys.all, "detail", projectId] as const,
  playbooks: (projectId: number) =>
    [...projectKeys.all, "detail", projectId, "playbooks"] as const,
};
