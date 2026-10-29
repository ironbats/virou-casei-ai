import { useEffect, useRef, useState } from "react";
import { api, ApiError, command } from "../shared/api";
import type { Draft, Project } from "../shared/types";
import {
  buildPayload,
  type AttachmentIssue,
  type BuildDraft,
} from "./guidedTypes";
import { useDraft } from "./useDraft";
import {
  preparationActive,
  type AcceptedField,
  type PreparationApplication,
  type TemplatePreparation,
} from "./templatePreparationTypes";
import {
  preparationError,
  useTemplatePreparation,
} from "./useTemplatePreparation";

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, canonical(item)]),
    );
  return value;
}

const fingerprint = (value: BuildDraft) =>
  JSON.stringify(canonical(buildPayload(value)));
const context = (value: BuildDraft) =>
  JSON.stringify(
    canonical({
      objective: value.objective,
      message_text: value.message_text,
    }),
  );

export function preparationFieldErrors(error: unknown): AttachmentIssue[] {
  if (
    !(error instanceof ApiError) ||
    !error.details ||
    typeof error.details !== "object" ||
    !("fields" in error.details) ||
    !Array.isArray(error.details.fields)
  )
    return [];
  return error.details.fields
    .filter(
      (value): value is AttachmentIssue =>
        !!value &&
        typeof value === "object" &&
        typeof value.attachment_id === "string" &&
        typeof value.code === "string" &&
        typeof value.message === "string" &&
        (value.field_id === undefined || typeof value.field_id === "string"),
    )
    .slice(0, 200);
}

