/**
 * Inventory ekranlarının kullandığı TanStack Query sarmalayıcıları.
 *
 * Query davranışı (retry, tazelik) tek yerde toplanır; sayfa bileşenleri
 * `queryClient` ile doğrudan uğraşmaz.
 */

import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { executionKeys } from "../executions/queryKeys";

import {
  cancelPingPreview,
  confirmPing,
  createInventory,
  createPingPreview,
  fetchInventories,
  fetchInventory,
  fetchInventoryHosts,
  fetchPingHistory,
} from "./api";
import { inventoryKeys } from "./queryKeys";
import type {
  CreateInventoryRequest,
  Inventory,
  PingPreviewRequest,
  PingPreviewResponse,
  PingRunResponse,
  PingTokenRequest,
} from "./types";

/** Kayıtlı inventory listesi. */
export function useInventories() {
  return useQuery({
    queryKey: inventoryKeys.list(),
    queryFn: fetchInventories,
  });
}

/** Tek bir inventory kaydı. */
export function useInventory(inventoryId: number) {
  return useQuery({
    queryKey: inventoryKeys.detail(inventoryId),
    queryFn: () => fetchInventory(inventoryId),
    enabled: Number.isInteger(inventoryId),
  });
}

/**
 * Inventory içeriği (host ve gruplar).
 *
 * Okuma sunucuda ayrı bir süreç çalıştırır ve dosya sistemini o an okur; sonuç
 * kısa ömürlüdür ve tekrar denemek kullanıcının kararıdır. Bu yüzden otomatik
 * retry kapalıdır: ayrıştırılamayan bir dosya için sessizce üç kez daha süreç
 * başlatmanın kullanıcıya faydası yok, maliyeti var.
 */
export function useInventoryHosts(inventoryId: number) {
  return useQuery({
    queryKey: inventoryKeys.hosts(inventoryId),
    queryFn: () => fetchInventoryHosts(inventoryId),
    enabled: Number.isInteger(inventoryId),
    retry: false,
    staleTime: 0,
  });
}

/**
 * Bir inventory'nin kalıcı ping ölçüm geçmişi (R1-V3J1A).
 *
 * **Bu gerçek zamanlı bir izleme kanalı değildir.** Sorgu yalnızca kullanıcı
 * tarafından başlatılmış, tamamlanmış ve kalıcılaştırılmış ölçümleri okur;
 * `refetchInterval` bilinçli olarak **yoktur** ve hiçbir arka plan yoklaması
 * kurulmaz. Ekranın kendiliğinden tazelenmesi, olmayan bir canlı akış varmış
 * izlenimi verirdi.
 *
 * `retry: false`: hata kullanıcıya açıkça bildirilir ve tekrar denemek onun
 * kararıdır. `staleTime: 0`: sayfaya dönüldüğünde veya ping çalıştırıldıktan
 * sonra veri bayat sayılır, böylece invalidation gerçekten yeni bir okuma
 * doğurur.
 */
export function usePingHistory(inventoryId: number) {
  return useQuery({
    queryKey: inventoryKeys.pingHistory(inventoryId),
    queryFn: () => fetchPingHistory(inventoryId),
    // Kimlik pozitif tam sayı değilse hiç istek çıkmaz; geçersiz bir URL
    // kurup 404'ü kullanıcıya hata olarak göstermenin faydası yok.
    enabled: Number.isSafeInteger(inventoryId) && inventoryId > 0,
    retry: false,
    staleTime: 0,
  });
}

/**
 * Sunucuda zaten var olan bir inventory dosyasının kaydını oluşturur.
 *
 * Başarıda yeni kaydı hem liste hem detay cache'ine yazar. Kayıt bir project'e
 * bağlıysa o project'in çalıştırma planı formunun okuduğu
 * `executionKeys.projectInventories` sorgusu da geçersizleştirilir; aksi hâlde
 * yeni inventory, kaydedildiği project'in plan formunda görünmezdi.
 */
