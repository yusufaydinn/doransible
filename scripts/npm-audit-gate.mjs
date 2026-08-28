#!/usr/bin/env node
/**
 * npm audit çıktısını değerlendiren fail-closed gate.
 *
 * Hem scripts/security-audit.ps1 hem scripts/security-audit.sh bu dosyayı
 * çağırır; böylece kural tek yerde tanımlıdır ve otomatik test edilebilir.
 *
 * Kullanım:
 *   npm audit --json | node scripts/npm-audit-gate.mjs --allowlist <json> --frontend <dir>
 *   node scripts/npm-audit-gate.mjs --allowlist <json> --frontend <dir> --input <dosya>
 *
 * Çıkış kodları:
 *   0  Kabul edilmemiş bulgu yok, guard'lar temiz.
 *   1  Kabul edilmemiş advisory veya ihlal edilmiş guard koşulu.
 *   2  AUDIT ALTYAPI HATASI - sonuç güvenilir değil (ağ/TLS/registry hatası,
 *      ayrıştırılamayan JSON, beklenen şemanın karşılanmaması).
 *
 * Önemli: npm, zafiyet bulduğunda da exit 1 verir. Bu yüzden npm'in çıkış
 * kodu altyapı hatasının göstergesi olarak KULLANILMAZ; ayrım yalnızca
 * çıktının JSON şeması üzerinden yapılır.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { basename, extname, join, relative } from "node:path";

export const EXIT_OK = 0;
export const EXIT_FINDINGS = 1;
export const EXIT_INFRA = 2;

const REQUIRED_COUNT_KEYS = ["info", "low", "moderate", "high", "critical", "total"];
const SCANNED_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"]);

/** Audit sonucunun güvenilmez olduğunu bildirir (exit 2). */
export class AuditInfrastructureError extends Error {}

/** UTF-8 BOM'unu kırpar; Windows araçları JSON dosyalarına BOM ekleyebilir. */
export function stripBom(text) {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/**
 * npm audit çıktısını katı biçimde doğrular ve ayrıştırır.
 *
 * @param {string} text Ham stdout.
 * @returns {object} Doğrulanmış rapor.
 * @throws {AuditInfrastructureError}
 */
export function parseAuditReport(text) {
  if (typeof text !== "string" || text.trim() === "") {
    throw new AuditInfrastructureError("npm audit çıktı üretmedi (boş stdout).");
  }

  const cleaned = stripBom(text).trim();

  let data;
  try {
    data = JSON.parse(cleaned);
  } catch (cause) {
    const preview = cleaned.slice(0, 200).replace(/\s+/g, " ");
    throw new AuditInfrastructureError(
      `npm audit çıktısı JSON olarak ayrıştırılamadı: ${cause.message} | ilk 200 karakter: ${preview}`,
    );
  }

  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    throw new AuditInfrastructureError("npm audit çıktısı bir JSON nesnesi değil.");
  }

  // npm ağ/TLS/registry hatalarını {"error": {...}} veya {"message": "..."} olarak yazar.
  if (data.error !== undefined) {
    // Boş string de bilgisiz sayılır; ham nesneye düşerek teşhis edilebilir kal.
    const candidates =
      typeof data.error === "string"
        ? [data.error]
        : [data.error?.summary, data.error?.detail, data.error?.code];
    const detail =
      candidates.find((value) => typeof value === "string" && value.trim() !== "") ??
      JSON.stringify(data.error);
    throw new AuditInfrastructureError(`npm audit hata döndürdü: ${detail}`);
  }

  if (data.auditReportVersion === undefined) {
    const hint = typeof data.message === "string" ? ` npm mesajı: ${data.message}` : "";
    throw new AuditInfrastructureError(
      `npm audit çıktısında 'auditReportVersion' yok; rapor üretilememiş.${hint}`,
    );
  }

  if (typeof data.auditReportVersion !== "number") {
    throw new AuditInfrastructureError("'auditReportVersion' sayısal değil.");
  }

  if (
    data.vulnerabilities === undefined ||
    data.vulnerabilities === null ||
    typeof data.vulnerabilities !== "object" ||
    Array.isArray(data.vulnerabilities)
  ) {
    throw new AuditInfrastructureError("npm audit çıktısında geçerli 'vulnerabilities' alanı yok.");
  }

  const metadata = data.metadata;
  if (metadata === undefined || metadata === null || typeof metadata !== "object") {
    throw new AuditInfrastructureError("npm audit çıktısında 'metadata' yok.");
  }

  const counts = metadata.vulnerabilities;
  if (counts === undefined || counts === null || typeof counts !== "object") {
    throw new AuditInfrastructureError("npm audit çıktısında 'metadata.vulnerabilities' yok.");
  }

  for (const key of REQUIRED_COUNT_KEYS) {
    if (typeof counts[key] !== "number" || !Number.isFinite(counts[key])) {
      throw new AuditInfrastructureError(
        `'metadata.vulnerabilities.${key}' sayısal değil (${JSON.stringify(counts[key])}).`,
      );
    }
  }

  return data;
}

