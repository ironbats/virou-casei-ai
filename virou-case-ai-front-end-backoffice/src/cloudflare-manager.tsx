import { useEffect, useId, useRef, useState } from "react";
import { Activity, ClipboardCheck, Cloud, Settings2 } from "lucide-react";
import { command, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog, useRemote } from "./integration-ui";
import { Qualification } from "./integration-qualification";
import { PublicationDiagnosticsPanel } from "./publication-diagnostics";
import { usePublicationDiagnostics } from "./use-publication-diagnostics";
import {
  managementSnapshot,
  runLabel,
  stateLabel,
} from "./cloudflare-snapshot";
import { ReadinessOverview, StaleNotice } from "./cloudflare-readiness";
import { stateLabels, type Connection } from "./integration-types";
import "./cloudflare-manager.css";

type Tab = "overview" | "verification" | "connection" | "qualification";
const tabs = [
  { id: "overview", label: "Visão geral", icon: Cloud },
  { id: "verification", label: "Verificação", icon: Activity },
  { id: "connection", label: "Conexão", icon: Settings2 },
  { id: "qualification", label: "Qualificação", icon: ClipboardCheck },
] as const;
const actions: Record<string, { label: string; description: string }> = {
  validate: {
    label: "Validar conexão",
    description:
      "Consulta a Cloudflare para verificar token, conta e permissões. Não cria nem publica projetos.",
  },
  activate: {
    label: "Ativar conexão",
    description:
      "Ativa esta versão validada. Publicações exigem verificação operacional aprovada ou relatório de qualificação válido.",
  },
  default: {
    label: "Definir como padrão",
    description:
      "Novos projetos serão publicados nesta conta. Projetos já vinculados permanecem na conta original.",
  },
  disable: {
    label: "Desativar conexão",
    description:
      "Bloqueia novas publicações e atualizações nesta conexão. Não apaga projetos nem desativa outra conta.",
  },
};

type CloudflareManagerProps = {
  connection: Connection;
  close: () => void;
  changed: () => void;
};

export function CloudflareManager(props: CloudflareManagerProps) {
  return (
    <CloudflareManagerSession key={props.connection.connection_id} {...props} />
  );
}

