import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./app/Shell";
import { SessionProvider } from "./app/session";
import { Experiment, Home, Login } from "./features/Public";
import { Projects } from "./features/Projects";
import { GuideDetail, Learning } from "./features/Learning";
import { Account, Usage } from "./features/Usage";
import { ProjectHistory, Publish, Workspace } from "./features/Workspace";
import "./shared/styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <SessionProvider>
        <a className="skip-link" href="#main">
          Pular para conteúdo
        </a>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/experimentacao" element={<Experiment />} />
          <Route path="/login" element={<Login />} />
          <Route path="/app" element={<Shell />}>
            <Route index element={<Navigate to="projetos" replace />} />
            <Route path="projetos" element={<Projects />} />
            <Route path="projetos/:projectId" element={<Workspace />} />
            <Route
              path="projetos/:projectId/historico"
              element={<ProjectHistory />}
            />
            <Route path="projetos/:projectId/publicar" element={<Publish />} />
            <Route path="aprendizado" element={<Learning />} />
            <Route path="aprendizado/:slug" element={<GuideDetail />} />
            <Route path="consumo" element={<Usage />} />
            <Route path="conta" element={<Account />} />
          </Route>
          <Route
            path="*"
            element={
              <main id="main" className="centered">
                <h1>Página não encontrada</h1>
                <Link to="/">Voltar ao início</Link>
              </main>
            }
          />
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  </StrictMode>,
);