/** Bir advisory nesnesinden kararlı bir kimlik üretir. */
function advisoryIdOf(via) {
  const match = /(GHSA-[a-z0-9-]+)/.exec(String(via.url ?? ""));
  return match ? match[1] : `npm-${via.source ?? "bilinmeyen"}`;
}

/**
 * `via` grafiğini çözerek kök advisory'leri toplar.
 *
 * npm aynı advisory'yi etkilenen her paket için raporlar; `via` içindeki
 * string değerler başka bir vulnerability kaydına yapılan transitif
 * referanstır. Bu fonksiyon grafiği gerçekten dolaşır ve **her** top-level
 * kaydın en az bir advisory nesnesine ulaştığını doğrular.
 *
 * Fail-closed: aşağıdakilerin her biri altyapı hatasıdır, çünkü hepsi
 * "raporu doğru anlayamıyoruz" anlamına gelir ve sessizce eksik değerlendirme
 * yapmaktansa audit'i durdurmak gerekir.
 *
 *   - `via` dizi değil veya boş
 *   - `via` içinde string/nesne dışında bir tip
 *   - string referansın hedefi raporda yok
 *   - referans zincirinde döngü
 *   - zincir hiçbir advisory nesnesine ulaşmıyor
 *
 * @param {object} report Doğrulanmış rapor.
 * @returns {Map<string, {severity: string, title: string, packages: Set<string>}>}
 */
