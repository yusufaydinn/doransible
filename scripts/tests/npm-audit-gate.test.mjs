/**
 * npm-audit-gate.mjs regresyon testleri.
 *
 * Çalıştırma:  node --test scripts/tests/
 *
 * Gate'in fail-closed olduğunu doğrular: bir güvenlik kapısının asla
 * "sessizce geçmemesi" gerekir, bu yüzden negatif senaryolar burada
 * açıkça test edilir.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPTS_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = dirname(SCRIPTS_DIR);
const GATE = join(SCRIPTS_DIR, "npm-audit-gate.mjs");
const REAL_FRONTEND = join(REPO_ROOT, "frontend");

const EXIT_OK = 0;
const EXIT_FINDINGS = 1;
const EXIT_INFRA = 2;

const ACCEPTED_ID = "GHSA-qwww-vcr4-c8h2";
let testAcceptedAllowlistPath;

/**
 * Kabul mekanizmasını sınayan test-only kayıt.
 *
 * Production allowlist'i bilinçli olarak boş olabilir; düzeltilmiş bir gerçek
 * advisory'yi sırf bu regresyon testleri için kabul edilmiş tutmak, bayat risk
 * kaydını ürün sözleşmesine dönüştürürdü. Bu fixture gate'in kabul ve guard
 * davranışını gerçek frontend üzerinde aynı fail-closed desenlerle ölçer.
 */
function testAcceptedAllowlist() {
  if (testAcceptedAllowlistPath !== undefined) return testAcceptedAllowlistPath;
  testAcceptedAllowlistPath = writeTempJson("accepted.json", {
    npm: [
      {
        id: ACCEPTED_ID,
        severity: "high",
        guards: {
          scan_dir: "src",
          forbidden_dependencies: [
            "@react-router/dev",
            "@react-router/node",
            "@react-router/serve",
            "@react-router/express",
            "@react-router/architect",
            "@react-router/cloudflare",
            "@react-router/fs-routes",
            "@react-router/remix-routes-option-adapter",
            "@vitejs/plugin-rsc",
          ],
          forbidden_source_patterns: [
            "createBrowserRouter",
            "createHashRouter",
            "createMemoryRouter",
            "createStaticRouter",
            "createStaticHandler",
            "RouterProvider",
            "StaticRouterProvider",
            "[\\\"']react-router/rsc[\\\"']",
            "[\\\"']react-router-dom/server[\\\"']",
            "[\\\"']react-dom/server[\\\"']",
            "[\\\"']react-dom/static[\\\"']",
            "renderToString",
            "renderToPipeableStream",
            "renderToReadableStream",
            "hydrateRoot",
            "\\bloader\\s*[:=]",
            "\\baction\\s*[:=]",
            "unstable_",
          ],
        },
      },
    ],
    pypi: [],
  });
  return testAcceptedAllowlistPath;
}

