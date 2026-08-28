/**
 * Inventory hata zarfını kullanıcıya gösterilebilir metne çevirir.
 *
 * İki kural bu modülü şekillendirir:
 *
 * 1. `details` nesnesi kullanıcıya **ham JSON olarak gösterilmez**. Yalnızca
 *    tip korumasından geçen, bilinen alanlar (`reason`, `stream`,
 *    `parser_message`, `inventory_id`, `job_id`) metne dönüştürülür. Bilinmeyen
 *    bir alan veya beklenmeyen bir tip sessizce yok sayılır ve genel mesaj
 *    kullanılır.
 * 2. Mesajlar uygulanabilir olmalıdır: kullanıcı ne yapacağını bilmelidir.
 *
 * Dosyanın ikinci yarısı ping akışına (T-204) aittir. Ping kodları ayrı bir
 * eşleyicidedir çünkü aynı kod **adıma göre** farklı anlam taşır; ping'e özgü
 * olmayan kodlar oradan bu dosyadaki inventory eşleyicisine devredilir ve
 * yeniden adlandırılmaz.
 *
 * `parser_message` özel bir durumdur: backend onu **temizleyerek** üretir
 * (mutlak yollar `<path>` ile değiştirilir, traceback çerçeveleri silinir,
 * secret biçimleri maskelenir, metin kırpılır — MIMARI.md bölüm 7). Arayüz onu
 * ancak string olduğunu doğruladıktan sonra gösterir ve içeriğine dokunmaz.
 */

import { ApiError } from "../../lib/apiClient";

/** Kullanıcıya gösterilecek hata bildirimi. */
export interface InventoryErrorNotice {
  /** Kısa başlık. */
  title: string;
  /** Ne olduğunu ve ne yapılacağını anlatan metin. */
  message: string;
  /**
   * Backend'in temizlediği parser açıklaması.
   *
   * Yalnızca `inventory_parse_failed` için ve yalnızca string type guard'ından
   * geçtiğinde doldurulur. Ham stderr **değildir**.
   */
  parserMessage?: string;
  /** Hata bir inventory kaydına işaret ediyorsa o kaydın kimliği. */
  relatedInventoryId?: number;
  /** Yeniden denemenin anlamlı olduğu geçici hatalar için true. */
  retryable?: boolean;
}

const UNKNOWN: InventoryErrorNotice = {
  title: "Beklenmeyen bir hata oluştu",
  message: "İşlem tamamlanamadı. Sayfayı yenileyip tekrar deneyin.",
  retryable: true,
};

