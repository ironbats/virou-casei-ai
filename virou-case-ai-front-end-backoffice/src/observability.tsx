import { useEffect, useId, useRef, useState } from "react";
import { Pause, Play, RefreshCw, Settings2, Terminal } from "lucide-react";
import { api, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationTraceDetail } from "./integration-trace-detail";
import { ObservabilityPolicy } from "./observability-policy";
import {
  captureLabels,
  captureStateForSection,
  type PayloadSection,
  phaseLabels,
  type MonitorFeed,
  type RuntimeEvent,
  type TraceMetadata,
} from "./observability-types";
import "./observability.css";

const services: Record<string, string> = {
  jev: "Jev",
  agents: "Agentes",
  cloudflare: "Cloudflare",
  platform: "Plataforma",
};
const levels: Record<string, string> = {
  info: "Informação",
  warning: "Atenção",
  error: "Erro",
};
const directions: Record<string, string> = {
  exchange: "Chamada ao provedor",
  normalized: "Resultado mapeado",
  fields: "Campos registrados",
};
type Filters = {
  correlation: string;
  project: string;
  state: string;
  since: string;
  until: string;
};
function sectionAvailability(trace: TraceMetadata, section: PayloadSection) {
  const state = captureStateForSection(trace, section);
  if (state === "captured") return "disponível";
  if (state === "protected") return "protegido e disponível";
  if (state === "truncated") return "captura parcial";
  return (captureLabels[state] || "Payload indisponível").toLocaleLowerCase();
}
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const emptyFilters: Filters = {
  correlation: "",
  project: "",
  state: "",
  since: "",
  until: "",
};

