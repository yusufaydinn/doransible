/** Testlerde kullanılan örnek API cevapları. */

import type { Inventory, InventoryHostsResponse } from "../features/inventories/types";
import type { PlaybookListResponse, Project } from "../features/projects/types";

export const activeProject: Project = {
  id: 7,
  name: "Web sunucuları",
  path: "/srv/ansible/web",
  description: "Nginx ve sertifika yönetimi",
  is_active: true,
  created_at: "2026-07-01T09:00:00Z",
  updated_at: "2026-07-20T12:30:00Z",
};

export const inactiveProject: Project = {
  ...activeProject,
  id: 8,
  name: "Eski project",
  is_active: false,
};

export const playbookResult: PlaybookListResponse = {
  project_id: activeProject.id,
  playbooks: [
    {
      path: "playbooks/tasks/deploy.yml",
      name: "deploy.yml",
      size_bytes: 812,
      modified_at: "2026-07-19T08:15:00Z",
    },
    {
      path: "site.yml",
      name: "site.yml",
      size_bytes: 240,
      modified_at: "2026-07-18T17:45:00Z",
    },
  ],
  skipped_unreadable_files: 0,
  skipped_unreadable_directories: 0,
  truncated: false,
  scanned_at: "2026-07-28T10:00:00Z",
};

export const emptyPlaybookResult: PlaybookListResponse = {
  ...playbookResult,
  playbooks: [],
};

/** Bir project'e bağlı inventory kaydı. */
export const linkedInventory: Inventory = {
  id: 3,
  project_id: activeProject.id,
  name: "Üretim sunucuları",
  path: "/srv/ansible/web/inventories/production.ini",
  source_type: "ini",
  created_at: "2026-07-02T09:00:00Z",
  updated_at: "2026-07-21T11:15:00Z",
};

/** Hiçbir project'e bağlı olmayan inventory kaydı. */
export const standaloneInventory: Inventory = {
  id: 4,
  project_id: null,
  name: "Laboratuvar",
  path: "/srv/ansible-data/inventories/lab.yml",
  source_type: "yaml",
  created_at: "2026-07-03T09:00:00Z",
  updated_at: "2026-07-22T08:05:00Z",
};

/**
 * Inventory içeriği.
 *
 * `ansible_password` değeri backend'in **zaten maskelediği** hâliyle durur;
 * fixture gerçek bir secret taşımaz (GUVENLIK.md bölüm 12).
 */
export const inventoryContents: InventoryHostsResponse = {
  inventory_id: linkedInventory.id,
  groups: [
    { name: "all", hosts: ["db01", "web01", "web02"] },
    { name: "database", hosts: ["db01"] },
    { name: "web", hosts: ["web01", "web02"] },
  ],
  hosts: [
    {
      name: "db01",
      groups: ["all", "database"],
      variables: { ansible_host: "10.0.0.20", ansible_port: 22 },
    },
    {
      name: "web01",
      groups: ["all", "web"],
      variables: {
        ansible_host: "10.0.0.10",
        ansible_password: "***",
        ansible_user: "deploy",
      },
    },
    {
      name: "web02",
      groups: ["all", "web"],
      variables: {},
    },
  ],
};

export const emptyInventoryContents: InventoryHostsResponse = {
  inventory_id: standaloneInventory.id,
  groups: [],
  hosts: [],
};
