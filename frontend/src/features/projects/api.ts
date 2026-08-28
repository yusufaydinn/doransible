/**
 * Project endpoint'lerinin tek erişim noktası.
 *
 * Sayfa bileşenleri doğrudan `fetch` çağırmaz; URL kurgusu yalnızca burada
 * bulunur (route/service katman ayrımı sözleşmesi).
 */

import { apiDelete, apiFetch, apiPost } from "../../lib/apiClient";

import type { CreateProjectRequest, PlaybookListResponse, Project } from "./types";

/** Aktif project kayıtlarını listeler. */
export function fetchProjects(): Promise<Project[]> {
  return apiFetch<Project[]>("/api/projects");
}

/** Tek bir project kaydını okur. */
export function fetchProject(projectId: number): Promise<Project> {
  return apiFetch<Project>(`/api/projects/${projectId}`);
}

/** Project altındaki playbook adaylarını keşfeder. */
export function fetchPlaybooks(projectId: number): Promise<PlaybookListResponse> {
  return apiFetch<PlaybookListResponse>(`/api/projects/${projectId}/playbooks`);
}

/** Var olan bir sunucu dizinini project olarak kaydeder. */
export function createProject(request: CreateProjectRequest): Promise<Project> {
  return apiPost<Project>("/api/projects", request);
}

/**
 * Project kaydını pasife alır.
 *
 * Bu çağrı sunucudaki dosyaları **silmez**; yalnızca kaydın aktiflik durumunu
 * değiştirir (MIMARI.md bölüm 7).
 */
export function deactivateProject(projectId: number): Promise<Project> {
  return apiDelete<Project>(`/api/projects/${projectId}`);
}
