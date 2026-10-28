import { createRoot } from "react-dom/client";
import { PublicationDiagnostics } from "../src/publication-diagnostics";
import type { Connection } from "../src/integration-types";
import "../src/styles.css";
import "../src/admin.css";

const connection: Connection = {
  id: "configuration-test", connection_id: "connection-test", name: "Synthetic test",
  is_default: true, version: 1, state: "active", settings: {}, validation: { ready: true }, has_token: true,
};

createRoot(document.getElementById("root")!).render(
  <main style={{ maxWidth: 760, padding: 16, margin: "auto" }}>
    <PublicationDiagnostics config={connection} blocked={false} onBusy={() => {}} changed={() => {}} />
  </main>,
);
