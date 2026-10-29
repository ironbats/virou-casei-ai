import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { Cpu, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import { api, command, date } from "./api";
import { useRemote } from "./integration-ui";
import type { AgentConfiguration } from "./agent-configuration-fields";
import {
  ModelExecutionFields,
  modelExecutionChanged,
  modelExecutionPayload,
} from "./model-execution-fields";

type Model = {
  id: string;
  model: string;
  name: string;
  version: number;
  enabled: boolean;
  verified: boolean;
  ready: boolean;
  input_usd_per_million: string;
  cached_usd_per_million: string;
  output_usd_per_million: string;
  fx_brl_per_usd: string;
  fx_source: string;
  evidence: string;
  effective_at: string;
  valid_until: string;
  max_output_tokens: number;
  execution?: Record<string, unknown> | null;
};
type Connection = {
  id: string;
  name: string;
  provider: "openai" | "gemini";
  auth_mode: "api_key" | "service_account";
  version: number;
  enabled: boolean;
  credential_configured: boolean;
  project_id: string;
  organization: string;
  location: string;
  models: Model[];
};
type Available = {
  id: string;
  name: string;
  methods: string[];
  source: "local" | "provider";
  status?: "reference" | "deprecated" | "retired";
};
type Catalog = {
  items: Available[];
  source: "local" | "provider" | "fallback";
  provider_count: number;
  catalog_version: string;
  warning: {
    code: string;
    message: string;
    stage: string;
    upstream_status: number | null;
  } | null;
};
const modelPath = (c: Connection, m?: Model) =>
  `/ai/connections/${c.id}/models${m ? "/" + m.id : ""}`;
function localDate(value: string) {
  const d = new Date(value);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function Modal({
  title,
  close,
  children,
  busy,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
  busy: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      className="ai-dialog"
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) close();
      }}
    >
      <div className="dialog-head">
        <h2>{title}</h2>
        <button
          className="icon-button"
          type="button"
          disabled={busy}
          aria-label="Fechar"
          onClick={close}
        >
          <X />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function AISettings({
  refreshToken,
  onChanged,
}: {
  refreshToken: number;
  onChanged?: () => void;
}) {
  const configuration = useRemote<AgentConfiguration>(
    "/ai/configuration-schema",
    refreshToken,
  );
  const [connections, setConnections] = useState<Connection[]>([]),
    [loading, setLoading] = useState(true);
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{
    kind: "connection" | "model" | "test";
    connection?: Connection;
    model?: Model;
  } | null>(null);
  const [provider, setProvider] = useState<"openai" | "gemini">("openai"),
    [auth, setAuth] = useState<"api_key" | "service_account">("api_key");
  const [credential, setCredential] = useState(""),
    [available, setAvailable] = useState<Available[]>([]),
    [discovering, setDiscovering] = useState(false);
  const [discoverMessage, setDiscoverMessage] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(false),
    [catalogVersion, setCatalogVersion] = useState(""),
    [catalogWarning, setCatalogWarning] = useState(""),
    [catalogSearch, setCatalogSearch] = useState(""),
    [modelId, setModelId] = useState(""),
    [modelName, setModelName] = useState("");
  const catalogRequest = useRef<AbortController | null>(null);
  useEffect(() => {
    if (editor?.kind !== "model" || !editor.connection) return;
    const controller = new AbortController();
    catalogRequest.current = controller;
    setCatalogLoading(true);
    api<Catalog>(`/ai/connections/${editor.connection.id}/catalog`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted) return;
        setAvailable(result.items);
        setCatalogVersion(result.catalog_version);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setCatalogWarning(
            "Não foi possível carregar o catálogo local. Você pode informar o identificador manualmente.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setCatalogLoading(false);
      });
    return () => controller.abort();
  }, [editor]);
  async function refresh() {
    const result = await api<{ items: Connection[] }>("/ai/connections");
    setConnections(result.items);
  }
  useEffect(() => {
    setLoading(true);
    refresh()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [refreshToken]);
  function open(
    kind: "connection" | "model" | "test",
    connection?: Connection,
    model?: Model,
  ) {
    setEditor({ kind, connection, model });
    setError("");
    setMessage("");
    setCredential("");
    setAvailable([]);
    setDiscoverMessage("");
    setCatalogWarning("");
    setCatalogSearch("");
    setCatalogVersion("");
    setModelId(model?.model ?? "");
    setModelName(model?.name ?? "");
    setProvider(connection?.provider ?? "openai");
    setAuth(connection?.auth_mode ?? "api_key");
  }
  function close() {
    setEditor(null);
    setCredential("");
    setError("");
  }
  async function discover() {
    if (!editor?.connection || discovering || catalogLoading) return;
    const signal = catalogRequest.current?.signal;
    setDiscovering(true);
    setError("");
    setDiscoverMessage("");
    setCatalogWarning("");
    try {
      const result = await api<Catalog>(
        `/ai/connections/${editor.connection.id}/discover`,
        { method: "POST", signal },
      );
      if (signal?.aborted) return;
      setAvailable(result.items);
      setCatalogVersion(result.catalog_version);
      if (result.warning) {
        const status = result.warning.upstream_status
          ? ` · HTTP ${result.warning.upstream_status}`
          : "";
        setCatalogWarning(
          `${result.warning.message} (${result.warning.code}${status}). O catálogo local continua disponível para configuração manual.`,
        );
        if (result.provider_count)
          setDiscoverMessage(
            `${result.provider_count} modelo(s) recebidos antes da falha. A consulta ficou incompleta.`,
          );
      } else {
        setDiscoverMessage(
          `${result.provider_count} modelo(s) retornados pelo provedor. As referências locais também estão disponíveis.`,
        );
      }
    } catch (e) {
      if (!signal?.aborted)
        setCatalogWarning(
          `${(e as Error).message} Você pode continuar a configuração manual.`,
        );
    } finally {
      if (!signal?.aborted) setDiscovering(false);
    }
  }
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!editor || busy) return;
    const f = new FormData(e.currentTarget),
      get = (name: string) => String(f.get(name) ?? "");
    setBusy(true);
    setError("");
    try {
      if (editor.kind === "connection") {
        const c = editor.connection;
        await command(`/ai/connections${c ? "/" + c.id : ""}`, {
          name: get("name"),
          provider,
          auth_mode: auth,
          credential: credential || null,
          project_id: get("project_id"),
          organization: get("organization"),
          location: get("location") || "global",
          enabled: f.has("enabled"),
          expected_version: c?.version ?? null,
          reason: "Configuração administrativa de IA",
        });
        setCredential("");
        setMessage(
          "Conexão salva. Cadastre e teste um modelo antes de habilitá-lo para geração.",
        );
      } else if (editor.kind === "model") {
        const c = editor.connection!,
          m = editor.model;
        if (!configuration.data || configuration.error)
          throw new Error("Carregue os parâmetros do modelo antes de salvar.");
        const execution = modelExecutionPayload(f, c.provider);
        const executionChanged = modelExecutionChanged(m?.execution, execution);
        await command(modelPath(c, m), {
          name: get("name"),
          model: get("model"),
          expected_version: m?.version ?? null,
          enabled: f.has("enabled") && !executionChanged,
          input_usd_per_million: get("input_usd_per_million"),
          cached_usd_per_million: get("cached_usd_per_million"),
          output_usd_per_million: get("output_usd_per_million"),
          fx_brl_per_usd: get("fx_brl_per_usd"),
          fx_source: get("fx_source"),
          evidence: get("evidence"),
          effective_at: new Date(get("effective_at")).toISOString(),
          valid_until: new Date(get("valid_until")).toISOString(),
          max_output_tokens: Number(get("max_output_tokens")),
          execution,
          reason: "Configuração administrativa de modelo",
        });
        setMessage(
          executionChanged
            ? "Execução salva. O modelo ficou desabilitado; teste a geração e habilite-o novamente após a confirmação."
            : "Modelo salvo. A geração usa apenas modelos testados, habilitados e com custos vigentes.",
        );
      } else {
        const result = await command<{ state: string }>(
          modelPath(editor.connection!, editor.model) + "/test",
          {
            expected_version: editor.model!.version,
            reason: "Teste explícito de geração do modelo",
          },
        );
        const messages: Record<string, string> = {
          verified:
            "Teste concluído: o modelo respondeu no formato esperado. Edite o modelo e habilite o uso na plataforma.",
          failed:
            "O teste não confirmou a integração. Confira credenciais, permissões e compatibilidade do modelo com geração de texto JSON.",
          uncertain:
            "O provedor não confirmou o resultado. Pode haver cobrança. Consulte o uso na conta do provedor antes de iniciar outro teste.",
          running:
            "Este teste já foi solicitado e está sem resultado confirmado. Consulte o uso no provedor antes de repetir.",
          configuration_changed:
            "A configuração mudou durante o teste. Atualize os dados e teste a versão atual.",
        };
        setMessage(
          messages[result.state] ??
            "Resultado indisponível. Consulte o provedor antes de repetir.",
        );
      }
      setEditor(null);
      await refresh();
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const c = editor?.connection,
    m = editor?.model;
  const alert = error && (
    <div className="notice error" role="alert">
      {error}
    </div>
  );
  return (
    <section className="ai-settings" aria-label="Configurações de IA">
      <div className="ai-intro panel">
        <div>
          <h2>
            <Cpu size={22} /> Conexões e modelos
          </h2>
          <p>
            Conecte OpenAI ou Google, consulte os modelos da sua conta e
            habilite os que serão usados para gerar projetos.
          </p>
          <p className="small muted">
            Após habilitar o primeiro modelo de um provedor, suas chaves e
            modelos passam a ser gerenciados aqui. Até lá, a configuração atual
            continua funcionando.
          </p>
        </div>
        <button className="button primary" onClick={() => open("connection")}>
          <Plus size={17} />
          Nova conexão
        </button>
      </div>
      {!editor && alert}
      {!!configuration.error && (
        <div className="notice error" role="alert">
          Não foi possível carregar os parâmetros de execução dos modelos.
          <button className="button" onClick={configuration.refresh}>
            Tentar carregar parâmetros
          </button>
        </div>
      )}
      {message && (
        <div className="notice" role="status">
          {message}
        </div>
      )}
      {loading ? (
        <p role="status">Carregando configurações…</p>
      ) : connections.length === 0 ? (
        <div className="empty-state">
          <ShieldCheck size={36} />
          <h2>Nenhuma conexão cadastrada</h2>
          <p>
            As credenciais ficam criptografadas no servidor e não são devolvidas
            ao navegador.
          </p>
        </div>
      ) : (
        <div className="ai-connections">
          {connections.map((connection) => (
            <article className="panel" key={connection.id}>
              <div className="card-title">
                <div>
                  <h2>{connection.name}</h2>
                  <p className="small muted">
                    {connection.provider === "openai"
                      ? "OpenAI"
                      : "Google Gemini"}{" "}
                    ·{" "}
                    {connection.auth_mode === "api_key"
                      ? "API key"
                      : "Service Account"}
                  </p>
                </div>
                <span className="badge">
                  {connection.enabled ? "Credencial salva" : "Desabilitada"}
                </span>
              </div>
              <div className="actions">
                <button
                  className="button"
                  onClick={() => open("connection", connection)}
                >
                  Editar conexão
                </button>
                <button
                  className="button primary"
                  disabled={!connection.enabled}
                  onClick={() => open("model", connection)}
                >
                  <Plus size={16} />
                  Adicionar modelo
                </button>
              </div>
              {connection.models.length === 0 && (
                <p className="muted">
                  Adicione um modelo para concluir a configuração.
                </p>
              )}
              <ul className="ai-models">
                {connection.models.map((model) => (
                  <li key={model.id}>
                    <div>
                      <strong>{model.name}</strong>
                      <code>{model.model}</code>
                      <span className={`badge ${model.ready ? "ready" : ""}`}>
                        {model.ready
                          ? "Pronto para uso"
                          : !connection.enabled
                            ? "Conexão desabilitada"
                            : !model.verified
                              ? "Teste necessário"
                              : !model.enabled
                                ? "Testado · desabilitado"
                                : "Custos fora da vigência"}
                      </span>
                      <small>
                        Custos válidos até {date(model.valid_until)}
                      </small>
                    </div>
                    <div className="actions">
                      <button
                        className="button"
                        onClick={() => open("model", connection, model)}
                      >
                        Editar modelo
                      </button>
                      <button
                        className="button"
                        disabled={!connection.enabled}
                        onClick={() => open("test", connection, model)}
                      >
                        Testar geração
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      )}
      {editor && (
        <Modal
          title={
            editor.kind === "connection"
              ? c
                ? "Editar conexão"
                : "Nova conexão de IA"
              : editor.kind === "model"
                ? m
                  ? "Editar modelo"
                  : "Adicionar modelo"
                : "Testar geração do modelo"
          }
          close={close}
          busy={busy || discovering}
        >
          {alert}
          {editor.kind === "model" && !!configuration.error && (
            <div className="notice error" role="alert">
              Não foi possível carregar os parâmetros de execução. Os valores
              digitados foram mantidos.
              <button
                className="button"
                type="button"
                onClick={configuration.refresh}
              >
                Tentar carregar parâmetros
              </button>
            </div>
          )}
          <form className="form" onSubmit={submit}>
            <fieldset disabled={busy} className="form-fieldset">
              {editor.kind === "connection" ? (
                <>
                  <label>
                    Nome da conexão
                    <input
                      name="name"
                      required
                      minLength={2}
                      maxLength={100}
                      defaultValue={c?.name}
                      placeholder="Ex.: OpenAI · produção"
                    />
                  </label>
                  <div className="form-grid">
                    <label>
                      Provedor
                      <select
                        aria-label="Provedor"
                        value={provider}
                        disabled={!!c}
                        onChange={(e) => {
                          setProvider(e.target.value as typeof provider);
                          setCredential("");
                        }}
                      >
                        <option value="openai">OpenAI · GPT</option>
                        <option value="gemini">Google · Gemini</option>
                      </select>
                    </label>
                    <label>
                      Autenticação
                      <select
                        aria-label="Autenticação"
                        value={auth}
                        disabled={!!c}
                        onChange={(e) => {
                          setAuth(e.target.value as typeof auth);
                          setCredential("");
                        }}
                      >
                        <option value="api_key">API key</option>
                        <option value="service_account">Service Account</option>
                      </select>
                    </label>
                  </div>
                  {auth === "service_account" && (
                    <div className="notice">
                      {provider === "openai"
                        ? "Na OpenAI, a Service Account autentica com uma API key do projeto. Informe essa chave e o Project ID abaixo."
                        : "No Google Cloud, envie o JSON da conta de serviço e informe o projeto com Vertex AI habilitado. A conta precisa de permissão para usar os modelos nesse projeto."}
                    </div>
                  )}
                  {provider === "gemini" && auth === "service_account" ? (
                    <>
                      <label>
                        Arquivo JSON da Service Account
                        <input
                          type="file"
                          accept=".json,application/json"
                          onChange={async (e) => {
                            const file = e.target.files?.[0];
                            if (!file) return;
                            if (file.size > 20000) {
                              setError("O arquivo deve ter no máximo 20 KB.");
                              return;
                            }
                            setCredential(await file.text());
                          }}
                        />
                      </label>
                      <label>
                        Credencial JSON
                        <textarea
                          name="credential"
                          value={credential}
                          onChange={(e) => setCredential(e.target.value)}
                          required={!c}
                          maxLength={20000}
                          rows={5}
                          spellCheck={false}
                          autoComplete="off"
                          placeholder={
                            c
                              ? "Deixe vazio para manter a credencial atual"
                              : "Cole o JSON da Service Account"
                          }
                        />
                      </label>
                    </>
                  ) : (
                    <label>
                      {auth === "service_account"
                        ? "API key da Service Account"
                        : "API key"}
                      <input
                        type="password"
                        value={credential}
                        onChange={(e) => setCredential(e.target.value)}
                        required={!c}
                        minLength={12}
                        maxLength={4096}
                        autoComplete="new-password"
                        placeholder={
                          c
                            ? "Deixe vazio para manter a chave atual"
                            : "Cole a chave do provedor"
                        }
                      />
                    </label>
                  )}
                  {(auth === "service_account" || provider === "openai") && (
                    <label>
                      Project ID {auth === "api_key" ? "(opcional)" : ""}
                      <input
                        name="project_id"
                        required={auth === "service_account"}
                        maxLength={100}
                        pattern="[A-Za-z0-9_-]+"
                        defaultValue={c?.project_id}
                      />
                    </label>
                  )}
                  {provider === "openai" && (
                    <label>
                      Organization ID (opcional)
                      <input
                        name="organization"
                        maxLength={100}
                        pattern="[A-Za-z0-9_-]+"
                        defaultValue={c?.organization}
                      />
                    </label>
                  )}
                  {provider === "gemini" && auth === "service_account" && (
                    <label>
                      Localização Vertex AI
                      <input
                        name="location"
                        required
                        pattern="[a-z][a-z0-9-]*"
                        maxLength={51}
                        defaultValue={c?.location ?? "global"}
                      />
                      <small>
                        Use global ou a região na qual o modelo está disponível,
                        como us-central1.
                      </small>
                    </label>
                  )}
                  <label className="check-row">
                    <input
                      name="enabled"
                      type="checkbox"
                      defaultChecked={c?.enabled ?? true}
                    />
                    Conexão habilitada
                  </label>
                  {c && (
                    <p className="small muted">
                      Salvar alterações exige testar os modelos novamente. As
                      chaves anteriores não serão exibidas.
                    </p>
                  )}
                </>
              ) : editor.kind === "model" ? (
                <>
                  <p className="muted">
                    {c!.name} · Geração de texto e código em JSON
                  </p>
                  <div className="actions">
                    <button
                      type="button"
                      className="button"
                      disabled={discovering || catalogLoading}
                      onClick={discover}
                    >
                      <RefreshCw size={16} />
                      {discovering
                        ? "Consultando…"
                        : "Consultar modelos do provedor"}
                    </button>
                  </div>
                  {discoverMessage && (
                    <p role="status" className="small">
                      {discoverMessage}
                    </p>
                  )}
                  {catalogWarning && (
                    <div role="alert" className="notice">
                      {catalogWarning}
                    </div>
                  )}
                  <p className="small muted" role="status">
                    {catalogLoading
                      ? "Carregando catálogo local…"
                      : `${available.length} referências disponíveis${catalogVersion ? ` · Catálogo local de ${catalogVersion}` : ""}.`}
                  </p>
                  <label>
                    Buscar no catálogo
                    <input
                      type="search"
                      value={catalogSearch}
                      onChange={(e) => setCatalogSearch(e.target.value)}
                      placeholder="Digite parte do nome ou identificador"
                    />
                  </label>
                  <label>
                    Selecionar modelo do catálogo
                    <select
                      value=""
                      disabled={catalogLoading || !available.length}
                      onChange={(e) => {
                        const item = available.find(
                          (candidate) => candidate.id === e.target.value,
                        );
                        if (item) {
                          setModelId(item.id);
                          setModelName(item.name.slice(0, 100));
                        }
                      }}
                    >
                      <option value="">
                        Selecione ou preencha o identificador abaixo
                      </option>
                      {available
                        .filter((item) =>
                          `${item.id} ${item.name}`
                            .toLowerCase()
                            .includes(catalogSearch.toLowerCase().trim()),
                        )
                        .map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.id} ·{" "}
                            {item.source === "provider" ? "Provedor" : "Local"}
                            {item.status === "retired"
                              ? " · Encerrado"
                              : item.status === "deprecated"
                                ? " · Descontinuado"
                                : ""}
                          </option>
                        ))}
                    </select>
                  </label>
                  {catalogSearch &&
                    !available.some((item) =>
                      `${item.id} ${item.name}`
                        .toLowerCase()
                        .includes(catalogSearch.toLowerCase().trim()),
                    ) && (
                      <p className="small" role="status">
                        Nenhuma referência encontrada. Informe o identificador
                        oficial abaixo.
                      </p>
                    )}
                  <label>
                    Identificador do modelo
                    <input
                      name="model"
                      required
                      maxLength={160}
                      pattern="[A-Za-z0-9_.:@\-]+"
                      value={modelId}
                      onChange={(e) => setModelId(e.target.value)}
                      placeholder="Selecione ou informe o ID oficial"
                    />
                  </label>
                  <label>
                    Nome para identificação
                    <input
                      name="name"
                      required
                      minLength={2}
                      maxLength={100}
                      value={modelName}
                      onChange={(e) => setModelName(e.target.value)}
                      placeholder="Ex.: Modelo principal"
                    />
                  </label>
                  <div className="notice">
                    O catálogo local reúne referências públicas do provedor. A
                    disponibilidade depende da conta, API e região. Modelos
                    encerrados, de imagem, áudio, vídeo ou embeddings podem
                    constar como referência. Informe custos e validade, salve e
                    teste a geração JSON antes de habilitar o modelo para uso. A
                    seleção não altera preços nem confirma acesso ao provedor.
                  </div>
                  <h3>Custos do provedor</h3>
                  <p className="small muted">
                    Informe os preços oficiais em USD por 1 milhão de tokens e o
                    câmbio utilizado. Isso mantém a reserva e a cobrança de
                    saldo corretas.
                  </p>
                  <div className="form-grid">
                    {(
                      [
                        ["input_usd_per_million", "Entrada (USD)"],
                        ["cached_usd_per_million", "Entrada em cache (USD)"],
                        ["output_usd_per_million", "Saída (USD)"],
                      ] as const
                    ).map(([name, label]) => (
                      <label key={name}>
                        {label}
                        <input
                          name={name}
                          type="number"
                          min="0"
                          max="1000000"
                          step="0.00000001"
                          required
                          defaultValue={m?.[name]}
                        />
                      </label>
                    ))}
                    <label>
                      Câmbio (BRL por USD)
                      <input
                        name="fx_brl_per_usd"
                        type="number"
                        min="0.00000001"
                        max="99.99999999"
                        step="0.00000001"
                        required
                        defaultValue={m?.fx_brl_per_usd}
                      />
                    </label>
                  </div>
                  <label>
                    Fonte do câmbio
                    <input
                      name="fx_source"
                      required
                      maxLength={300}
                      defaultValue={m?.fx_source}
                      placeholder="Fonte e data da cotação"
                    />
                  </label>
                  <label>
                    Referência dos preços
                    <input
                      name="evidence"
                      required
                      maxLength={300}
                      defaultValue={m?.evidence}
                      placeholder="URL ou referência da tabela oficial"
                    />
                  </label>
                  <div className="form-grid">
                    <label>
                      Início da vigência
                      <input
                        name="effective_at"
                        type="datetime-local"
                        required
                        defaultValue={localDate(
                          m?.effective_at ?? new Date().toISOString(),
                        )}
                      />
                    </label>
                    <label>
                      Fim da vigência
                      <input
                        name="valid_until"
                        type="datetime-local"
                        required
                        defaultValue={m ? localDate(m.valid_until) : ""}
                      />
                    </label>
                  </div>
                  {configuration.data ? (
                    <ModelExecutionFields
                      configuration={configuration.data}
                      provider={c!.provider}
                      model={modelId}
                      initialMaxOutputTokens={m?.max_output_tokens}
                      initialExecution={m?.execution}
                    />
                  ) : (
                    <p role="status">Carregando parâmetros de execução…</p>
                  )}
                  <label className="check-row">
                    <input
                      name="enabled"
                      type="checkbox"
                      disabled={!m?.verified || !c!.enabled}
                      defaultChecked={m?.enabled && m.verified && c!.enabled}
                    />
                    Habilitar para uso na plataforma
                  </label>
                  {!m?.verified && (
                    <p className="small muted">
                      Salve o modelo e use “Testar geração”. Depois do teste
                      bem-sucedido, habilite-o aqui.
                    </p>
                  )}
                </>
              ) : (
                <>
                  <p>
                    <strong>{m!.name}</strong> · {m!.model}
                  </p>
                  <div className="notice">
                    Este teste envia uma solicitação curta ao provedor e pode
                    gerar cobrança na sua conta de IA. Não utiliza o saldo de um
                    aluno.
                  </div>
                  <p>
                    O teste verifica autenticação, acesso ao modelo, formato
                    JSON e informações de consumo. Modelos incompatíveis
                    permanecem desabilitados.
                  </p>
                </>
              )}
              <div className="actions">
                <button
                  className="button"
                  type="button"
                  disabled={busy || discovering}
                  onClick={close}
                >
                  Cancelar
                </button>
                <button
                  className="button primary"
                  disabled={
                    busy ||
                    discovering ||
                    (editor.kind === "model" &&
                      (!configuration.data || !!configuration.error))
                  }
                >
                  {busy
                    ? "Processando…"
                    : editor.kind === "test"
                      ? "Confirmar teste com possível cobrança"
                      : "Salvar configuração"}
                </button>
              </div>
            </fieldset>
          </form>
        </Modal>
      )}
    </section>
  );
}
