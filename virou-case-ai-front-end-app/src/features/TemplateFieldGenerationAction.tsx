import { useId } from "react";
import { LoaderCircle, Sparkles } from "lucide-react";
import type { TemplateField } from "./guidedTypes";
import type { FieldGenerationStatus } from "./useTemplateFieldGeneration";

export function TemplateFieldGenerationAction({
  field,
  empty,
  status,
  blockedReason,
  onGenerate,
}: {
  field: TemplateField;
  empty: boolean;
  status?: FieldGenerationStatus;
  blockedReason: string;
  onGenerate: () => void;
}) {
  const consumptionId = useId();
  if (field.sensitivity === "secret") return null;
  const working =
    !!status &&
    ["saving", "requesting", "queued", "filling", "reconciling"].includes(
      status.state,
    );
  const uncertain = status?.state === "uncertain";
  const failed =
    !!status && ["failed", "stale", "cancelled"].includes(status.state);
  const requestId =
    status?.request_id && /^[\w.-]{1,80}$/.test(status.request_id)
      ? status.request_id
      : "";
  const message = working
    ? status?.state === "saving"
      ? "Salvando o pedido antes de gerar…"
      : status?.state === "reconciling"
        ? "Conferindo o resultado desta geração…"
        : "Gerando este campo com o pedido atual…"
    : status?.message ||
      (failed
        ? "Não foi possível gerar este campo. Tente novamente ou preencha manualmente."
        : "");
  if (!empty && !working && !failed && !uncertain) return null;
  return (
    <div className="template-field-generation">
      {(empty || working) && (
        <button
          type="button"
          className="button template-generate-button"
          disabled={working || !!blockedReason}
          title={blockedReason || undefined}
          aria-describedby={empty && !working ? consumptionId : undefined}
          aria-label={`${working ? "Gerando" : uncertain ? "Verificar tentativa" : "Gerar"} · ${field.label}`}
          onClick={onGenerate}
        >
          {working ? (
            <LoaderCircle
              size={15}
              className="preparation-spinner"
              aria-hidden="true"
            />
          ) : (
            <Sparkles size={15} aria-hidden="true" />
          )}
          {working ? "Gerando…" : uncertain ? "Verificar tentativa" : "Gerar"}
        </button>
      )}
      {empty && !working && (
        <span
          id={consumptionId}
          className="small muted template-generation-cost"
        >
          Pode consumir uso de IA.
        </span>
      )}
      {message && (
        <p
          className={`small template-generation-message ${failed || uncertain ? "preparation-field-error" : "muted"}`}
          role={failed || uncertain ? "alert" : "status"}
        >
          {message}
          {requestId && <span> Referência para suporte: {requestId}</span>}
        </p>
      )}
    </div>
  );
}
