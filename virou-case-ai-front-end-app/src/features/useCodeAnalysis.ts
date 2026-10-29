import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, command } from "../shared/api";
import type { Capabilities, Operation, Page } from "../shared/types";
import { inProgress } from "./useProjectOperations";
import { requestCodeCorrection } from "./codeCorrection";

type Context = {
  projectId: string;
  revisionId: string | null;
  source: "publication" | "preview" | "editor";
  publicationOperationId?: string;
  runtimeError?: string;
};

export function useCodeAnalysis(context: Context) {
  const { projectId, revisionId, publicationOperationId } = context;
  const publicationContext = context.source === "publication";
  const [operation, setOperation] = useState<Operation | null>(null);
  const [correction, setCorrection] = useState<Operation | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [syncError, setSyncError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [recovering, setRecovering] = useState(false);
  const [recoveringCorrection, setRecoveringCorrection] = useState(false);
  const [sending, setSending] = useState<"analysis" | "correction" | null>(
    null,
  );
  const [refreshKey, setRefreshKey] = useState(0);
  const busy = useRef(false);
  const epoch = useRef(0);
  const scope = `${projectId}:${revisionId}:${publicationContext ? "publication" : "workspace"}:${publicationOperationId ?? ""}`;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const known = useRef({ operation, correction });
  const uncertainAnalysis = useRef<string | null>(null);
  const recoveryAttempts = useRef(0);
  const mounted = useRef(true);
  const correctionRequest = useRef<AbortController | null>(null);
  const refresh = useCallback(() => setRefreshKey((value) => value + 1), []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      correctionRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    epoch.current += 1;
    setOperation(null);
    setCorrection(null);
    setError(null);
    setSyncError(null);
    setLoading(true);
    known.current = { operation: null, correction: null };
    uncertainAnalysis.current = null;
    recoveryAttempts.current = 0;
    setRecovering(false);
    setRecoveringCorrection(false);
    if (correctionRequest.current) {
      correctionRequest.current.abort();
      correctionRequest.current = null;
      busy.current = false;
      setSending(null);
    }
  }, [scope]);

  useEffect(() => {
    if (!revisionId) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let delay = 1500;
    function validate(value: Operation, type: string, expectedId: string) {
      if (
        value.id !== expectedId ||
        value.project_id !== projectId ||
        value.type !== type ||
        (type === "code_analysis" &&
          value.result_ref.revision_id !== revisionId)
      )
        throw new Error("A resposta recebida não pertence a este projeto.");
      return value;
    }
    async function load() {
      const requestEpoch = epoch.current;
      const current = () =>
        !controller.signal.aborted &&
        epoch.current === requestEpoch &&
        currentScope.current === scope;
      let repeat = false;
      try {
        let analysis = known.current.operation;
        if (analysis && uncertainAnalysis.current === null) {
          const expectedId = analysis.id;
          analysis = validate(
            await api<Operation>(
              `/operations/${encodeURIComponent(analysis.id)}`,
              {
                signal: AbortSignal.any([
                  controller.signal,
                  AbortSignal.timeout(12000),
                ]),
              },
            ),
            "code_analysis",
            expectedId,
          );
        } else {
          const page = await api<Page<Operation>>(
            `/operations?project_id=${encodeURIComponent(projectId)}&latest=true&kind=code_analysis`,
            {
              signal: AbortSignal.any([
                controller.signal,
                AbortSignal.timeout(12000),
              ]),
            },
          );
          analysis =
            page.items.find(
              (item) =>
                item.project_id === projectId &&
                item.type === "code_analysis" &&
                item.result_ref.revision_id === revisionId &&
                item.id !== uncertainAnalysis.current &&
                (!publicationContext ||
                  !!publicationOperationId ||
                  (item.result_ref.source === "publication" &&
                    !item.result_ref.publication_operation_id)) &&
                (!publicationOperationId ||
                  item.result_ref.publication_operation_id ===
                    publicationOperationId),
            ) ?? null;
        }
        if (!current()) return;
        let fix = known.current.correction;
        const correctionId =
          analysis?.result_ref.correction_operation_id ?? fix?.id;
        if (correctionId)
          fix = validate(
            await api<Operation>(
              `/operations/${encodeURIComponent(correctionId)}`,
              {
                signal: AbortSignal.any([
                  controller.signal,
                  AbortSignal.timeout(12000),
                ]),
              },
            ),
            "generation",
            correctionId,
          );
        if (!current()) return;
        known.current = { operation: analysis, correction: fix };
        setOperation(analysis);
        setCorrection(fix);
        setSyncError(null);
        if (analysis && uncertainAnalysis.current !== null) {
          uncertainAnalysis.current = null;
          recoveryAttempts.current = 0;
          setRecovering(false);
          setError(null);
        }
        repeat = !!(
          (analysis && inProgress(analysis)) ||
          (fix && inProgress(fix))
        );
        delay = 1500;
        if (!analysis && uncertainAnalysis.current !== null) {
          recoveryAttempts.current += 1;
          repeat = recoveryAttempts.current < 5;
          delay = Math.min(1500 * 2 ** recoveryAttempts.current, 15000);
          if (!repeat) {
            setRecovering(false);
            setError(
              new Error(
                "O recebimento da análise ainda não foi confirmado. Tente a mesma solicitação novamente para recuperar seu resultado com segurança.",
              ),
            );
          }
        }
      } catch (failure) {
        if (!current()) return;
        setSyncError(failure);
        repeat = true;
        if (uncertainAnalysis.current !== null) {
          recoveryAttempts.current += 1;
          repeat = recoveryAttempts.current < 5;
          if (!repeat) setRecovering(false);
        }
        if (
          failure instanceof ApiError &&
          [401, 403, 404].includes(failure.status)
        ) {
          known.current = { operation: null, correction: null };
          setOperation(null);
          setCorrection(null);
          setRecovering(false);
          repeat = false;
        }
        delay = Math.min(delay * 2, 15000);
      } finally {
        if (current()) setLoading(false);
      }
      if (current() && repeat) timer = setTimeout(load, delay);
    }
    void load();
    window.addEventListener("online", refresh);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener("online", refresh);
    };
  }, [
    projectId,
    revisionId,
    publicationOperationId,
    publicationContext,
    refreshKey,
    refresh,
  ]);

  async function policy() {
    const capabilities = await api<Capabilities>("/capabilities", {
      signal: AbortSignal.timeout(12000),
    });
    if (!(capabilities.code_analysis ?? capabilities.generation)?.enabled)
      throw new Error(
        "A análise de código está indisponível. Confira a configuração do agente de IA.",
      );
    return capabilities.policy_version;
  }

  async function analyze() {
    if (!revisionId || busy.current) return;
    busy.current = true;
    epoch.current += 1;
    setSending("analysis");
    setError(null);
    const startedScope = scope;
    const previousId =
      known.current.operation?.id ?? uncertainAnalysis.current ?? "";
    const intent =
      known.current.operation?.id ?? uncertainAnalysis.current ?? undefined;
    let dispatched = false;
    try {
      const policyVersion = await policy();
      if (!mounted.current || currentScope.current !== startedScope) return;
      dispatched = true;
      const accepted = await command<Operation>(
        `/projects/${encodeURIComponent(projectId)}/code-analyses`,
        {
          revision_id: revisionId,
          source: context.source,
          ...(publicationOperationId
            ? { publication_operation_id: publicationOperationId }
            : {}),
          ...(context.runtimeError
            ? { runtime_error: context.runtimeError.slice(0, 4000) }
            : {}),
          policy_version: policyVersion,
        },
        "POST",
        undefined,
        intent,
      );
      if (!mounted.current || currentScope.current !== startedScope) return;
      if (
        accepted.project_id !== projectId ||
        accepted.type !== "code_analysis" ||
        accepted.result_ref.revision_id !== revisionId
      )
        throw new Error(
          "Não foi possível confirmar a análise deste projeto. Atualize o acompanhamento.",
        );
      known.current = { operation: accepted, correction: null };
      epoch.current += 1;
      uncertainAnalysis.current = null;
      recoveryAttempts.current = 0;
      setRecovering(false);
      setOperation(accepted);
      setCorrection(null);
      refresh();
    } catch (failure) {
      if (mounted.current && currentScope.current === startedScope) {
        epoch.current += 1;
        setError(failure);
        if (dispatched && !(failure instanceof ApiError)) {
          uncertainAnalysis.current = previousId;
          recoveryAttempts.current = 0;
          setRecovering(true);
          setError(
            new Error(
              "A solicitação pode ter sido recebida. Estamos conferindo o resultado da análise.",
            ),
          );
          known.current = { operation: null, correction: null };
          setOperation(null);
          setCorrection(null);
        }
        refresh();
      }
    } finally {
      busy.current = false;
      if (mounted.current) setSending(null);
    }
  }

  async function requestCorrection(
    onAccepted?: (operation: Operation) => void,
  ) {
    const analysis = known.current.operation;
    if (!analysis || !revisionId || busy.current) return;
    busy.current = true;
    epoch.current += 1;
    setSending("correction");
    setError(null);
    const startedScope = scope;
    const controller = new AbortController();
    correctionRequest.current = controller;
    const currentRequest = () =>
      mounted.current &&
      currentScope.current === startedScope &&
      correctionRequest.current === controller;
    try {
      const accepted = await requestCodeCorrection({
        projectId,
        analysisId: analysis.id,
        revisionId,
        previousId: known.current.correction?.id,
        signal: controller.signal,
        onRecovering: () => {
          if (currentRequest()) setRecoveringCorrection(true);
        },
      });
      if (!currentRequest()) return;
      if (accepted.project_id !== projectId || accepted.type !== "generation")
        throw new Error(
          "Não foi possível confirmar a correção deste projeto. Atualize o acompanhamento.",
        );
      known.current = { operation: analysis, correction: accepted };
      epoch.current += 1;
      setCorrection(accepted);
      onAccepted?.(accepted);
      refresh();
    } catch (failure) {
      if (currentRequest()) {
        epoch.current += 1;
        setError(failure);
        refresh();
      }
    } finally {
      if (currentRequest()) {
        correctionRequest.current = null;
        busy.current = false;
        setSending(null);
        setRecoveringCorrection(false);
      }
    }
  }

  return {
    operation,
    correction,
    error,
    syncError,
    loading,
    recovering,
    recoveringCorrection,
    sending,
    analyze,
    requestCorrection,
    refresh,
  };
}
