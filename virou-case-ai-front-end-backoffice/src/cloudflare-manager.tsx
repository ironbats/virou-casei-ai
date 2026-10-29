import { useId, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  ClipboardCheck,
  Cloud,
  Settings2,
} from "lucide-react";
import { command, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog, useRemote } from "./integration-ui";
import { Qualification } from "./integration-qualification";
import { PublicationDiagnosticsPanel } from "./publication-diagnostics";
import { AutomaticPublicationStatus } from "./automatic-publication-status";
import {
  verificationStageLabel,
  verificationStateLabel,
} from "./publication-verification-status";
import {
  publicationReadiness,
  usePublicationDiagnostics,
} from "./use-publication-diagnostics";
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

export function CloudflareManager({
  connection,
  close,
  changed,
}: {
  connection: Connection;
  close: () => void;
  changed: () => void;
}) {
  const base = `/integrations/cloudflare/connections/${connection.connection_id}`;
  const remote = useRemote<Connection>(base);
  const config = remote.data ?? connection;
  const diagnostics = usePublicationDiagnostics(config);
  const [tab, setTab] = useState<Tab>("overview");
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
  const diagnostic = diagnostics.data;
  const automatic = diagnostic?.automatic_verification;
  const validatedAt =
    automatic?.last_succeeded_at &&
    (!config.validated_at ||
      Date.parse(automatic.last_succeeded_at) > Date.parse(config.validated_at))
      ? automatic.last_succeeded_at
      : config.validated_at;
  const verification = diagnostic?.verification;
  const active =
    !!verification && ["queued", "running"].includes(verification.state);
  const pending = verification?.state === "reconciling";
  const partial =
    diagnostic?.can_publish &&
    diagnostic.stack_readiness?.some((profile) => !profile.can_publish);
  const selectTab = (next: Tab, focus = false) => {
    setTab(next);
    content.current?.closest("dialog")?.scrollTo({ top: 0 });
    if (focus)
      tabButtons.current[tabs.findIndex((item) => item.id === next)]?.focus();
  };
  const chooseAction = (next: string) => {
    setAction(next);
    selectTab("connection", true);
  };
  const nextStep = active
    ? {
        title: "A verificação está em andamento",
        description: "Acompanhe a etapa atual e o resultado de cada perfil.",
        label: "Acompanhar verificação",
        run: () => selectTab("verification", true),
      }
    : pending
      ? {
          title: automatic?.enabled
            ? "A verificação será retomada automaticamente"
            : "A verificação precisa ser retomada",
          description: automatic?.enabled
            ? "A plataforma retomará a operação existente. Consulte o diagnóstico para acompanhar a recuperação."
            : "Consulte o diagnóstico e registre o motivo para retomar a operação existente.",
          label: "Revisar verificação",
          run: () => selectTab("verification", true),
        }
      : !["validated", "active"].includes(config.state)
        ? {
            title: "Valide as credenciais desta versão",
            description:
              "Confira o acesso à conta Cloudflare antes de ativar a conexão.",
            label: "Validar conexão",
            run: () => chooseAction("validate"),
          }
        : config.state === "validated"
          ? {
              title: "Ative a conexão validada",
              description:
                "Depois, confira a verificação dos perfis que poderão publicar.",
              label: "Ativar conexão",
              run: () => chooseAction("activate"),
            }
          : automatic?.enabled &&
              !diagnostic?.can_publish &&
              ["scheduled", "retry_wait"].includes(automatic.state)
            ? {
                title: "Aguarde a verificação automática",
                description: automatic.message,
                label: "Acompanhar verificação",
                run: () => selectTab("verification", true),
              }
            : diagnostic?.can_publish && !config.is_default
              ? {
                  title: "Conexão disponível para publicação",
                  description:
                    "Para receber novos projetos nesta conta, defina esta conexão como padrão.",
                  label: "Definir como padrão",
                  run: () => chooseAction("default"),
                }
              : diagnostic?.can_publish
                ? {
                    title: partial
                      ? "Confira os perfis aprovados"
                      : "Conexão pronta para receber projetos",
                    description: partial
                      ? "Os perfis aprovados podem publicar. Revise os demais na verificação."
                      : automatic?.enabled
                        ? "A conexão está ativa e definida como padrão. A verificação será renovada automaticamente antes do vencimento."
                        : "A conexão está ativa e definida como padrão. Consulte os resultados ou execute uma nova verificação quando necessário.",
                    label: "Ver resultados da verificação",
                    run: () => selectTab("verification", true),
                  }
                : {
                    title: "Confira a liberação da publicação",
                    description: automatic?.enabled
                      ? "Confira os impedimentos abaixo. A verificação será retomada automaticamente quando a conexão e o executor estiverem prontos."
                      : "Veja os impedimentos e execute a verificação quando a conexão e o executor estiverem prontos.",
                    label: "Revisar verificação",
                    run: () => selectTab("verification", true),
                  };

  return (
    <IntegrationDialog
      title={`Gerenciar ${connection.name}`}
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
              <span className={`integration-badge ${config.state}`}>
                {stateLabels[config.state] || config.state}
              </span>
              {config.is_default && (
                <span className="integration-badge default">
                  Conexão padrão
                </span>
              )}
              <span>Versão {config.version}</span>
              {!!config.settings.environment && (
                <span>{String(config.settings.environment)}</span>
              )}
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
            <section
              id={`${id}-overview-panel`}
              role="tabpanel"
              aria-labelledby={`${id}-overview-tab`}
              hidden={tab !== "overview"}
              tabIndex={0}
            >
              <div className="cloudflare-section-heading">
                <div>
                  <h3>Prontidão da conexão</h3>
                  <p className="field-help">
                    Veja o estado atual e o próximo passo.
                  </p>
                </div>
                <button
                  type="button"
                  className="button"
                  disabled={diagnostics.loading || busy}
                  onClick={diagnostics.refresh}
                >
                  Atualizar estado
                </button>
              </div>
              <ErrorNotice error={diagnostics.error} />
              <div className="cloudflare-readiness-grid">
                <article className="cloudflare-readiness-card">
                  <span>Publicação</span>
                  <strong>
                    {publicationReadiness(diagnostic, diagnostics.loading)}
                  </strong>
                  <p>
                    {diagnostic?.can_publish
                      ? partial
                        ? "Somente os perfis aprovados estão liberados."
                        : "Os perfis desta conexão estão liberados."
                      : diagnostic?.reasons[0]?.message ||
                        "A disponibilidade depende da conexão, dos perfis e do executor."}
                  </p>
                </article>
                <article className="cloudflare-readiness-card">
                  <span>Última verificação</span>
                  <strong>
                    {verification
                      ? verificationStateLabel(verification)
                      : diagnostics.loading
                        ? "Consultando…"
                        : diagnostics.error
                          ? "Consulta indisponível"
                          : "Sem verificação disponível"}
                  </strong>
                  <p>
                    {verification
                      ? verificationStageLabel(verification.stage)
                      : diagnostics.error
                        ? "Atualize o estado para consultar a última verificação."
                        : automatic?.enabled
                          ? "A primeira verificação será executada automaticamente quando a conexão e o executor estiverem prontos."
                          : "Execute uma verificação para acompanhar os resultados."}
                  </p>
                </article>
              </div>
              <AutomaticPublicationStatus
                verification={automatic}
                canPublish={!!diagnostic?.can_publish}
              />
              <div className="cloudflare-next-step">
                <div>
                  <h3>{nextStep.title}</h3>
                  <p>{nextStep.description}</p>
                </div>
                <button
                  type="button"
                  className="button primary"
                  disabled={busy || remote.loading}
                  onClick={nextStep.run}
                >
                  {nextStep.label}
                  <ArrowRight size={16} aria-hidden="true" />
                </button>
              </div>
              <dl className="integration-summary cloudflare-account-summary">
                <div>
                  <dt>Conta Cloudflare</dt>
                  <dd>
                    <code>
                      {String(config.settings.account_id || "Não informada")}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>Última validação</dt>
                  <dd>{date(validatedAt)}</dd>
                </div>
              </dl>
              <p className="field-help cloudflare-scope-note">
                <CheckCircle2 size={16} aria-hidden="true" />A conexão padrão
                recebe novos projetos. Publicações existentes continuam na conta
                original.
              </p>
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
                remote={diagnostics}
                blocked={
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
                blocked={actionBusy || verificationBusy || remote.loading}
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
          </>
        )}
      </div>
    </IntegrationDialog>
  );
}