export function collectRootAdvisories(report) {
  const vulnerabilities = report.vulnerabilities;
  const packageNames = new Set(Object.keys(vulnerabilities));
  const advisories = new Map();
  const resolvedPackages = new Map();

  const resolvePackage = (pkg, chain) => {
    const cached = resolvedPackages.get(pkg);
    if (cached !== undefined) return cached;

    if (chain.includes(pkg)) {
      throw new AuditInfrastructureError(
        `'via' referans zincirinde döngü: ${[...chain, pkg].join(" -> ")}`,
      );
    }

    const entry = vulnerabilities[pkg];
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      throw new AuditInfrastructureError(`'vulnerabilities.${pkg}' bir nesne değil.`);
    }
    if (!Array.isArray(entry.via)) {
      throw new AuditInfrastructureError(`'vulnerabilities.${pkg}.via' bir dizi değil.`);
    }
    if (entry.via.length === 0) {
      throw new AuditInfrastructureError(
        `'${pkg}' kaydının 'via' listesi boş; kök advisory'ye çözülemiyor.`,
      );
    }

    const ids = new Set();
    const nextChain = [...chain, pkg];

    for (const via of entry.via) {
      if (typeof via === "string") {
        if (!packageNames.has(via)) {
          throw new AuditInfrastructureError(
            `'${pkg}' kaydı '${via}' paketine referans veriyor ancak o kayıt raporda yok.`,
          );
        }
        for (const id of resolvePackage(via, nextChain)) ids.add(id);
      } else if (via !== null && typeof via === "object" && !Array.isArray(via)) {
        const id = advisoryIdOf(via);
        if (!advisories.has(id)) {
          advisories.set(id, {
            severity: String(via.severity ?? "unknown"),
            title: String(via.title ?? "(başlık yok)"),
            packages: new Set(),
          });
        }
        advisories.get(id).packages.add(pkg);
        ids.add(id);
      } else {
        throw new AuditInfrastructureError(
          `'${pkg}.via' içinde geçersiz öğe tipi: ${via === null ? "null" : typeof via}`,
        );
      }
    }

    if (ids.size === 0) {
      throw new AuditInfrastructureError(`'${pkg}' hiçbir kök advisory'ye çözülemedi.`);
    }

    resolvedPackages.set(pkg, ids);
    return ids;
  };

  for (const pkg of packageNames) {
    resolvePackage(pkg, []);
  }

  // Ek savunma: npm zafiyet saydığı hâlde hiç kayıt yoksa yukarıdaki döngü
  // hiç çalışmaz; bu da şemayı yanlış anladığımız anlamına gelir.
  if (report.metadata.vulnerabilities.total > 0 && advisories.size === 0) {
    throw new AuditInfrastructureError(
      `npm ${report.metadata.vulnerabilities.total} zafiyet bildirdi ancak kök advisory çıkarılamadı; ` +
        "çıktı şeması beklenenden farklı.",
    );
  }

  return advisories;
}

function listSourceFiles(root) {
  const files = [];
  let entries;
  try {
    entries = readdirSync(root, { withFileTypes: true, recursive: true });
  } catch {
    return files;
  }
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (!SCANNED_EXTENSIONS.has(extname(entry.name))) continue;
    files.push(join(entry.parentPath ?? entry.path ?? root, entry.name));
  }
  return files;
}

/**
 * Kabul kaydındaki `guards` koşullarını makine üzerinde doğrular.
 *
 * Kabul, "RSC/SSR/data router kullanılmıyor" varsayımına dayanır. Bu varsayım
 * bozulursa kabul geçersizdir ve gate başarısız olmalıdır.
 *
 * @returns {string[]} İhlal açıklamaları; boşsa guard'lar temiz.
 */
export function evaluateGuards(entry, frontendDir) {
  const guards = entry?.guards;
  if (!guards) return [];

  const violations = [];

  const forbiddenDeps = guards.forbidden_dependencies ?? [];
  if (forbiddenDeps.length > 0) {
    let pkg = {};
    try {
      pkg = JSON.parse(stripBom(readFileSync(join(frontendDir, "package.json"), "utf8")));
    } catch (cause) {
      throw new AuditInfrastructureError(
        `Guard değerlendirilemedi, frontend/package.json okunamadı: ${cause.message}`,
      );
    }
    const declared = new Set([
      ...Object.keys(pkg.dependencies ?? {}),
      ...Object.keys(pkg.devDependencies ?? {}),
      ...Object.keys(pkg.peerDependencies ?? {}),
      ...Object.keys(pkg.optionalDependencies ?? {}),
    ]);
    for (const name of forbiddenDeps) {
      if (declared.has(name)) {
        violations.push(`yasak bağımlılık bildirildi: ${name} (package.json)`);
      }
    }
  }

  const patterns = guards.forbidden_source_patterns ?? [];
  const scanRoot = join(frontendDir, guards.scan_dir ?? "src");
  if (patterns.length > 0) {
    let rootExists = true;
    try {
      statSync(scanRoot);
    } catch {
      rootExists = false;
    }
    if (!rootExists) {
      throw new AuditInfrastructureError(
        `Guard değerlendirilemedi, taranacak dizin yok: ${scanRoot}`,
      );
    }

    const files = listSourceFiles(scanRoot);
    if (files.length === 0) {
      throw new AuditInfrastructureError(
        `Guard değerlendirilemedi, ${scanRoot} altında taranacak kaynak dosya bulunamadı.`,
      );
    }

    for (const file of files) {
      const content = readFileSync(file, "utf8");
      for (const pattern of patterns) {
        const regex = new RegExp(pattern);
        const hit = regex.exec(content);
        if (hit) {
          const line = content.slice(0, hit.index).split("\n").length;
          violations.push(
            `yasak desen "${pattern}" eşleşti: ${relative(frontendDir, file)}:${line}`,
          );
        }
      }
    }
  }

  return violations;
}

