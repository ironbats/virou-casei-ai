import { useEffect, useId, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, command, date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog } from "./integration-ui";
import { ProtectedJson } from "./protected-json";
import {
  captureLabels,
  captureReasonLabels,
  captureStateForSection,
  phaseLabels,
  sectionLabels,
  type FieldComparison,
  type PayloadSection,
  type TraceMetadata,
  type TracePayload,
} from "./observability-types";
import "./integration-settings.css";

const sections = Object.keys(sectionLabels) as PayloadSection[];
const sources: Record<string, string> = {
  request: "Pedido",
  project_context: "Contexto verificado",
  user_answer: "Resposta do usuário",
  policy_suggestion: "Sugestão de política",
  manual: "Edição manual",
  user: "Edição do usuário",
  user_edit: "Edição do usuário",
};
const states: Record<string, string> = {
  suggested: "Sugerido",
  applied: "Aplicado",
  user_edited: "Editado pelo usuário",
  conflict: "Conflito",
  snapshot: "Snapshot enviado",
};
function firstSection(trace: TraceMetadata): PayloadSection {
  return (
    sections.find((section) => trace.available_sections.includes(section)) ||
    "request"
  );
}
function fieldValue(item: FieldComparison, key: keyof FieldComparison) {
  return Object.prototype.hasOwnProperty.call(item, key)
    ? typeof item[key] === "string"
      ? item[key]
      : JSON.stringify(item[key])
    : "Não registrado";
}
function useTraceResource<T>(path: string | null, revision: number) {
  const [result, setResult] = useState<{
    path: string | null;
    data: T | null;
    error: unknown;
    loading: boolean;
  }>({ path, data: null, error: null, loading: !!path });
  useEffect(() => {
    if (!path) {
      setResult({ path, data: null, error: null, loading: false });
      return;
    }
    const controller = new AbortController();
    let disposed = false;
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    setResult({ path, data: null, error: null, loading: true });
    void api<T>(path, { signal: controller.signal })
      .then((data) => {
        if (!disposed) setResult({ path, data, error: null, loading: false });
      })
      .catch((failure) => {
        if (!disposed)
          setResult({
            path,
            data: null,
            error: controller.signal.aborted
              ? new Error(
                  "A consulta demorou mais que o esperado. Tente carregar novamente.",
                )
              : failure,
            loading: false,
          });
      })
      .finally(() => window.clearTimeout(timeout));
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [path, revision]);
  return result.path === path
    ? result
    : { path, data: null, error: null, loading: !!path };
}

export function IntegrationTraceDetail({
  trace,
  canInspect,
  close,
}: {
  trace: TraceMetadata;
  canInspect: boolean;
  close: () => void;
}) {
  const [anchor, setAnchor] = useState(trace);
  const [section, setSection] = useState<PayloadSection>(() =>
    firstSection(trace),
  );
  const [copying, setCopying] = useState(false);
  const [revision, setRevision] = useState(0);
  const [timelineRevision, setTimelineRevision] = useState(0);
  const identity = useId();
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const timeline = useTraceResource<{
    items: TraceMetadata[];
    can_inspect_payloads: boolean;
  }>(
    `/observability/traces/${encodeURIComponent(trace.id)}/timeline`,
    timelineRevision,
  );
  const correlated =
    anchor.operation_id && anchor.attempt != null
      ? (timeline.data?.items ?? [])
          .filter(
            (item) =>
              item.id !== anchor.id &&
              item.trace_id === anchor.trace_id &&
              item.operation_id === anchor.operation_id &&
              item.stage === anchor.stage &&
              item.attempt === anchor.attempt &&
              item.available_sections.includes(section),
          )
          .sort((left, right) => right.sequence - left.sequence)
      : [];
  const selected = anchor.available_sections.includes(section)
    ? anchor
    : correlated[0] || anchor;
  const allowed = canInspect && timeline.data?.can_inspect_payloads !== false;
  const hasSection = selected.available_sections.includes(section);
  const content = useTraceResource<TracePayload>(
    allowed && hasSection
      ? `/observability/traces/${encodeURIComponent(selected.id)}/payload?section=${section}`
      : null,
    revision,
  );
  const visibleError = content.error || (!hasSection ? timeline.error : null);
  const payload = content.data;
  const captureState =
    payload?.capture_state || captureStateForSection(selected, section);
  const captureReason = payload?.capture_reason || selected.capture_reason;
  const showCaptureReason =
    ["expired", "unavailable", "not_captured"].includes(captureState) &&
    captureReason &&
    !(captureReason === "NON_JSON_REQUEST" && section !== "request") &&
    !(captureReason === "NON_JSON_RESPONSE" && section !== "response");
  const available =
    allowed &&
    !!payload &&
    ["captured", "protected", "truncated"].includes(payload.capture_state);
  const fieldPayload = payload?.payload as { items?: unknown } | null;
  const fields =
    section === "fields" &&
    available &&
    fieldPayload &&
    Array.isArray(fieldPayload.items)
      ? (fieldPayload.items as FieldComparison[])
      : [];
  const grouped = new Map<string, TraceMetadata[]>();
  for (const item of [...(timeline.data?.items ?? [])].sort(
    (left, right) => left.sequence - right.sequence,
  )) {
    const key = `${item.stage}:${item.operation_id}`;
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }
  return (
    <IntegrationDialog
      title="Detalhes da integração"
      close={close}
      busy={copying}
    >
      <div className="trace-detail">
        <p className="trace-protection-note">
          Payload real com dados protegidos. O resultado mapeado é separado da
          requisição e da resposta do provedor.
        </p>
        <div className="trace-record-summary">
          <h3>
            {phaseLabels[selected.stage] || selected.stage} · {selected.status}
          </h3>
          <p>
            {selected.provider || "Provedor não informado"}
            {selected.model && <> · {selected.model}</>} ·{" "}
            {selected.attempt == null
              ? "Tentativa não informada"
              : `Tentativa ${selected.attempt}`}
            {selected.http_status != null && (
              <> · HTTP {selected.http_status}</>
            )}
          </p>
          <p>
            {date(selected.at)} ·{" "}
            {selected.duration_ms == null
              ? "Duração não informada"
              : `${selected.duration_ms} ms`}
          </p>
        </div>
        {selected.error_code && (
          <p className="notice error">
            Código registrado: {selected.error_code}
          </p>
        )}
        <details className="trace-timeline">
          <summary>
            Linha do tempo da jornada
            {timeline.data && <> · {timeline.data.items.length} registros</>}
          </summary>
          <ErrorNotice error={timeline.error} />
          {!!timeline.error && (
            <button
              className="button"
              onClick={() => setTimelineRevision((value) => value + 1)}
            >
              Tentar carregar a jornada
            </button>
          )}
          <button
            type="button"
            className="button"
            disabled={timeline.loading || copying}
            onClick={() => setTimelineRevision((value) => value + 1)}
          >
            <RefreshCw size={15} />
            Atualizar jornada
          </button>
          <ol>
            {[...grouped.entries()].map(([key, attempts]) => (
              <li key={key}>
                <strong>
                  {phaseLabels[attempts[0].stage] || attempts[0].stage}
                </strong>
                <div className="trace-attempts">
                  {attempts.map((item) => (
                    <button
                      type="button"
                      className="button"
                      aria-pressed={selected.id === item.id}
                      key={item.id}
                      disabled={copying}
                      onClick={() => {
                        setAnchor(item);
                        setSection(firstSection(item));
                      }}
                    >
                      <span>
                        {item.attempt == null
                          ? "Tentativa não informada"
                          : `Tentativa ${item.attempt}`}{" "}
                        · {item.provider || "Provedor não informado"}
                      </span>
                      <small>
                        {item.status} · {item.direction}
                      </small>
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ol>
          {!timeline.data?.items.length && !timeline.error && (
            <p className="small">
              {timeline.loading
                ? "Consultando etapas registradas…"
                : "Nenhuma outra etapa registrada para esta jornada."}
            </p>
          )}
        </details>
        {selected.retry_reason && (
          <p className="small">
            Motivo da nova tentativa:{" "}
            {selected.retry_reason === "UNSUPPORTED_OPTIONAL_PARAMETER"
              ? "parâmetro opcional não suportado pelo provedor"
              : selected.retry_reason}
            .
          </p>
        )}
        <details className="trace-context-disclosure">
          <summary>Contexto e versões da integração</summary>
          <dl className="trace-context">
            <div>
              <dt>Jornada / sequência</dt>
              <dd>
                {selected.trace_id}
                <br />
                Registro {selected.sequence}
              </dd>
            </div>
            <div>
              <dt>Projeto / conta</dt>
              <dd>
                {selected.project_id || "Projeto não informado"}
                <br />
                {selected.account_id || "Conta não informada"}
              </dd>
            </div>
            <div>
              <dt>Span anterior</dt>
              <dd>{selected.parent_span_id || "Sem vínculo registrado"}</dd>
            </div>
            <div>
              <dt>Operação</dt>
              <dd>{selected.operation_id || "Não informada"}</dd>
            </div>
            <div>
              <dt>Modelo / conexão</dt>
              <dd>
                {selected.model || "Não informado"}
                <br />
                {selected.connection_id || "Não informada"}
              </dd>
            </div>
            <div>
              <dt>Quando / duração</dt>
              <dd>
                {date(selected.at)} ·{" "}
                {selected.duration_ms == null
                  ? "Duração não informada"
                  : `${selected.duration_ms} ms`}
              </dd>
            </div>
            <div>
              <dt>Resposta do provedor</dt>
              <dd>
                {selected.http_status == null
                  ? "HTTP não informado"
                  : `HTTP ${selected.http_status}`}
                <br />
                {selected.provider_request_id || "Request ID não informado"}
              </dd>
            </div>
            <div>
              <dt>Rascunho / preparação</dt>
              <dd>
                {selected.draft_revision ?? "Revisão não informada"}
                <br />
                {selected.preparation_id || "Preparação não informada"}
              </dd>
            </div>
            <div>
              <dt>Snapshot / span</dt>
              <dd>
                {selected.snapshot_id || "Snapshot não informado"}
                <br />
                {selected.span_id}
              </dd>
            </div>
            <div>
              <dt>Endpoint observado</dt>
              <dd>{selected.endpoint || "Não registrado"}</dd>
            </div>
            <div>
              <dt>Revisão do projeto</dt>
              <dd>{selected.project_revision || "Não registrada"}</dd>
            </div>
            <div>
              <dt>Versão do schema</dt>
              <dd>{selected.schema_version ?? "Não registrada"}</dd>
            </div>
          </dl>
          {!!selected.template_versions?.length && (
            <>
              <h4>Versões dos templates</h4>
              <ul>
                {selected.template_versions.map((item) => (
                  <li key={`${item.attachment_id}:${item.source_version_id}`}>
                    {item.attachment_id} · Origem {item.source_version_id} ·{" "}
                    {item.attachment_revision === null
                      ? "Versão do anexo não registrada"
                      : `Anexo v${item.attachment_revision}`}
                  </li>
                ))}
              </ul>
            </>
          )}
        </details>
        <p className="small muted">
          O status registrado é independente do código HTTP. Uma resposta HTTP
          200 não confirma preenchimento, validação ou construção.
        </p>
        <div
          className="trace-tabs"
          role="tablist"
          aria-label="Conteúdo da integração"
        >
          {sections.map((name, index) => (
            <button
              type="button"
              role="tab"
              id={`${identity}-${name}`}
              key={name}
              aria-selected={section === name}
              aria-controls={`${identity}-panel`}
              tabIndex={section === name ? 0 : -1}
              disabled={copying}
              ref={(element) => {
                tabRefs.current[index] = element;
              }}
              onClick={() => setSection(name)}
              onKeyDown={(event) => {
                let next = index;
                if (event.key === "ArrowRight")
                  next = (index + 1) % sections.length;
                else if (event.key === "ArrowLeft")
                  next = (index + sections.length - 1) % sections.length;
                else if (event.key === "Home") next = 0;
                else if (event.key === "End") next = sections.length - 1;
                else return;
                event.preventDefault();
                setSection(sections[next]);
                tabRefs.current[next]?.focus();
              }}
            >
              {sectionLabels[name]}
            </button>
          ))}
        </div>
        <section
          role="tabpanel"
          id={`${identity}-panel`}
          aria-labelledby={`${identity}-${section}`}
          tabIndex={0}
        >
          {!allowed ? (
            <p className="notice">
              A leitura de metadados está disponível. Para inspecionar ou copiar
              payloads, é necessária a permissão específica configurada em
              Acesso a payloads.
            </p>
          ) : (
            <>
              <div className="trace-content-state">
                <p className="small" role="status">
                  {visibleError
                    ? "Consulta indisponível"
                    : !hasSection && timeline.loading
                      ? "Consultando registros da jornada…"
                      : content.loading
                        ? "Carregando conteúdo protegido…"
                        : captureLabels[captureState] || "Payload indisponível"}
                  {payload && (
                    <>
                      {payload.policy_version != null && (
                        <> · Política v{payload.policy_version}</>
                      )}
                      {payload.expires_at && (
                        <> · Expira em {date(payload.expires_at)}</>
                      )}
                    </>
                  )}
                </p>
                {hasSection && (
                  <button
                    type="button"
                    className="button"
                    disabled={content.loading || copying}
                    onClick={() => setRevision((value) => value + 1)}
                  >
                    <RefreshCw size={15} />
                    Atualizar conteúdo
                  </button>
                )}
              </div>
              {showCaptureReason && (
                <p className="notice">
                  {captureReasonLabels[captureReason] || captureReason}
                </p>
              )}
              <ErrorNotice error={visibleError} />
              {!!visibleError && (
                <button
                  className="button"
                  onClick={() => {
                    if (!hasSection && timeline.error)
                      setTimelineRevision((value) => value + 1);
                    else setRevision((value) => value + 1);
                  }}
                >
                  Tentar carregar novamente
                </button>
              )}
              {!!payload?.redacted_paths.length && (
                <details>
                  <summary>
                    Dados protegidos ({payload.redacted_paths.length} caminhos)
                  </summary>
                  <ul>
                    {payload.redacted_paths.map((path) => (
                      <li key={path}>
                        <code>{path}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {!!payload?.truncated_paths.length && (
                <div className="notice">
                  <strong>Esta captura é parcial.</strong>
                  <p>Os limites afetaram somente a cópia de observabilidade.</p>
                  <ul>
                    {payload.truncated_paths.map((path) => (
                      <li key={path}>
                        <code>{path}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {fields.length > 0 && (
                <div
                  className="trace-fields-table"
                  role="region"
                  tabIndex={0}
                  aria-label="Comparação dos campos por identificador"
                >
                  <table>
                    <thead>
                      <tr>
                        <th>Campo / versão</th>
                        <th>Valor inicial</th>
                        <th>Sugestão</th>
                        <th>Edição humana</th>
                        <th>Valor final</th>
                        <th>Origem / estado</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fields.map((field) => (
                        <tr key={`${field.attachment_id}:${field.field_id}`}>
                          <th scope="row">
                            {field.attachment_id}
                            <br />
                            <code>{field.field_id}</code>
                            <br />
                            {field.attachment_revision == null
                              ? "Versão não registrada"
                              : `v${field.attachment_revision}`}
                            {field.snapshot_id && (
                              <>
                                <br />
                                Snapshot: {field.snapshot_id}
                              </>
                            )}
                          </th>
                          <td>{fieldValue(field, "original_value")}</td>
                          <td>{fieldValue(field, "suggested_value")}</td>
                          <td>{fieldValue(field, "edited_value")}</td>
                          <td>{fieldValue(field, "final_value")}</td>
                          <td>
                            {field.source_type
                              ? sources[field.source_type] || field.source_type
                              : "Origem não registrada"}
                            <br />
                            {field.state
                              ? states[field.state] || field.state
                              : "Estado não registrado"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <ProtectedJson
                key={`${selected.id}:${section}`}
                label={`JSON protegido: ${sectionLabels[section]}`}
                value={payload?.payload}
                available={available}
                onCopyingChange={setCopying}
                onCopy={
                  available
                    ? async () => {
                        const result = await command<TracePayload>(
                          `/observability/traces/${encodeURIComponent(selected.id)}/copy`,
                          { section },
                        );
                        if (
                          !["captured", "protected", "truncated"].includes(
                            result.capture_state,
                          )
                        )
                          throw new Error(
                            "O payload não está mais disponível para cópia. Atualize o conteúdo.",
                          );
                        return JSON.stringify(result.payload, null, 2);
                      }
                    : undefined
                }
              />
            </>
          )}
        </section>
      </div>
    </IntegrationDialog>
  );
}
