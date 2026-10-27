import type { ProjectDraft } from "../shared/types";

export function mergeProjectDraft(
  local: ProjectDraft,
  saved: ProjectDraft,
  remote: ProjectDraft,
): ProjectDraft | null {
  const result = { ...remote };
  for (const key of [
    "base_revision_id",
    "files",
    "prompt",
    "provenance",
  ] as const) {
    const changedHere =
      JSON.stringify(local[key]) !== JSON.stringify(saved[key]);
    const changedThere =
      JSON.stringify(remote[key]) !== JSON.stringify(saved[key]);
    if (
      changedHere &&
      changedThere &&
      JSON.stringify(local[key]) !== JSON.stringify(remote[key])
    )
      return null;
    if (changedHere) Object.assign(result, { [key]: local[key] });
  }
  if (
    local.files !== null &&
    local.base_revision_id !== remote.base_revision_id
  )
    return null;
  return result;
}
