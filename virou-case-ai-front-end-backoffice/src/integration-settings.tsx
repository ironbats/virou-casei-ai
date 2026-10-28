import { useState } from "react";
import { Cloud, Layers, Plus, SlidersHorizontal } from "lucide-react";
import { ErrorNotice } from "./admin-ui";
import { IntegrationEditor } from "./integration-editor";
import { IntegrationManager } from "./integration-manager";
import { Pagination, useRemote } from "./integration-ui";
import {
  providerLabels,
  stateLabels,
  type Connection,
  type Provider,
} from "./integration-types";
import { TemplateSettings } from "./template-settings";
import "./integration-settings.css";

export function IntegrationSettings({
  refreshToken = 0,
}: {
  refreshToken?: number;
}) {
  const [provider, setProvider] = useState<Provider | "templates">(
    "cloudflare",
  );
  return (
    <section className="panel integration-settings">
      <div
        className="integration-tabs"
        role="group"
        aria-label="Configurações de integração"
      >
        <button
          aria-pressed={provider === "cloudflare"}
          onClick={() => setProvider("cloudflare")}
        >
          <Cloud size={18} />
          Cloudflare
        </button>
        <button
          aria-pressed={provider === "jev"}
          onClick={() => setProvider("jev")}
        >
          <SlidersHorizontal size={18} />
          Fluxo guiado e Jev
        </button>
        <button
          aria-pressed={provider === "templates"}
          onClick={() => setProvider("templates")}
        >
          <Layers size={18} />
          Templates
        </button>
      </div>
      {provider === "templates" ? (
        <TemplateSettings refreshToken={refreshToken} />
      ) : (
        <Connections
          key={provider}
          provider={provider}
          refreshToken={refreshToken}
        />
      )}
    </section>
  );
}

