import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, command } from "../shared/api";
import type { Operation } from "../shared/types";
import {
  recoveryRequestActive,
  recoverySnapshot,
  type RecoverySnapshot,
} from "./publicationRecovery";

export function usePublicationRecovery(
  operation: Operation,
  projectId: string,
  refreshKey: number,
  onUpdated: () => void,
) {
  const [data, setData] = useState<RecoverySnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [tick, setTick] = useState(0);
  const busy = useRef(false);
  const epoch = useRef(0);
  const mounted = useRef(true);
  const requestController = useRef<AbortController | null>(null);
  const update = useRef(onUpdated);
  const scope = useRef(`${projectId}:${operation.id}`);
  const completedRequest = useRef<string | null>(null);
  scope.current = `${projectId}:${operation.id}`;
  const uncertainRequest = useRef<{
    requestedAt: string | null;
    requestId: string | null;
  } | null>(null);
  const refresh = useCallback(() => setTick((value) => value + 1), []);
  const path = `/operations/${encodeURIComponent(operation.id)}/publication-recovery`;
  update.current = onUpdated;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestController.current?.abort();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    let active = true;
    const requestEpoch = epoch.current;
    setLoading(true);
    setLoadError(null);
    void api<unknown>(path, { signal: controller.signal })
      .then((response) => {
        if (!active || busy.current || requestEpoch !== epoch.current) return;
        const snapshot = recoverySnapshot(
          response,
          operation.id,
          projectId,
          operation.result_ref.revision_id,
        );
        if (!snapshot)
          throw new ApiError(
            "RECOVERY_RESPONSE_INVALID",
            "O diagnóstico de recuperação não pôde ser validado. Atualize o acompanhamento antes de continuar.",
            502,
          );
        setData(snapshot);
        if (
          snapshot.request &&
          ["resolved", "attention"].includes(snapshot.request.state)
        ) {
          const completion = `${snapshot.request.id}:${snapshot.request.state}`;
          if (completedRequest.current !== completion) {
            completedRequest.current = completion;
            update.current();
          }
        }
        if (
          uncertainRequest.current &&
          ((snapshot.request?.id &&
            snapshot.request.id !== uncertainRequest.current.requestId) ||
            (snapshot.requested_at &&
              snapshot.requested_at !== uncertainRequest.current.requestedAt))
        ) {
          uncertainRequest.current = null;
          setActionError(null);
          setFeedback(
            snapshot.resolution
              ? "A solicitação de recuperação foi confirmada. Acompanhe o resultado desta tentativa."
              : "A solicitação de conferência foi confirmada. Acompanhe o resultado desta tentativa.",
          );
        }
      })
      .catch((error) => {
        if (!active || busy.current || requestEpoch !== epoch.current) return;
        if (error instanceof ApiError && [401, 403, 404].includes(error.status))
          setData(null);
        setLoadError(
          error instanceof ApiError
            ? error
            : new Error(
                "Não foi possível consultar a recuperação. O último diagnóstico disponível foi preservado.",
              ),
        );
      })
      .finally(() => {
        clearTimeout(timeout);
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timeout);
    };
  }, [
    path,
    operation.id,
    projectId,
    operation.result_ref.revision_id,
    operation.updated_at,
    operation.state,
    tick,
    refreshKey,
  ]);
  const cooldownUntil = data?.resolution
    ? data.resolution.cooldown_until
    : data?.cooldown_until;
  const cooldownCode = data?.resolution
    ? data.resolution.blocked_code
    : data?.blocked_code;
  const canRequest = data?.resolution
    ? data.capabilities?.can_check_and_resolve
    : data?.can_check;
  useEffect(() => {
    if (!cooldownUntil || canRequest || cooldownCode !== "RECOVERY_COOLDOWN")
      return;
    const wait = Date.parse(cooldownUntil) - Date.now();
    if (!Number.isFinite(wait) || wait <= 0) return;
    const timer = setTimeout(refresh, Math.min(wait + 250, 2147483647));
    return () => clearTimeout(timer);
  }, [cooldownUntil, canRequest, cooldownCode, refresh]);
  useEffect(() => {
    if (!recoveryRequestActive(data?.request) || loading || sending) return;
    const timer = setTimeout(refresh, loadError ? 10000 : 2000);
    return () => clearTimeout(timer);
  }, [
    data?.request?.id,
    data?.request?.state,
    loading,
    sending,
    loadError,
    tick,
    refresh,
  ]);
  async function execute(resolve: boolean) {
    if (
      !data ||
      (resolve
        ? !data.capabilities?.can_check_and_resolve || !data.state_version
        : !data.can_check) ||
      recoveryRequestActive(data.request) ||
      loading ||
      loadError ||
      busy.current ||
      operation.state !== "reconciling" ||
      data.operation_state !== "reconciling"
    )
      return;
    busy.current = true;
    epoch.current += 1;
    setSending(true);
    setActionError(null);
    setFeedback("");
    const controller = new AbortController();
    requestController.current = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    const before = data.requested_at;
    const beforeRequest = data.request?.id ?? null;
    const requestScope = scope.current;
    try {
      const response = await command<unknown>(
        path + (resolve ? "/resolve" : "/check"),
        resolve
          ? {
              expected_state_version: data.state_version,
              action: "check_and_close_if_no_effect",
              consent: "verify_and_close_failed_attempt_when_proven_safe",
            }
          : {},
        "POST",
        undefined,
        resolve
          ? data.state_version
          : `${data.requested_at ?? ""}:${data.last_checked_at ?? ""}`,
        controller.signal,
      );
      if (!mounted.current || scope.current !== requestScope) return;
      const snapshot = recoverySnapshot(
        response,
        operation.id,
        projectId,
        operation.result_ref.revision_id,
      );
      if (!snapshot)
        throw new ApiError(
          "RECOVERY_RESPONSE_INVALID",
          resolve
            ? "A resposta da recuperação não pôde ser validada. Consulte o acompanhamento antes de repetir a ação."
            : "A resposta da conferência não pôde ser validada. Consulte o acompanhamento antes de repetir a ação.",
          502,
        );
      uncertainRequest.current = null;
      epoch.current += 1;
      setData(snapshot);
      setFeedback(
        resolve
          ? "Recuperação solicitada. A plataforma verificará as evidências e encerrará a tentativa somente se for seguro."
          : "Conferência solicitada para esta tentativa. O resultado será atualizado no acompanhamento.",
      );
      refresh();
      update.current();
    } catch (error) {
      if (!mounted.current || scope.current !== requestScope) return;
      epoch.current += 1;
      uncertainRequest.current = {
        requestedAt: before,
        requestId: beforeRequest,
      };
      setActionError(
        error instanceof ApiError
          ? error
          : new Error(
              "Não foi possível confirmar a solicitação. A plataforma consultará o estado antes de permitir outra tentativa.",
            ),
      );
      refresh();
      update.current();
    } finally {
      clearTimeout(timeout);
      requestController.current = null;
      busy.current = false;
      if (mounted.current && scope.current === requestScope) setSending(false);
    }
  }
  return {
    data,
    loading,
    loadError,
    actionError,
    sending,
    feedback,
    refresh,
    check: () => execute(false),
    resolve: () => execute(true),
  };
}
