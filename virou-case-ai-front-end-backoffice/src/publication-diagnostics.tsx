import { useEffect, useState } from "react";
import { command } from "./api";
import { ErrorNotice } from "./admin-ui";
import type { Connection } from "./integration-types";
import { PublicationVerificationStatus } from "./publication-verification-status";
import { AutomaticPublicationStatus } from "./automatic-publication-status";
import {
  usePublicationDiagnostics,
  type PublicationDiagnosticsRemote,
} from "./use-publication-diagnostics";
import "./observability.css";
import "./cloudflare-manager.css";

type Props = {
  config: Connection;
  blocked: boolean;
  onBusy: (busy: boolean) => void;
  onDirty?: (dirty: boolean) => void;
  changed: () => void;
};

export function PublicationDiagnostics(props: Props) {
  const remote = usePublicationDiagnostics(props.config);
  return <PublicationDiagnosticsPanel {...props} remote={remote} />;
}

export function PublicationDiagnosticsPanel({
  config,
  blocked,
  onBusy,
  onDirty,
  changed,
  remote,
}: Props & { remote: PublicationDiagnosticsRemote }) {
  const { data, error: remoteError, loading, refresh } = remote;
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    onDirty?.(!!reason);
  }, [reason, onDirty]);
  useEffect(() => {
    setCopied(false);
  }, [config.connection_id, config.version]);
  const active =
    data?.verification &&
    ["queued", "running"].includes(data.verification.state);
  const resuming = data?.verification?.state === "reconciling";
  const executorBlocked =
    !resuming &&
    (data?.can_verify === false ||
      (data &&
        (!data.workers.length ||
          !data.workers.every((worker) => worker.available))));
  const diagnosticText = data ? JSON.stringify(data, null, 2) : "";
  const automaticallyPending =
    data?.automatic_verification?.enabled &&
    ["scheduled", "queued", "running", "retry_wait"].includes(
      data.automatic_verification.state,
    );
  const disabled =
    !data ||
    busy ||
    blocked ||
    !!active ||
    !!executorBlocked ||
    reason.trim().length < 8 ||
    !["active", "validated"].includes(config.state);
  return (
    <section
      className="publication-diagnostics"
      aria-label="Diagnóstico da publicação"
    >
      <div className="cloudflare-section-heading">
        <div>
          <h3>Verificação da publicação</h3>
          <p className="field-help">
            Acompanhe a conexão e os perfis disponíveis para publicação.
          </p>
        </div>
        <button
          type="button"
          className="button"
          disabled={busy || loading}
          onClick={refresh}
        >
          Atualizar diagnóstico
        </button>
      </div>
      <ErrorNotice error={remoteError || error} />
      <p
        className={`notice ${data?.can_publish ? "success" : ""}`}
        role="status"
      >
        {!data
          ? loading
            ? "Consultando executor…"
            : "Não foi possível consultar o diagnóstico. Tente atualizar."
          : data.can_publish
            ? data.stack_readiness?.some((profile) => !profile.can_publish)
              ? "Publicação liberada para os perfis aprovados abaixo."
              : "Publicação liberada para esta conexão."
            : automaticallyPending
              ? "A publicação aguarda a verificação automática. O estado será atualizado aqui."
              : "Publicação bloqueada: resolva os itens abaixo."}
      </p>
      {data && (
        <p className="field-help">
          Esta verificação valida a conexão e os perfis. A preparação do código,
          as migrations e o resultado de cada projeto são conferidos na própria
          publicação.
        </p>
      )}
      {!!data?.reasons.length && (
        <ul className="cloudflare-reasons">
          {data.reasons.map((item) => (
            <li key={item.code}>
              {item.message} <small>({item.code})</small>
            </li>
          ))}
        </ul>
      )}
      {!!data?.stack_readiness?.length && (
        <ul
          className="cloudflare-profiles"
          aria-label="Disponibilidade por perfil"
        >
          {data.stack_readiness.map((profile) => (
            <li key={profile.stack}>
              <code>{profile.stack}</code>
              <strong
                className={profile.can_publish ? "cloudflare-success" : ""}
              >
                {profile.can_publish
                  ? "Publicação liberada"
                  : "Publicação bloqueada"}
              </strong>
              {profile.reasons.map((item) => (
                <p key={item.code}>
                  {item.message} <small>({item.code})</small>
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
      <AutomaticPublicationStatus
        verification={data?.automatic_verification}
        canPublish={!!data?.can_publish}
      />
      {data?.verification && (
        <PublicationVerificationStatus verification={data.verification} />
      )}
      <form
        className="cloudflare-verification-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (disabled) return;
          setBusy(true);
          onBusy(true);
          setError(null);
          try {
            const pending =
              data?.verification?.state === "reconciling"
                ? data.verification
                : null;
            await command(
              pending
                ? `/publication-checks/${pending.id}/resume`
                : `/integration-connections/${config.connection_id}/verify-publication`,
              { reason },
              "POST",
              pending ? undefined : config.version,
            );
            setReason("");
            refresh();
            changed();
          } catch (failure) {
            setError(failure);
          } finally {
            setBusy(false);
            onBusy(false);
          }
        }}
      >
        <h3>
          {resuming
            ? "Retomar a verificação"
            : active
              ? "Verificação em andamento"
              : "Executar verificação"}
        </h3>
        <p className="field-help">
          {data?.automatic_verification?.enabled &&
            "A renovação automática está habilitada e não exige verificações manuais. Use esta ação para antecipar uma conferência. "}
          O resultado é atualizado automaticamente. Workers e bancos D1 de
          verificação são preservados na Cloudflare.
        </p>
        <details className="cloudflare-explanation">
          <summary>O que esta verificação faz?</summary>
          <p>
            A verificação cria ou reutiliza um Worker e um banco D1 por perfil
            na conta configurada, verifica isolamento, build, publicação e
            leitura/escrita, e preserva esses recursos ao terminar, inclusive em
            caso de falha. Novos recursos precisam de vagas livres. Projetos
            existentes não são alterados.
          </p>
        </details>
        {executorBlocked && (
          <div className="notice" role="status">
            <p>
              A verificação remota não pode iniciar enquanto o executor ou a
              conexão não estiverem prontos. Corrija o diagnóstico abaixo e
              atualize o estado. Nenhuma permissão Cloudflare remove um bloqueio
              local do sandbox.
            </p>
            {!!data?.verification_reasons?.length && (
              <ul>
                {data.verification_reasons.map((item) => (
                  <li key={item.code}>{item.message}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        <label>
          Motivo da verificação
          <input
            required
            minLength={8}
            value={reason}
            maxLength={300}
            disabled={busy || blocked || !!active}
            onChange={(event) => setReason(event.target.value)}
            aria-describedby="verification-reason-help"
          />
        </label>
        <p className="field-help" id="verification-reason-help">
          Descreva o motivo com pelo menos 8 caracteres para registrar esta
          ação.
        </p>
        <div className="actions">
          <button type="submit" className="button primary" disabled={disabled}>
            {busy
              ? "Agendando…"
              : active
                ? "Verificação em andamento"
                : resuming
                  ? "Retomar verificação"
                  : "Verificar publicação"}
          </button>
          {reason && (
            <button
              type="button"
              className="button"
              disabled={busy || blocked}
              onClick={() => setReason("")}
            >
              Limpar motivo
            </button>
          )}
        </div>
      </form>
      <details className="cloudflare-technical" open={!!executorBlocked}>
        <summary>Executor e diagnóstico técnico</summary>
        {data?.workers.length === 0 && (
          <p className="notice">
            Nenhum worker enviou sinal nos últimos 60 segundos. Inicie ou
            verifique o serviço worker.
          </p>
        )}
        {data?.workers.map((worker) => (
          <div
            key={worker.id}
            className={`notice ${worker.available ? "success" : "error"}`}
            role="status"
          >
            <strong>
              {worker.available
                ? "Executor disponível"
                : "Executor indisponível"}
            </strong>{" "}
            {worker.code && <small>({worker.code})</small>}
            <p>{worker.message}</p>
            {worker.failed_checks && worker.failed_checks.length > 0 && (
              <small>
                Controles não aprovados: {worker.failed_checks.join(", ")}
              </small>
            )}
            {worker.diagnostic_id && (
              <p className="diagnostic-id">
                Diagnóstico: <code>{worker.diagnostic_id}</code>
              </p>
            )}
            {worker.remediation && (
              <p>
                <strong>Próxima ação:</strong> {worker.remediation}
              </p>
            )}
            {worker.details && (
              <details className="executor-details">
                <summary>Detalhes técnicos do executor</summary>
                <p>
                  Etapa: <code>{worker.details.stage ?? "não informada"}</code>{" "}
                  · Código de saída:{" "}
                  <code>{worker.details.exit_code ?? "não iniciado"}</code>
                </p>
                {worker.details.exception_type && (
                  <p>
                    Exceção: <code>{worker.details.exception_type}</code>
                  </p>
                )}
                {worker.details.stderr && (
                  <pre aria-label="Erro sanitizado do executor">
                    {worker.details.stderr}
                  </pre>
                )}
                {worker.environment && (
                  <pre aria-label="Ambiente do executor">
                    {JSON.stringify(worker.environment, null, 2)}
                  </pre>
                )}
              </details>
            )}
            <small>
              Último heartbeat:{" "}
              {new Date(worker.checked_at).toLocaleString("pt-BR")}
            </small>
          </div>
        ))}
        {data && (
          <details className="executor-details">
            <summary>Diagnóstico completo para suporte</summary>
            <div className="actions">
              <button
                type="button"
                className="button"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(diagnosticText);
                    setCopied(true);
                  } catch {
                    setCopied(false);
                    setError(
                      new Error(
                        "Não foi possível copiar. Selecione e copie o texto do diagnóstico abaixo.",
                      ),
                    );
                  }
                }}
              >
                {copied ? "Diagnóstico copiado" : "Copiar diagnóstico"}
              </button>
            </div>
            <pre>{diagnosticText}</pre>
          </details>
        )}
      </details>
    </section>
  );
}
