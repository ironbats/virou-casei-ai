import { useState } from "react";
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

function Fixture() {
  const [open, setOpen] = useState(false);
  return (
    <main style={{ padding: 16 }}>
      <button className="button" onClick={() => setOpen(true)}>
        Gerenciar Cloudflare produção
      </button>
      {open && (
        <CloudflareManager
          connection={connection}
          close={() => setOpen(false)}
          changed={() => {}}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
