export function migrationPathKey(path: string) {
  return path
    .replaceAll("\\", "/")
    .split("/")
    .filter((part) => part && part !== ".")
    .join("/")
    .normalize("NFC")
    .toLowerCase();
}

export function isMigrationPath(path: string) {
  const key = migrationPathKey(path);
  return key.startsWith("migrations/") && key.endsWith(".sql");
}

export function protectedMigration(path: string, base: Record<string, string>) {
  const key = migrationPathKey(path);
  return (
    isMigrationPath(path) &&
    Object.keys(base).some(
      (entry) => isMigrationPath(entry) && migrationPathKey(entry) === key,
    )
  );
}

export function nextMigrationPath(files: Record<string, string>) {
  const last = Object.keys(files).filter(isMigrationPath).sort().at(-1);
  if (!last) return "migrations/0001.sql";
  const stem = last.slice(0, -4);
  const match = stem.match(/^(.*?)(\d+)([^\d]*)$/);
  if (match) {
    const next = (BigInt(match[2]) + 1n)
      .toString()
      .padStart(match[2].length, "0");
    const candidate = `${match[1]}${next}${match[3]}.sql`;
    if (candidate > last && !(candidate in files)) return candidate;
  }
  return `${stem}_0001.sql`;
}
