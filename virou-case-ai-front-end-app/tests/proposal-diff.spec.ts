import { expect, test } from "@playwright/test";
import { compareLines, visibleLines } from "../src/features/proposalDiff";

const fixtures: [string, string][] = [
  ["", ""],
  ["", "const modal = true;\n"],
  ["obsolete\n", ""],
  ["alpha\nbeta\ngamma\n", "alpha\nnew\ngamma\n"],
  ["same\nsame\nlast\n", "same\nlast\nsame\n"],
  ["linha\r\nsegunda\r\n", "linha\nsegunda\n"],
  ["sem quebra", "sem quebra\n"],
  ["a\n\n", "\na\n"],
  ["<script>alert(1)</script>\n", "<img src=x onerror=alert(2)>\n"],
  ["ação 😀\nresultado\n", "ação 😀\nresultado correto\n"],
];

test("diff conserva todos os bytes e números de linha nas duas revisões", () => {
  let seed = 41;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return seed;
  };
  const cases = [...fixtures];
  for (let sample = 0; sample < 80; sample++) {
    const before = Array.from(
      { length: random() % 35 },
      () => `${random() % 9}\n`,
    );
    const after = [...before];
    for (let edit = 0; edit < 12; edit++)
      after.splice(
        random() % (after.length + 1),
        random() % 3,
        `${random() % 9}\n`,
      );
    cases.push([before.join(""), after.join("")]);
  }
  for (const [before, after] of cases) {
    const diff = compareLines(before, after);
    const old = diff.lines.filter((line) => line.kind !== "added"),
      next = diff.lines.filter((line) => line.kind !== "removed");
    expect(old.map((line) => line.text + line.newline).join("")).toBe(before);
    expect(next.map((line) => line.text + line.newline).join("")).toBe(after);
    expect(old.map((line) => line.oldLine)).toEqual(
      old.map((_, index) => index + 1),
    );
    expect(next.map((line) => line.newLine)).toEqual(
      next.map((_, index) => index + 1),
    );
  }
});

test("diff localiza alteração pequena sem marcar arquivo inteiro", () => {
  const before = Array.from({ length: 1500 }, (_, index) => `linha ${index}\n`);
  const after = [...before];
  after[500] = "nova linha\n";
  after.splice(1100, 0, "mais uma\n");
  const diff = compareLines(before.join(""), after.join(""));
  expect(diff.added).toBe(2);
  expect(diff.removed).toBe(1);
  expect(diff.simplified).toBe(false);
  const compact = visibleLines(diff.lines, false);
  expect(compact.filter((line) => "hidden" in line).length).toBe(3);
  expect(visibleLines(diff.lines, true)).toHaveLength(diff.lines.length);
});

test("diff extenso limita cálculo e informa comparação por blocos sem perder conteúdo", () => {
  const before = Array.from(
    { length: 4000 },
    (_, index) => `original ${index}\n`,
  ).join("");
  const after = Array.from(
    { length: 4000 },
    (_, index) => `novo ${index}\n`,
  ).join("");
  const diff = compareLines(before, after);
  expect(diff.simplified).toBe(true);
  expect(diff.added).toBe(4000);
  expect(diff.removed).toBe(4000);
  expect(
    diff.lines
      .filter((line) => line.kind === "removed")
      .map((line) => line.text + line.newline)
      .join(""),
  ).toBe(before);
  expect(
    diff.lines
      .filter((line) => line.kind === "added")
      .map((line) => line.text + line.newline)
      .join(""),
  ).toBe(after);
});
