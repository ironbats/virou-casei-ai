export function GuidedSteps({ stageNumber }: { stageNumber: number }) {
  if (!stageNumber) return null;
  return (
    <>
      <div className="guided-progress-label">Etapa {stageNumber} de 3</div>
      <ol className="guided-steps" aria-label="Etapas de preparação">
        {["Ideia", "Templates", "Revisão"].map((label, index) => (
          <li
            key={label}
            aria-current={index + 1 === stageNumber ? "step" : undefined}
            className={index + 1 <= stageNumber ? "current" : ""}
          >
            <span>{index + 1}</span>
            {label}
          </li>
        ))}
      </ol>
    </>
  );
}