/** Herhangi bir hata değerini kullanıcıya gösterilebilir bildirime çevirir. */
export function describeInventoryError(error: unknown): InventoryErrorNotice {
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

    case "not_found":
      return {
        title: "Inventory bulunamadı",
        message:
          "İstenen inventory kaydı yok. Silinmiş veya adres yanlış yazılmış olabilir.",
      };

    case "invalid_path":
      return {
        title: "Dosya yolu geçersiz",
        message:
          "Girilen yol çözümlenemedi. Controller'daki tam (mutlak) bir dosya yolu yazın; " +
          "örneğin /srv/ansible/web/inventories/production.ini veya " +
          "C:\\ansible\\envanterler\\production.yml.",
      };

    case "path_not_allowed":
      return {
        title: "Bu yola izin verilmiyor",
        message:
          "Yol, controller'da inventory olarak kaydedilmesine izin verilen dizinlerin " +
          "dışında. İzin verilen dizini controller yöneticisinden ya da yapılandırmasından " +
          "öğrenin veya dosyayı o dizinin altına taşıyın.",
      };

    case "path_not_found":
      return {
        title: "Dosya controller'da bulunamadı",
        message:
          "Controller'da bu yolda bir dosya yok. Yolun DORAnsible controller'ındaki bir " +
          "dosyayı gösterdiğinden ve yazımının doğru olduğundan emin olun.",
      };

    case "path_not_a_file":
      return {
        title: "Yol bir dosya değil",
        message:
          "Girilen yol normal bir dosyayı göstermiyor; örneğin bir dizin ya da özel bir " +
          "dosya olabilir. Inventory dosyasının kendisinin tam yolunu yazın.",
      };

    case "inventory_path_outside_project":
      return {
        title: "Dosya, seçilen project'in dışında",
        message:
          "Bir project'e bağlanan inventory dosyası, o project'in kendi dizini altında " +
          "olmalıdır. Yolu düzeltin ya da dosyayı bağımsız (standalone) olarak kaydedin.",
      };

    case "project_inactive":
      return {
        title: "Project pasif durumda",
        message:
          "Seçilen project artık aktif değil; pasif bir project'e yeni inventory " +
          "bağlanamaz. Farklı bir project seçin ya da bağımsız (standalone) olarak " +
          "kaydedin.",
      };

    case "inventory_path_unavailable":
      return describePathUnavailable(error);

    case "inventory_parser_unavailable":
      return {
        title: "Inventory parser kullanılamıyor",
        message:
          "Controller'da ansible-inventory çalıştırılamadı. Genellikle ansible-core kurulu " +
          "değildir ya da kurulum bu platformda çalışmamaktadır; Ansible, Windows'u " +
          "control node olarak desteklemez. Inventory kaydınız etkilenmedi, yalnızca " +
          "içerik önizlemesi yapılamıyor.",
        relatedInventoryId: readInventoryId(error.details),
      };

    case "inventory_parse_timeout":
      return {
        title: "Inventory okuma zaman aşımına uğradı",
        message:
          "Parser verilen süre içinde tamamlanmadı ve durduruldu. Çok büyük bir inventory " +
          "veya yanıt vermeyen bir kaynak buna yol açabilir. Tekrar deneyin; sorun sürerse " +
          "inventory'yi küçültün.",
        relatedInventoryId: readInventoryId(error.details),
        retryable: true,
      };

    case "inventory_parse_output_too_large":
      return describeOutputTooLarge(error);

    case "inventory_parse_invalid_output":
      return {
        title: "Parser çıktısı anlaşılamadı",
        message:
          "ansible-inventory beklenen JSON biçiminde cevap vermedi. Controller'daki Ansible " +
          "sürümü desteklenmiyor veya kurulum bozuk olabilir.",
        relatedInventoryId: readInventoryId(error.details),
      };

    case "inventory_parse_failed":
      return {
        title: "Inventory dosyası ayrıştırılamadı",
        message:
          "Ansible bu dosyayı okuyamadı. Kök neden bu sonuçtan tek başına kesin biçimde " +
          "sınıflandırılamaz; genellikle dosya içeriğiyle ilgilidir. Aşağıdaki açıklamaya " +
          "göre dosyayı gözden geçirip tekrar deneyin.",
        parserMessage: readParserMessage(error.details),
        relatedInventoryId: readInventoryId(error.details),
      };

    case "request_validation_error":
      return {
        title: "Gönderilen bilgiler geçersiz",
        message: "Sunucu isteği kabul etmedi. Alanları gözden geçirip tekrar gönderin.",
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

/**
 * Kayıtlı dosyanın artık kullanılamamasını açıklar.
 *
 * Kayıt anındaki kontroller kalıcı bir garanti değildir; dosya sistemi sonradan
 * değişir (MIMARI.md bölüm 7).
 */
function describePathUnavailable(error: ApiError): InventoryErrorNotice {
  const relatedInventoryId = readInventoryId(error.details);

  switch (readReason(error.details)) {
    case "missing":
      return {
        title: "Inventory dosyası controller'da bulunamadı",
        message:
          "Kayıt oluşturulduktan sonra dosya silinmiş veya taşınmış görünüyor. Dosyayı " +
          "eski yerine geri koyun ya da inventory'yi doğru yolla yeniden kaydedin.",
        relatedInventoryId,
      };

    case "not_a_file":
      return {
        title: "Inventory yolu artık bir dosya değil",
        message:
          "Kayıtlı yol şu anda bir dosyayı göstermiyor. Dosyayı geri getirin ya da " +
          "inventory'yi doğru yolla yeniden kaydedin.",
        relatedInventoryId,
      };

    default:
      return {
        title: "Inventory dosyası kullanılabilir değil",
        message:
          "Kayıtlı dosyaya şu anda erişilemiyor. Dosyanın controller'da durduğunu doğrulayıp " +
          "tekrar deneyin.",
        relatedInventoryId,
        retryable: true,
      };
  }
}

/**
 * Çıktı sınırı aşımını, hangi akışın taştığına göre açıklar.
 *
 * İki durum kullanıcı için farklı anlam taşır: `stdout` sonucun kendisinin çok
 * büyük olması, `stderr` ise parser'ın sınırsız hata metni üretmesidir.
 * Doğrulanamayan bir değer için genel mesaj kullanılır.
 */
function describeOutputTooLarge(error: ApiError): InventoryErrorNotice {
  const relatedInventoryId = readInventoryId(error.details);
  const base = {
    title: "Inventory çıktısı boyut sınırını aştı",
    relatedInventoryId,
  };

  switch (readStream(error.details)) {
    case "stdout":
      return {
        ...base,
        message:
          "Inventory'nin çözümlenmiş hâli kabul edilen boyut sınırını aştığı için işlem " +
          "durduruldu. Inventory'yi daha küçük dosyalara bölmeniz ya da controller'daki " +
          "çıktı sınırının yükseltilmesi gerekir.",
      };

    case "stderr":
      return {
        ...base,
        message:
          "Parser, sınırı aşacak kadar çok hata metni ürettiği için işlem durduruldu. Bu " +
          "genellikle inventory dosyasında tekrar eden bir hata olduğunu gösterir; dosyayı " +
          "gözden geçirin.",
      };

    default:
      return {
        ...base,
        message:
          "Parser kabul edilen boyut sınırından fazla çıktı ürettiği için işlem durduruldu. " +
          "Inventory dosyasını gözden geçirin.",
      };
  }
}

/* --- Ping (T-204) ---------------------------------------------------------- */

/**
 * Hatanın hangi ping adımında oluştuğu.
 *
 * Adım bilgisi gereklidir çünkü aynı hata kodu farklı adımlarda **farklı**
 * anlam taşır: cevapsız kalan bir `confirm` isteği gerçekten çalışmış olabilir,
 * cevapsız kalan bir `preview` isteği ise hiçbir şey çalıştırmamıştır.
 */
export type PingStage = "preview" | "confirm" | "cancel";

/** Ping akışına özgü ek alanlarla genişletilmiş bildirim. */
export interface PingErrorNotice extends InventoryErrorNotice {
  /** Hata bir Job'a işaret ediyorsa, canonical UUID doğrulamasından geçmiş kimliği. */
  jobId?: string;
  /**
   * Mevcut onayın tükendiği ve yeni bir önizleme gerektiği.
   *
   * Confirm akışında token **en başta** claim edilir; bu yüzden başarısız bir
   * istek de onu tüketir (ADR-019 Karar 6). Tek istisna, isteğin servise hiç
   * ulaşmadan şema doğrulamasında elenmesidir.
   */
  requiresNewPreview?: boolean;
}

/**
 * Ping hata zarfını kullanıcıya gösterilebilir metne çevirir.
 *
 * Saf bir fonksiyondur: React, router veya `queryClient` bilmez. `details` ham
 * JSON olarak **gösterilmez**; yalnızca `reason`, `stream` ve canonical UUID
 * biçimindeki `job_id` tip korumasından geçer. Token ve doğrulanamayan hiçbir
 * değer metne girmez.
 *
 * Ping'e özgü olmayan kodlar (inventory, path, parser) mevcut inventory
 * eşleyicisine devredilir; ping onları yeniden adlandırmaz.
 */
export function describePingError(error: unknown, stage: PingStage): PingErrorNotice {
  if (!(error instanceof ApiError)) {
    return describeNonApiError(stage);
  }

  return { ...describePingCode(error, stage), ...consumedToken(error, stage) };
}

/**
 * `ApiError` olmayan bir arızayı açıklar.
 *
 * Hata nesnesi veya metni hiçbir adımda **basılmaz**: kaynağı bilinmeyen bir
 * değer, redaction hattından geçmemiş sunucu ayrıntısı ya da yığın izi
 * taşıyabilir.
 *
 * Confirm adımında güvenli taraf, işin başlamış olabileceğini varsaymaktır.
 * İstek gönderildikten sonra oluşan beklenmedik bir arıza, token'ın claim
 * edildiği ve ping'in çalıştığı bir dünyayla tümüyle uyumludur; bunun aksini
 * istemci doğrulayamaz.
 */
function describeNonApiError(stage: PingStage): PingErrorNotice {
  if (stage !== "confirm") {
    return UNKNOWN;
  }

  return {
    title: "Ping isteği beklenmedik biçimde sonuçlandı",
    message:
      "İşlem tamamlanamadı ve sonucu doğrulanamadı; ping başlamış, hatta tamamlanmış " +
      "olabilir. Aynı onayla tekrar denemeyin. Önce sunucudaki iş kaydından ping'in " +
      "durumunu doğrulayın; yeniden çalıştırmak gerekirse ancak bundan sonra yeni bir " +
      "önizleme oluşturun.",
    retryable: false,
    requiresNewPreview: true,
  };
}

/**
 * Onayın tükenip tükenmediğini adım ve koda göre belirler.
 *
 * `request_validation_error` gövdeyi servise hiç ulaştırmaz, yani claim
 * yapılmamıştır. Diğer bütün confirm arızaları claim'den **sonradır**; taşıma
 * hatası da güvenli tarafta tükenmiş sayılır, çünkü isteğin sunucuya ulaşıp
 * ulaşmadığı istemciden bilinemez.
 */
function consumedToken(error: ApiError, stage: PingStage): { requiresNewPreview?: boolean } {
  if (stage !== "confirm" || error.code === "request_validation_error") {
    return {};
  }
  return { requiresNewPreview: true };
}

function describePingCode(error: ApiError, stage: PingStage): PingErrorNotice {
  switch (error.code) {
    case "network_error":
      return describePingNetworkError(stage);

    case "request_validation_error":
      return {
        title: "İstek sunucu tarafından kabul edilmedi",
        message:
          stage === "preview"
            ? "Gönderilen limit değeri beklenen biçimde değil. Alanı boş bırakırsanız " +
              "inventory'nin tamamı hedeflenir."
            : "Onay bilgisi beklenen biçimde değil. Yeni bir önizleme oluşturup planı " +
              "tekrar onaylayın.",
      };

    case "ping_invalid_limit":
      return {
        title: "Limit kabul edilmedi",
        message:
          "Girilen host pattern'i çözümlenemedi. Alanı boş bırakırsanız inventory'nin " +
          "tamamı hedeflenir; bir değer yazacaksanız inventory'de bulunan bir host ya da " +
          "grup adı kullanın.",
      };

    case "ping_no_hosts_matched":
      return {
        title: "Limit hiçbir host ile eşleşmedi",
        message:
          "Verilen pattern bu inventory'deki hiçbir host'a denk gelmiyor. Host ve grup " +
          "adlarını inventory içeriğinden doğrulayıp tekrar deneyin.",
      };

    case "ping_inventory_unsafe":
      return {
        title: "Inventory güvenli biçimde ping'lenemiyor",
        message:
          "Inventory, desteklenmeyen bir bağlantı değişkeni, hedef adresi veya kimlik " +
          "yöntemi içeriyor. Desteklenen tek kimlik yöntemi, izin verilen secrets kökü " +
          "altındaki doğrulanmış bir private key dosyasıdır. Inventory'nin bağlantı " +
          "değişkenlerini gözden geçirin.",
      };

    case "ping_preview_unavailable":
      return describePreviewUnavailable(stage);

    case "ping_preview_invalid":
      return describePreviewInvalid(error);

    case "job_already_running":
      return {
        title: "Bu inventory için bir ping işi zaten çalışıyor",
        message:
          "Aynı inventory üzerinde aynı anda yalnızca bir ping çalışabilir. Çalışan işin " +
          "bitmesini bekleyin, sonra yeni bir önizleme oluşturup tekrar onaylayın.",
        jobId: readJobId(error.details),
      };

    case "ping_artifact_unavailable":
      return {
        title: "Ping işi başlatılamadı",
        message:
          "Sunucu iş kaydını veya sonuç dizinini hazırlayamadı; bu adım bağlantı " +
          "kurulmadan önce gelir, yani hiçbir host'a ping gönderilmedi. Yeni bir " +
          "önizleme oluşturup tekrar deneyin.",
        retryable: true,
      };

    case "ping_artifact_write_failed":
      return {
        title: "Ping sonucu kaydedilemedi",
        message:
          "Ping çalıştı ancak sonucu kalıcı olarak kaydedilemedi ya da iş terminal " +
          "duruma alınamadı. Sonuç sunucuda korunmuş olabilir; tekrar çalıştırmadan önce " +
          "iş kaydının durumunu doğrulayın.",
        jobId: readJobId(error.details),
      };

    // Aynı kod iki farklı anda dönebilir: çalışma alanı **hazırlanırken**
    // (ping başlamamıştır) ve ping ile Job tamamlandıktan sonra alan
    // **temizlenirken** (ping çalışmış, sonuç kaydedilmiş olabilir). İstemci
    // ikisini ayırt edemez, bu yüzden mesaj hiçbirini varsaymaz.
    case "ping_snapshot_unavailable":
      return {
        title: "Ping çalışma alanı arızası",
        message:
          "Sunucu, onaylanan plana ait geçici çalışma alanını hazırlayamadı ya da iş " +
          "bittikten sonra temizleyemedi. Bu iki durum dışarıdan ayırt edilemez: ping hiç " +
          "başlamamış olabileceği gibi, çalışmış ve sonucu kaydedilmiş de olabilir. Aynı " +
          "onayla tekrar denemeyin. Önce sunucudaki iş kaydından ping'in durumunu " +
          "doğrulayın; yeni bir önizleme ancak bundan sonra oluşturulmalıdır.",
        retryable: false,
      };

    case "ping_known_hosts_unavailable":
      return {
        title: "Host anahtarı dosyası hazırlanamadı",
        message:
          "SSH host anahtarı doğrulaması için gereken dosya hazırlanamadı. Doğrulama " +
          "olmadan bağlantı kurulmaz, bu yüzden ping başlatılmadı. Yeni bir önizleme " +
          "oluşturup tekrar deneyin.",
        retryable: true,
      };

    case "ansible_unavailable":
      return {
        title: "Ansible çalıştırılamıyor",
        message:
          "Sunucuda ansible süreci başlatılamadı. Genellikle ansible-core kurulu " +
          "değildir ya da kurulum bu platformda çalışmamaktadır; Ansible, Windows'u " +
          "control node olarak desteklemez.",
      };

    case "ping_timeout":
      return {
        title: "Ping zaman aşımına uğradı",
        message:
          "Süreç verilen süre içinde tamamlanmadı ve durduruldu. Host'lar yanıt vermiyor " +
          "ya da ağ çok yavaş olabilir. Daha dar bir limit ile yeni bir önizleme " +
          "oluşturmayı deneyin.",
      };

    case "ping_output_too_large":
      return describePingOutputTooLarge(error);

    case "ping_invalid_output":
      return {
        title: "Ping çıktısı doğrulanamadı",
        message:
          "Ansible çıktısı güvenli biçimde doğrulanamadı. Kök neden bu sonuçtan tek " +
          "başına belirlenemez; controller üzerindeki çalışma kayıtları incelenmelidir.",
      };

    default:
      // Inventory, path ve parser kodları T-202'deki hâliyle döner; ping onları
      // yeniden adlandırmaz (MIMARI.md bölüm 7).
      return describeInventoryError(error);
  }
}

/**
 * Taşıma hatasını adıma göre açıklar.
 *
 * Hiçbir adımda "istek sunucuya ulaşmadı" denmez: `fetch` arızası isteğin
 * gönderilmediğini değil, **cevabın alınamadığını** gösterir. Bu yüzden mesajlar
 * sunucu tarafında ne olduğuna dair kesinlik iddia etmez; yalnızca o adımın
 * yapısal olarak neyi yapamayacağını söyler (örneğin preview'ın ping
 * çalıştırmaması).
 *
 * `confirm` bilinçli olarak farklıdır: istek ulaşmış ve ping başlamış olabilir.
 * Otomatik tekrar, kullanıcının bir kez onayladığı işi ikinci kez çalıştırma
 * riski taşır.
 */
function describePingNetworkError(stage: PingStage): PingErrorNotice {
  if (stage === "confirm") {
    return {
      title: "Sunucudan cevap alınamadı; ping başlamış olabilir",
      message:
        "İstek sunucuya ulaşmış ve ping çalışmaya başlamış olabilir; yalnızca cevabı " +
        "alamadık. Bu isteği otomatik olarak tekrar etmeyin. Önce sunucudaki iş kaydından " +
        "sonucu doğrulayın; yeniden çalıştırmak gerekirse yeni bir önizleme oluşturup " +
        "planı tekrar onaylayın.",
      retryable: false,
    };
  }

  if (stage === "preview") {
    return {
      title: "Sunucudan cevap alınamadı",
      message:
        "İsteğin sunucuya ulaşıp ulaşmadığı doğrulanamadı, bu yüzden onay planının " +
        "oluşup oluşmadığı da bilinemez. Kesin olan şu: bu adım hiçbir SSH bağlantısı " +
        "kurmaz ve hiçbir host'a ping göndermez, yani çalışan bir iş bırakmaz. Size bir " +
        "onay dönmediği için oluşmuş olabilecek plan onaylanamaz ve süresi dolduğunda " +
        "sunucuda temizlenir. Backend servisinin çalıştığını doğrulayıp yeni bir " +
        "önizleme oluşturun.",
      retryable: true,
    };
  }

  return {
    title: "İptal sonucu doğrulanamadı",
    message:
      "İptal isteğinin sunucuya ulaşıp ulaşmadığı ve planın gerçekten temizlenip " +
      "temizlenmediği doğrulanamadı. Bu onayı yeniden kullanmayın. Onaylanmamış bir " +
      "plan süresi dolduğunda kendiliğinden geçersizleşir; ping çalıştırmak isterseniz " +
      "açıkça yeni bir önizleme oluşturun.",
    retryable: true,
  };
}

/**
 * Preview state deposunun arızasını adıma göre açıklar.
 *
 * Aynı kod üç farklı anda dönebilir ve üçünün sonucu farklıdır: preview
 * yayımlanırken, cancel temizlerken ve confirm akışının **sonunda** claim
 * edilmiş state atılırken. Sonuncusu ping ile Job tamamlandıktan sonradır; bu
 * yüzden confirm adımında "ping çalışmadı" demek yanlış olurdu.
 */
function describePreviewUnavailable(stage: PingStage): PingErrorNotice {
  switch (stage) {
    case "preview":
      return {
        title: "Ping önizlemesi hazırlanamadı",
        message:
          "Sunucu onay planını kaydedemedi veya okuyamadı. Planın sunucuda oluşup " +
          "oluşmadığı buradan bilinemez; ancak size bir onay dönmediği için o plan " +
          "onaylanamaz ve süresi dolduğunda temizlenir. Bu adım hiçbir SSH bağlantısı " +
          "kurmaz ve ping çalıştırmaz. Inventory kaydınız etkilenmedi; yeni bir önizleme " +
          "oluşturmayı deneyebilirsiniz.",
        retryable: true,
      };

    case "cancel":
      return {
        title: "Önizleme iptali doğrulanamadı",
        message:
          "Sunucu iptal isteğini tamamlayamadı; planın gerçekten temizlenip temizlenmediği " +
          "doğrulanamıyor. Bu onayı yeniden kullanmayın. Onaylanmamış bir plan süresi " +
          "dolduğunda kendiliğinden geçersizleşir; ping çalıştırmak isterseniz açıkça yeni " +
          "bir önizleme oluşturun.",
        retryable: true,
      };

    default:
      return {
        title: "Ping onay durumu okunamadı",
        message:
          "Sunucu onay state'ini okuyamadı veya temizleyemedi. Bu arıza akışın sonunda da " +
          "oluşabilir: ping başlamış, hatta tamamlanmış ve sonucu kaydedilmiş olabilir. " +
          "Aynı onayla tekrar denemeyin. Önce sunucudaki iş kaydından ping'in durumunu " +
          "doğrulayın; yeni bir execution ancak bundan sonra başlatılmalıdır.",
        retryable: false,
      };
  }
}

/**
 * Geçersiz onayı, backend'in verdiği `reason` alt durumuna göre açıklar.
 *
 * `reason` yalnızca üç değer alabilir; `already_used` diye bir garanti
 * **yoktur**: kullanılmış token'ın state'i silindiği için "kullanılmış" ile
 * "hiç var olmamış" ayırt edilemez (ADR-018 Karar 10).
 */
function describePreviewInvalid(error: ApiError): PingErrorNotice {
  switch (readPreviewReason(error.details)) {
    case "expired":
      return {
        title: "Onay süresi doldu",
        message:
          "Plan, geçerlilik süresi içinde onaylanmadı ve güvenlik gereği geçersiz kılındı. " +
          "Yeni bir önizleme oluşturup planı tekrar gözden geçirin.",
      };

    case "mismatch":
      return {
        title: "Plan artık geçerli değil",
        message:
          "Onaylanan plan ile sunucunun güncel durumu birebir eşleşmiyor; örneğin host " +
          "anahtarı politikası onaydan sonra değişmiş olabilir. Hiçbir süreç " +
          "başlatılmadı. Yeni bir önizleme oluşturun ve planı yeniden onaylayın.",
      };

    default:
      return {
        title: "Onay geçerli değil",
        message:
          "Bu onay tanınmadı ya da daha önce kullanıldı. Onaylar tek kullanımlıktır; " +
          "yeni bir önizleme oluşturup planı tekrar onaylayın.",
      };
  }
}

/** Ping çıktısı sınırının hangi akışta aşıldığını açıklar. */
function describePingOutputTooLarge(error: ApiError): PingErrorNotice {
  const base = { title: "Ping çıktısı boyut sınırını aştı" };

  switch (readStream(error.details)) {
    case "stdout":
      return {
        ...base,
        message:
          "Ansible, kabul edilen sınırdan fazla sonuç ürettiği için işlem durduruldu. " +
          "Daha dar bir limit ile yeni bir önizleme oluşturup daha az host hedefleyin.",
      };

    case "stderr":
      return {
        ...base,
        message:
          "Ansible, sınırı aşacak kadar çok hata metni ürettiği için işlem durduruldu. Bu " +
          "genellikle her host'ta tekrar eden bir bağlantı sorununu gösterir; inventory'nin " +
          "bağlantı değişkenlerini gözden geçirin.",
      };

    default:
      return {
        ...base,
        message:
          "Ping kabul edilen boyut sınırından fazla çıktı ürettiği için işlem durduruldu. " +
          "Daha dar bir limit ile tekrar deneyin.",
      };
  }
}

/** `reason` yalnızca backend'in verdiği üç değerden biriyse kabul edilir. */
function readPreviewReason(details: unknown): "expired" | "mismatch" | "invalid" | undefined {
  const value = asRecord(details)?.["reason"];
  return value === "expired" || value === "mismatch" || value === "invalid"
    ? value
    : undefined;
}

/**
 * Job kimliğini yalnızca canonical UUID biçimindeyse okur.
 *
 * Backend kimlikleri canonical UUID4 olarak üretir. Biçim doğrulaması, hata
 * zarfından gelen serbest bir metnin ekrana basılmasını engeller: yanlış tip,
 * dizi, nesne veya beklenmeyen bir değer sessizce yok sayılır.
 */
function readJobId(details: unknown): string | undefined {
  const value = asRecord(details)?.["job_id"];
  return typeof value === "string" && CANONICAL_UUID.test(value) ? value : undefined;
}

const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function asRecord(details: unknown): Record<string, unknown> | null {
  if (typeof details !== "object" || details === null || Array.isArray(details)) {
    return null;
  }
  return details as Record<string, unknown>;
}

function readInventoryId(details: unknown): number | undefined {
  const value = asRecord(details)?.["inventory_id"];
  return typeof value === "number" && Number.isInteger(value) ? value : undefined;
}

function readReason(details: unknown): string | undefined {
  const value = asRecord(details)?.["reason"];
  return typeof value === "string" ? value : undefined;
}

/** `stream` yalnızca bilinen iki değerden biriyse kabul edilir. */
function readStream(details: unknown): "stdout" | "stderr" | undefined {
  const value = asRecord(details)?.["stream"];
  return value === "stdout" || value === "stderr" ? value : undefined;
}

/**
 * Temizlenmiş parser açıklamasını okur.
 *
 * String değilse (nesne, dizi, sayı, eksik) gösterilmez: kullanıcıya yalnızca
 * metin gösterilir, hiçbir koşulda serileştirilmiş bir yapı gösterilmez.
 * Boş veya yalnızca boşluktan oluşan metin de gösterilmez.
 */
function readParserMessage(details: unknown): string | undefined {
  const value = asRecord(details)?.["parser_message"];
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}
