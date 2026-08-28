/**
 * Backend hata zarfını kullanıcıya gösterilebilir metne çevirir.
 *
 * İki kural bu modülü şekillendirir:
 *
 * 1. `details` nesnesi kullanıcıya **ham JSON olarak gösterilmez**. Yalnızca
 *    tip korumasından geçen, bilinen alanlar metne dönüştürülür.
 * 2. Mesajlar uygulanabilir olmalıdır: kullanıcı ne yapacağını bilmelidir.
 *    Sunucudaki izin verilen kökler gibi bilgiler burada üretilmez; backend
 *    de bunları sızdırmaz (GUVENLIK.md bölüm 3-4).
 */

import { ApiError } from "../../lib/apiClient";

/** Kullanıcıya gösterilecek hata bildirimi. */
export interface ErrorNotice {
  /** Kısa başlık. */
  title: string;
  /** Ne olduğunu ve ne yapılacağını anlatan metin. */
  message: string;
  /**
   * Hata başka bir project kaydına işaret ediyorsa o kaydın kimliği.
   * Arayüz buradan detay sayfasına bağlantı verebilir.
   */
  relatedProjectId?: number;
  /** Yeniden denemenin anlamlı olduğu geçici hatalar için true. */
  retryable?: boolean;
}

const UNKNOWN: ErrorNotice = {
  title: "Beklenmeyen bir hata oluştu",
  message: "İşlem tamamlanamadı. Sayfayı yenileyip tekrar deneyin.",
  retryable: true,
};

/** Herhangi bir hata değerini kullanıcıya gösterilebilir bildirime çevirir. */
export function describeApiError(error: unknown): ErrorNotice {
  if (!(error instanceof ApiError)) {
    return UNKNOWN;
  }

  switch (error.code) {
    case "network_error":
      return {
        title: "Backend'e ulaşılamadı",
        message:
          "Sunucuya bağlanılamadı. Backend servisinin çalıştığını doğrulayıp tekrar deneyin.",
        retryable: true,
      };

    case "invalid_path":
      return {
        title: "Dizin yolu geçersiz",
        message:
          "Girilen yol çözümlenemedi. Controller'daki tam (mutlak) bir dizin yolu yazın; " +
          "örneğin C:\\ansible\\projeler\\web veya /srv/ansible/web.",
      };

    case "path_not_allowed":
      return {
        title: "Bu dizine izin verilmiyor",
        message:
          "Yol, controller'da project olarak kaydedilmesine izin verilen dizinlerin dışında. " +
          "İzin verilen dizini controller yöneticisinden ya da yapılandırmasından öğrenin " +
          "veya project'i o dizinin altına taşıyın.",
      };

    case "path_not_found":
      return {
        title: "Dizin bulunamadı",
        message:
          "Controller'da bu yolda bir dizin yok. Yolun DORAnsible controller'ındaki bir " +
          "dizini gösterdiğinden ve yazımının doğru olduğundan emin olun.",
      };

    case "path_not_a_directory":
      return {
        title: "Yol bir dizin değil",
        message:
          "Bu yol bir dosyayı gösteriyor. Project kökü, playbook'ları içeren dizinin kendisi olmalıdır.",
      };

    case "project_already_exists":
      return describeDuplicate(error);

    case "project_inactive":
      return {
        title: "Project pasif durumda",
        message:
          "Pasife alınmış bir project'te playbook keşfi yapılamaz. Kayıt yalnızca " +
          "geçmişe referans olarak saklanıyor.",
        relatedProjectId: readProjectId(error.details),
      };

    case "project_path_unavailable":
      return describePathUnavailable(error);

    case "not_found":
      return {
        title: "Kayıt bulunamadı",
        message: "İstenen project kaydı yok. Silinmiş veya adres yanlış yazılmış olabilir.",
      };

    case "request_validation_error":
      return {
        title: "Gönderilen bilgiler geçersiz",
        message:
          "Sunucu isteği kabul etmedi. Alanları gözden geçirip tekrar gönderin.",
      };

    default:
      return {
        title: "İşlem tamamlanamadı",
        // Backend mesajları kullanıcıya gösterilmek üzere yazılır ve secret içermez.
        message: error.message,
        retryable: error.status >= 500 || error.status === 0,
      };
  }
}

function describeDuplicate(error: ApiError): ErrorNotice {
  const projectId = readProjectId(error.details);
  const isActive = readIsActive(error.details);

  if (isActive === false) {
    return {
      title: "Bu dizin daha önce kaydedilmiş",
      message:
        "Aynı dizin için bir kayıt zaten var ve şu anda pasif durumda. Aynı dizin ikinci kez " +
        "kaydedilemez. Mevcut kaydı detay sayfasından inceleyebilirsiniz.",
      relatedProjectId: projectId,
    };
  }

  return {
    title: "Bu dizin zaten kayıtlı",
    message:
      "Aynı dizin için aktif bir project kaydı bulunuyor. Mevcut kaydı kullanın veya " +
      "farklı bir dizin girin.",
    relatedProjectId: projectId,
  };
}

function describePathUnavailable(error: ApiError): ErrorNotice {
  const relatedProjectId = readProjectId(error.details);

  switch (readReason(error.details)) {
    case "missing":
      return {
        title: "Project dizini controller'da bulunamadı",
        message:
          "Kayıt oluşturulduktan sonra dizin silinmiş veya taşınmış görünüyor. Dizini eski " +
          "yerine geri koyun ya da project'i pasife alıp doğru yolla yeniden kaydedin.",
        relatedProjectId,
      };

    case "not_a_directory":
      return {
        title: "Project yolu artık bir dizin değil",
        message:
          "Kayıtlı yol şu anda bir dosyayı gösteriyor. Dizini geri getirin ya da project'i " +
          "pasife alıp doğru yolla yeniden kaydedin.",
        relatedProjectId,
      };

    case "changed_during_scan":
      return {
        title: "Dizin tarama sırasında değişti",
        message:
          "Tarama sürerken project dizini değiştiği için sonuç güvenilir değil. Dizinde " +
          "işlem yapan başka bir süreç yoksa tekrar deneyin.",
        relatedProjectId,
        retryable: true,
      };

    default:
      return {
        title: "Project dizini kullanılabilir değil",
        message:
          "Kayıtlı dizine şu anda erişilemiyor. Dizinin controller'da durduğunu doğrulayıp " +
          "tekrar deneyin.",
        relatedProjectId,
        retryable: true,
      };
  }
}

function asRecord(details: unknown): Record<string, unknown> | null {
  if (typeof details !== "object" || details === null || Array.isArray(details)) {
    return null;
  }
  return details as Record<string, unknown>;
}

function readProjectId(details: unknown): number | undefined {
  const value = asRecord(details)?.["project_id"];
  return typeof value === "number" && Number.isInteger(value) ? value : undefined;
}

function readIsActive(details: unknown): boolean | undefined {
  const value = asRecord(details)?.["is_active"];
  return typeof value === "boolean" ? value : undefined;
}

function readReason(details: unknown): string | undefined {
  const value = asRecord(details)?.["reason"];
  return typeof value === "string" ? value : undefined;
}