function Connections({
  provider,
  refreshToken,
}: {
  provider: Provider;
  refreshToken: number;
}) {
  const remote = useRemote<{
    items: Connection[];
    defaults: Record<string, unknown>;
  }>(`/integrations/${provider}/connections`, refreshToken);
  const [query, setQuery] = useState("");
  const [state, setState] = useState("");
  const [page, setPage] = useState(0);
  const [message, setMessage] = useState("");
  const [modal, setModal] = useState<{
    mode: "edit" | "manage";
    connection: Connection | null;
  } | null>(null);
  const items = remote.data?.items || [];
  const filtered = items.filter(
    (c) =>
      (!state || c.state === state) &&
      [c.name, c.settings.account_id, c.settings.environment, c.settings.model]
        .join(" ")
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
  );
  const currentPage = Math.min(
    page,
    Math.max(0, Math.ceil(filtered.length / 10) - 1),
  );
  return (
    <>
      <div className="integration-intro">
        <div>
          <h2>
            {provider === "cloudflare" ? "Contas Cloudflare" : "Conexões Jev"}
          </h2>
          <p>
            {provider === "cloudflare"
              ? "Gerencie contas de hospedagem, credenciais e permissões de publicação."
              : "Gerencie conexões do recomendador e os limites do fluxo guiado."}
          </p>
        </div>
        <button
          className="button primary"
          disabled={!remote.data || remote.loading}
          onClick={() => setModal({ mode: "edit", connection: null })}
        >
          <Plus size={17} />
          Nova conexão {providerLabels[provider]}
        </button>
      </div>
      <p className="integration-hint">
        {provider === "cloudflare"
          ? "A conexão padrão recebe novos projetos. Publicações existentes continuam vinculadas à sua conta original."
          : "Cadastre quantas conexões precisar. Apenas a padrão fornece as recomendações e os limites do fluxo guiado."}
      </p>
      <ErrorNotice error={remote.error} />
      {remote.error && (
        <button className="button" onClick={remote.refresh}>
          Tentar novamente
        </button>
      )}
      {message && (
        <p className="notice success" role="status">
          {message}
        </p>
      )}
      <div className="integration-filters">
        <label>
          Buscar conexões
          <input
            type="search"
            placeholder={
              provider === "cloudflare"
                ? "Nome, conta ou ambiente"
                : "Nome ou modelo"
            }
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
          />
        </label>
        <label>
          Estado
          <select
            value={state}
            onChange={(e) => {
              setState(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Todos os estados</option>
            {Object.entries(stateLabels)
              .filter(([id]) => id !== "missing")
              .map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
          </select>
        </label>
      </div>
      {remote.loading ? (
        <p className="integration-empty" role="status">
          Carregando conexões…
        </p>
      ) : !remote.data ? null : !filtered.length ? (
        <div className="integration-empty" role="status">
          <strong>
            {items.length
              ? "Nenhuma conexão corresponde aos filtros"
              : `Nenhuma conexão ${providerLabels[provider]} cadastrada`}
          </strong>
          <p>
            {items.length
              ? "Altere a busca ou o estado para encontrar a conexão."
              : "Use Nova conexão para cadastrar a primeira conta."}
          </p>
        </div>
      ) : (
        <div
          className="table-wrap"
          role="region"
          aria-label={`Conexões ${providerLabels[provider]}`}
          tabIndex={0}
        >
          <table>
            <caption className="sr-only">
              Listagem de conexões {providerLabels[provider]}
            </caption>
            <thead>
              <tr>
                <th scope="col">Conexão</th>
                <th scope="col">
                  {provider === "cloudflare"
                    ? "Conta / ambiente"
                    : "Modelo / chamadas"}
                </th>
                <th scope="col">Estado</th>
                <th scope="col">Versão</th>
                <th scope="col">Ações</th>
              </tr>
            </thead>
            <tbody>
              {filtered
                .slice(currentPage * 10, currentPage * 10 + 10)
                .map((c) => (
                  <tr key={c.connection_id}>
                    <th scope="row">
                      <strong>{c.name}</strong>
                      {c.is_default && (
                        <span className="integration-badge default">
                          Padrão
                        </span>
                      )}
                      <small>
                        {c.has_token ? "Token cadastrado" : "Sem token"}
                      </small>
                    </th>
                    <td>
                      {provider === "cloudflare" ? (
                        <>
                          <code>{String(c.settings.account_id)}</code>
                          <small>{String(c.settings.environment)}</small>
                        </>
                      ) : (
                        <>
                          {String(c.settings.model)}
                          <small>
                            {c.settings.recommendation_enabled
                              ? "Chamadas habilitadas após ativação"
                              : "Chamadas desabilitadas"}
                          </small>
                        </>
                      )}
                    </td>
                    <td>
                      <span className={`integration-badge ${c.state}`}>
                        {stateLabels[c.state] || c.state}
                      </span>
                    </td>
                    <td>v{c.version}</td>
                    <td>
                      <div className="integration-row-actions">
                        <button
                          className="button"
                          aria-label={`Editar ${c.name}`}
                          onClick={() =>
                            setModal({ mode: "edit", connection: c })
                          }
                        >
                          Editar
                        </button>
                        <button
                          className="button"
                          aria-label={`Gerenciar ${c.name}`}
                          onClick={() =>
                            setModal({ mode: "manage", connection: c })
                          }
                        >
                          Gerenciar
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}
      {remote.data && !remote.loading && (
        <Pagination
          total={filtered.length}
          page={currentPage}
          setPage={setPage}
        />
      )}
      {modal?.mode === "edit" && (
        <IntegrationEditor
          provider={provider}
          current={modal.connection}
          defaults={remote.data?.defaults || {}}
          close={() => setModal(null)}
          saved={(name) => {
            setModal(null);
            setMessage(
              `Conexão ${name} salva. Valide e ative a nova versão em Gerenciar.`,
            );
            remote.refresh();
          }}
        />
      )}
      {modal?.mode === "manage" && modal.connection && (
        <IntegrationManager
          provider={provider}
          connection={modal.connection}
          close={() => setModal(null)}
          changed={remote.refresh}
        />
      )}
    </>
  );
}
