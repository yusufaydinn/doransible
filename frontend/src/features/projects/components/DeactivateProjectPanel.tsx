import { useEffect, useId, useRef, useState } from "react";

import { StatusMessage } from "../../../components/StatusMessage";
import { useDeactivateProject } from "../hooks";
import type { Project } from "../types";
import { ErrorNoticePanel } from "./ErrorNoticePanel";

interface DeactivateProjectPanelProps {
  project: Project;
}

const INACTIVE_EXPLANATION =
  "Bu project pasif durumda. Kayıt geçmişe referans olarak saklanıyor; controller'daki " +
  "dizin ve dosyalar bu işlemden etkilenmedi. Kaydı arayüzden yeniden etkinleştirmek " +
  "şu anda mümkün değil.";

/**
 * Project kaydını pasife alma bölümü.
 *
 * "Sil" ifadesi tek başına kullanılmaz: işlem sunucudaki dosyalara dokunmaz,
 * yalnızca kaydı pasifleştirir (MIMARI.md bölüm 7). Yanlış tıklamayı önlemek
 * için ayrı bir onay adımı vardır ve onay açıldığında odak onay butonuna
 * taşınır.
 */
export function DeactivateProjectPanel({ project }: DeactivateProjectPanelProps) {
  const [isConfirming, setIsConfirming] = useState(false);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const confirmTitleId = useId();
  const mutation = useDeactivateProject(project.id);

  useEffect(() => {
    if (isConfirming) {
      confirmButtonRef.current?.focus();
    }
  }, [isConfirming]);

  if (mutation.isSuccess) {
    // İşlem başarılı olunca kayıt pasife düşer. Aşağıdaki "pasif kayıt"
    // dalına devretmek, kullanıcının az önce yaptığı işlemin sonucunu
    // ekrandan silerdi; bu yüzden sonuç burada gösterilir.
    return (
      <section className="section">
        <h3>Kaydın durumu</h3>
        <StatusMessage tone="success" title="Project kaydı pasife alındı">
          <p>
            Kayıt artık varsayılan listede görünmüyor. Controller'daki dizin ve dosyalara
            dokunulmadı.
          </p>
        </StatusMessage>
        <p>{INACTIVE_EXPLANATION}</p>
      </section>
    );
  }

  if (!project.is_active) {
    // Pasif kayıt için işlem yoktur. Yeniden etkinleştirme MVP 1 kapsamında
    // olmadığı için burada bilinçli olarak buton gösterilmez.
    return (
      <section className="section">
        <h3>Kaydın durumu</h3>
        <p>{INACTIVE_EXPLANATION}</p>
      </section>
    );
  }

  return (
    <section className="section">
      <h3>Project kaydını pasife al</h3>
      <p>
        Bu işlem controller'daki <strong>hiçbir dosyayı silmez</strong>. Project dizini,
        playbook'lar ve roller diskte olduğu gibi kalır. Yalnızca uygulamadaki kayıt
        pasife alınır: project varsayılan listede görünmez ve playbook keşfi
        yapılamaz.
      </p>
      <p>
        Aynı dizin daha sonra ikinci kez kaydedilemez ve pasif kayıt arayüzden yeniden
        etkinleştirilemez.
      </p>

      {mutation.isError && <ErrorNoticePanel error={mutation.error} />}

      {!isConfirming && (
        <button type="button" onClick={() => setIsConfirming(true)}>
          Project kaydını pasife al
        </button>
      )}

      {isConfirming && (
        <div className="confirm" role="group" aria-labelledby={confirmTitleId}>
          <p id={confirmTitleId}>
            <strong>{project.name}</strong> kaydını pasife almak istediğinizden emin
            misiniz? Dosyalar silinmez, kayıt listede görünmez olur.
          </p>
          <button
            type="button"
            ref={confirmButtonRef}
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "Pasife alınıyor…" : "Evet, kaydı pasife al"}
          </button>
          <button
            type="button"
            onClick={() => setIsConfirming(false)}
            disabled={mutation.isPending}
          >
            Vazgeç
          </button>
        </div>
      )}
    </section>
  );
}
