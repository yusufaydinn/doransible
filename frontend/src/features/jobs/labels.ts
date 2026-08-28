/** Job alan değerlerinin kullanıcıya gösterilen karşılıkları. */

import { EXECUTION_MODE_LABELS } from "../../lib/executionMode";
import type { JobStatus, PublicErrorCode } from "./types";

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  pending: "Beklemede",
  running: "Çalışıyor",
  successful: "Başarılı",
  failed: "Başarısız",
  canceled: "İptal edildi",
};

/**
 * Job'un `mode` alanının kullanıcıya gösterilen karşılığı (R1-V3H2B).
 *
 * `lib/executionMode`'daki tek doğruluk kaynağının yeniden dışa aktarımıdır:
 * Job listesi/detayı burayı, execution plan formu kendi kopyasını değil aynı
 * sözlüğü okur.
 */
export const JOB_MODE_LABELS = EXECUTION_MODE_LABELS;

export interface JobErrorMessage {
  title: string;
  description: string;
}

/**
 * Hata kodlarının kullanıcı dili (R1-V3G1).
 *
 * Tip bilinçli olarak `Record<PublicErrorCode, …>`'dur: `PublicErrorCode`
 * union'ına yeni bir kod eklenip bu sözlük güncellenmezse `tsc --noEmit`
 * kırılır, yani hiçbir kod sessizce dilsiz kalamaz.
 *
 * Metin kuralları:
 *
 * - Hiçbiri kök neden iddia etmez; sınıflandırma ile teşhis karıştırılmaz.
 * - Serbest hata ayrıntısı (stdout/stderr, komut, yol, istisna metni) taşımaz;
 *   metinler sabittir.
 * - Anlam yalnız renkten değil metnin kendisinden çıkar.
 */
export const JOB_ERROR_MESSAGES: Record<PublicErrorCode, JobErrorMessage> = {
  playbook_failed: {
    title: "Çalıştırma tamamlandı; playbook başarısız sonuç bildirdi",
    description:
      "Ansible çalıştırması güvenilir bir terminal sonucu üretti; playbook bazı task'ların " +
      "başarısız olduğunu veya bazı host'lara erişilemediğini raporladı. Bu kod, " +
      "başarısızlığın kök nedenini sınıflandırmaz.",
  },
  runner_failed: {
    title: "Çalıştırma sonucu kesin sınıflandırılamadı",
    description:
      "Bu kod genel ve legacy bir toplayıcıdır: playbook içeriği, erken çıkış, çelişkili " +
      "sonuç sinyalleri, çalıştırma altyapısı veya önceki sürümden kalan bir kayıt yüzünden " +
      "görülmüş olabilir. Kod tek başına ne kök nedeni ne de task/host sonucunu " +
      "kesinleştirir; varsa sanitize edilmiş recap ayrıca gösterilir.",
  },
  runner_start_failed: {
    title: "Çalıştırma başlatılamadı",
    description:
      "Çalıştırma süreci hiç başlatılamadı; bu yüzden herhangi bir task veya host sonucu oluşmadı.",
  },
  runner_timeout: {
    title: "Çalıştırma zaman aşımına uğradı",
    description:
      "Çalıştırma tanımlı süre sınırını aştığı için sonlandırıldı; ortaya çıkan sonuç eksik kalmış olabilir.",
  },
  runner_output_invalid: {
    title: "Çalıştırma çıktısı okunamadı",
    description:
      "Çalıştırma çıktısı beklenen biçimde olmadığı için güvenilir bir sonuç belgesine dönüştürülemedi.",
  },
  runner_no_hosts: {
    title: "Hiçbir host işlenmedi",
    description:
      "Ansible terminal sonucu hiçbir host'un işlendiğini göstermedi. Inventory eşleşmesi " +
      "veya playbook hedeflemesi olası nedenlerdir; kesin neden bu koddan anlaşılmaz.",
  },
  workspace_unavailable: {
    title: "Çalışma alanı hazırlanamadı",
    description:
      "Çalıştırma için gereken izole çalışma alanı kullanılamadığından süreç yürütülmedi.",
  },
  workspace_integrity_failed: {
    title: "Çalışma alanı bütünlük denetimini geçemedi",
    description:
      "Çalışma alanı içeriği beklenen bütünlük denetimini geçemediği için çalıştırma yapılmadı.",
  },
  result_limit_exceeded: {
    title: "Sonuç boyut sınırını aştı",
    description:
      "Üretilen sonuç tanımlı boyut sınırını aştığı için bütünüyle saklanamadı; kayıt eksiktir.",
  },
  execution_binding_invalid: {
    title: "Çalıştırma bağlaması geçersiz",
    description:
      "Kaydedilen çalıştırma bağlaması doğrulanamadığı için sonuç güvenilir sayılmadı.",
  },
  interrupted_by_restart: {
    title: "Çalıştırma yeniden başlatmayla kesildi",
    description:
      "Servis yeniden başladığı için çalıştırma yarıda kaldı ve terminal bir sonuç alınamadı.",
  },
  unknown_failure: {
    title: "Sınıflandırılamayan başarısızlık",
    description:
      "Başarısızlık bilinen bir sınıfa yerleştirilemedi; ayrıntısı kullanıcıya gösterilmez.",
  },
};
