import { useEffect, useState } from "react";
import { api, command } from "./api";
import { ErrorNotice } from "./admin-ui";
import type { Connection } from "./integration-types";
import { PublicationVerificationStatus, type Verification } from "./publication-verification-status";
import "./observability.css";

type Diagnostics = {
  can_publish: boolean; can_verify?: boolean; verification_reasons?: { code: string; message: string }[]; reasons: { code: string; message: string }[];
  stack_readiness?: { stack: string; can_publish: boolean; reasons: { code: string; message: string }[] }[];
  workers: { id: string; available: boolean; code?: string; message: string; checked_at: string; failed_checks?: string[]; diagnostic_id?: string; remediation?: string | null; details?: { stage?: string; exit_code?: number | null; stderr?: string; exception_type?: string; duration_ms?: number }; environment?: Record<string, unknown> }[];
  verification: Verification | null;
};
export function PublicationDiagnostics({ config, blocked, onBusy, changed }: {
  config: Connection; blocked: boolean; onBusy: (busy: boolean) => void; changed: () => void;
}) {
  const [data, setData] = useState<Diagnostics | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    setData(null); setCopied(false);
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const result = await api<Diagnostics>(`/integration-connections/${config.connection_id}/publication-diagnostics`, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(result); setError(null); }
      } catch (e) { if (!controller.signal.aborted) { setData(null); setError(e); } }
      if (!controller.signal.aborted) timer = window.setTimeout(load, 4000);
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [config.connection_id, config.version, config.state, refresh]);
  const active = data?.verification && ["queued", "running"].includes(data.verification.state);
  const resuming = data?.verification?.state === "reconciling";
  const executorBlocked = !resuming && (data?.can_verify === false || (data && (!data.workers.length || !data.workers.every((worker) => worker.available))));
  const diagnosticText = data ? JSON.stringify(data, null, 2) : "";
  return <section className="publication-diagnostics" aria-label="Diagnóstico da publicação">
    <h3>Publicação: diagnóstico e verificação</h3>
    <p role="status">{!data ? "Consultando executor…" : data.can_publish ? data.stack_readiness?.some((profile) => !profile.can_publish) ? "Publicação liberada para os perfis aprovados abaixo." : "Publicação liberada para esta conexão." : "Publicação bloqueada: resolva os itens abaixo."}</p>
    <ul>{data?.reasons.map((r) => <li key={r.code}>{r.message} <small>({r.code})</small></li>)}</ul>
    {!!data?.stack_readiness?.length && <ul aria-label="Disponibilidade por perfil">{data.stack_readiness.map((profile) => <li key={profile.stack}>
      <code>{profile.stack}</code>: <strong>{profile.can_publish ? "Publicação liberada" : "Publicação bloqueada"}</strong>
      {profile.reasons.map((item) => <p key={item.code}>{item.message} <small>({item.code})</small></p>)}
    </li>)}</ul>}
    {data?.workers.length === 0 && <p className="notice">Nenhum worker enviou sinal nos últimos 60 segundos. Inicie ou verifique o serviço worker.</p>}
    {data?.workers.map((worker) => <div key={worker.id} className={`notice ${worker.available ? "success" : "error"}`} role="status">
      <strong>{worker.available ? "Executor disponível" : "Executor indisponível"}</strong> {worker.code && <small>({worker.code})</small>}
      <p>{worker.message}</p>
      {worker.failed_checks && worker.failed_checks.length > 0 && <small>Controles não aprovados: {worker.failed_checks.join(", ")}</small>}
      {worker.diagnostic_id && <p className="diagnostic-id">Diagnóstico: <code>{worker.diagnostic_id}</code></p>}
      {worker.remediation && <p><strong>Próxima ação:</strong> {worker.remediation}</p>}
      {worker.details && <details className="executor-details">
        <summary>Detalhes técnicos do executor</summary>
        <p>Etapa: <code>{worker.details.stage ?? "não informada"}</code> · Código de saída: <code>{worker.details.exit_code ?? "não iniciado"}</code></p>
        {worker.details.exception_type && <p>Exceção: <code>{worker.details.exception_type}</code></p>}
        {worker.details.stderr && <pre aria-label="Erro sanitizado do executor">{worker.details.stderr}</pre>}
        {worker.environment && <pre aria-label="Ambiente do executor">{JSON.stringify(worker.environment, null, 2)}</pre>}
      </details>}
      <small>Último heartbeat: {new Date(worker.checked_at).toLocaleString("pt-BR")}</small>
    </div>)}
    {data?.verification && <PublicationVerificationStatus verification={data.verification} />}
    <p>A verificação cria ou reutiliza um Worker e um banco D1 por perfil na conta configurada, verifica isolamento, build, publicação e leitura/escrita, e preserva esses recursos ao terminar, inclusive em caso de falha. Novos recursos precisam de vagas livres. Projetos existentes não são alterados.</p>
    {executorBlocked && <p className="notice" role="status">A verificação remota não pode iniciar enquanto o executor ou a conexão não estiverem prontos. Corrija o diagnóstico acima e atualize o estado. Nenhuma permissão Cloudflare remove um bloqueio local do sandbox.</p>}
    <label>Motivo da verificação<input value={reason} maxLength={300} disabled={busy || blocked} onChange={(e) => setReason(e.target.value)} /></label>
    <ErrorNotice error={error} />
    <div className="actions">
      <button className="button primary" disabled={!data || busy || blocked || !!active || !!executorBlocked || reason.trim().length < 8 || !["active", "validated"].includes(config.state)} onClick={async () => {
        setBusy(true); onBusy(true); setError(null);
        try {
          const pending = data?.verification?.state === "reconciling" ? data.verification : null;
          await command(pending ? `/publication-checks/${pending.id}/resume` : `/integration-connections/${config.connection_id}/verify-publication`, { reason }, "POST", pending ? undefined : config.version);
          setReason(""); setRefresh((n) => n + 1); changed();
        } catch (e) { setError(e); } finally { setBusy(false); onBusy(false); }
      }}>{busy ? "Agendando…" : active ? "Verificação em andamento" : data?.verification?.state === "reconciling" ? "Retomar verificação" : "Verificar publicação"}</button>
      <button className="button" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Atualizar diagnóstico</button>
      <button className="button" disabled={!data} onClick={async () => {
        try { await navigator.clipboard.writeText(diagnosticText); setCopied(true); }
        catch { setCopied(false); setError(new Error("Não foi possível copiar. Abra o diagnóstico completo abaixo e copie o texto.")); }
      }}>{copied ? "Diagnóstico copiado" : "Copiar diagnóstico"}</button>
    </div>
    {data && <details className="executor-details"><summary>Diagnóstico completo para suporte</summary><pre>{diagnosticText}</pre></details>}
  </section>;
}
