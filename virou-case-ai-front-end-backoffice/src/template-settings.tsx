import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { command } from "./api";
import { ErrorNotice } from "./admin-ui";
import { IntegrationDialog, Pagination, useRemote } from "./integration-ui";

type PersonalField = {
  key: string;
  label: string;
  required: boolean;
  max_length: number;
};
type Template = {
  id: string;
  version: number;
  title: string;
  category: string;
  summary: string;
  content: string;
  fields: PersonalField[];
  available: boolean;
};
const categories: Record<string, string> = {
  planning: "Planejamento",
  context: "Contexto",
  quality: "Qualidade",
  security: "Segurança",
};
const emptyTemplate: Template = {
  id: "",
  version: 0,
  title: "",
  category: "planning",
  summary: "",
  content: "",
  fields: [],
  available: true,
};

export function TemplateSettings({
  refreshToken = 0,
}: {
  refreshToken?: number;
}) {
  const remote = useRemote<{ items: Template[] }>("/templates", refreshToken);
  const [selected, setSelected] = useState<Template | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [state, setState] = useState("");
  const [page, setPage] = useState(0);
  const [message, setMessage] = useState("");
  const items = remote.data?.items || [];
  const filtered = items.filter(
    (t) =>
      (!category || category === t.category) &&
      (!state || String(t.available) === state) &&
      `${t.title} ${t.summary} ${t.id}`
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
          <h2>Catálogo de templates</h2>
          <p>
            Crie modelos reutilizáveis e personalize os campos sem editar JSON.
          </p>
        </div>
        <button
          className="button primary"
          onClick={() => setSelected(emptyTemplate)}
          disabled={remote.loading || !remote.data}
        >
          <Plus size={17} />
          Novo template
        </button>
      </div>
      <p className="integration-hint">
        Cada edição cria uma versão. Desativar impede novos envios; conteúdos já
        enviados são preservados.
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
      <div className="integration-filters templates">
        <label>
          Buscar templates
          <input
            type="search"
            placeholder="Título, descrição ou identificador"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
          />
        </label>
        <label>
          Filtrar categoria
          <select
            value={category}
            onChange={(e) => {
              setCategory(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Todas as categorias</option>
            {Object.entries(categories).map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Disponibilidade
          <select
            value={state}
            onChange={(e) => {
              setState(e.target.value);
              setPage(0);
            }}
          >
            <option value="">Todos</option>
            <option value="true">Disponíveis</option>
            <option value="false">Desativados</option>
          </select>
        </label>
      </div>
      {remote.loading ? (
        <p className="integration-empty" role="status">
          Carregando templates…
        </p>
      ) : !remote.data ? null : !filtered.length ? (
        <div className="integration-empty" role="status">
          <strong>Nenhum template encontrado</strong>
          <p>
            {items.length
              ? "Revise os filtros para encontrar o template."
              : "Crie o primeiro template usando Novo template."}
          </p>
        </div>
      ) : (
        <div
          className="table-wrap"
          role="region"
          aria-label="Catálogo de templates"
          tabIndex={0}
        >
          <table>
            <caption className="sr-only">Listagem de templates</caption>
            <thead>
              <tr>
                <th scope="col">Template</th>
                <th scope="col">Categoria</th>
                <th scope="col">Disponibilidade</th>
                <th scope="col">Versão</th>
                <th scope="col">Ações</th>
              </tr>
            </thead>
            <tbody>
              {filtered
                .slice(currentPage * 10, currentPage * 10 + 10)
                .map((t) => (
                  <tr key={t.id}>
                    <th scope="row">
                      <strong>{t.title}</strong>
                      <small>
                        {t.id} · {t.fields.length} campo
                        {t.fields.length === 1 ? "" : "s"}
                      </small>
                      <p className="template-summary">{t.summary}</p>
                    </th>
                    <td>{categories[t.category]}</td>
                    <td>
                      <span
                        className={`integration-badge ${t.available ? "active" : "disabled"}`}
                      >
                        {t.available ? "Disponível" : "Desativado"}
                      </span>
                    </td>
                    <td>v{t.version}</td>
                    <td>
                      <button
                        className="button"
                        aria-label={`Editar ${t.title}`}
                        onClick={() => setSelected(t)}
                      >
                        Editar
                      </button>
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
      {selected && (
        <TemplateEditor
          current={selected}
          close={() => setSelected(null)}
          saved={(name) => {
            setSelected(null);
            setMessage(`Template ${name} salvo com sucesso.`);
            remote.refresh();
          }}
        />
      )}
    </>
  );
}

function TemplateEditor({
  current,
  close,
  saved,
}: {
  current: Template;
  close: () => void;
  saved: (name: string) => void;
}) {
  const [draft, setDraft] = useState<Template>(current);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const dirty = !!reason || JSON.stringify(draft) !== JSON.stringify(current);
  const set = <K extends keyof Template>(key: K, value: Template[K]) =>
    setDraft((old) => ({ ...old, [key]: value }));
  const changeField = (index: number, change: Partial<PersonalField>) =>
    set(
      "fields",
      draft.fields.map((field, i) =>
        i === index ? { ...field, ...change } : field,
      ),
    );
  return (
    <IntegrationDialog
      title={current.version ? "Editar template" : "Novo template"}
      close={close}
      dirty={dirty}
      busy={busy}
    >
      <p className="field-help">
        {current.version
          ? `Editando ${current.title} · versão ${current.version}. O conteúdo anterior será preservado no histórico.`
          : "Defina o conteúdo e, se necessário, os campos que o usuário preencherá ao aplicar o template."}
      </p>
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy) return;
          setError(null);
          if (
            new Set(draft.fields.map((f) => f.key)).size !== draft.fields.length
          ) {
            setError("Cada campo pessoal precisa de uma chave diferente.");
            return;
          }
          setBusy(true);
          try {
            await command("/templates", {
              slug: draft.id,
              expected_version: current.version,
              title: draft.title,
              category: draft.category,
              summary: draft.summary,
              content: draft.content,
              fields: draft.fields,
              available: draft.available,
              reason,
            });
            saved(draft.title);
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <ErrorNotice error={error} />
        <fieldset className="form-fieldset" disabled={busy}>
          <fieldset className="integration-section">
            <legend>Identificação</legend>
            <div className="form-grid">
              <label>
                Título
                <input
                  autoFocus
                  required
                  minLength={3}
                  maxLength={100}
                  value={draft.title}
                  onChange={(e) => set("title", e.target.value)}
                />
              </label>
              <label>
                Identificador
                <input
                  required
                  pattern="[a-z0-9][a-z0-9\-]{1,79}"
                  maxLength={80}
                  value={draft.id}
                  readOnly={current.version > 0}
                  onChange={(e) => set("id", e.target.value)}
                />
              </label>
              <label>
                Categoria
                <select
                  value={draft.category}
                  onChange={(e) => set("category", e.target.value)}
                >
                  {Object.entries(categories).map(([id, label]) => (
                    <option key={id} value={id}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={draft.available}
                  onChange={(e) => set("available", e.target.checked)}
                />
                Disponível para novos envios
              </label>
            </div>
            <p className="field-help">
              O identificador é único, usa letras minúsculas, números e hífens e
              não pode ser alterado após o cadastro.
            </p>
            <label>
              Descrição
              <textarea
                required
                minLength={10}
                maxLength={500}
                rows={2}
                value={draft.summary}
                onChange={(e) => set("summary", e.target.value)}
              />
            </label>
          </fieldset>
          <fieldset className="integration-section">
            <legend>Conteúdo e personalização</legend>
            <label>
              Conteúdo
              <textarea
                className="template-content"
                required
                rows={8}
                minLength={10}
                maxLength={24000}
                value={draft.content}
                onChange={(e) => set("content", e.target.value)}
              />
            </label>
            <p className="field-help">
              Para inserir um campo no conteúdo, use sua chave entre chaves
              duplas. Exemplo: {"{{objetivo}}"}.
            </p>
            <div className="integration-fields-head">
              <h3>Campos pessoais ({draft.fields.length}/20)</h3>
              <button
                className="button"
                type="button"
                disabled={draft.fields.length >= 20}
                onClick={() =>
                  set("fields", [
                    ...draft.fields,
                    { key: "", label: "", required: false, max_length: 1200 },
                  ])
                }
              >
                <Plus size={16} />
                Adicionar campo
              </button>
            </div>
            {!draft.fields.length && (
              <p className="field-help">
                Nenhum campo pessoal. O template poderá ser aplicado com o
                conteúdo acima.
              </p>
            )}
            {draft.fields.map((field, index) => (
              <fieldset key={index} className="personal-field">
                <legend>Campo {index + 1}</legend>
                <div className="form-grid">
                  <label>
                    Chave do campo {index + 1}
                    <input
                      required
                      pattern="[A-Za-z][A-Za-z0-9_]{0,49}"
                      maxLength={50}
                      value={field.key}
                      onChange={(e) =>
                        changeField(index, { key: e.target.value })
                      }
                      placeholder="objetivo"
                    />
                  </label>
                  <label>
                    Nome exibido do campo {index + 1}
                    <input
                      required
                      maxLength={120}
                      value={field.label}
                      onChange={(e) =>
                        changeField(index, { label: e.target.value })
                      }
                      placeholder="Objetivo do projeto"
                    />
                  </label>
                  <label>
                    Limite de caracteres do campo {index + 1}
                    <input
                      type="number"
                      required
                      min={1}
                      max={12000}
                      value={field.max_length || ""}
                      onChange={(e) =>
                        changeField(index, {
                          max_length: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      checked={field.required}
                      onChange={(e) =>
                        changeField(index, { required: e.target.checked })
                      }
                    />
                    Campo {index + 1} obrigatório
                  </label>
                </div>
                <button
                  type="button"
                  className="button"
                  aria-label={`Remover campo ${index + 1}`}
                  onClick={() =>
                    set(
                      "fields",
                      draft.fields.filter((_, i) => i !== index),
                    )
                  }
                >
                  <Trash2 size={15} />
                  Remover campo
                </button>
              </fieldset>
            ))}
          </fieldset>
          {!draft.available && (
            <p className="notice">
              Este template ficará indisponível para novos envios. O conteúdo já
              enviado será preservado.
            </p>
          )}
          <label>
            Motivo da alteração
            <textarea
              required
              minLength={8}
              maxLength={300}
              rows={2}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
        </fieldset>
        <div className="integration-footer">
          <span className="field-help">
            {current.version
              ? `Será criada a versão ${current.version + 1}`
              : "Será criada a primeira versão"}
          </span>
          <button className="button primary" disabled={busy}>
            {busy
              ? "Salvando…"
              : current.version
                ? "Salvar nova versão"
                : "Cadastrar template"}
          </button>
        </div>
      </form>
    </IntegrationDialog>
  );
}
