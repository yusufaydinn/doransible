/**
 * Execution plan ekranının kullandığı veri erişimi.
 *
 * Plan üretimi bilinçli olarak TanStack mutation **değildir**: her açık
 * kullanıcı eylemi tam olarak bir istek üretmelidir ve otomatik retry
 * istenmez. Plan aynı sebeple cache'lenmez — seçim değiştiği anda eski plan
 * geçersizdir ve bayat bir planın ekranda kalması, kullanıcının okuduğu özet
 * ile seçtiği girdilerin ayrışması demek olurdu.
 */

import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  createExecutionPlan,
  fetchProjectInventories,
  launchPreparedExecution,
  prepareExecutionPlan,
} from "./api";
import { executionKeys } from "./queryKeys";
import type {
  ExecutionLaunchRequest,
  ExecutionLaunchResponse,
  ExecutionPlan,
  ExecutionPlanRequest,
  PreparedExecutionPlan,
} from "./types";

/** Project'e bağlı inventory listesi. */
export function useProjectInventories(projectId: number, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: executionKeys.projectInventories(projectId),
    queryFn: () => fetchProjectInventories(projectId),
    enabled: Number.isInteger(projectId) && (options?.enabled ?? true),
    retry: false,
  });
}

/** Plan üreten, durumsuz eylem. */
export function useCreateExecutionPlan(
  projectId: number,
): (request: ExecutionPlanRequest) => Promise<ExecutionPlan> {
  return useCallback(
    (request: ExecutionPlanRequest) => createExecutionPlan(projectId, request),
    [projectId],
  );
}

/**
 * Planı onaya hazırlayan, durumsuz eylem (R1-V2).
 *
 * Bu da bilinçli olarak mutation **değildir**: cevap tek kullanımlık bir token
 * taşır ve token'ın query/mutation cache'ine, devtools'a veya bir retry
 * denemesine girmesi istenmez. Sonuç yalnızca çağıran bileşenin belleğinde
 * yaşar.
 */
export function usePrepareExecutionPlan(
  projectId: number,
): (request: ExecutionPlanRequest) => Promise<PreparedExecutionPlan> {
  return useCallback(
    (request: ExecutionPlanRequest) => prepareExecutionPlan(projectId, request),
    [projectId],
  );
}

/**
 * Hazırlanmış planı çalıştırmaya alan, durumsuz eylem (R1-V3D1).
 *
 * Mutation değildir; sebep `usePrepareExecutionPlan` ile aynıdır — istek gövdesi
 * tek kullanımlık bir token taşır ve bunun cache/devtools üzerinden ikinci bir
 * yola sızması istenmez.
 */
export function useLaunchExecution(
  projectId: number,
): (request: ExecutionLaunchRequest) => Promise<ExecutionLaunchResponse> {
  return useCallback(
    (request: ExecutionLaunchRequest) => launchPreparedExecution(projectId, request),
    [projectId],
  );
}
