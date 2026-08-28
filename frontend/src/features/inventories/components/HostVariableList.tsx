import { formatVariableValue } from "../hostVariables";

interface HostVariableListProps {
  variables: Record<string, unknown>;
}

/**
 * Tek bir host'un değişkenlerini gösterir.
 *
 * Maskelenmiş değerler yalnızca maskenin kendisiyle (`***`) görünür ve
 * yanlarında görünür bir "gizlendi" etiketi taşır: kullanıcı değişkenin var
 * olduğunu bilir ama içeriğini görmez. Arayüz maskeyi açmaya çalışmaz.
 */
export function HostVariableList({ variables }: HostVariableListProps) {
  const names = Object.keys(variables).sort((left, right) => left.localeCompare(right));

  if (names.length === 0) {
    return <span className="muted">Değişken tanımlı değil</span>;
  }

  return (
    <dl className="variables">
      {names.map((name) => {
        const value = formatVariableValue(variables[name]);

        return (
          <div className="variables__row" key={name}>
            <dt>
              <code>{name}</code>
            </dt>
            <dd>
              <code>{value.text}</code>
              {value.masked && <span className="muted"> (gizlendi)</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
