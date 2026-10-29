import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { command } from "../src/shared/api";
import { ProposalReview } from "../src/features/ProposalReview";
import type { Proposal, ReviewView } from "../src/features/proposalTypes";
import "../src/shared/styles.css";

const paths = [
  "public/modal.ts",
  "src/auth.py",
  "public/index.html",
  "public/style.css",
  "migrations/0002.sql",
  "config.json",
  "notes.unknown",
  "public/obsolete.txt",
  "public/long.js",
];
const proposal: Proposal = {
  proposal_id: "fixture-proposal",
  proposal_version: "synthetic-version",
  base_project_revision: "base-revision",
  candidate_revision_id: "candidate-revision",
  current_project_revision: "base-revision",
  expected_project_version: 2,
  state: new URLSearchParams(location.search).has("conflict")
    ? "conflict"
    : "generated",
  files: paths.map((path, index) => ({
    id: String(index),
    path,
    change: index === 0 ? "created" : index === 7 ? "deleted" : "modified",
    before_hash: "synthetic-before",
    after_hash: "synthetic-after",
    before_bytes: 20,
    after_bytes: 40,
    content_type: "text",
  })),
  verification_results: [
    { kind: "structure", state: "passed", message: "Estrutura conferida" },
    {
      kind: "tests",
      state: "not_run",
      message: "Testes do projeto gerado não executados",
    },
    {
      kind: "execution",
      state: "not_run",
      message: "Execução do projeto gerado não realizada",
    },
  ],
  warnings: [],
  conflict_paths: ["public/index.html"],
};

function Fixture() {
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [result, setResult] = useState("");
  const memory = useRef(new Map<string, ReviewView>());
  return (
    <main style={{ padding: 24 }}>
      <h1>Proposta de teste</h1>
      <button className="button" onClick={() => setOpen(true)}>
        Revisar alterações
      </button>
      <p role="status">{result}</p>
      {open && (
        <ProposalReview
          pid="fixture-project"
          proposal={proposal}
          busy={busy}
          blocked={busy}
          dirty={false}
          error={null}
          memory={memory.current}
          onClose={() => setOpen(false)}
          onRefresh={async () => {
            setResult("Estado consultado");
          }}
          onCompare={async () => {
            setResult("Comparação solicitada");
          }}
          onApply={async () => {
            setBusy(true);
            try {
              await command(
                "/projects/fixture-project/proposals/fixture-proposal/apply",
                {
                  expected_proposal_version: proposal.proposal_version,
                  expected_project_revision: proposal.base_project_revision,
                },
                "POST",
                proposal.expected_project_version,
              );
              setResult("Conjunto confirmado");
              setOpen(false);
            } finally {
              setBusy(false);
            }
          }}
          onDiscard={async () => {
            await command(
              "/projects/fixture-project/proposals/fixture-proposal/discard",
            );
            setResult("Descarte confirmado");
            setOpen(false);
          }}
        />
      )}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
