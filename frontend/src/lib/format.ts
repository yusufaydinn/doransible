/**
 * Kullanıcıya gösterilen değerlerin biçimlendirilmesi.
 *
 * Backend'den gelen değerler doğrulanmış sayılmaz: bozuk bir tarih dizesi veya
 * beklenmeyen bir tip arayüzü çökertmemelidir.
 */

const DATE_TIME_FORMAT = new Intl.DateTimeFormat("tr-TR", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** Tarih gösterilemiyorsa kullanılan metin. */
export const UNKNOWN_DATE_TEXT = "Bilinmiyor";

/**
 * ISO tarih dizesini okunabilir yerel biçime çevirir.
 *
 * Değer eksik, boş veya çözümlenemez ise `UNKNOWN_DATE_TEXT` döner; istisna
 * fırlatmaz.
 */
export function formatDateTime(value: unknown): string {
  if (typeof value !== "string" || value.trim() === "") {
    return UNKNOWN_DATE_TEXT;
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return UNKNOWN_DATE_TEXT;
  }

  try {
    return DATE_TIME_FORMAT.format(parsed);
  } catch {
    return UNKNOWN_DATE_TEXT;
  }
}

/** Dosya boyutunu kısa ve okunabilir biçimde gösterir. */
export function formatBytes(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "Bilinmiyor";
  }
  if (value < 1024) {
    return `${Math.round(value)} B`;
  }
  return `${(value / 1024).toFixed(1)} KB`;
}