/** Gate'i verilen stdin ve allowlist ile çalıştırır. */
function runGate(
  stdinText,
  { allowlist = testAcceptedAllowlist(), frontend = REAL_FRONTEND } = {},
) {
  const result = spawnSync(
    process.execPath,
    [GATE, "--allowlist", allowlist, "--frontend", frontend],
    { input: stdinText, encoding: "utf8" },
  );
  return { code: result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function writeTempJson(name, value) {
  const dir = mkdtempSync(join(tmpdir(), "audit-gate-"));
  const file = join(dir, name);
  writeFileSync(file, typeof value === "string" ? value : JSON.stringify(value), "utf8");
  return file;
}

/** Geçerli şemaya sahip, sıfır zafiyetli bir npm audit raporu. */
function cleanReport() {
  return {
    auditReportVersion: 2,
    vulnerabilities: {},
    metadata: {
      vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0 },
      dependencies: { prod: 1, dev: 0, optional: 0, peer: 0, peerOptional: 0, total: 1 },
    },
  };
}

/** Tek kök advisory içeren, iki pakete yayılmış rapor (gerçek npm davranışı). */
function reportWithAdvisory(id) {
  return {
    auditReportVersion: 2,
    vulnerabilities: {
      "react-router": {
        name: "react-router",
        severity: "high",
        via: [
          {
            source: 1124282,
            name: "react-router",
            title: "React Router: RSC Mode CSRF Bypass",
            url: `https://github.com/advisories/${id}`,
            severity: "high",
            range: "7.12.0 - 8.2.0",
          },
        ],
      },
      "react-router-dom": {
        name: "react-router-dom",
        severity: "high",
        via: ["react-router"],
      },
    },
    metadata: {
      vulnerabilities: { info: 0, low: 0, moderate: 0, high: 2, critical: 0, total: 2 },
      dependencies: { prod: 2, dev: 0, optional: 0, peer: 0, peerOptional: 0, total: 2 },
    },
  };
}

// ---------------------------------------------------------------------------
// Senaryo 1: temiz audit -> exit 0
// ---------------------------------------------------------------------------
test("temiz audit exit 0 verir", () => {
  const { code, stdout } = runGate(JSON.stringify(cleanReport()));
  assert.equal(code, EXIT_OK);
  assert.match(stdout, /Kabul edilmemiş bulgu yok/);
});

// ---------------------------------------------------------------------------
// Senaryo 2: kabul edilmiş advisory -> exit 0
// ---------------------------------------------------------------------------
test("kabul edilmiş advisory exit 0 verir ve tek kök advisory'ye indirger", () => {
  const { code, stdout } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)));
  assert.equal(code, EXIT_OK);
  assert.match(stdout, new RegExp(`kabul edilmiş: ${ACCEPTED_ID}`));
  // npm iki kayıt raporlar; gate bunu tek kök advisory olarak saymalıdır.
  assert.match(stdout, /npm toplam kayıt: 2, kök advisory: 1/);
});

// ---------------------------------------------------------------------------
// Senaryo 3: kabul edilmemiş advisory -> exit 1
// ---------------------------------------------------------------------------
test("kabul edilmemiş advisory exit 1 verir", () => {
  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory("GHSA-aaaa-bbbb-cccc")));
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /YENI BULGU\s*: GHSA-aaaa-bbbb-cccc/);
});

test("allowlist boşaltılırsa gerçek bulgu exit 1 verir", () => {
  const allowlist = writeTempJson("allow.json", { npm: [], pypi: [] });
  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { allowlist });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, new RegExp(`YENI BULGU\\s*: ${ACCEPTED_ID}`));
});

// ---------------------------------------------------------------------------
// Senaryo 4: bozuk/eksik JSON ve şema ihlalleri -> exit 2 (altyapı hatası)
// ---------------------------------------------------------------------------
test("boş stdin altyapı hatası verir", () => {
  const { code, stderr } = runGate("");
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /AUDIT ALTYAPI HATASI/);
  assert.match(stderr, /boş stdout/);
});

test("bozuk JSON altyapı hatası verir", () => {
  const { code, stderr } = runGate('{"auditReportVersion": 2, ');
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /ayrıştırılamadı/);
});

test("JSON olmayan metin altyapı hatası verir", () => {
  const { code, stderr } = runGate("npm ERR! code ENOTFOUND\nnpm ERR! network request failed");
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /ayrıştırılamadı/);
});

test("JSON dizisi altyapı hatası verir", () => {
  const { code, stderr } = runGate("[]");
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /JSON nesnesi değil/);
});

test("npm error cevabı altyapı hatası verir", () => {
  const payload = {
    error: { code: "ENOTFOUND", summary: "request to registry failed", detail: "network" },
  };
  const { code, stderr } = runGate(JSON.stringify(payload));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /npm audit hata döndürdü: request to registry failed/);
});

test("boş summary/detail içeren npm error yine de teşhis edilebilir", () => {
  // Gerçek "registry erişilemiyor" cevabında summary ve detail boş gelebilir.
  const payload = { error: { code: "ECONNREFUSED", summary: "", detail: "" } };
  const { code, stderr } = runGate(JSON.stringify(payload));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /npm audit hata döndürdü: ECONNREFUSED/);
});

