import { ArrowRight, BookOpen, Code2, Globe, ShieldCheck } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { LoginForm, useSession } from "../app/session";
import { api } from "../shared/api";
import { Brand, ErrorNotice, useLoad, WhatsApp } from "../shared/ui";
import type { Offer } from "../shared/types";

export function Home() {
  return (
    <div className="public-page">
      <header className="public-header">
        <Brand />
        <nav aria-label="Acesso">
          <Link to="/login">Entrar</Link>
          <Link className="button primary" to="/experimentacao">
            Conheça agora <ArrowRight size={17} />
          </Link>
        </nav>
      </header>
      <main id="main">
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow">EXPERIMENTAÇÃO · APRENDIZADO NA PRÁTICA</p>
            <h1>
              Aprenda com IA.
              <br />
              Coloque suas
              <br />
              <em>ideias no ar.</em>
            </h1>
            <p className="hero-description">
              Orientação para aprender, criar aplicações
              <br className="desktop-break" /> e construir seu primeiro projeto
              funcional.
            </p>
            <div className="actions">
              <Link to="/experimentacao" className="button primary large">
                Conheça agora <ArrowRight size={20} />
              </Link>
              <Link to="/login" className="button large">
                Entrar
              </Link>
            </div>
            <p className="muted small">Participe da lista de experimentação.</p>
          </div>
          <div
            className="hero-scene"
            aria-label="Ilustração do ambiente de criação"
          >
            <div className="scene-steps">
              <span>01 Aprender</span>
              <span>02 Criar</span>
              <span>03 Publicar</span>
            </div>
            <div className="scene-wash" />
            <div className="mock-window editor-mock">
              <div className="window-bar">
                <i />
                <i />
                <i />
                <span>Seu espaço de criação</span>
              </div>
              <div className="mock-layout">
                <div className="mock-nav">
                  <span>Projeto</span>
                  <span>Conversa</span>
                  <span>Arquivos</span>
                </div>
                <div className="mock-code">
                  <strong>Uma ideia. Muitas possibilidades.</strong>
                  <p>Contexto + direção + criatividade</p>
                  <pre>
                    <span className="code-blue">const</span>
                    {" projeto = {\n\n  ideia: "}
                    <span className="code-green">'o seu próximo passo'</span>
                    {",\n  possibilidades: "}
                    <span className="code-blue">Infinity</span>
                    {",\n\n  começar: "}
                    <span className="code-green">'agora'</span>
                    {"\n\n}"}
                  </pre>
                </div>
              </div>
            </div>
            <div className="mock-window site-mock">
              <div className="window-bar">
                <i />
                <i />
                <i />
                <span>Seu projeto</span>
              </div>
              <div className="site-mock-content">
                <span className="eyebrow">FEITO A PARTIR DA SUA IDEIA</span>
                <h2>
                  Ideias que
                  <br />
                  <em>ganham forma.</em>
                </h2>
                <div className="art-landscape">
                  <div />
                  <div />
                  <div />
                </div>
                <div className="mini-features">
                  <span>Planeje</span>
                  <span>Construa</span>
                  <span>Compartilhe</span>
                </div>
              </div>
            </div>
            <span className="scene-caption">
              Ilustração de conceito · publicação sujeita à validação
            </span>
          </div>
        </section>
        <section className="how">
          <h2>Da primeira ideia ao seu projeto.</h2>
          <div className="how-grid">
            {[
              [
                BookOpen,
                "Aprenda",
                "Explore conceitos, guias e templates para usar IA com mais clareza.",
              ],
              [
                Code2,
                "Crie",
                "Transforme o que você imaginou em código, com contexto e orientação.",
              ],
              [
                Globe,
                "Publique",
                "Prepare sua aplicação para ir ao ar, com isolamento e revisão.",
              ],
            ].map(([Icon, title, text], i) => {
              const I = Icon as typeof BookOpen;
              return (
                <article key={String(title)}>
                  <span className="step-number">0{i + 1}</span>
                  <div>
                    <I size={23} />
                    <h3>{String(title)}</h3>
                    <p>{String(text)}</p>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      </main>
      <footer>
        <Brand />
        <span>Aprender. Construir. Evoluir.</span>
      </footer>
    </div>
  );
}
export function Experiment() {
  const { data: offer, error } = useLoad(() => api<Offer>("/offer"));
  return (
    <div className="public-page">
      <header className="public-header">
        <Brand />
        <Link to="/login">Entrar</Link>
      </header>
      <main id="main" className="experiment">
        <p className="eyebrow">SEU PRÓXIMO PASSO</p>
        <h1>
          Uma ideia merece
          <br />
          <em>sair do papel.</em>
        </h1>
        <p>
          Entre na lista de experimentação. Converse com a gente para conhecer a
          proposta e verificar a disponibilidade.
        </p>
        <div className="offer-card">
          <ShieldCheck size={28} />
          <h2>Aprenda e construa com IA</h2>
          <p>Até dois projetos, guias completos e construção assistida.</p>
          {offer && (
            <p className="price">
              {new Intl.NumberFormat("pt-BR", {
                style: "currency",
                currency: offer.currency,
              }).format(Number(offer.amount_minor) / 100)}{" "}
              <span>por {offer.access_days} dias</span>
            </p>
          )}
          <ErrorNotice error={error} />
          <WhatsApp url={offer?.whatsapp_url}>
            Participar da experimentação
          </WhatsApp>
          <p className="small muted">
            Pagamento e renovação manuais. O contato não cria uma conta nem
            concede créditos. Publicação depende da validação técnica do
            ambiente.
          </p>
        </div>
      </main>
    </div>
  );
}
export function Login() {
  const navigate = useNavigate(),
    { refresh } = useSession();
  return (
    <main id="main" className="login-page">
      <Brand />
      <section className="login-card">
        <p className="eyebrow">BOM TER VOCÊ POR AQUI</p>
        <h1>
          Continue sua
          <br />
          <em>próxima ideia.</em>
        </h1>
        <p className="muted">Use o login e a senha fornecidos para acessar.</p>
        <LoginForm
          onSuccess={async () => {
            await refresh();
            navigate("/app/projetos");
          }}
        />
        <p className="small">
          Precisa de ajuda?{" "}
          <a
            href="https://wa.me/5511941810735"
            target="_blank"
            rel="noopener noreferrer"
          >
            Fale com a gente
          </a>
        </p>
      </section>
    </main>
  );
}
