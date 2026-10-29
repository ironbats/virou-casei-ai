import { Check } from "lucide-react";

export function GuidedSteps({
  stageNumber,
  onBack,
  disabled = false,
}: {
  stageNumber: number;
  onBack?: (stage: "idea" | "templates") => void;
  disabled?: boolean;
}) {
  return (
    <>
      <div className="guided-progress-label">
        {stageNumber > 3 ? "Preparação concluída" : `Etapa ${stageNumber} de 3`}
      </div>
      <ol className="guided-steps" aria-label="Etapas de preparação">
        {["Ideia", "Templates", "Revisão"].map((label, index) => {
          const completed = index + 1 < stageNumber;
          return (
            <li
              key={label}
              aria-current={index + 1 === stageNumber ? "step" : undefined}
              className={
                completed
                  ? "completed"
                  : index + 1 === stageNumber
                    ? "current"
                    : ""
              }
            >
              <button
                type="button"
                disabled={disabled || !completed || !onBack}
                onClick={() => onBack?.(index === 0 ? "idea" : "templates")}
                aria-label={`${label}${completed ? ", etapa concluída. Voltar" : index + 1 === stageNumber ? ", etapa atual" : ", etapa futura"}`}
              >
                <span aria-hidden="true">
                  {completed ? <Check size={18} /> : index + 1}
                </span>
                {label}
              </button>
            </li>
          );
        })}
      </ol>
    </>
  );
}