function parseArgs(argv) {
  const args = { allowlist: null, frontend: null, input: null };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--allowlist") args.allowlist = argv[++i];
    else if (key === "--frontend") args.frontend = argv[++i];
    else if (key === "--input") args.input = argv[++i];
  }
  if (!args.allowlist || !args.frontend) {
    throw new AuditInfrastructureError("Kullanım: --allowlist <json> --frontend <dir> [--input <dosya>]");
  }
  return args;
}

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

export function run(argv, stdinText) {
  const args = parseArgs(argv);

  let allowlist;
  try {
    // Windows araçları UTF-8 dosyalara BOM ekleyebilir; JSON.parse bunu kabul etmez.
    allowlist = JSON.parse(stripBom(readFileSync(args.allowlist, "utf8")));
  } catch (cause) {
    throw new AuditInfrastructureError(`Allowlist okunamadı (${args.allowlist}): ${cause.message}`);
  }

  const text = args.input !== null ? readFileSync(args.input, "utf8") : stdinText;
  const report = parseAuditReport(text);
  const found = collectRootAdvisories(report);

  const acceptedEntries = Array.isArray(allowlist.npm) ? allowlist.npm : [];
  const acceptedById = new Map(acceptedEntries.map((entry) => [entry.id, entry]));

  let failures = 0;

  for (const [id, item] of found) {
    const packages = [...item.packages].sort().join(", ");
    const entry = acceptedById.get(id);
    if (entry) {
      console.log(`    kabul edilmiş: ${id} [${item.severity}] ${packages}`);
      console.log(`                   ${item.title}`);

      const violations = evaluateGuards(entry, args.frontend);
      for (const violation of violations) {
        console.error(`    GUARD IHLALI : ${id} -> ${violation}`);
        failures += 1;
      }
      if (violations.length > 0) {
        console.error(
          `                   Kabul gerekçesi artık geçerli değil. ` +
            `Bkz. accepted-vulnerabilities.json -> revalidate_if.`,
        );
      }
    } else {
      console.error(`    YENI BULGU   : ${id} [${item.severity}] ${packages}`);
      console.error(`                   ${item.title}`);
      failures += 1;
    }
  }

  for (const id of acceptedById.keys()) {
    if (!found.has(id)) {
      console.log(`    BAYAT KABUL  : ${id} artık raporlanmıyor, listeden silinebilir.`);
    }
  }

  if (failures === 0) {
    const total = report.metadata.vulnerabilities.total;
    console.log(
      `    Kabul edilmemiş bulgu yok (npm toplam kayıt: ${total}, kök advisory: ${found.size}).`,
    );
    return EXIT_OK;
  }

  return EXIT_FINDINGS;
}

const invokedDirectly =
  process.argv[1] && basename(process.argv[1]) === "npm-audit-gate.mjs";

if (invokedDirectly) {
  try {
    process.exit(run(process.argv.slice(2), readStdin()));
  } catch (error) {
    if (error instanceof AuditInfrastructureError) {
      console.error(`    AUDIT ALTYAPI HATASI: ${error.message}`);
      process.exit(EXIT_INFRA);
    }
    console.error(`    AUDIT ALTYAPI HATASI (beklenmeyen): ${error.stack ?? error}`);
    process.exit(EXIT_INFRA);
  }
}
