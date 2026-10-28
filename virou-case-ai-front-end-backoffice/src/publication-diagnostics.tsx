import { useEffect, useState } from "react";
import { api, command } from "./api";
import { ErrorNotice } from "./admin-ui";
import type { Connection } from "./integration-types";
import "./observability.css";

type Diagnostics = {
  can_publish: boolean; reasons: { code: string; message: string }[];
  workers: { id: string; available: boolean; message: string; checked_at: string }[];
  verification: { id: string; state: string; stage: string; error_code: string | null } | null;
};
export function PublicationDiagnostics({ config, blocked, onBusy, changed }: {
  config: Connection; blocked: boolean; onBusy: (busy: boolean) => void; changed: () => void;
}) {
  const [data, setData] = useState<Diagnostics | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const result = await api<Diagnostics>(`/integration-connections/${config.connection_id}/publication-diagnostics`, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(result); setError(null); }
      } catch (e) { if (!controller.signal.aborted) setError(e); }
      if (!controller.signal.aborted) timer = window.setTimeout(load, 4000);
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [config.connection_id, config.version, config.state, refresh]);
  const active = data?.verification && ["queued", "running"].includes(data.verification.state);
  return <section className="publication-diagnostics" aria-label="Diagnóstico da publicação">
    <h3>Publicação: diagnóstico e verificação</h3>
    <p role="status">{!data ? "Consultando executor…" : data.can_publish ? "Publicação liberada para esta conexão." : "Publicação bloqueada: resolva os itens abaixo."}</p>
    <ul>{data?.reasons.map((r) => <li key={r.code}>{r.message} <small>({r.code})</small></li>)}</ul>
    {data?.workers.length === 0 && <p className="notice">Nenhum worker enviou sinal nos últimos 60 segundos. Inicie ou verifique o serviço worker.</p>}
    {data?.workers.map((worker) => <p key={worker.id}>{worker.message}</p>)}
    {data?.verification && <p>Verificação: <strong>{data.verification.state}</strong> · {data.verification.stage} {data.verification.error_code && `· ${data.verification.error_code}`}<br /><small>Operação: {data.verification.id}</small></p>}
    <p>O teste cria um Worker e um banco D1 temporários na conta configurada, verifica isolamento, build, publicação e leitura/escrita, e remove os recursos ao terminar. Precisa de uma vaga livre. Projetos existentes não são alterados.</p>
    <label>Motivo da verificação<input value={reason} maxLength={300} disabled={busy || blocked} onChange={(e) => setReason(e.target.value)} /></label>
    <ErrorNotice error={error} />
    <div className="actions">
      <button className="button primary" disabled={busy || blocked || !!active || reason.trim().length < 8 || !["active", "validated"].includes(config.state)} onClick={async () => {
        setBusy(true); onBusy(true); setError(null);
        try {
          const pending = data?.verification?.state === "reconciling" ? data.verification : null;
          await command(pending ? `/publication-checks/${pending.id}/resume` : `/integration-connections/${config.connection_id}/verify-publication`, { reason }, "POST", pending ? undefined : config.version);
          setReason(""); setRefresh((n) => n + 1); changed();
        } catch (e) { setError(e); } finally { setBusy(false); onBusy(false); }
      }}>{busy ? "Agendando…" : active ? "Verificação em andamento" : data?.verification?.state === "reconciling" ? "Retomar verificação e limpeza" : "Verificar publicação"}</button>
      <button className="button" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Atualizar diagnóstico</button>
    </div>
  </section>;
}
