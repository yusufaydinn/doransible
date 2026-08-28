import type { ReactNode } from "react";

/**
 * Tek biçimli durum kutusu.
 *
 * Erişilebilirlik kararları:
 *
 * - Anlam yalnızca renkle verilmez; her tonun görünür bir metin etiketi vardır
 *   ("Hata", "Uyarı", "Bilgi", "Tamamlandı").
 * - Hata ve uyarılar `role="alert"` ile anında duyurulur; nötr durumlar
 *   `role="status"` ile araya girmeden okunur.
 */
export type StatusTone = "info" | "success" | "warning" | "error";

const TONE_LABELS: Record<StatusTone, string> = {
  info: "Bilgi",
  success: "Tamamlandı",
  warning: "Uyarı",
  error: "Hata",
};

interface StatusMessageProps {
  tone: StatusTone;
  title: string;
  children?: ReactNode;
  /** Kutunun bir başlık düzeyi taşıması gerekmiyorsa `false` verilebilir. */
  headingLevel?: 2 | 3 | 4;
}

export function StatusMessage({ tone, title, children, headingLevel = 3 }: StatusMessageProps) {
  const Heading = `h${headingLevel}` as "h2" | "h3" | "h4";
  const isUrgent = tone === "error" || tone === "warning";

  return (
    <div
      className={`status status--${tone}`}
      role={isUrgent ? "alert" : "status"}
      aria-live={isUrgent ? "assertive" : "polite"}
    >
      <p className="status__label">{TONE_LABELS[tone]}</p>
      <Heading className="status__title">{title}</Heading>
      {children}
    </div>
  );
}
