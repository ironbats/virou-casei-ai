import { useEffect, useId, useRef, useState } from "react";
import { date } from "./api";
import { ErrorNotice } from "./admin-ui";
import { useAdminPolicy } from "./admin-policy-editor";
import { IntegrationDialog, useRemote } from "./integration-ui";
import "./integration-settings.css";

type CaptureSettings = {
  capture_enabled: boolean;
  retention_days: number;
  max_payload_bytes: number;
  max_operation_bytes: number;
  max_total_bytes: number;
  allowed_categories: string[];
};
type CapturePolicy = {
  version: number;
  settings: CaptureSettings;
  can_inspect_payloads: boolean;
  updated_at: string;
};
type PayloadAccess = {
  version: number;
  admins: { id: string; name: string; login: string; allowed: boolean }[];
  can_inspect_payloads: boolean;
};
const categories: Record<string, string> = {
  technical_metadata: "Metadados técnicos",
  schemas: "Estruturas e schemas",
  field_identifiers: "Identificadores de campos",
  usage: "Consumo e utilização",
};

export function ObservabilityPolicy({
  close,
  saved,
}: {
  close: () => void;
  saved: () => void;
}) {
  const capture = useRemote<CapturePolicy>("/observability/policy");
  const access = useRemote<PayloadAccess>("/observability/payload-access");
  const [tab, setTab] = useState(0);
  const [captureDirty, setCaptureDirty] = useState(false),
    [accessDirty, setAccessDirty] = useState(false);
  const [captureBusy, setCaptureBusy] = useState(false),
    [accessBusy, setAccessBusy] = useState(false);
  const identity = useId();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const busy = captureBusy || accessBusy;
  return (
    <IntegrationDialog
      title="Políticas de observabilidade"
      close={close}
      dirty={captureDirty || accessDirty}
      busy={busy}
    >
      <div
        className="trace-tabs"
        role="tablist"
        aria-label="Políticas de observabilidade"
      >
        {["Captura e retenção", "Acesso a payloads"].map((name, index) => (
          <button
            type="button"
            role="tab"
            id={`${identity}-tab-${index}`}
            aria-selected={tab === index}
            aria-controls={`${identity}-panel-${index}`}
            tabIndex={tab === index ? 0 : -1}
            disabled={busy}
            key={name}
            ref={(element) => {
              tabs.current[index] = element;
            }}
            onClick={() => setTab(index)}
            onKeyDown={(event) => {
              if (
                !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              )
                return;
              event.preventDefault();
              const next =
                event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
              setTab(next);
              tabs.current[next]?.focus();
            }}
          >
            {name}
            {(index === 0 ? captureDirty : accessDirty) && (
              <span className="policy-unsaved">Não salvo</span>
            )}
          </button>
        ))}
      </div>
      <section
        role="tabpanel"
        id={`${identity}-panel-0`}
        aria-labelledby={`${identity}-tab-0`}
        hidden={tab !== 0}
      >
        <ErrorNotice error={capture.error} />
        {!!capture.error && (
          <button className="button" onClick={capture.refresh}>
            Tentar carregar captura
          </button>
        )}
        {capture.loading && !capture.data && (
          <p role="status">Consultando política de captura…</p>
        )}
        {capture.data && (
          <CaptureEditor
            initial={capture.data}
            saved={saved}
            onDirty={setCaptureDirty}
            onBusy={setCaptureBusy}
          />
        )}
      </section>
      <section
        role="tabpanel"
        id={`${identity}-panel-1`}
        aria-labelledby={`${identity}-tab-1`}
        hidden={tab !== 1}
      >
        <ErrorNotice error={access.error} />
        {!!access.error && (
          <button className="button" onClick={access.refresh}>
            Tentar carregar acesso
          </button>
        )}
        {access.loading && !access.data && (
          <p role="status">Consultando permissões…</p>
        )}
        {access.data && (
          <AccessEditor
            initial={access.data}
            saved={saved}
            onDirty={setAccessDirty}
            onBusy={setAccessBusy}
          />
        )}
      </section>
    </IntegrationDialog>
  );
}