export function useCreateInventory() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (request: CreateInventoryRequest) => createInventory(request),
    onSuccess: (inventory: Inventory) => {
      queryClient.setQueryData(inventoryKeys.detail(inventory.id), inventory);
      void queryClient.invalidateQueries({ queryKey: inventoryKeys.list() });
      if (inventory.project_id !== null) {
        void queryClient.invalidateQueries({
          queryKey: executionKeys.projectInventories(inventory.project_id),
        });
      }
    },
  });
}

/**
 * Ping eylemleri (T-204). **Bilinçli olarak TanStack mutation değildir.**
 *
 * `useMutation` her çağrıyı `MutationCache`'e yazar: `variables` isteğin
 * gövdesini, `data` da cevabı taşır. Ping akışında bunların ikisi de onay
 * token'ıdır — preview cevabı token döner, confirm/cancel istekleri token
 * gönderir. `reset()` bu kaydı **silmez**; yalnızca observer'ı ayırır ve
 * varsayılan garbage collection süresini (5 dakika) başlatır. Dahası, istek
 * sürerken bileşen unmount olursa `mutate()` callback'leri hiç çalışmayabilir
 * ve `reset()` güvencesi tümüyle devre dışı kalır.
 *
 * Bu yüzden çözüm "cache'i sonradan temizlemek" değil, token'ı o cache'e hiç
 * sokmamaktır: aşağıdaki yüzey API fonksiyonlarını doğrudan çağıran, durum
 * tutmayan imperative `Promise`'lerden ibarettir. Sonuç olarak `preview_token`
 * hiçbir anda mutation cache'inin `data`, `variables`, `key` veya `meta`
 * alanına girmez.
 *
 * Diğer iki kural da bu yüzeyle sağlanır:
 *
 * - **Otomatik retry yoktur.** Her açık kullanıcı eylemi tam olarak bir API
 *   çağrısı üretir; yeniden deneme mantığı hiç mevcut değildir. Kullanıcının
 *   görmediği bir tekrar, onaylamadığı ikinci bir execution demek olurdu.
 * - **Inventory kaydı ve dosya içeriği tazelenmez.** Ping ne kaydı ne de
 *   dosyayı değiştirir. Başarılı bir `confirm` sonrasında yalnızca
 *   `inventoryKeys.pingHistory` geçersizleştirilir: ölçüm tam da o anda kalıcı
 *   hâle geldiği için geçmiş listesi eskimiştir. Invalidation'a **yalnızca**
 *   query key verilir; token invalidation çağrısına hiç girmez.
 *
 * URL kurgusu `api.ts` içinde kalır; bileşenler doğrudan `fetch` çağırmaz.
 */
export interface PingActions {
  /** Onay planını ve tek kullanımlık token'ı üretir; hiçbir şey çalıştırmaz. */
  preview: (request: PingPreviewRequest) => Promise<PingPreviewResponse>;
  /** Onaylanmamış bir preview'ı iptal eder; başarıda gövde dönmez. */
  cancel: (request: PingTokenRequest) => Promise<void>;
  /** Onaylanmış planı çalıştırır. */
  confirm: (request: PingTokenRequest) => Promise<PingRunResponse>;
}

export function usePingActions(inventoryId: number): PingActions {
  const queryClient = useQueryClient();

  const preview = useCallback(
    (request: PingPreviewRequest) => createPingPreview(inventoryId, request),
    [inventoryId],
  );

  const cancel = useCallback(
    (request: PingTokenRequest) => cancelPingPreview(inventoryId, request),
    [inventoryId],
  );

  const confirm = useCallback(
    async (request: PingTokenRequest) => {
      const run = await confirmPing(inventoryId, request);
      // Sıra bilinçlidir: geçmiş **yalnızca** confirm başarıyla döndükten sonra
      // geçersizleştirilir. Hata yolunda bu satıra hiç gelinmez, dolayısıyla
      // eski geçmiş ekranda olduğu gibi kalır ve başarısız bir istek yüzünden
      // gereksiz bir okuma yapılmaz.
      void queryClient.invalidateQueries({
        queryKey: inventoryKeys.pingHistory(inventoryId),
      });
      return run;
    },
    [inventoryId, queryClient],
  );

  return useMemo(
    () => ({ preview, cancel, confirm }),
    [preview, cancel, confirm],
  );
}
