import { createRoot } from "react-dom/client";
import { PreparationSettings } from "../src/preparation-settings";
import { Observability } from "../src/observability";
import { session } from "../src/api";
import "../src/styles.css";
import "../src/admin.css";
session("admin-fixture", "csrf-fixture");
createRoot(document.getElementById("root")!).render(
  <main className="admin-main monitor-main">
    <h1>Observabilidade</h1>
    {new URLSearchParams(location.search).get("view") === "preparation" ? (
      <PreparationSettings refreshToken={0} />
    ) : (
      <Observability refreshToken={0} />
    )}
  </main>,
);
