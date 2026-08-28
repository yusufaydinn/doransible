import type { ReactNode } from "react";

interface StepHeadingProps {
  index: number;
  children: ReactNode;
  level?: 2 | 3 | 4;
}

/**
 * Bir sürecin parçası olan bölüm başlığı (ör. project detayındaki
 * Playbook → Inventory → Plan akışı).
 *
 * Sıra numarası `aria-hidden` bir rozettir ve başlığın erişilebilir adına
 * karışmaz (bkz. WAI-ARIA "name from content" — `aria-hidden` alt ağaçlar bu
 * hesaba katılmaz). Rozet, başlığın **içinde** ilk çocuk olarak durur; böylece
 * başlık DOM'da hâlâ aynı ebeveynin doğrudan çocuğudur ve yalnızca tam metniyle
 * ("Çalıştırma planı" gibi) bu başlığı arayıp `parentElement`'ini okuyan mevcut
 * testler bozulmaz.
 */
export function StepHeading({ index, children, level = 3 }: StepHeadingProps) {
  const Heading = `h${level}` as "h2" | "h3" | "h4";
  return (
    <Heading className="step-heading">
      <span className="step-heading__badge" aria-hidden="true">
        {index}
      </span>
      {children}
    </Heading>
  );
}
