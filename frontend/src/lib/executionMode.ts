/**
 * Execution mode'un frontend tarafındaki tek doğruluk kaynağı (R1-V3H2B).
 *
 * Backend'deki `app/models/execution_mode.py` ile birebir aynı iki üyeyi
 * taşır. Plan, hazırlama, launch ve Job tipleri (`features/executions`,
 * `features/jobs`) buradan içe aktarır; tip iki ayrı yerde tekrar
 * tanımlansaydı biri genişletildiğinde diğeri sessizce eski kalır ve plan ile
 * Job'ın "aynı" kipi taşıdığı iddiası tip düzeyinde hiçbir şey ifade etmezdi.
 */
export type ExecutionMode = "check" | "normal";

/** Formda ve testlerde sıralı biçimde dolaşmak için. */
export const EXECUTION_MODES: readonly ExecutionMode[] = ["check", "normal"];

/**
 * Kullanıcıya gösterilen kısa mod adı; genelde `<code>{mode}</code>` ile
 * birlikte kullanılır.
 *
 * `check` etiketi bilinçli olarak "deneme çalıştırması" veya "kontrol modu"
 * gibi soyut bir ad **demez** (R1-V3H2B-AUDIT-FIX1/FIX1.1): böyle bir ifade
 * check mode'un zararsız bir simülasyon olduğunu düşündürür, oysa
 * `ExecutionPlanPanel` ve `ExecutionPlanSection`'ın kendi metinleri check
 * mode'un yan etkisizlik garantisi vermediğini açıkça söyler (R0 ölçümünde
 * `check_mode: false` taşıyan bir task'ın check altında gerçekten çalıştığı
 * görülmüştür). Etiket bu yüzden doğrudan kurulan CLI bayrağını (`--check`)
 * adlandırır, sonucu hakkında bir iddia taşımaz.
 */
export const EXECUTION_MODE_LABELS: Record<ExecutionMode, string> = {
  check: "Check (Ansible --check)",
  normal: "Normal (gerçek uygulama)",
};
