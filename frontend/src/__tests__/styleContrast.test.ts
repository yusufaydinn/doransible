/**
 * Marka renginin buton kontrastı (AUDIT-FIX1 bulgu 2).
 *
 * `#0d9488` üzerinde beyaz metin yalnızca 3.74:1 veriyordu; WCAG AA normal
 * metin için asgari 4.5:1 ister. Bu test, `styles.css`'teki `:root` (açık
 * tema) ve `@media (prefers-color-scheme: dark)` (koyu tema) bloklarındaki
 * `--brand`/`--brand-contrast` çiftlerinin kontrastını gerçekten hesaplar;
 * bir renk tokenı yeniden ayarlandığında regresyonu erken yakalar.
 */

import { describe, expect, it } from "vitest";

import rawCss from "../styles.css?raw";

const AA_NORMAL_TEXT_MIN_CONTRAST = 4.5;

function hexToRgb(hex: string): [number, number, number] {
  const value = parseInt(hex.replace("#", ""), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function linearize(channel: number): number {
  const s = channel / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

/** WCAG 2.x kontrast oranı (1:1 ile 21:1 arası). */
function contrastRatio(hexA: string, hexB: string): number {
  const lA = relativeLuminance(hexToRgb(hexA));
  const lB = relativeLuminance(hexToRgb(hexB));
  const lighter = Math.max(lA, lB);
  const darker = Math.min(lA, lB);
  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * `--brand`/`--brand-contrast` çiftlerini sırayla döndürür.
 *
 * Dosyada bu değişkenler iki kez tanımlanır: önce açık tema `:root` bloğunda,
 * sonra `@media (prefers-color-scheme: dark)` içindeki koyu tema
 * override'ında. `matchAll` dosya sırasına göre eşleşir; bu yüzden ilk çift
 * açık, ikinci çift koyu temaya aittir.
 */
function extractHexValues(variableName: string): string[] {
  const pattern = new RegExp(`${variableName}:\\s*(#[0-9a-fA-F]{6})`, "g");
  return [...rawCss.matchAll(pattern)].map((match) => match[1] as string);
}

describe("Buton marka rengi kontrastı", () => {
  const brandValues = extractHexValues("--brand");
  const contrastValues = extractHexValues("--brand-contrast");

  it("styles.css hem açık hem koyu tema için --brand/--brand-contrast tanımlar", () => {
    expect(brandValues).toHaveLength(2);
    expect(contrastValues).toHaveLength(2);
  });

  it("açık temada birincil buton (--brand arka plan, --brand-contrast metin) AA sağlar", () => {
    const [lightBrand] = brandValues;
    const [lightContrast] = contrastValues;
    const ratio = contrastRatio(lightBrand as string, lightContrast as string);
    expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL_TEXT_MIN_CONTRAST);
  });

  it("koyu temada birincil buton (--brand arka plan, --brand-contrast metin) AA sağlar", () => {
    const [, darkBrand] = brandValues;
    const [, darkContrast] = contrastValues;
    const ratio = contrastRatio(darkBrand as string, darkContrast as string);
    expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL_TEXT_MIN_CONTRAST);
  });

  it("mevcut durum renklerinin (bilgi/başarı/uyarı/hata) token adları korunur", () => {
    // Marka rengi değişikliği durum anlamına dokunmamalı: bu token'lar hâlâ
    // ayrı ve mevcut olmalı (AUDIT-FIX1 bulgu 2 — "mevcut durum renklerinin
    // anlamını bozma").
    for (const token of ["--info", "--success", "--warning", "--error"]) {
      expect(rawCss).toContain(`${token}:`);
    }
  });
});
