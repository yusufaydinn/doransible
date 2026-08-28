/**
 * Component testleri için ortak kurulum.
 *
 * Testler gerçek `App` bileşenini gerçek router ile render eder; yalnızca
 * `fetch` sahtelenir. Böylece backend kodu testte yeniden uygulanmaz, sadece
 * HTTP sınırı taklit edilir ve gönderilen istekler doğrulanabilir.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { App } from "../App";

/**
 * Testte öngörülebilir davranan query client.
 *
 * `staleTime: Infinity` bilinçlidir: veri kendiliğinden tazelenmez, yalnızca
 * açık bir `invalidateQueries` veya `refetch` yeni istek doğurur. Böylece
 * cache invalidation testleri gerçekten bir şey kanıtlar.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: 60_000 },
      mutations: { retry: false },
    },
  });
}

/** Uygulamayı verilen adreste render eder. */
export function renderApp(initialRoute = "/") {
  const queryClient = createTestQueryClient();

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialRoute]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  return { ...utils, queryClient };
}

/**
 * `apiClient`'ın kullandığı yüzeyi taklit eden sahte cevap.
 *
 * Gerçek `Response` yerine düz nesne kullanılır: `apiClient` yalnızca `ok`,
 * `status` ve `json()` alanlarına dokunur ve bu, jsdom'un fetch desteğine
 * bağımlılığı ortadan kaldırır.
 */
export function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

/** Standart backend hata zarfı üretir. */
export function errorResponse(
  status: number,
  code: string,
  message: string,
  details: unknown = null,
) {
  return jsonResponse({ error: { code, message, details } }, status);
}

/** Tek bir kaydedilmiş istek. */
export interface RecordedRequest {
  method: string;
  url: string;
  body: unknown;
}

type Responder = (request: RecordedRequest) => unknown;

/**
 * `globalThis.fetch` yerine geçen sahte istemci.
 *
 * `responder` bir sahte cevap döndürürse o kullanılır; `undefined` dönerse
 * test eksik bir kural bırakmış demektir ve istek yüksek sesle başarısız olur.
 */
export function installFetchMock(responder: Responder) {
  const requests: RecordedRequest[] = [];

  const fetchMock = vi.fn(async (input: unknown, init?: RequestInit) => {
    const request: RecordedRequest = {
      method: (init?.method ?? "GET").toUpperCase(),
      url: String(input),
      body: parseBody(init?.body),
    };
    requests.push(request);

    const response = responder(request);
    if (response === undefined) {
      throw new Error(`Test sahte cevabı tanımlamadı: ${request.method} ${request.url}`);
    }
    return response;
  });

  vi.stubGlobal("fetch", fetchMock);
  return { requests, fetchMock };
}

/**
 * Dışarıdan tamamlanabilen bir promise.
 *
 * "İstek sürerken buton kilitli mi?" gibi testlerde cevabın ne zaman
 * döneceğini test kontrol eder.
 */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

/** Ağ hatasını (backend kapalı) taklit eder. */
export function installNetworkFailure() {
  const fetchMock = vi.fn(async () => {
    throw new TypeError("Failed to fetch");
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock };
}

function parseBody(body: BodyInit | null | undefined): unknown {
  if (typeof body !== "string") {
    return undefined;
  }
  try {
    return JSON.parse(body);
  } catch {
    return body;
  }
}
