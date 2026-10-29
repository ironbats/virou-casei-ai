import {
  buildPayload,
  type Attachment,
  type BuildDraft,
  type Template,
} from "./guidedTypes";
import { renderTemplate } from "./templateFields";

const equal = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);

export function mergeBuildAttachment(
  local: Attachment,
  saved: Attachment,
  remote: Attachment,
  template?: Template,
): Attachment | null {
  const next = { ...remote, fields: { ...remote.fields } };
  for (const key of ["source_version_id", "enabled"] as const) {
    const here = !equal(local[key], saved[key]);
    const there = !equal(remote[key], saved[key]);
    if (here && there && !equal(local[key], remote[key])) return null;
    if (here) Object.assign(next, { [key]: local[key] });
  }
  for (const key of new Set([
    ...Object.keys(saved.fields),
    ...Object.keys(local.fields),
    ...Object.keys(remote.fields),
  ])) {
    const here = !equal(local.fields[key], saved.fields[key]);
    const there = !equal(remote.fields[key], saved.fields[key]);
    if (here && there && !equal(local.fields[key], remote.fields[key])) {
      const automatic = remote.field_values?.[key];
      if (
        !automatic ||
        automatic.edited_by_user ||
        automatic.state !== "applied"
      )
        return null;
    }
    if (here) {
      if (key in local.fields) next.fields[key] = local.fields[key];
      else delete next.fields[key];
      if (next.field_values?.[key]) {
        next.field_values = { ...next.field_values };
        delete next.field_values[key];
      }
    }
  }
  const contentHere = local.edited_content !== saved.edited_content;
  const contentThere = remote.edited_content !== saved.edited_content;
  if (
    contentHere &&
    contentThere &&
    local.edited_content !== remote.edited_content
  ) {
    if (
      !template ||
      local.source_version_id !== remote.source_version_id ||
      remote.edited_content !== renderTemplate(template, remote.fields)
    )
      return null;
    if (local.edited_content === renderTemplate(template, local.fields)) {
      next.edited_content = renderTemplate(template, next.fields);
    } else {
      const changed = Object.keys(remote.fields).filter(
        (key) => !equal(remote.fields[key], saved.fields[key]),
      );
      if (
        !changed.length ||
        changed.some(
          (key) =>
            remote.field_values?.[key]?.state !== "applied" ||
            remote.field_values[key].edited_by_user,
        )
      )
        return null;
      next.edited_content = local.edited_content;
    }
  } else if (contentHere) next.edited_content = local.edited_content;
  return next;
}

export function mergeBuildDraft(
  local: BuildDraft,
  saved: BuildDraft,
  remote: BuildDraft,
  templates: Record<string, Template> = {},
): BuildDraft | null {
  const result = { ...remote, attachments: [...remote.attachments] };
  for (const key of [
    "stage",
    "objective",
    "message_text",
    "recommendation_id",
  ] as const) {
    const here = !equal(local[key], saved[key]);
    const there = !equal(remote[key], saved[key]);
    if (here && there && !equal(local[key], remote[key])) return null;
    if (here) Object.assign(result, { [key]: local[key] });
  }
  const plain = (attachment: Attachment) =>
    buildPayload({ ...saved, attachments: [attachment] }).attachments[0];
  for (const id of new Set(
    [...local.attachments, ...saved.attachments, ...remote.attachments].map(
      (attachment) => attachment.template_id,
    ),
  )) {
    const localItem = local.attachments.find(
      (attachment) => attachment.template_id === id,
    );
    const savedItem = saved.attachments.find(
      (attachment) => attachment.template_id === id,
    );
    const remoteItem = remote.attachments.find(
      (attachment) => attachment.template_id === id,
    );
    let next: Attachment | undefined | null;
    if (localItem && savedItem && remoteItem)
      next = mergeBuildAttachment(
        localItem,
        savedItem,
        remoteItem,
        templates[localItem.source_version_id],
      );
    else {
      const here = !equal(
        localItem && plain(localItem),
        savedItem && plain(savedItem),
      );
      const there = !equal(
        remoteItem && plain(remoteItem),
        savedItem && plain(savedItem),
      );
      if (
        here &&
        there &&
        !equal(localItem && plain(localItem), remoteItem && plain(remoteItem))
      )
        return null;
      next = here ? localItem : remoteItem;
    }
    if (next === null) return null;
    const index = result.attachments.findIndex(
      (attachment) => attachment.template_id === id,
    );
    if (index >= 0 && next) result.attachments[index] = next;
    else if (index >= 0) result.attachments.splice(index, 1);
    else if (next) result.attachments.push(next);
  }
  return result;
}
