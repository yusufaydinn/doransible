/**
 * Host değişkenlerinin güvenli gösterimi.
 *
 * Değerler backend tarafından **zaten maskelenmiş** gelir: secret anahtarları
 * ve secret görünümlü değerler `***` olur ve maskeleme iç içe yapılarda da
 * uygulanır (GUVENLIK.md bölüm 9). Arayüzün buradaki tek işi, gelen değeri
 * çökmeden ve olduğu gibi göstermektir.
 *
 * Arayüz maskeyi **açmaya, tahmin etmeye veya yeniden üretmeye çalışmaz**;
 * maskeli değer için yalnızca maskenin kendisi gösterilir.
 */

/** Backend'in kullandığı maske (`app/services/security/redaction.py`). */
export const MASKED_VALUE = "***";

/** Gösterime hazır tek bir değişken değeri. */
export interface FormattedVariable {
  /** Ekrana basılacak metin. */
  text: string;
  /** Değerin tamamı backend tarafından maskelenmişse true. */
  masked: boolean;
}

/** Değer, backend tarafından tamamen maskelenmiş mi. */
export function isMaskedValue(value: unknown): boolean {
  return value === MASKED_VALUE;
}

/**
 * Bir değişken değerini gösterilebilir metne çevirir.
 *
 * Backend'den gelen değer doğrulanmış sayılmaz; hiçbir girdi istisna
 * fırlatmamalıdır. İç içe yapılar JSON olarak basılır — bunlar da backend
 * tarafından özyinelemeli olarak maskelenmiştir, yani gösterilen metin
 * maskelenmemiş bir değer içermez.
 */
export function formatVariableValue(value: unknown): FormattedVariable {
  if (isMaskedValue(value)) {
    return { text: MASKED_VALUE, masked: true };
  }

  if (typeof value === "string") {
    return { text: value, masked: false };
  }

  if (typeof value === "number" || typeof value === "boolean") {
    return { text: String(value), masked: false };
  }

  if (value === null) {
    return { text: "null", masked: false };
  }

  try {
    const serialized = JSON.stringify(value);
    // `undefined` gibi JSON'da karşılığı olmayan değerler için stringify
    // `undefined` döndürür; o durumda yer tutucu gösterilir.
    return { text: serialized ?? "—", masked: false };
  } catch {
    // Beklenmeyen bir yapı (döngüsel referans vb.) arayüzü çökertmez.
    return { text: "—", masked: false };
  }
}