export function useTemplatePreparationFlow({
  pid,
  draft,
  available,
  allowed,
}: {
  pid: string;
  draft: ReturnType<typeof useDraft<BuildDraft>>;
  available: boolean;
  allowed: boolean;
}) {
  const tracking = useTemplatePreparation(pid, !!draft.data && available);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [issues, setIssues] = useState<AttachmentIssue[]>([]);
  const [notice, setNotice] = useState("");
  const current = useRef(draft.data);
  const alive = useRef(true);
  const scope = useRef(pid);
  const mutation = useRef("");
  const previousContext = useRef<{ id: string; value: string } | null>(null);
  const uncertainIds = useRef<Set<string> | null>(null);
  current.current = draft.data;
  scope.current = pid;
  useEffect(() => {
    alive.current = true;
    previousContext.current = null;
    uncertainIds.current = null;
    mutation.current = "";
    setError(null);
    setIssues([]);
    setNotice("");
    setBusy("");
    return () => {
      alive.current = false;
    };
  }, [pid]);
  useEffect(() => {
    const preparation = tracking.current;
    if (!preparation || !current.current) return;
    if (previousContext.current?.id !== preparation.id)
      previousContext.current = {
        id: preparation.id,
        value: context(current.current),
      };
    if (
      uncertainIds.current &&
      (!uncertainIds.current.has(preparation.id) ||
        preparationActive(preparation))
    ) {
      uncertainIds.current = null;
      tracking.setAwaiting(false);
      setError(null);
    }
  }, [tracking.current]);
  const contextChanged =
    !!tracking.current &&
    !!draft.data &&
    previousContext.current?.id === tracking.current.id &&
    previousContext.current.value !== context(draft.data);
  const withinScope = () => alive.current && scope.current === pid;
  async function snapshot() {
    await draft.flush();
    const [saved, project] = await Promise.all([
      api<Draft<BuildDraft>>(`/projects/${pid}/build-draft`),
      api<Project>(`/projects/${pid}`),
    ]);
    if (!withinScope())
      throw new Error("A preparação continua vinculada ao projeto original.");
    if (
      !current.current ||
      fingerprint(saved.data) !== fingerprint(current.current)
    ) {
      await draft.reload();
      throw new Error(
        "O pedido mudou durante a consulta. Confira o rascunho atual antes de continuar.",
      );
    }
    if (project.id !== pid || !project.current_revision_id)
      throw new Error(
        "Salve uma revisão do projeto antes de preparar os templates.",
      );
    return { saved, project };
  }
  async function run<T>(name: string, work: () => Promise<T>): Promise<T> {
    if (mutation.current) throw new Error("Aguarde a solicitação atual.");
    if (name !== "cancel" && tracking.error)
      throw new Error("Atualize o status da preparação antes de continuar.");
    if (!allowed)
      throw new Error(
        "O pedido não está disponível para edição neste momento.",
      );
    mutation.current = name;
    setBusy(name);
    setError(null);
    setIssues([]);
    setNotice("");
    try {
      return await work();
    } catch (cause) {
      if (withinScope()) {
        setError(preparationError(cause));
        setIssues(preparationFieldErrors(cause));
        if (
          cause instanceof TypeError ||
          (cause instanceof DOMException && cause.name === "AbortError") ||
          (cause instanceof ApiError && cause.status >= 500)
        ) {
          uncertainIds.current = new Set(tracking.items.map((item) => item.id));
          tracking.setAwaiting(true);
        }
        await tracking.refresh().catch(() => {});
        if (name === "apply") await draft.reload().catch(() => {});
      }
      throw preparationError(cause);
    } finally {
      if (withinScope()) {
        mutation.current = "";
        setBusy("");
      }
    }
  }
  async function start() {
    if (preparationActive(tracking.current) || tracking.awaiting) return;
    return run("start", async () => {
      const { saved, project } = await snapshot();
      const selected = saved.data.attachments.filter(
        (attachment) => attachment.enabled,
      );
      if (!selected.length)
        throw new Error(
          "Selecione pelo menos um template para preparar com IA.",
        );
      const baseContext = context(saved.data);
      const result = await command<TemplatePreparation>(
        `/projects/${pid}/template-preparations`,
        {
          expected_draft_revision: saved.version,
          base_project_revision: project.current_revision_id,
          attachment_ids: selected.map((attachment) => attachment.template_id),
          expected_attachment_revisions: Object.fromEntries(
            selected.map((attachment) => [
              attachment.template_id,
              attachment.attachment_revision ?? 0,
            ]),
          ),
        },
      );
      if (!withinScope()) return;
      previousContext.current = { id: result.id, value: baseContext };
      tracking.accept(result);
    });
  }
  async function apply(fields: AcceptedField[]) {
    return run("apply", async () => {
      const preparation = tracking.current;
      if (!preparation?.can_apply || contextChanged || !fields.length)
        throw new Error(
          "Confira o contexto e as sugestões válidas antes de aceitar.",
        );
      const { saved, project } = await snapshot();
      if (project.current_revision_id !== preparation.project_revision)
        throw new Error(
          "A revisão salva do projeto mudou. Prepare novamente o contexto atual antes de continuar.",
        );
      const selected = saved.data.attachments.filter(
        (attachment) => attachment.enabled,
      );
      const result = await command<PreparationApplication>(
        `/projects/${pid}/template-preparations/${preparation.id}/apply`,
        {
          expected_state_version: preparation.state_version,
          expected_draft_revision: saved.version,
          expected_attachment_revisions: Object.fromEntries(
            selected.map((attachment) => [
              attachment.template_id,
              attachment.attachment_revision ?? 0,
            ]),
          ),
          accepted_fields: fields,
        },
      );
      if (!withinScope()) return result.draft.data;
      tracking.accept(result.preparation);
      await draft.reload();
      setIssues(result.conflicts);
      if (!result.applied_fields.length && result.conflicts.length)
        throw new Error(result.conflicts[0].message);
      setNotice(
        `${result.applied_fields.length} campo(s) confirmado(s). Confira o conteúdo final antes de construir.${result.conflicts.length ? " Alguns campos preservam edições ou pendências." : ""}`,
      );
      return result.draft.data;
    });
  }
  async function answer(answers: { question_id: string; value: string }[]) {
    return run("answers", async () => {
      const preparation = tracking.current;
      if (!preparation || contextChanged || !answers.length)
        throw new Error("Confira a preparação atual antes de responder.");
      const { saved, project } = await snapshot();
      if (project.current_revision_id !== preparation.project_revision)
        throw new Error(
          "A revisão salva do projeto mudou. Prepare novamente o contexto atual antes de continuar.",
        );
      const baseContext = context(saved.data);
      const result = await command<TemplatePreparation>(
        `/projects/${pid}/template-preparations/${preparation.id}/answers`,
        {
          expected_state_version: preparation.state_version,
          expected_draft_revision: saved.version,
          answers,
        },
      );
      if (!withinScope()) return;
      previousContext.current = { id: result.id, value: baseContext };
      tracking.accept(result);
    });
  }
  async function cancel() {
    return run("cancel", async () => {
      const preparation = tracking.current;
      if (!preparation || !preparationActive(preparation)) return;
      const result = await command<TemplatePreparation>(
        `/projects/${pid}/template-preparations/${preparation.id}/cancel`,
      );
      if (withinScope()) tracking.accept(result);
    });
  }
  async function refresh() {
    try {
      const values = await tracking.refresh();
      if (!withinScope()) return;
      setError(null);
      if (uncertainIds.current && !values.some(preparationActive)) {
        uncertainIds.current = null;
        tracking.setAwaiting(false);
      }
    } catch {}
  }
  return {
    ...tracking,
    busy,
    error: error || tracking.error,
    queryError: tracking.error,
    issues,
    notice,
    contextChanged,
    start,
    apply,
    answer,
    cancel,
    refresh,
  };
}
