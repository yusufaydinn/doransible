/**
 * Vitest ortak kurulumu.
 *
 * `@testing-library/jest-dom/vitest` DOM'a özgü matcher'ları ve tip
 * genişletmelerini ekler; `cleanup` her testten sonra render edilen ağacı
 * söker.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
