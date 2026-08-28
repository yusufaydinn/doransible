/**
 * Project ekranlarının kullandığı TanStack Query sarmalayıcıları.
 *
 * Cache invalidation kuralları tek yerde toplanır; sayfa bileşenleri
 * `queryClient` ile doğrudan uğraşmaz.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createProject,
  deactivateProject,
  fetchPlaybooks,
  fetchProject,
  fetchProjects,
} from "./api";
import { projectKeys } from "./queryKeys";
import type { CreateProjectRequest, Project } from "./types";

/** Aktif project listesi. */
export function useProjects() {
  return useQuery({
    queryKey: projectKeys.list(),
    queryFn: fetchProjects,
  });
}

/** Tek bir project kaydı. */
export function useProject(projectId: number) {
  return useQuery({
    queryKey: projectKeys.detail(projectId),
    queryFn: () => fetchProject(projectId),
    enabled: Number.isInteger(projectId),
  });
}

/**
 * Project altındaki playbook'lar.
 *
 * Keşif dosya sistemini tarar; sonuç kısa ömürlüdür ve tekrar denemek
 * kullanıcının kararıdır. Bu yüzden otomatik retry kapalıdır.
 */
export function usePlaybooks(projectId: number, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: projectKeys.playbooks(projectId),
    queryFn: () => fetchPlaybooks(projectId),
    enabled: Number.isInteger(projectId) && (options?.enabled ?? true),
    retry: false,
    staleTime: 0,
  });
}

/** Yeni project kaydı oluşturur ve listeyi tazeler. */
export function useCreateProject() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: CreateProjectRequest) => createProject(request),
    onSuccess: (project: Project) => {
      queryClient.setQueryData(projectKeys.detail(project.id), project);
      void queryClient.invalidateQueries({ queryKey: projectKeys.list() });
    },
  });
}

/**
 * Project kaydını pasife alır.
 *
 * Başarıdan sonra hem liste hem de ilgili detay/playbook sorguları
 * geçersizleştirilir: pasif kayıt varsayılan listede görünmemeli ve
 * playbook keşfi artık `project_inactive` döndürmelidir.
 */
export function useDeactivateProject(projectId: number) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => deactivateProject(projectId),
    onSuccess: (project: Project) => {
      queryClient.setQueryData(projectKeys.detail(project.id), project);
      void queryClient.invalidateQueries({ queryKey: projectKeys.list() });
      void queryClient.invalidateQueries({ queryKey: projectKeys.playbooks(project.id) });
    },
  });
}
