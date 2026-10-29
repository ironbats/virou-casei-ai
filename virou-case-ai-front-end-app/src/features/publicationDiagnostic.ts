import type { Operation } from "../shared/types";

const phases: Record<string, string> = {
  prepared: "Plano persistido; envio ainda não iniciado",
  sql_sending: "Envio da migration ao banco",
  sql_confirmed: "SQL confirmado; recibo ainda não confirmado",
  receipt_sending: "Registro do recibo da migration",
  receipt_confirmed: "Recibo da migration confirmado",
  sql_rejected: "Recusa da migration; ausência de efeitos confirmada",
  publication_building: "Preparação da aplicação",
  publication_provisioning: "Preparação dos recursos",
  publication_migrating: "Aplicação das migrations",
  publication_uploading: "Envio à hospedagem",
  publication_routing: "Configuração do endereço",
  publication_verifying: "Verificação da aplicação publicada",
};

export function publicationDiagnostic(operation: Operation) {
  const value = operation.result_ref.publication_diagnostic;
  return value?.version === 1 ? value : undefined;
}

export function publicationPhase(operation: Operation, fallbackPhase?: string) {
  const phase = publicationDiagnostic(operation)?.phase ?? fallbackPhase;
  const stage = operation.result_ref.publication_failure?.stage;
  return (
    (phase && phases[phase]) ||
    (stage && phases[stage]) ||
    "Não identificada nesta tentativa"
  );
}

export function migrationPending(operation: Operation) {
  const diagnostic = publicationDiagnostic(operation);
  return (
    (!!diagnostic?.migration &&
      [
        "prepared",
        "sql_sending",
        "sql_confirmed",
        "receipt_sending",
        "sql_rejected",
        "publication_migrating",
      ].includes(diagnostic.phase || "")) ||
    !!operation.result_ref.publication_failure?.migration ||
    operation.error_code?.startsWith("MIGRATION_") ||
    operation.result_ref.publication_recovery?.last_error_code?.startsWith(
      "MIGRATION_",
    ) ||
    operation.result_ref.publication_failure?.stage === "publication_migrating"
  );
}

export function publicationAttentionTitle(operation: Operation) {
  if (migrationPending(operation)) {
    const recoveryCode =
      operation.result_ref.publication_recovery?.last_error_code;
    if (recoveryCode === "MIGRATION_CHECKSUM_CONFLICT")
      return "Histórico de migrations divergente";
    if (
      publicationDiagnostic(operation)?.effect_status === "confirmed" &&
      publicationDiagnostic(operation)?.phase !== "receipt_confirmed"
    )
      return "Migration aplicada; recibo precisa de confirmação";
    return "Resultado da migration precisa de confirmação";
  }
  return "Resultado da publicação precisa de confirmação";
}

export function publicationBlockedReason(operation: Operation) {
  if (operation.state !== "reconciling")
    return "Já existe uma publicação em andamento. Aguarde o resultado para iniciar outro envio.";
  const recovery = operation.result_ref.publication_recovery;
  if (migrationPending(operation))
    return "Um novo envio está bloqueado porque o resultado da migration ainda não foi confirmado. Repetir a execução poderia duplicar efeitos no banco.";
  return recovery?.state === "attention"
    ? "Um novo envio está bloqueado até a confirmação do resultado externo desta tentativa. Consulte o impedimento atual e o próximo passo."
    : "O resultado desta publicação está em conferência. Aguarde a confirmação antes de iniciar outro envio.";
}

export function publicationRecoveryAction(operation: Operation) {
  const recovery = operation.result_ref.publication_recovery;
  if (recovery?.required_action) return recovery.required_action;
  if (operation.state === "reconciling" && recovery?.state === "attention")
    return migrationPending(operation)
      ? "Solicite ao responsável pela operação a conferência do recibo e do resultado desta migration no banco. A ausência de recibo não prova que o SQL deixou de executar."
      : "Solicite ao responsável pela operação a conferência do resultado externo usando o identificador desta tentativa. O diagnóstico disponível não permite confirmar o resultado automaticamente.";
  if (operation.state === "reconciling")
    return "Acompanhe a próxima conferência desta mesma operação. Atualizar status apenas consulta o resultado registrado.";
  if (
    ["CONFIGURATION_INVALID", "CONFIGURATION_PENDING"].includes(
      operation.error_code || "",
    )
  )
    return "Peça ao responsável pela operação que confira o diagnóstico da conexão configurada. Esta tela não solicita credenciais nem altera a configuração.";
  if (
    ["QUOTA_EXCEEDED", "DEPENDENCY_UNAVAILABLE"].includes(
      operation.error_code || "",
    )
  )
    return "Aguarde a disponibilidade do provedor e consulte o status antes de tentar outra publicação.";
  if (operation.error_code === "MIGRATION_SQL_REJECTED")
    return "Revise o diagnóstico da migration e prepare uma correção compatível com o histórico protegido. O novo envio depende da revisão salva e da disponibilidade da publicação.";
  return "Revise o diagnóstico desta tentativa e salve as correções necessárias antes de publicar novamente.";
}
