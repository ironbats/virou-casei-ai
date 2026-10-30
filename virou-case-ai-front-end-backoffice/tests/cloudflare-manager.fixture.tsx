import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { CloudflareManager } from "../src/cloudflare-manager";
import { session } from "../src/api";
import type { Connection } from "../src/integration-types";
import "../src/styles.css";
import "../src/admin.css";
import "../src/integration-settings.css";

session("operator-test", "csrf-test");

const connection: Connection = {
  id: "configuration-test",
  connection_id: "connection-test",
  name: "Cloudflare produção",
  is_default: true,
  version: 7,
  state: "active",
  settings: { account_id: "a".repeat(32), environment: "production" },
  validation: { ready: true },
  has_token: true,
  validated_at: "2026-10-28T12:00:00Z",
};

const secondary: Connection = {
  ...connection,
  id: "configuration-second",
  connection_id: "connection-second",
  name: "Cloudflare homologação",
  is_default: false,
  version: 3,
  settings: { account_id: "b".repeat(32), environment: "staging" },
};

function Fixture() {
  const [selected, setSelected] = useState<Connection | null>(null);
  useEffect(() => {
    const update = (event: Event) => {
      const detail = (event as CustomEvent<Connection>).detail;
      setSelected(detail);
    };
    window.addEventListener("fixture-connection", update);
    return () => window.removeEventListener("fixture-connection", update);
  }, []);
  return (
    <main style={{ padding: 16 }}>
      <h1>Integrações e templates</h1>
      <p>Contas Cloudflare de teste</p>
      <button className="button" onClick={() => setSelected(connection)}>
        Gerenciar Cloudflare produção
      </button>
      <button className="button" onClick={() => setSelected(secondary)}>
        Gerenciar Cloudflare homologação
      </button>
      {selected && (
        <CloudflareManager
          connection={selected}
          close={() => setSelected(null)}
          changed={() => {}}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
