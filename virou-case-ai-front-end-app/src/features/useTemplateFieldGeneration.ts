import { useEffect, useRef, useState } from "react";
import { ApiError, command } from "../shared/api";
import type { Attachment, BuildDraft, FieldGeneration } from "./guidedTypes";
import { fieldIsEmpty } from "./templateFields";
import { uncertainRetry } from "./TemplateFillRetryDialog";
import type { useDraft } from "./useDraft";

type FieldIntent = {
  expected_draft_revision: number;
  attachment_ids: [string];
  target_field_id: string;
};

export type FieldGenerationStatus = {
  state: FieldGeneration["state"] | "saving" | "requesting" | "uncertain";
  message?: string;
  request_id?: string;
};

const fieldKey = (attachmentId: string, fieldId: string) =>
  JSON.stringify([attachmentId, fieldId]);
const storageKey = (pid: string) => `vira-intent:template-fields:${pid}`;
const activeStates = new Set(["queued", "filling", "reconciling"]);

function loadIntents(pid: string): Record<string, FieldIntent> {
  try {
    const saved: unknown = JSON.parse(
      sessionStorage.getItem(storageKey(pid)) || "{}",
    );
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(
      Object.entries(saved).filter(([key, value]) => {
        const intent = value as Partial<FieldIntent> | null;
        return (
          intent &&
          Number.isSafeInteger(intent.expected_draft_revision) &&
          Number(intent.expected_draft_revision) > 0 &&
          Array.isArray(intent.attachment_ids) &&
          intent.attachment_ids.length === 1 &&
          typeof intent.attachment_ids[0] === "string" &&
          typeof intent.target_field_id === "string" &&
          key === fieldKey(intent.attachment_ids[0], intent.target_field_id)
        );
      }),
    );
  } catch {
    return {};
  }
}

