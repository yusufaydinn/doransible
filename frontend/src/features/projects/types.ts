/**
 * Project API sözleşmesinin TypeScript karşılığı (T-102, T-103).
 *
 * Alan adları backend şemasıyla birebir aynıdır (`app/schemas/project.py`).
 * Backend'in `path_key` gibi iç alanları API'de dönmez ve burada da yoktur.
 */

/** `GET /api/projects` ve `GET /api/projects/{id}` cevabı. */
export interface Project {
  id: number;
  name: string;
  /** Sunucu üzerindeki normalize edilmiş mutlak dizin yolu. */
  path: string;
  description: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

/** `POST /api/projects` istek gövdesi. */
export interface CreateProjectRequest {
  name: string;
  path: string;
  description?: string;
}

/** Keşfedilmiş tek bir playbook adayı. */
export interface PlaybookEntry {
  /** Project köküne **göreli** POSIX yol. Mutlak yol asla dönmez. */
  path: string;
  name: string;
  size_bytes: number;
  modified_at: string;
}

/** `GET /api/projects/{id}/playbooks` cevabı. */
export interface PlaybookListResponse {
  project_id: number;
  playbooks: PlaybookEntry[];
  skipped_unreadable_files: number;
  skipped_unreadable_directories: number;
  truncated: boolean;
  scanned_at: string;
}
