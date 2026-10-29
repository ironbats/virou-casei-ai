import type { Operation } from "../src/shared/types";
import {
  migrationPending,
  publicationAttentionTitle,
} from "../src/features/publicationDiagnostic";
import { expect, test } from "@playwright/test";
import {
  isMigrationPath,
  nextMigrationPath,
  protectedMigration,
} from "../src/features/migrationFiles";

test("histórico protege aliases de migration salva mas permite novo rascunho", () => {
  const base = { "migrations/0001.sql": "CREATE TABLE users(id INTEGER);" };
  expect(protectedMigration("migrations/0001.sql", base)).toBe(true);
  expect(protectedMigration("MIGRATIONS\\0001.SQL", base)).toBe(true);
  expect(protectedMigration("./migrations//0001.sql", base)).toBe(true);
  expect(protectedMigration("migrations/0002.sql", base)).toBe(false);
  expect(isMigrationPath("src/query.sql")).toBe(false);
});

test("novo nome mantém sequência lexicográfica inclusive na virada de largura numérica", () => {
  for (const [files, expected] of [
    [{}, "migrations/0001.sql"],
    [{ "migrations/0001.sql": "" }, "migrations/0002.sql"],
    [{ "migrations/9999.sql": "" }, "migrations/9999_0001.sql"],
    [{ "migrations/9999_0001.sql": "" }, "migrations/9999_0002.sql"],
    [{ "migrations/z_last.sql": "" }, "migrations/z_last_0001.sql"],
  ] as [Record<string, string>, string][]) {
    const suggestion = nextMigrationPath(files);
    expect(suggestion).toBe(expected);
    for (const existing of Object.keys(files))
      expect(suggestion > existing).toBe(true);
  }
});

test("falha após recibo confirmado não atribui erro de upload à migration anterior", () => {
  const operation = {
    state: "reconciling",
    error_code: "PUBLICATION_UPLOAD_FAILED",
    result_ref: {
      publication_diagnostic: {
        version: 1,
        phase: "publication_uploading",
        effect_status: "confirmed",
        migration: { name: "migrations/0001.sql", sha256: "a".repeat(64) },
      },
      publication_failure: { stage: "publication_uploading" },
      publication_recovery: {
        state: "attention",
        last_error_code: "PUBLICATION_RECOVERY_EXHAUSTED",
      },
    },
  } as Operation;
  expect(migrationPending(operation)).toBe(false);
  expect(publicationAttentionTitle(operation)).toBe(
    "Resultado da publicação precisa de confirmação",
  );
});
