/**
 * Execution plan hatalarını kullanıcıya gösterilebilir metne çevirir.
 *
 * Yalnızca bu akışa özgü kodlar burada karşılanır; geri kalanı ortak
 * `describeApiError` çözer. `details` nesnesi hiçbir durumda ham JSON olarak
 * gösterilmez ve backend zaten sunucu yolu, hostvar veya key bilgisi
 * döndürmez (GUVENLIK.md bölüm 3).
 */

import { ApiError } from "../../lib/apiClient";
import { describeApiError, type ErrorNotice } from "../projects/errorMessages";

export function describeExecutionPlanError(error: unknown): ErrorNotice {
  if (!(error instanceof ApiError)) {
    return describeApiError(error);
  }

  switch (error.code) {
    case "inventory_not_linked_to_project":
      return {
        title: "Inventory bu project'e bağlı değil",
        message:
          "Plan yalnızca seçilen project'e bağlı bir inventory ile üretilebilir. " +
          "Bağımsız (project'siz) inventory'ler bu akışta kullanılamaz.",
      };

    case "playbook_not_discovered":
      return {
        title: "Playbook listede yok",
        message:
          "Seçilen playbook project'in güncel keşif sonucunda görünmüyor. Dosya " +
          "silinmiş ya da taşınmış olabilir; listeyi yenileyip tekrar seçin.",
        retryable: true,
      };

    case "inventory_path_unavailable":
      return {
        title: "Inventory dosyası kullanılabilir değil",
        message:
          "Kayıtlı inventory dosyası sunucuda bulunamadı veya artık bir dosya değil. " +
          "Dosyayı yerine koyun ya da inventory kaydını güncelleyin.",
      };

    case "inventory_path_outside_project":
      return {
        title: "Inventory project kökünün dışında",
        message:
          "Kayıtlı inventory dosyası artık project dizininin içinde değil. Kaydı " +
          "gözden geçirin.",
      };

    case "inventory_parse_failed":
      return {
        title: "Inventory ayrıştırılamadı",
        message:
          "Sunucu inventory dosyasını okuyamadı. Dosyanın söz dizimini düzeltip " +
          "tekrar deneyin.",
        retryable: true,
      };

    case "ping_inventory_unsafe":
      return {
        title: "Inventory güvenli çalıştırma için uygun değil",
        message:
          "Inventory, bu uygulamanın desteklemediği bir bağlantı tanımı içeriyor " +
          "(örneğin parola alanı ya da izin verilmeyen bir anahtar yolu). Plan " +
          "üretilmedi.",
      };

    case "execution_workspace_unsafe":
      return {
        title: "Project dondurulamıyor",
        message:
          "Project dizini onay için güvenle kopyalanamadı: bağlantı (symlink), " +
          "normal olmayan bir dosya ya da izin verilen boyut/adet sınırının " +
          "aşılması söz konusu olabilir. Plan hazırlanmadı.",
      };

    case "execution_workspace_unavailable":
      return {
        title: "Plan hazırlanamadı",
        message:
          "Sunucu, onay için gereken dondurulmuş kopyayı oluşturamadı. Daha sonra " +
          "tekrar deneyin.",
        retryable: true,
      };

    case "execution_plan_invalid":
      return {
        title: "Hazırlanan planın süresi doldu",
        message:
          "Planın süresi dolmuş veya daha önce kullanılmış. Planı yeniden hazırlayın.",
        retryable: true,
      };

    case "execution_launch_unavailable":
      return {
        title: "Çalıştırma kuyruğa alınamadı",
        message:
          "Çalıştırma kuyruğa alınamadı. Planı yeniden hazırlayıp tekrar deneyin.",
        retryable: true,
      };

    default:
      return describeApiError(error);
  }
}