function CloudflareManagerSession({
  connection,
  close,
  changed,
}: CloudflareManagerProps) {
  const base = `/integrations/cloudflare/connections/${connection.connection_id}`;
  const remote = useRemote<Connection>(base);
  const config = remote.data ?? connection;
  const diagnostics = usePublicationDiagnostics(config);
  const [tab, setTab] = useState<Tab>("overview");
  const [diagnosticTarget, setDiagnosticTarget] = useState(0);
  const [action, setAction] = useState("");
  const [reason, setReason] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [verificationBusy, setVerificationBusy] = useState(false);
  const [qualificationBusy, setQualificationBusy] = useState(false);
  const [verificationDirty, setVerificationDirty] = useState(false);
  const [qualificationDirty, setQualificationDirty] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState("");
  const id = useId();
  const tabButtons = useRef<(HTMLButtonElement | null)[]>([]);
  const content = useRef<HTMLDivElement>(null);
  const busy = actionBusy || verificationBusy || qualificationBusy;
  const snapshot = managementSnapshot(diagnostics.data, config);
  const validatedAt = config.validated_at;
  const context = snapshot?.connection;
  const versionMismatch =
    !!context &&
    String(context.configuration_version) !== String(config.version);
  const mismatchRequested = useRef("");
  useEffect(() => {
    if (versionMismatch && context) {
      const target = `${context.id}:${context.configuration_version}`;
      if (mismatchRequested.current !== target) {
        mismatchRequested.current = target;
        remote.refresh();
      }
    }
  }, [versionMismatch, context?.id, context?.configuration_version]);
  const selectTab = (next: Tab, focus = false) => {
    setTab(next);
    content.current?.querySelector(".cloudflare-panels")?.scrollTo({ top: 0 });
    if (focus)
      tabButtons.current[tabs.findIndex((item) => item.id === next)]?.focus();
  };
  const chooseAction = (next: string) => {
    setAction(next);
    selectTab("connection", true);
  };
  const refreshState = () => {
    diagnostics.refresh();
    if (versionMismatch || remote.error) remote.refresh();
  };
  const diagnose = () => {
    selectTab("verification", true);
    setDiagnosticTarget((value) => value + 1);
  };

  return (
    <IntegrationDialog
      title={`Gerenciar ${context?.name || config.name}`}
      className="cloudflare-dialog"
      focusClose
      close={close}
      busy={busy}
      dirty={!!reason || verificationDirty || qualificationDirty}
    >
      <div className="cloudflare-manager" ref={content}>
        <ErrorNotice error={remote.error || error} />
        {message && (
          <p className="notice success" role="status">
            {message}
          </p>
        )}
        {remote.loading && !remote.data ? (
          <p role="status">Carregando conexão…</p>
        ) : !remote.data ? (
          <button className="button" onClick={remote.refresh}>
            Tentar novamente
          </button>
        ) : (
          <>
            <div className="cloudflare-context">
              <span
                className={`integration-badge ${context?.state || config.state}`}
              >
                {stateLabels[context?.state || config.state] ||
                  context?.state ||
                  config.state}
              </span>
              {(context?.is_default ?? config.is_default) && (
                <span className="integration-badge default">
                  Conexão padrão
                </span>
              )}
              <span>
                Versão {context?.configuration_version ?? config.version}
              </span>
              {context?.environment || config.settings.environment ? (
                <span>
                  {context?.environment || String(config.settings.environment)}
                </span>
              ) : null}
            </div>
            <div
              className="cloudflare-manager-tabs"
              role="tablist"
              aria-label="Gerenciamento Cloudflare"
            >
              {tabs.map((item, index) => (
                <button
                  type="button"
                  key={item.id}
                  ref={(element) => {
                    tabButtons.current[index] = element;
                  }}
                  id={`${id}-${item.id}-tab`}
                  role="tab"
                  aria-selected={tab === item.id}
                  aria-controls={`${id}-${item.id}-panel`}
                  tabIndex={tab === item.id ? 0 : -1}
                  onClick={() => selectTab(item.id)}
                  onKeyDown={(event) => {
                    let position = index;
                    if (event.key === "ArrowRight")
                      position = (index + 1) % tabs.length;
                    else if (event.key === "ArrowLeft")
                      position = (index + tabs.length - 1) % tabs.length;
                    else if (event.key === "Home") position = 0;
                    else if (event.key === "End") position = tabs.length - 1;
                    else return;
                    event.preventDefault();
                    selectTab(tabs[position].id, true);
                  }}
                >
                  <item.icon size={17} aria-hidden="true" />
                  {item.label}
                </button>
              ))}
            </div>
            <div
              className="cf-sr-only"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {diagnostics.stale
                ? "Atualização indisponível. Prontidão atual não confirmada."
                : snapshot
                  ? `Publicação: ${snapshot.publication.allowed ? (snapshot.publication.state === "partial" ? "parcialmente liberada" : "liberada") : "bloqueada"}. ${runLabel(snapshot.current_run || snapshot.previous_result)}. ${
                      snapshot.current_run?.steps
                        .filter((step) =>
                          snapshot.current_run?.current_step_ids.includes(
                            step.id,
                          ),
                        )
                        .map(
                          (step) => `${step.label}: ${stateLabel(step.state)}`,
                        )
                        .join(". ") || ""
                    }`
                  : "Consultando estado da conexão."}
            </div>
            {versionMismatch && (
              <p className="cf-callout cf-warning">
                A configuração mudou em outra sessão.{" "}
                {remote.loading
                  ? "Sincronizando a versão atual…"
                  : "Atualize o estado na Visão geral para sincronizar esta versão antes de executar ações."}
              </p>
            )}
            <div className="cloudflare-panels">
              <section
                id={`${id}-overview-panel`}
                role="tabpanel"
                aria-labelledby={`${id}-overview-tab`}
                hidden={tab !== "overview"}
                tabIndex={0}
              >
                <ErrorNotice error={diagnostics.error} />
                {diagnostics.stale && (
                  <StaleNotice
                    snapshot={snapshot}
                    receivedAt={diagnostics.receivedAt}
                  />
                )}
                <ReadinessOverview
                  snapshot={snapshot}
                  stale={diagnostics.stale}
                  loading={diagnostics.loading}
                  disabled={busy}
                  refresh={refreshState}
                  navigate={() => selectTab("verification", true)}
                  diagnose={diagnose}
                  chooseAction={chooseAction}
                />
              </section>
              <section
                id={`${id}-verification-panel`}
                role="tabpanel"
                aria-labelledby={`${id}-verification-tab`}
                hidden={tab !== "verification"}
                tabIndex={0}
              >
                {qualificationDirty && (
                  <p className="notice">
                    Registre ou limpe o relatório na aba Qualificação antes de
                    executar outra ação.
                  </p>
                )}
                <PublicationDiagnosticsPanel
                  config={config}
                  remote={{ ...diagnostics, refresh: refreshState }}
                  diagnosticTarget={diagnosticTarget}
                  blocked={
                    versionMismatch ||
                    actionBusy ||
                    qualificationBusy ||
                    qualificationDirty ||
                    remote.loading
                  }
                  onBusy={setVerificationBusy}
                  onDirty={setVerificationDirty}
                  changed={changed}
                />
              </section>
              <section
                id={`${id}-connection-panel`}
                role="tabpanel"
                aria-labelledby={`${id}-connection-tab`}
                hidden={tab !== "connection"}
                tabIndex={0}
              >
                <div className="cloudflare-section-heading">
                  <div>
                    <h3>Estado e uso da conexão</h3>
                    <p className="field-help">
                      Valide, ative ou altere o uso desta conta. Toda ação exige
                      um motivo.
                    </p>
                  </div>
                </div>
                <dl className="integration-summary">
                  <div>
                    <dt>Estado</dt>
                    <dd>{stateLabels[config.state] || config.state}</dd>
                  </div>
                  <div>
                    <dt>Conexão padrão</dt>
                    <dd>{config.is_default ? "Sim" : "Não"}</dd>
                  </div>
                  <div>
                    <dt>Versão</dt>
                    <dd>{config.version}</dd>
                  </div>
                  <div>
                    <dt>Última validação</dt>
                    <dd>{date(validatedAt)}</dd>
                  </div>
                </dl>
                {(qualificationDirty || verificationDirty) && (
                  <p className="notice">
                    {qualificationDirty
                      ? "Registre ou limpe o relatório na aba Qualificação"
                      : "Conclua ou limpe o motivo na aba Verificação"}{" "}
                    antes de executar uma ação na conexão.
                  </p>
                )}
                <form
                  className="form"
                  onSubmit={async (event) => {
                    event.preventDefault();
                    if (
                      busy ||
                      remote.loading ||
                      versionMismatch ||
                      qualificationDirty ||
                      verificationDirty ||
                      !action ||
                      reason.trim().length < 8
                    )
                      return;
                    setActionBusy(true);
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
                    } catch (failure) {
                      setError(failure);
                    } finally {
                      setActionBusy(false);
                    }
                  }}
                >
                  <fieldset
                    className="form-fieldset"
                    disabled={
                      busy ||
                      remote.loading ||
                      versionMismatch ||
                      qualificationDirty ||
                      verificationDirty
                    }
                  >
                    <label>
                      Ação
                      <select
                        required
                        value={action}
                        onChange={(event) => setAction(event.target.value)}
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
                          disabled={
                            config.is_default || config.state !== "active"
                          }
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
                      <p
                        className={`notice ${action === "disable" ? "error" : ""}`}
                      >
                        {actions[action].description}
                      </p>
                    )}
                    <label>
                      Motivo da ação
                      <textarea
                        required
                        minLength={8}
                        maxLength={300}
                        rows={2}
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                      />
                    </label>
                    <div className="actions">
                      <button
                        type="submit"
                        className="button primary"
                        disabled={!action || reason.trim().length < 8}
                      >
                        {actionBusy
                          ? "Processando…"
                          : actions[action]?.label || "Executar ação"}
                      </button>
                      {reason && (
                        <button
                          type="button"
                          className="button"
                          onClick={() => setReason("")}
                        >
                          Limpar motivo
                        </button>
                      )}
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
              </section>
              <section
                id={`${id}-qualification-panel`}
                role="tabpanel"
                aria-labelledby={`${id}-qualification-tab`}
                hidden={tab !== "qualification"}
                tabIndex={0}
              >
                <Qualification
                  config={config}
                  standalone
                  blocked={
                    actionBusy ||
                    verificationBusy ||
                    remote.loading ||
                    versionMismatch
                  }
                  onBusy={setQualificationBusy}
                  onDirty={setQualificationDirty}
                  reload={async () => {
                    remote.refresh();
                    diagnostics.refresh();
                    changed();
                    setQualificationDirty(false);
                  }}
                />
              </section>
            </div>
          </>
        )}
      </div>
    </IntegrationDialog>
  );
}
