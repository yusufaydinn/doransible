/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // GUVENLIK.md bölüm 10: development sunucusu yalnızca localhost'ta dinler.
    host: "127.0.0.1",
    port: 5173,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.tsx", "src/**/*.test.ts"],
    // Testler global `describe`/`it` yerine açık import kullanır; böylece
    // tsconfig'in `types` listesi genişletilmek zorunda kalmaz.
    globals: false,
    restoreMocks: true,
    unstubGlobals: true,
    // Varsayılan (`false`) tüm `.css` import'larını boş bir modüle indirger;
    // `?raw` sorgusu bile bunu atlatamaz. `styleContrast.test.ts` marka
    // rengi kontrastını gerçek `styles.css` metninden hesapladığı için
    // gerçek işleme açık olmalı (AUDIT-FIX1 bulgu 2 regresyon testi).
    css: true,
  },
});
