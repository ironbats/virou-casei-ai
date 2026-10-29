import { api, ApiError, command } from "../shared/api";
import type { Capabilities, Operation } from "../shared/types";

type CorrectionRequest = {
  projectId: string;
  analysisId: string;
  revisionId: string;
  previousId?: string;
  signal?: AbortSignal;
  onRecovering?: () => void;
};

export function correctionConversationPath(
  projectId: string,
  operationId: string,
) {
  return `/app/projetos/${encodeURIComponent(projectId)}?conversation=${encodeURIComponent(operationId)}`;
}

function validate(
  operation: Operation,
  request: CorrectionRequest,
  expectedId?: string,
) {
  if (
    typeof operation.id !== "string" ||
    !operation.id ||
    operation.project_id !== request.projectId ||
    operation.type !== "generation" ||
    (expectedId && operation.id !== expectedId) ||
    (operation.analysis_id && operation.analysis_id !== request.analysisId) ||
    (operation.analysis_revision_id &&
      operation.analysis_revision_id !== request.revisionId)
  )
    throw new Error(
      "A correção recebida não pertence a este projeto e diagnóstico.",
    );
  return operation;
}

export async function requestCodeCorrection(request: CorrectionRequest) {
  const signals = () =>
    request.signal
      ? AbortSignal.any([request.signal, AbortSignal.timeout(12000)])
      : AbortSignal.timeout(12000);
  const capabilities = await api<Capabilities>("/capabilities", {
    signal: signals(),
  });
  if (!(capabilities.code_analysis ?? capabilities.generation)?.enabled)
    throw new Error(
      "A correção de código está indisponível. Confira a configuração do agente de IA.",
    );
  request.signal?.throwIfAborted();
  try {
    const operation = await command<Operation>(
      `/projects/${encodeURIComponent(request.projectId)}/code-analyses/${encodeURIComponent(request.analysisId)}/correction`,
      {
        base_project_revision: request.revisionId,
        policy_version: capabilities.policy_version,
      },
      "POST",
      undefined,
      request.previousId,
      request.signal
        ? AbortSignal.any([request.signal, AbortSignal.timeout(20000)])
        : AbortSignal.timeout(20000),
    );
    return validate(operation, request);
  } catch (failure) {
    if (failure instanceof ApiError && failure.status < 500) throw failure;
    request.signal?.throwIfAborted();
    request.onRecovering?.();
    for (let attempt = 0; attempt < 5; attempt++) {
      if (attempt)
        await new Promise((resolve) =>
          setTimeout(resolve, 500 * 2 ** (attempt - 1)),
        );
      request.signal?.throwIfAborted();
      try {
        const analysis = await api<Operation>(
          `/operations/${encodeURIComponent(request.analysisId)}`,
          {
            signal: signals(),
          },
        );
        if (
          analysis.id !== request.analysisId ||
          analysis.project_id !== request.projectId ||
          analysis.type !== "code_analysis" ||
          analysis.result_ref.revision_id !== request.revisionId
        )
          throw new Error(
            "O diagnóstico recuperado não pertence à revisão solicitada.",
          );
        const identity = analysis.result_ref.correction_operation_id;
        if (identity && identity !== request.previousId) {
          const operation = await api<Operation>(
            `/operations/${encodeURIComponent(identity)}`,
            {
              signal: signals(),
            },
          );
          return validate(operation, request, identity);
        }
      } catch (recoveryFailure) {
        if (
          recoveryFailure instanceof ApiError &&
          [401, 403, 404].includes(recoveryFailure.status)
        )
          throw recoveryFailure;
      }
    }
    throw new Error(
      "O recebimento da correção ainda não foi confirmado. Tente a mesma solicitação novamente para recuperar seu resultado com segurança.",
    );
  }
}
