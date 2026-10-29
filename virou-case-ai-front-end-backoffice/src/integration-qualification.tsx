import { useEffect, useRef, useState } from "react";
import { api, command } from "./api";
import type { Connection } from "./integration-types";

export function Qualification({
  config,
  reload,
  blocked,
  onBusy,
  onDirty,
  standalone = false,
}: {
  config: Connection;
  blocked: boolean;
  onBusy: (busy: boolean) => void;
  onDirty: (dirty: boolean) => void;
  reload: () => Promise<void>;
  standalone?: boolean;
}) {
  const upload = useRef<HTMLInputElement>(null);
  const Container = standalone ? "section" : "details";
  const [report, setReport] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [checks, setChecks] = useState<string[]>([]);
  useEffect(() => {
    onDirty(!!report);
  }, [report, onDirty]);
  useEffect(() => {
    void api<{ checks: string[] }>("/publication-qualification-requirements")
      .then((r) => setChecks(r.checks))
      .catch((e) => setError(e.message));
  }, []);
  return (
    <Container
      className={standalone ? "cloudflare-qualification form" : undefined}
    >
      {standalone ? (
        <h3>Relatório de qualificação da publicação</h3>
      ) : (
        <summary>Relatório de qualificação da publicação</summary>
      )}
      <p>
        A verificação operacional pode liberar a publicação. Para qualificação
        formal adicional, anexe os resultados reais dos testes de cada perfil,
        revise as evidências e registre o relatório desta versão. Relatórios
        incompletos, vencidos ou com falhas não qualificam esta versão.
      </p>
      <p className="small">
        Configuração: {config.id || "Salve primeiro"}
        <br />
        Digest da configuração: {config.configuration_digest || "—"}
        <br />
        Digest do executor: {config.executor_digest || "Executor não instalado"}
      </p>
      <details>
        <summary>Cenários exigidos</summary>
        <ul>
          {checks.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      </details>
      <label>
        Carregar relatório JSON
        <input
          ref={upload}
          disabled={busy || blocked}
          type="file"
          accept=".json,application/json"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            if (file.size > 100000) {
              setError("O relatório deve ter até 100 KB.");
              return;
            }
            try {
              setReport(await file.text());
              setError("");
            } catch {
              setError("Não foi possível ler o arquivo selecionado.");
            }
          }}
        />
      </label>
      <label>
        Conteúdo do relatório
        <textarea
          disabled={busy || blocked}
          rows={12}
          value={report}
          maxLength={100000}
          onChange={(e) => setReport(e.target.value)}
        />
      </label>
      <p>
        O relatório deve conter identificação da configuração, conta, datas,
        runner e hash, perfis, referência dos resultados, motivo da revisão e
        cada cenário com resultado esperado, observado e hash da evidência.
        Consulte PUBLICACAO.md.
      </p>
      {error && (
        <p role="alert" className="notice error">
          {error}
        </p>
      )}
      <div className="actions">
        <button
          className="button"
          disabled={
            busy ||
            blocked ||
            !report ||
            !["validated", "active"].includes(config.state)
          }
          onClick={async () => {
            setBusy(true);
            onBusy(true);
            setError("");
            try {
              await command(
                `/integration-connections/${config.connection_id}/qualification`,
                JSON.parse(report),
                "POST",
                config.version,
              );
              await reload();
              setReport("");
              if (upload.current) upload.current.value = "";
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
              onBusy(false);
            }
          }}
        >
          Registrar resultados e revisão responsável
        </button>
        {report && (
          <button
            type="button"
            className="button"
            disabled={busy || blocked}
            onClick={() => {
              setReport("");
              setError("");
              if (upload.current) upload.current.value = "";
            }}
          >
            Limpar relatório
          </button>
        )}
      </div>
    </Container>
  );
}
