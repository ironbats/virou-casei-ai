import { useEffect, useRef, useState } from "react";
import { Pause, Play, RefreshCw, Terminal } from "lucide-react";
import { api, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import "./observability.css";

type Event = {
  id: number; at: string; service: string; level: string; account_id: string | null;
  project_id: string | null; correlation_id: string; stage: string; state: string;
  error_code: string | null; duration_ms: number | null; message: string;
};
type Feed = { items: Event[]; next_cursor: number | null; server_time: string; retention_days: number };
const services: Record<string, string> = { jev: "Jev", agents: "Agentes", cloudflare: "Cloudflare", platform: "Plataforma" };
const levels: Record<string, string> = { info: "Informação", warning: "Atenção", error: "Erro" };

export function Observability({ refreshToken }: { refreshToken: number }) {
  const [service, setService] = useState("");
  const [level, setLevel] = useState("");
  const [correlation, setCorrelation] = useState("");
  const [project, setProject] = useState("");
  const [filters, setFilters] = useState({ correlation: "", project: "" });
  const [paused, setPaused] = useState(false);
  const [cursor, setCursor] = useState<number | null>(null);
  const [feed, setFeed] = useState<Feed | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(true);
  const [following, setFollowing] = useState(true);
  const terminal = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    setLoading(true);
    setError(null);
    const poll = async () => {
      const params = new URLSearchParams({ limit: "200" });
      if (service) params.set("service", service);
      if (level) params.set("level", level);
      if (filters.correlation) params.set("correlation", filters.correlation);
      if (filters.project) params.set("project", filters.project);
      if (cursor) params.set("before", String(cursor));
      try {
        const result = await api<Feed>(`/observability/events?${params}`, { signal: controller.signal });
        if (!controller.signal.aborted) { setFeed(result); setError(null); }
      } catch (e) {
        if (!controller.signal.aborted) setError(e);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (!paused && !cursor) timer = window.setTimeout(poll, 3000);
        }
      }
    };
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [service, level, filters, cursor, paused, tick, refreshToken]);
  useEffect(() => {
    if (following && terminal.current && !cursor) terminal.current.scrollTop = terminal.current.scrollHeight;
  }, [feed, following, cursor]);
  function reset() { setCursor(null); setFeed(null); }
  return <section className="observability" aria-label="Monitoria em tempo de execução">
    <form className="monitor-filters" onSubmit={(e) => { e.preventDefault(); reset(); setFilters({ correlation: correlation.trim(), project: project.trim() }); }}>
      <label>Serviço<select value={service} onChange={(e) => { reset(); setService(e.target.value); }}>
        <option value="">Todos os serviços</option>{Object.entries(services).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>
      <label>Nível<select value={level} onChange={(e) => { reset(); setLevel(e.target.value); }}>
        <option value="">Todos os níveis</option>{Object.entries(levels).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
      </select></label>
      <label>ID da operação ou recomendação<input value={correlation} maxLength={36} onChange={(e) => setCorrelation(e.target.value)} /></label>
      <label>ID do projeto<input value={project} maxLength={36} onChange={(e) => setProject(e.target.value)} /></label>
      <button className="button" type="submit">Aplicar filtros</button>
    </form>
    <div className="monitor-toolbar">
      <span role="status">{cursor ? "Histórico" : paused ? "Atualização pausada" : "Atualização a cada 3 segundos"} · {feed ? date(feed.server_time) : "Conectando…"}</span>
      <div className="actions">
        <label className="monitor-follow"><input type="checkbox" checked={following} onChange={(e) => setFollowing(e.target.checked)} />Acompanhar últimas linhas</label>
        <button className="button" onClick={() => setPaused((v) => !v)}>{paused ? <Play size={16} /> : <Pause size={16} />}{paused ? "Retomar" : "Pausar"}</button>
        <button className="button" disabled={loading} onClick={() => setTick((v) => v + 1)}><RefreshCw size={16} />Atualizar monitor</button>
      </div>
    </div>
    <ErrorNotice error={error} />
    {!!error && <p className="notice">Conexão interrompida. Os últimos dados são mantidos; a atualização automática tentará novamente.</p>}
    <div className="runtime-console" ref={terminal} tabIndex={0} role="region" aria-label="Console de eventos">
      <div className="console-heading"><Terminal size={18} />viroucaseai / runtime</div>
      {loading && !feed && <p role="status">Consultando eventos…</p>}
      {!loading && !error && feed?.items.length === 0 && <p>Nenhum evento para os filtros atuais. Utilize a plataforma para acompanhar novas execuções.</p>}
      {[...(feed?.items ?? [])].reverse().map((event) => <article className={`console-event ${event.level}`} key={event.id}>
        <div className="console-line"><time dateTime={event.at}>{date(event.at)}</time><strong>[{services[event.service]}]</strong><span>{levels[event.level]}</span><span>{event.state}</span>{event.duration_ms !== null && <span>{event.duration_ms} ms</span>}</div>
        <p>{event.message} {event.error_code && <strong>{event.error_code}</strong>}</p>
        <details><summary>{event.stage} · operação {event.correlation_id}</summary>
          <dl><dt>Conta</dt><dd>{event.account_id || "—"}</dd><dt>Projeto</dt><dd>{event.project_id || "—"}</dd><dt>Correlação</dt><dd>{event.correlation_id}</dd></dl>
          <button className="button" onClick={() => { setCorrelation(event.correlation_id); setFilters({ correlation: event.correlation_id, project: "" }); reset(); }}>Filtrar esta execução</button>
        </details>
      </article>)}
    </div>
    <div className="monitor-footer"><p className="small muted">Até 200 eventos por página · retenção de 7 dias · horários locais · somente metadados operacionais, sem prompts ou credenciais.</p>
      <div className="actions">{cursor && <button className="button" onClick={() => { reset(); setPaused(false); }}>Voltar ao vivo</button>}
        {feed?.next_cursor && <button className="button" disabled={loading} onClick={() => { setCursor(feed.next_cursor); setFeed(null); }}>Eventos anteriores</button>}
      </div>
    </div>
  </section>;
}