type EditorProps<T> = {
  initial: T;
  saved: () => void;
  onDirty: (dirty: boolean) => void;
  onBusy: (busy: boolean) => void;
};
function CaptureEditor({
  initial,
  saved,
  onDirty,
  onBusy,
}: EditorProps<CapturePolicy>) {
  const reasonId = useId();
  const form = useAdminPolicy(
    "/observability/policy",
    initial,
    (result) => result.settings,
    (settings, reason) => ({ settings, reason }),
    saved,
  );
  useEffect(() => onDirty(form.dirty), [form.dirty, onDirty]);
  useEffect(() => onBusy(form.busy), [form.busy, onBusy]);
  const set = <K extends keyof CaptureSettings>(
    key: K,
    value: CaptureSettings[K],
  ) => form.setValue((current) => ({ ...current, [key]: value }));
  const quotasValid =
    form.value.max_payload_bytes <= form.value.max_operation_bytes &&
    form.value.max_operation_bytes <= form.value.max_total_bytes;
  return (
    <form
      className="form policy-form"
      onSubmit={(event) => {
        if (!quotasValid) {
          event.preventDefault();
          return;
        }
        void form.submit(event);
      }}
    >
      <p className="small muted">
        Política v{form.current.version} · {date(form.current.updated_at)}. A
        captura protege conteúdo livre antes de persistir. Ativá-la não recupera
        payloads de execuções anteriores.
      </p>
      <ErrorNotice error={form.error} />
      {!!form.error && (
        <p className="small">
          Sua edição foi mantida. Em caso de conflito de versão, feche e reabra
          para consultar a política atual.
        </p>
      )}
      {form.message && (
        <p className="notice" role="status">
          {form.message}
        </p>
      )}
      <fieldset className="integration-section" disabled={form.busy}>
        <legend>Captura protegida</legend>
        <label className="policy-checkbox">
          <input
            type="checkbox"
            checked={form.value.capture_enabled}
            onChange={(event) => set("capture_enabled", event.target.checked)}
          />
          Habilitar captura de payloads protegidos
        </label>
        <p className="field-help">
          Request, Response, Resultado mapeado e Campos ficam separados.
          Conteúdo não permitido recebe um marcador de proteção; credenciais não
          são disponibilizadas.
        </p>
        <fieldset className="integration-section">
          <legend>Categorias técnicas permitidas</legend>
          {Object.entries(categories).map(([key, label]) => (
            <label className="policy-checkbox" key={key}>
              <input
                type="checkbox"
                checked={form.value.allowed_categories.includes(key)}
                onChange={(event) =>
                  set(
                    "allowed_categories",
                    Object.keys(categories).filter((name) =>
                      name === key
                        ? event.target.checked
                        : form.value.allowed_categories.includes(name),
                    ),
                  )
                }
              />
              {label}
            </label>
          ))}
        </fieldset>
      </fieldset>
      <fieldset className="integration-section" disabled={form.busy}>
        <legend>Retenção e limites de armazenamento</legend>
        <div className="form-grid">
          <label>
            Retenção (dias)
            <input
              type="number"
              min={1}
              max={30}
              required
              value={form.value.retention_days}
              onChange={(event) =>
                set("retention_days", Number(event.target.value))
              }
            />
            <span className="field-help">Entre 1 e 30 dias.</span>
          </label>
          <label>
            Limite por payload (bytes)
            <input
              type="number"
              min={1024}
              max={262144}
              required
              value={form.value.max_payload_bytes}
              onChange={(event) =>
                set("max_payload_bytes", Number(event.target.value))
              }
            />
            <span className="field-help">1.024 a 262.144 bytes.</span>
          </label>
          <label>
            Limite por operação (bytes)
            <input
              type="number"
              min={4096}
              max={2097152}
              required
              value={form.value.max_operation_bytes}
              onChange={(event) =>
                set("max_operation_bytes", Number(event.target.value))
              }
            />
            <span className="field-help">4.096 a 2.097.152 bytes.</span>
          </label>
          <label>
            Limite total (bytes)
            <input
              type="number"
              min={1048576}
              max={268435456}
              required
              value={form.value.max_total_bytes}
              onChange={(event) =>
                set("max_total_bytes", Number(event.target.value))
              }
            />
            <span className="field-help">1.048.576 a 268.435.456 bytes.</span>
          </label>
        </div>
        <p className="field-help">
          Os limites afetam somente a cópia de observabilidade. O payload
          operacional continua intacto. Capturas parciais ou expiradas são
          identificadas no detalhe.
        </p>
        {!quotasValid && (
          <p className="notice error">
            O limite por payload não pode superar o limite da operação, e o
            limite da operação não pode superar o total.
          </p>
        )}
      </fieldset>
      <div className="policy-reason">
        <label htmlFor={reasonId}>Motivo da alteração</label>
        <textarea
          id={reasonId}
          required
          minLength={8}
          maxLength={300}
          rows={3}
          value={form.reason}
          disabled={form.busy}
          onChange={(event) => form.setReason(event.target.value)}
        />
      </div>
      <div className="integration-footer">
        <span>Alterações versionadas e auditadas.</span>
        <button
          type="submit"
          className="button primary"
          disabled={
            form.busy ||
            !form.changed ||
            form.reason.trim().length < 8 ||
            !quotasValid
          }
        >
          {form.busy ? "Salvando…" : "Salvar política de captura"}
        </button>
      </div>
    </form>
  );
}

