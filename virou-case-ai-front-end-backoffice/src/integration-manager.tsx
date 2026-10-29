import { useState } from "react";
import { CloudflareManager } from "./cloudflare-manager";
import { command, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog, useRemote } from "./integration-ui";
import { PublicationDiagnostics } from "./publication-diagnostics";
import { Qualification } from "./integration-qualification";
import {
  stateLabels,
  type Connection,
  type Provider,
} from "./integration-types";

type ManagerProps = {
  provider: Provider;
  connection: Connection;
  close: () => void;
  changed: () => void;
};

export function IntegrationManager(props: ManagerProps) {
  return props.provider === "cloudflare" ? (
    <CloudflareManager
      connection={props.connection}
      close={props.close}
      changed={props.changed}
    />
  ) : (
    <ConnectionManager {...props} />
  );
}

function ConnectionManager({
  provider,
  connection,
  close,
  changed,
}: {
  provider: Provider;
  connection: Connection;
  close: () => void;
  changed: () => void;
}) {
  const base = `/integrations/${provider}/connections/${connection.connection_id}`;
  const remote = useRemote<Connection>(base);
  const [action, setAction] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportDirty, setReportDirty] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState("");
  const config = remote.data;
  const actions: Record<string, { label: string; description: string }> = {
    validate: {
      label: "Validar conexão",
      description:
        provider === "cloudflare"
          ? "Consulta a Cloudflare para verificar token, conta e permissões. Não cria nem publica projetos."
          : "Valida os parâmetros locais. Não realiza chamadas ao Jev.",
    },
    activate: {
      label: "Ativar conexão",
      description:
        provider === "cloudflare"
          ? "Ativa esta versão validada. Publicações exigem verificação operacional aprovada ou relatório de qualificação válido."
          : "Ativa esta conexão. O recomendador utiliza apenas a conexão padrão.",
    },
    default: {
      label: "Definir como padrão",
      description:
        provider === "cloudflare"
          ? "Novos projetos serão publicados nesta conta. Projetos já vinculados permanecem na conta original."
          : "Os limites do fluxo guiado e as próximas recomendações usarão esta conexão.",
    },
    disable: {
      label: "Desativar conexão",
      description:
        provider === "cloudflare"
          ? "Bloqueia novas publicações e atualizações nesta conexão. Não apaga projetos nem desativa outra conta."
          : "Interrompe as chamadas ao recomendador desta conexão. Os limites locais do fluxo guiado são preservados.",
    },
  };
  return (
    <IntegrationDialog
      title={`Gerenciar ${connection.name}`}
      close={close}
      busy={busy || reportBusy}
      dirty={!!reason || reportDirty}
    >
      <ErrorNotice error={remote.error || error} />
      {message && (
        <p className="notice success" role="status">
          {message}
        </p>
      )}
      {remote.loading ? (
        <p role="status">Carregando conexão…</p>
      ) : !config ? (
        <button className="button" onClick={remote.refresh}>
          Tentar novamente
        </button>
      ) : (
        <>
          <dl className="integration-summary">
            <div>
              <dt>Estado</dt>
              <dd>
                <span className={`integration-badge ${config.state}`}>
                  {stateLabels[config.state]}
                </span>
              </dd>
            </div>
            <div>
              <dt>Versão</dt>
              <dd>{config.version}</dd>
            </div>
            <div>
              <dt>Conexão padrão</dt>
              <dd>{config.is_default ? "Sim" : "Não"}</dd>
            </div>
            <div>
              <dt>Última validação</dt>
              <dd>{date(config.validated_at)}</dd>
            </div>
          </dl>
          <form
            className="form"
            onSubmit={async (e) => {
              e.preventDefault();
              if (busy || reportBusy || reportDirty || !action) return;
              setBusy(true);
              setError(null);
              setMessage("");
              try {
                await command(
                  `${base}/${action}`,
                  { reason },
                  "POST",
                  config.version,
                );
                setReason("");
                setAction("");
                setMessage(
                  "Ação concluída. O estado da conexão foi atualizado.",
                );
                remote.refresh();
                changed();
              } catch (e) {
                setError(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            {reportDirty && (
              <p className="notice">
                Registre ou limpe o relatório preenchido antes de executar outra
                ação nesta conexão.
              </p>
            )}
            <fieldset
              className="form-fieldset"
              disabled={busy || reportBusy || reportDirty}
            >
              <label>
                Ação
                <select
                  required
                  value={action}
                  onChange={(e) => setAction(e.target.value)}
                >
                  <option value="">Selecione uma ação</option>
                  <option value="validate">Validar conexão</option>
                  <option
                    value="activate"
                    disabled={config.state !== "validated"}
                  >
                    Ativar conexão
                  </option>
                  <option
                    value="default"
                    disabled={config.is_default || config.state !== "active"}
                  >
                    Definir como padrão
                  </option>
                  <option
                    value="disable"
                    disabled={config.state === "disabled"}
                  >
                    Desativar conexão
                  </option>
                </select>
              </label>
              {action && (
                <p className="notice">{actions[action].description}</p>
              )}
              <label>
                Motivo da ação
                <textarea
                  required
                  minLength={8}
                  maxLength={300}
                  rows={2}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="actions">
                <button className="button primary" disabled={!action}>
                  {busy
                    ? "Processando…"
                    : actions[action]?.label || "Executar ação"}
                </button>
              </div>
            </fieldset>
          </form>
          {Object.keys(config.validation).length > 0 && (
            <details>
              <summary>Resultados da validação</summary>
              <pre className="config-evidence">
                {JSON.stringify(config.validation, null, 2)}
              </pre>
            </details>
          )}
          {provider === "cloudflare" && (
            <PublicationDiagnostics
              config={config}
              blocked={busy || reportDirty}
              onBusy={setReportBusy}
              changed={changed}
            />
          )}
          {provider === "cloudflare" && (
            <Qualification
              config={config}
              blocked={busy}
              onBusy={setReportBusy}
              onDirty={setReportDirty}
              reload={async () => {
                remote.refresh();
                changed();
                setReportDirty(false);
              }}
            />
          )}
        </>
      )}
    </IntegrationDialog>
  );
}