test("auditReportVersion eksikse altyapı hatası verir", () => {
  const report = cleanReport();
  delete report.auditReportVersion;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /auditReportVersion/);
});

test("message iceren ama rapor olmayan cevap altyapı hatası verir", () => {
  const { code, stderr } = runGate(JSON.stringify({ message: "Invalid auth token" }));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /npm mesajı: Invalid auth token/);
});

test("vulnerabilities eksikse altyapı hatası verir", () => {
  const report = cleanReport();
  delete report.vulnerabilities;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'vulnerabilities' alanı yok/);
});

test("metadata eksikse altyapı hatası verir", () => {
  const report = cleanReport();
  delete report.metadata;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'metadata' yok/);
});

test("metadata sayaçları sayısal değilse altyapı hatası verir", () => {
  const report = cleanReport();
  report.metadata.vulnerabilities.high = "2";
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /metadata\.vulnerabilities\.high' sayısal değil/);
});

test("metadata sayacı eksikse altyapı hatası verir", () => {
  const report = cleanReport();
  delete report.metadata.vulnerabilities.total;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /metadata\.vulnerabilities\.total' sayısal değil/);
});

test("hiçbir kayıt yokken zafiyet sayılırsa altyapı hatası verir", () => {
  const report = cleanReport();
  report.metadata.vulnerabilities.high = 1;
  report.metadata.vulnerabilities.total = 1;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /kök advisory çıkarılamadı/);
});

// ---------------------------------------------------------------------------
// via grafiğinin çözümlenmesi
// ---------------------------------------------------------------------------
test("via hedefi raporda yoksa altyapı hatası verir", () => {
  const report = cleanReport();
  report.vulnerabilities = { "bir-paket": { name: "bir-paket", via: ["olmayan-paket"] } };
  report.metadata.vulnerabilities.high = 1;
  report.metadata.vulnerabilities.total = 1;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'olmayan-paket' paketine referans veriyor ancak o kayıt raporda yok/);
});

test("via zincirinde döngü altyapı hatası verir", () => {
  const report = cleanReport();
  report.vulnerabilities = {
    a: { name: "a", via: ["b"] },
    b: { name: "b", via: ["a"] },
  };
  report.metadata.vulnerabilities.high = 2;
  report.metadata.vulnerabilities.total = 2;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /döngü/);
});

test("via dizi değilse altyapı hatası verir", () => {
  const report = cleanReport();
  report.vulnerabilities = { a: { name: "a", via: "react-router" } };
  report.metadata.vulnerabilities.total = 1;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'vulnerabilities\.a\.via' bir dizi değil/);
});

test("boş via listesi altyapı hatası verir", () => {
  const report = cleanReport();
  report.vulnerabilities = { a: { name: "a", via: [] } };
  report.metadata.vulnerabilities.total = 1;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'via' listesi boş/);
});

test("via içinde geçersiz tip altyapı hatası verir", () => {
  const report = cleanReport();
  report.vulnerabilities = { a: { name: "a", via: [42] } };
  report.metadata.vulnerabilities.total = 1;
  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /geçersiz öğe tipi: number/);
});

test("çok adımlı transitif zincir doğru çözülür", () => {
  const report = reportWithAdvisory(ACCEPTED_ID);
  // react-router-dom -> ara-paket -> react-router -> advisory
  report.vulnerabilities["ara-paket"] = { name: "ara-paket", via: ["react-router"] };
  report.vulnerabilities["react-router-dom"].via = ["ara-paket"];
  report.metadata.vulnerabilities.high = 3;
  report.metadata.vulnerabilities.total = 3;

  const { code, stdout } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_OK);
  assert.match(stdout, /kök advisory: 1/);
});