export function useTemplateFieldGeneration({
  pid,
  draft,
  current,
  blockedReason,
}: {
  pid: string;
  draft: ReturnType<typeof useDraft<BuildDraft>>;
  current: () => BuildDraft | null;
  blockedReason: string;
}) {
  const intents = useRef<Record<string, FieldIntent>>(loadIntents(pid));
  const [local, setLocal] = useState<Record<string, FieldGenerationStatus>>(
    () =>
      Object.fromEntries(
        Object.keys(intents.current).map((key) => [
          key,
          {
            state: "uncertain",
            message:
              "A resposta anterior não foi confirmada. Verifique a mesma tentativa antes de gerar novamente.",
          },
        ]),
      ),
  );
  const submitting = useRef("");
  const alive = useRef(true);
  const scope = useRef(pid);
  scope.current = pid;
  const withinScope = () => alive.current && scope.current === pid;

  function persist() {
    try {
      if (Object.keys(intents.current).length)
        sessionStorage.setItem(
          storageKey(pid),
          JSON.stringify(intents.current),
        );
      else sessionStorage.removeItem(storageKey(pid));
    } catch {}
  }

  useEffect(() => {
    alive.current = true;
    intents.current = loadIntents(pid);
    submitting.current = "";
    setLocal(
      Object.fromEntries(
        Object.keys(intents.current).map((key) => [
          key,
          {
            state: "uncertain",
            message:
              "A resposta anterior não foi confirmada. Verifique a mesma tentativa antes de gerar novamente.",
          },
        ]),
      ),
    );
    return () => {
      alive.current = false;
    };
  }, [pid]);

  useEffect(() => {
    const completed = Object.entries(intents.current)
      .filter(([key, intent]) => {
        if (key === submitting.current) return false;
        const attachment = draft.data?.attachments.find(
          (item) => item.template_id === intent.attachment_ids[0],
        );
        return (
          attachment &&
          !fieldIsEmpty(attachment.fields[intent.target_field_id]) &&
          !activeStates.has(
            attachment.field_generations?.[intent.target_field_id]?.state || "",
          )
        );
      })
      .map(([key]) => key);
    if (!completed.length) return;
    for (const key of completed) delete intents.current[key];
    persist();
    setLocal((old) => {
      const next = { ...old };
      for (const key of completed) delete next[key];
      return next;
    });
  }, [draft.data]);

  useEffect(() => {
    setLocal((old) => {
      let changed = false;
      const next = { ...old };
      for (const attachment of draft.data?.attachments || []) {
        for (const [fieldId, generation] of Object.entries(
          attachment.field_generations || {},
        )) {
          const key = fieldKey(attachment.template_id, fieldId);
          if (
            next[key]?.state === "queued" &&
            generation &&
            !intents.current[key]
          ) {
            delete next[key];
            changed = true;
          }
        }
      }
      return changed ? next : old;
    });
  }, [draft.data]);

  const serverActive = !!draft.data?.attachments.some(
    (attachment) =>
      (attachment.enabled && activeStates.has(attachment.fill_state || "")) ||
      Object.values(attachment.field_generations || {}).some((item) =>
        activeStates.has(item.state),
      ),
  );
  const localPending = Object.entries(local).filter(([, value]) =>
    ["saving", "requesting", "queued", "uncertain"].includes(value.state),
  );
  const localActive = localPending[0]?.[0];
  const reviewPending = !!draft.data?.attachments.some(
    (attachment) =>
      attachment.enabled &&
      (activeStates.has(attachment.fill_state || "") ||
        Object.values(attachment.field_generations || {}).some((item) =>
          activeStates.has(item.state),
        ) ||
        localPending.some(
          ([key]) => JSON.parse(key)[0] === attachment.template_id,
        )),
  );

  function status(attachment: Attachment, fieldId: string) {
    const own = local[fieldKey(attachment.template_id, fieldId)];
    const remote = attachment.field_generations?.[fieldId];
    if (own && ["saving", "requesting"].includes(own.state)) return own;
    if (remote && activeStates.has(remote.state)) return remote;
    return own || remote;
  }

  function reason(attachment: Attachment, fieldId: string) {
    if (blockedReason) return blockedReason;
    if (!attachment.enabled)
      return "Selecione este template para gerar um campo.";
    if (serverActive)
      return "Aguarde a geração em andamento. Você pode continuar editando os campos.";
    if (
      localActive &&
      localActive !== fieldKey(attachment.template_id, fieldId)
    )
      return "Conclua ou verifique a tentativa atual antes de gerar outro campo.";
    return "";
  }

  async function generate(
    attachment: Attachment,
    fieldId: string,
    saveEditor?: () => Promise<void>,
  ) {
    const key = fieldKey(attachment.template_id, fieldId);
    if (submitting.current || reason(attachment, fieldId)) return;
    submitting.current = key;
    setLocal((old) => ({ ...old, [key]: { state: "saving" } }));
    try {
      let intent = intents.current[key];
      if (!intent) {
        await saveEditor?.();
        await draft.flush();
        if (!withinScope()) return;
        const latest = current()?.attachments.find(
          (item) => item.template_id === attachment.template_id,
        );
        if (
          !latest?.enabled ||
          latest.source_version_id !== attachment.source_version_id
        )
          throw new Error(
            "A seleção deste template mudou. Confira o pedido atual antes de gerar.",
          );
        if (!fieldIsEmpty(latest.fields[fieldId]))
          throw new Error(
            "Este campo já foi preenchido. O valor atual foi preservado.",
          );
        intent = {
          expected_draft_revision: draft.version(),
          attachment_ids: [attachment.template_id],
          target_field_id: fieldId,
        };
        intents.current[key] = intent;
        persist();
      }
      if (!withinScope()) return;
      setLocal((old) => ({ ...old, [key]: { state: "requesting" } }));
      await command(`/projects/${pid}/build-fill`, intent);
      if (!withinScope()) return;
      delete intents.current[key];
      persist();
      setLocal((old) => ({ ...old, [key]: { state: "queued" } }));
      await draft.reload().catch(() => {});
    } catch (cause) {
      if (!withinScope()) return;
      const uncertain = !!intents.current[key] && uncertainRetry(cause);
      if (!uncertain) {
        delete intents.current[key];
        persist();
      }
      setLocal((old) => ({
        ...old,
        [key]: {
          state: uncertain ? "uncertain" : "failed",
          message: uncertain
            ? "Não foi possível confirmar a resposta. Verifique a mesma tentativa para evitar uma solicitação duplicada."
            : cause instanceof Error
              ? cause.message
              : "Não foi possível gerar este campo. Tente novamente ou preencha manualmente.",
          request_id: cause instanceof ApiError ? cause.requestId : undefined,
        },
      }));
      await draft.reload().catch(() => {});
    } finally {
      if (withinScope()) submitting.current = "";
    }
  }

  return {
    generate,
    status,
    reason,
    pending: serverActive || !!localActive,
    reviewPending,
  };
}