function AccessEditor({
  initial,
  saved,
  onDirty,
  onBusy,
}: EditorProps<PayloadAccess>) {
  const reasonId = useId();
  const form = useAdminPolicy(
    "/observability/payload-access",
    initial,
    (result) =>
      result.admins
        .filter((admin) => admin.allowed)
        .map((admin) => admin.id)
        .sort(),
    (admin_ids, reason) => ({ admin_ids, reason }),
    saved,
  );
  useEffect(() => onDirty(form.dirty), [form.dirty, onDirty]);
  useEffect(() => onBusy(form.busy), [form.busy, onBusy]);
  return (
    <form className="form policy-form" onSubmit={form.submit}>
      <p className="notice">
        Acesso a metadados não concede acesso a payloads. Cada leitura e cópia
        exige esta permissão específica e é auditada pelo backend.
      </p>
      <p className="small muted">
        Permissões v{form.current.version} · Seu acesso atual:{" "}
        {form.current.can_inspect_payloads ? "autorizado" : "não autorizado"}.
      </p>
      <ErrorNotice error={form.error} />
      {!!form.error && (
        <p className="small">
          Sua seleção foi mantida. Em caso de conflito de versão, feche e reabra
          para consultar as permissões atuais.
        </p>
      )}
      {form.message && (
        <p className="notice" role="status">
          {form.message}
        </p>
      )}
      <fieldset className="integration-section" disabled={form.busy}>
        <legend>Administradores autorizados a inspecionar e copiar</legend>
        {form.current.admins.map((admin) => (
          <label className="policy-checkbox" key={admin.id}>
            <input
              type="checkbox"
              checked={form.value.includes(admin.id)}
              onChange={(event) =>
                form.setValue((current) =>
                  event.target.checked
                    ? [...current, admin.id].sort()
                    : current.filter((id) => id !== admin.id),
                )
              }
            />
            <span>
              {admin.name}
              <small>{admin.login}</small>
            </span>
          </label>
        ))}
        {!form.current.admins.length && (
          <p>Nenhum administrador disponível para concessão.</p>
        )}
      </fieldset>
      <div className="policy-reason">
        <label htmlFor={reasonId}>Motivo da alteração de acesso</label>
        <textarea
          id={reasonId}
          required
          minLength={8}
          maxLength={300}
          rows={3}
          value={form.reason}
          disabled={form.busy}
          onChange={(event) => form.setReason(event.target.value)}
        />
      </div>
      <div className="integration-footer">
        <span>
          {form.value.length} administrador{form.value.length === 1 ? "" : "es"}{" "}
          selecionado{form.value.length === 1 ? "" : "s"}.
        </span>
        <button
          type="submit"
          className="button primary"
          disabled={form.busy || !form.changed || form.reason.trim().length < 8}
        >
          {form.busy ? "Salvando…" : "Salvar acesso a payloads"}
        </button>
      </div>
    </form>
  );
}