// ---------------------------------------------------------------------------
// Karışık rapor: geçerli advisory + çözülemeyen kayıt -> exit 2
// ---------------------------------------------------------------------------
test("kabul edilmiş advisory ile çözülemeyen kayıt birlikteyse altyapı hatası verir", () => {
  const report = reportWithAdvisory(ACCEPTED_ID);
  // Geçerli advisory duruyor; ikinci kayıt hiçbir yere çözülmüyor.
  report.vulnerabilities["yetim-paket"] = { name: "yetim-paket", via: ["kayip-paket"] };
  report.metadata.vulnerabilities.high = 3;
  report.metadata.vulnerabilities.total = 3;

  const { code, stderr, stdout } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA, `stdout=${stdout} stderr=${stderr}`);
  assert.match(stderr, /'kayip-paket' paketine referans veriyor ancak o kayıt raporda yok/);
  // Kabul edilmiş advisory'nin varlığı hatayı gizlememelidir.
  assert.doesNotMatch(stdout, /Kabul edilmemiş bulgu yok/);
});

test("geçerli advisory ile boş via'lı kayıt birlikteyse altyapı hatası verir", () => {
  const report = reportWithAdvisory(ACCEPTED_ID);
  report.vulnerabilities["yetim-paket"] = { name: "yetim-paket", via: [] };
  report.metadata.vulnerabilities.total = 3;

  const { code, stderr } = runGate(JSON.stringify(report));
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /'via' listesi boş/);
});

test("okunamayan allowlist altyapı hatası verir", () => {
  const { code, stderr } = runGate(JSON.stringify(cleanReport()), {
    allowlist: join(tmpdir(), "olmayan-allowlist-dosyasi.json"),
  });
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /Allowlist okunamadı/);
});

// ---------------------------------------------------------------------------
// Senaryo 5: erişilemeyen registry -> exit 1 (script seviyesinde), gate exit 2
// ---------------------------------------------------------------------------
test("erişilemeyen registry ile gerçek npm audit altyapı hatası verir", () => {
  // 127.0.0.1:1 kapalı bir porttur; dış ağa çıkmadan gerçek bir
  // "registry erişilemiyor" senaryosu üretir.
  const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
  const audit = spawnSync(
    npmCommand,
    ["audit", "--json", "--registry", "http://127.0.0.1:1/"],
    {
      cwd: REAL_FRONTEND,
      encoding: "utf8",
      timeout: 10_000,
      env: {
        ...process.env,
        npm_config_fetch_retries: "0",
        npm_config_fetch_timeout: "1000",
      },
    },
  );

  const { code, stderr } = runGate(audit.stdout ?? "");
  assert.equal(
    code,
    EXIT_INFRA,
    `Beklenen altyapı hatası. npm exit=${audit.status}, stdout=${(audit.stdout ?? "").slice(0, 300)}`,
  );
  assert.match(stderr, /AUDIT ALTYAPI HATASI/);
});

// ---------------------------------------------------------------------------
// Guard koşulları: revalidate_if'in makine tarafından doğrulanan hâli
// ---------------------------------------------------------------------------
test("gerçek frontend üzerinde guard'lar temizdir", () => {
  const { code, stdout, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)));
  assert.equal(code, EXIT_OK, stderr);
  assert.doesNotMatch(stdout + stderr, /GUARD IHLALI/);
});