export function Observability({ refreshToken }: { refreshToken: number }) {
  const [mode, setMode] = useState<"events" | "traces">("events");
  const [service, setService] = useState("");
  const [level, setLevel] = useState("");
  const [phase, setPhase] = useState("");
  const [draft, setDraft] = useState<Filters>(emptyFilters);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [filterError, setFilterError] = useState<unknown>(null);
  const [paused, setPaused] = useState(false);
  const pausedRef = useRef(false);
  const [cursor, setCursor] = useState<string | number | null>(null);
  const [feed, setFeed] = useState<MonitorFeed<
    RuntimeEvent | TraceMetadata
  > | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(true);
  const [following, setFollowing] = useState(true);
  const [detail, setDetail] = useState<TraceMetadata | null>(null);
  const [policy, setPolicy] = useState(false);
  const terminal = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const identity = useId();
  useEffect(() => {
    let disposed = false;
    let request: AbortController | null = null;
    setError(null);
    const poll = async () => {
      const controller = new AbortController();
      request = controller;
      const timeout = window.setTimeout(() => controller.abort(), 12000);
      setLoading(true);
      const params = new URLSearchParams({ limit: "200" });
      if (service) params.set("service", service);
      if (level) params.set("level", level);
      if (phase) params.set("phase", phase);
      for (const [key, value] of Object.entries(filters))
        if (value) params.set(key, value);
      if (cursor !== null) params.set("before", String(cursor));
      try {
        const result = await api<MonitorFeed<RuntimeEvent | TraceMetadata>>(
          `/observability/${mode}?${params}`,
          { signal: controller.signal },
        );
        if (!disposed) {
          setFeed(result);
          setError(null);
        }
      } catch (failure) {
        if (!disposed)
          setError(
            controller.signal.aborted
              ? new Error(
                  "A consulta demorou mais que o esperado. Tente atualizar o monitor.",
                )
              : failure,
          );
      } finally {
        window.clearTimeout(timeout);
        if (!disposed) {
          setLoading(false);
          if (!pausedRef.current && cursor === null)
            timer.current = window.setTimeout(poll, 3000);
        }
      }
    };
    void poll();
    return () => {
      disposed = true;
      request?.abort();
      window.clearTimeout(timer.current);
    };
  }, [mode, service, level, phase, filters, cursor, tick, refreshToken]);
  useEffect(() => {
    if (following && terminal.current && cursor === null)
      terminal.current.scrollTop = terminal.current.scrollHeight;
  }, [feed, following, cursor]);
  function reset() {
    setCursor(null);
    setFeed(null);
  }
  function pause(value: boolean) {
    pausedRef.current = value;
    setPaused(value);
    window.clearTimeout(timer.current);
    if (!value) setTick((current) => current + 1);
  }
  function changeMode(next: "events" | "traces") {
    if (next !== mode) {
      reset();
      setMode(next);
    }
  }
  function changeDraft(key: keyof Filters, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }
  return (
    <section
      className="observability"
      aria-label="Monitoria em tempo de execução"
    >
      <div className="monitor-intro">
        <div>
          <h2>Acompanhe a jornada</h2>
          <p>
            Eventos operacionais e integrações registradas, da recomendação à
            construção.
          </p>
        </div>
        <button className="button" onClick={() => setPolicy(true)}>
          <Settings2 size={16} />
          Políticas de observabilidade
        </button>
      </div>
      <div
        className="trace-tabs monitor-tabs"
        role="tablist"
        aria-label="Visões do monitor"
      >
        {(["events", "traces"] as const).map((name, index) => (
          <button
            type="button"
            role="tab"
            key={name}
            id={`${identity}-${name}`}
            aria-selected={mode === name}
            aria-controls={`${identity}-feed`}
            tabIndex={mode === name ? 0 : -1}
            ref={(element) => {
              tabRefs.current[index] = element;
            }}
            onClick={() => changeMode(name)}
            onKeyDown={(event) => {
              if (
                !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              )
                return;
              event.preventDefault();
              const next =
                event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
              changeMode(next === 0 ? "events" : "traces");
              tabRefs.current[next]?.focus();
            }}
          >
            {name === "events"
              ? "Eventos operacionais"
              : "Integrações e payloads"}
          </button>
        ))}
      </div>
      <form
        className="monitor-filters"
        onSubmit={(event) => {
          event.preventDefault();
          if (
            draft.since &&
            draft.until &&
            new Date(draft.since).getTime() > new Date(draft.until).getTime()
          ) {
            setFilterError(
              new Error("A data inicial não pode ser posterior à data final."),
            );
            return;
          }
          setFilterError(null);
          reset();
          setFilters({
            correlation: draft.correlation.trim(),
            project: draft.project.trim(),
            state: draft.state.trim(),
            since: draft.since ? new Date(draft.since).toISOString() : "",
            until: draft.until ? new Date(draft.until).toISOString() : "",
          });
        }}
      >
        <label>
          Serviço
          <select
            value={service}
            onChange={(event) => {
              reset();
              setService(event.target.value);
            }}
          >
            <option value="">Todos os serviços</option>
            {Object.entries(services).map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Nível
          <select
            value={level}
            onChange={(event) => {
              reset();
              setLevel(event.target.value);
            }}
          >
            <option value="">Todos os níveis</option>
            {Object.entries(levels).map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Etapa
          <select
            value={phase}
            onChange={(event) => {
              reset();
              setPhase(event.target.value);
            }}
          >
            <option value="">Todas as etapas</option>
            {Object.entries(phaseLabels).map(([key, name]) => (
              <option key={key} value={key}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Estado registrado
          <input
            value={draft.state}
            maxLength={24}
            onChange={(event) => changeDraft("state", event.target.value)}
            placeholder="Ex.: ready, failed, stale"
          />
        </label>
        <label>
          ID da operação ou recomendação
          <input
            value={draft.correlation}
            maxLength={36}
            onChange={(event) => changeDraft("correlation", event.target.value)}
          />
        </label>
        <label>
          ID do projeto
          <input
            value={draft.project}
            maxLength={36}
            onChange={(event) => changeDraft("project", event.target.value)}
          />
        </label>
        <label>
          Desde (horário local)
          <input
            type="datetime-local"
            value={draft.since}
            onChange={(event) => changeDraft("since", event.target.value)}
          />
        </label>
        <label>
          Até (horário local)
          <input
            type="datetime-local"
            value={draft.until}
            onChange={(event) => changeDraft("until", event.target.value)}
          />
        </label>
        <button className="button monitor-apply" type="submit">
          Aplicar filtros
        </button>
      </form>
      <ErrorNotice error={filterError} />
      <div className="monitor-toolbar">
        <span role="status">
          {cursor !== null
            ? "Histórico"
            : paused
              ? "Atualização pausada"
              : "Atualização a cada 3 segundos"}{" "}
          · {feed ? date(feed.server_time) : "Conectando…"}
        </span>
        <div className="actions">
          <label className="monitor-follow">
            <input
              type="checkbox"
              checked={following}
              onChange={(event) => setFollowing(event.target.checked)}
            />
            Acompanhar últimas linhas
          </label>
          <button className="button" onClick={() => pause(!paused)}>
            {paused ? <Play size={16} /> : <Pause size={16} />}
            {paused ? "Retomar" : "Pausar"}
          </button>
          <button
            className="button"
            disabled={loading}
            onClick={() => setTick((value) => value + 1)}
          >
            <RefreshCw size={16} />
            Atualizar monitor
          </button>
        </div>
      </div>
      <ErrorNotice error={error} />
      {!!error && (
        <p className="notice">
          Conexão interrompida. Os últimos dados são mantidos;{" "}
          {paused || cursor !== null
            ? "use Atualizar monitor para tentar novamente."
            : "a atualização automática tentará novamente."}
        </p>
      )}
      <section
        role="tabpanel"
        id={`${identity}-feed`}
        aria-labelledby={`${identity}-${mode}`}
      >
        {mode === "traces" && (
          <p className="trace-protection-note">
            Request e Response representam capturas reais, quando disponíveis.
            Conteúdo livre é protegido; dados ausentes não são reconstruídos.
            {feed &&
              !feed.can_inspect_payloads &&
              " Seu acesso atual permite apenas metadados. Configure a permissão específica em Acesso a payloads para inspecionar e copiar."}
          </p>
        )}
        <div
          className="runtime-console"
          ref={terminal}
          tabIndex={0}
          role="region"
          aria-label={
            mode === "events" ? "Console de eventos" : "Integrações registradas"
          }
        >
          <div className="console-heading">
            <Terminal size={18} />
            viroucaseai / {mode === "events" ? "runtime" : "integrações"}
          </div>
          {loading && !feed && (
            <p role="status">
              Consultando {mode === "events" ? "eventos" : "integrações"}…
            </p>
          )}
          {!loading && !error && feed?.items.length === 0 && (
            <p>
              {mode === "events"
                ? "Nenhum evento para os filtros atuais. Utilize a plataforma para acompanhar novas execuções."
                : "Nenhuma integração registrada para os filtros atuais. Capturas históricas não são reconstruídas."}
            </p>
          )}
          {mode === "events"
            ? [...((feed?.items ?? []) as RuntimeEvent[])]
                .reverse()
                .map((event) => (
                  <article
                    className={`console-event ${event.level}`}
                    key={event.id}
                  >
                    <div className="console-line">
                      <time dateTime={event.at}>{date(event.at)}</time>
                      <strong>
                        [{services[event.service] || event.service}]
                      </strong>
                      <span>{levels[event.level] || event.level}</span>
                      <span>{event.state}</span>
                      {event.duration_ms !== null && (
                        <span>{event.duration_ms} ms</span>
                      )}
                    </div>
                    <p>
                      {event.message}{" "}
                      {event.error_code && <strong>{event.error_code}</strong>}
                    </p>
                    <details>
                      <summary>
                        {event.stage} · operação {event.correlation_id}
                      </summary>
                      <dl>
                        <dt>Conta</dt>
                        <dd>{event.account_id || "—"}</dd>
                        <dt>Projeto</dt>
                        <dd>{event.project_id || "—"}</dd>
                        <dt>Correlação</dt>
                        <dd>{event.correlation_id}</dd>
                      </dl>
                      <button
                        className="button"
                        onClick={() => {
                          setDraft((current) => ({
                            ...current,
                            correlation: event.correlation_id,
                            project: "",
                          }));
                          setFilters((current) => ({
                            ...current,
                            correlation: event.correlation_id,
                            project: "",
                          }));
                          reset();
                        }}
                      >
                        Filtrar esta execução
                      </button>
                    </details>
                  </article>
                ))
            : [...((feed?.items ?? []) as TraceMetadata[])]
                .reverse()
                .map((trace) => (
                  <article
                    className={`console-event trace-event ${trace.level}`}
                    key={trace.id}
                  >
                    <div className="console-line">
                      <time dateTime={trace.at}>{date(trace.at)}</time>
                      <strong>{phaseLabels[trace.stage] || trace.stage}</strong>
                      <span>{trace.status}</span>
                      {trace.duration_ms != null && (
                        <span>{trace.duration_ms} ms</span>
                      )}
                    </div>
                    <p>
                      {trace.provider || "Provedor não informado"} ·{" "}
                      {directions[trace.direction] || trace.direction} ·{" "}
                      {trace.attempt == null
                        ? "Tentativa não informada"
                        : `Tentativa ${trace.attempt}`}
                      {trace.model && <> · {trace.model}</>}
                    </p>
                    <p className="trace-section-availability">
                      Request: {sectionAvailability(trace, "request")} ·
                      Response: {sectionAvailability(trace, "response")}
                    </p>
                    <p>
                      <span className="trace-capture-state">
                        {captureLabels[trace.capture_state] ||
                          "Payload indisponível"}
                      </span>
                      {trace.truncated &&
                        trace.capture_state !== "truncated" && (
                          <> · Captura parcial</>
                        )}
                      {trace.redacted &&
                        trace.capture_state !== "protected" && (
                          <> · Dados protegidos</>
                        )}
                      {trace.error_code && (
                        <>
                          {" "}
                          · <strong>{trace.error_code}</strong>
                        </>
                      )}
                    </p>
                    <div className="trace-row-footer">
                      <span>
                        Operação{" "}
                        <code>{trace.operation_id || "Não informada"}</code>
                        {trace.draft_revision != null && (
                          <> · Rascunho v{trace.draft_revision}</>
                        )}
                        {trace.snapshot_id && (
                          <>
                            {" "}
                            · Snapshot <code>{trace.snapshot_id}</code>
                          </>
                        )}
                      </span>
                      <button
                        className="button"
                        onClick={() => setDetail(trace)}
                      >
                        Ver detalhes da integração
                      </button>
                    </div>
                  </article>
                ))}
        </div>
      </section>
      <div className="monitor-footer">
        <p className="small muted">
          Até 200 registros por página
          {feed && <> · retenção de {feed.retention_days} dias</>} · horários
          locais ({timezone}) ·{" "}
          {mode === "events"
            ? "somente metadados operacionais, sem prompts ou credenciais."
            : "payloads protegidos consultados apenas ao abrir o detalhe."}
        </p>
        <div className="actions">
          {cursor !== null && (
            <button
              className="button"
              onClick={() => {
                reset();
                pause(false);
              }}
            >
              Voltar ao vivo
            </button>
          )}
          {feed?.next_cursor != null && (
            <button
              className="button"
              disabled={loading}
              onClick={() => {
                setCursor(feed.next_cursor);
                setFeed(null);
              }}
            >
              {mode === "events"
                ? "Eventos anteriores"
                : "Integrações anteriores"}
            </button>
          )}
        </div>
      </div>
      {detail && (
        <IntegrationTraceDetail
          key={detail.id}
          trace={detail}
          canInspect={feed?.can_inspect_payloads === true}
          close={() => setDetail(null)}
        />
      )}
      {policy && (
        <ObservabilityPolicy
          close={() => setPolicy(false)}
          saved={() => setTick((value) => value + 1)}
        />
      )}
    </section>
  );
}
