import { apiFetch } from "../../lib/apiClient";

export interface HealthResponse {
  status: "ok";
  app_name: string;
  version: string;
  environment: string;
}

/** Backend `/health` endpoint'ini sorgular. */
export function fetchHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}