test("data router kullanımı kabulü düşürür", () => {
  const frontend = mkdtempSync(join(tmpdir(), "guard-src-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(join(frontend, "package.json"), JSON.stringify({ dependencies: {} }), "utf8");
  writeFileSync(
    join(frontend, "src", "main.tsx"),
    'import { createBrowserRouter, RouterProvider } from "react-router-dom";\n',
    "utf8",
  );

  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /GUARD IHLALI/);
  assert.match(stderr, /createBrowserRouter/);
});

test("SSR giriş noktası kabulü düşürür", () => {
  const frontend = mkdtempSync(join(tmpdir(), "guard-ssr-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(join(frontend, "package.json"), JSON.stringify({ dependencies: {} }), "utf8");
  writeFileSync(
    join(frontend, "src", "entry.server.tsx"),
    'import { renderToPipeableStream } from "react-dom/server";\n',
    "utf8",
  );

  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /GUARD IHLALI/);
});

test("route loader/action kullanımı kabulü düşürür", () => {
  const frontend = mkdtempSync(join(tmpdir(), "guard-loader-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(join(frontend, "package.json"), JSON.stringify({ dependencies: {} }), "utf8");
  writeFileSync(
    join(frontend, "src", "routes.ts"),
    "export const routes = [{ path: '/', loader: fetchThing }];\n",
    "utf8",
  );

  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /GUARD IHLALI/);
});

test("yasak react-router server bağımlılığı kabulü düşürür", () => {
  const frontend = mkdtempSync(join(tmpdir(), "guard-dep-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(
    join(frontend, "package.json"),
    JSON.stringify({ dependencies: { "@react-router/node": "^7.0.0" } }),
    "utf8",
  );
  writeFileSync(join(frontend, "src", "main.tsx"), "export const x = 1;\n", "utf8");

  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /yasak bağımlılık bildirildi: @react-router\/node/);
});

/** Guard testleri için tek kaynak dosyalı sahte bir frontend üretir. */
function frontendWithSource(source, { fileName = "main.tsx", dependencies = {} } = {}) {
  const frontend = mkdtempSync(join(tmpdir(), "guard-variant-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(join(frontend, "package.json"), JSON.stringify({ dependencies }), "utf8");
  writeFileSync(join(frontend, "src", fileName), source, "utf8");
  return frontend;
}

const IMPORT_VARIANTS = [
  ['çift tırnak from', 'import { renderToPipeableStream } from "react-dom/server";'],
  ["tek tırnak from", "import { renderToPipeableStream } from 'react-dom/server';"],
  ["aralıklı from", "import  {  x  }   from    'react-dom/server' ;"],
  ["satır sonuna sarkan from", "import {\n  x,\n}\nfrom\n'react-dom/server';"],
  ["side-effect import", "import 'react-dom/server';"],
  ["dynamic import", "const m = await import('react-dom/server');"],
  ["require", "const m = require('react-dom/server');"],
  ["tek tırnak rsc", "import { x } from 'react-router/rsc';"],
  ["çift tırnak rsc", 'import { x } from "react-router/rsc";'],
  ["aralıklı rsc", "export  *  from   'react-router/rsc' ;"],
  ["react-router-dom/server", "import { x } from 'react-router-dom/server';"],
];

for (const [label, source] of IMPORT_VARIANTS) {
  test(`guard import varyasyonunu yakalar: ${label}`, () => {
    const frontend = frontendWithSource(source);
    const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
    assert.equal(code, EXIT_FINDINGS, `Yakalanmadı: ${label} -> ${stderr}`);
    assert.match(stderr, /GUARD IHLALI/);
  });
}

test("guard masum react-dom/client importunu yakalamaz", () => {
  const frontend = frontendWithSource(
    'import { createRoot } from "react-dom/client";\nimport { BrowserRouter } from "react-router-dom";\n',
  );
  const { code, stdout, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), {
    frontend,
  });
  assert.equal(code, EXIT_OK, stderr);
  assert.doesNotMatch(stdout + stderr, /GUARD IHLALI/);
});

test("guard aralıklı loader/action tanımını yakalar", () => {
  const frontend = frontendWithSource(
    "export const routes = [{ path: '/', loader   :   fetchThing }];\n",
    { fileName: "routes.ts" },
  );
  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_FINDINGS);
  assert.match(stderr, /GUARD IHLALI/);
});

test("guard taranacak kaynak bulamazsa altyapı hatası verir", () => {
  const frontend = mkdtempSync(join(tmpdir(), "guard-empty-"));
  mkdirSync(join(frontend, "src"), { recursive: true });
  writeFileSync(join(frontend, "package.json"), JSON.stringify({ dependencies: {} }), "utf8");

  const { code, stderr } = runGate(JSON.stringify(reportWithAdvisory(ACCEPTED_ID)), { frontend });
  assert.equal(code, EXIT_INFRA);
  assert.match(stderr, /taranacak kaynak dosya bulunamadı/);
});
