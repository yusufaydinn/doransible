/**
 * Job endpoint'lerinin hatalarını kullanıcıya gösterilebilir metne çevirir.
 *
 * `details` nesnesi hiçbir durumda ham JSON olarak gösterilmez; backend zaten
 * bu iki hata için sabit, sızdırmayan bir mesaj döner (GUVENLIK.md bölüm 3).
 */

import { ApiError } from "../../lib/apiClient";
import { describeApiError, type ErrorNotice } from "../projects/errorMessages";

export function describeJobError(error: unknown): ErrorNotice {
  if (!(error instanceof ApiError)) {
    return describeApiError(error);
  }

  switch (error.code) {
    case "job_not_found":
      return {
        title: "Çalıştırma kaydı bulunamadı",
        message: "Çalıştırma kaydı bulunamadı.",
      };

    case "job_result_unavailable":
      return {
        title: "Sonuç şu anda okunamıyor",
        message: "Çalıştırma sonucu şu anda okunamıyor.",
        retryable: true,
      };

    default:
      return describeApiError(error);
  }
}
