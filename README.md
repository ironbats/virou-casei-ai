# VIRA CASE AI — implementação candidata

Produto novo, organizado em três projetos independentes: `backend`, `frontend` e `backoffice`. A implementação parte dos cinco documentos anexados de Produto, UX, Arquitetura, Frontend e Backend. O histórico de validação está em `docs/STATUS.md` e a cobertura de todos os critérios numerados em `docs/MATRIZ.md`.

**O MVP completo ainda não está aprovado.** Preview, build e publicação de código dos alunos na Cloudflare permanecem bloqueados. O adapter de publicação e o executor isolado ainda precisam ser implementados e comprovados em um ambiente autorizado. Isso preserva o gate exigido nas especificações e não representa aceite de um escopo reduzido.

## Subir localmente

Requisitos: Linux ou WSL2, Python 3.12+, Node.js 22.12+ ou 24 LTS, npm e Docker com Compose v2. O primeiro uso baixa as dependências; reserve acesso à Internet para os registries. Use `127.0.0.1` nos endereços abaixo para manter as origens de sessão consistentes.

Em três terminais, a partir desta pasta:

```bash
cd backend
bash script/run-local.sh
```

```bash
cd frontend
bash script/run-local.sh
```

```bash
cd backoffice
bash script/run-local.sh
```

O script do backend cria `.env` com senhas aleatórias caso ele não exista, inicia somente PostgreSQL em Docker, espera a saúde do banco, instala as dependências, aplica migrations e solicita login/senha do primeiro operador. A API pública, a API administrativa e o worker rodam como processos Python na máquina local. Ctrl+C encerra os processos; o volume do banco é preservado.

| Serviço | Endereço local |
| --- | --- |
| Frontend público | http://127.0.0.1:5173 |
| Backoffice | http://127.0.0.1:5174 |
| API pública | http://127.0.0.1:8000/api/v1 |
| API administrativa | http://127.0.0.1:8001/admin-api/v1 |
| PostgreSQL | 127.0.0.1:55432 |

No backoffice, entre como operador, provisione a conta do aluno e confirme a ativação com uma referência única de pagamento. Não há cadastro público, checkout ou renovação automática. As credenciais E2E são exclusivamente sintéticas e não são criadas pelo `run-local.sh`.

## O que está implementado

- Login por sessão HttpOnly, CSRF, origens permitidas, logout e redefinição administrativa de senha.
- Oferta de R$ 75 por 30 dias; grants de uso, pagamentos manuais, recargas e ajustes auditados; nenhum custo interno nos DTOs do aluno.
- Limite de dois projetos, seleção automática de stack, armazenamento segregado de fonte, revisões imutáveis, ETags, conflitos e revisões candidatas.
- Editor com salvamento explícito, confirmação antes de descartar edições, histórico, restauração e export ZIP da revisão escolhida.
- Jobs SQL duráveis, leases, idempotência, reserva decimal, liquidação uma única vez, cancelamento e reconciliação após resultado incerto.
- Adapters HTTP de OpenAI/Gemini com catálogo restrito e desativado por padrão. Não ocorre chamada paga ao instalar ou abrir guias.
- Biblioteca de 14 guias completos, filtros, conteúdo de segurança e templates copiáveis sem consumo.
- Regras de dia 30/dia 32, bloqueio de download, fence de expurgo, remoção da fonte e preservação do financeiro. Backups externos exigem evidência específica do operador.
- Interface pública, aplicação do aluno e backoffice separados, com a direção visual off-white/azul-marinho das referências.

## IA: configuração necessária

Configure as credenciais somente no `.env` do backend, nos campos `VIRA_OPENAI_API_KEY` e/ou `VIRA_GEMINI_API_KEY`. Nunca coloque chaves no frontend, no projeto do aluno ou no controle de versão.

Revise `backend/config/models.json`: modelo exato, preço aplicável à sua conta, categorias de cobrança, câmbio, fonte, vigência e evidência. Somente marque `enabled` e `verified_account` após essa verificação. Políticas vencidas, modelos não permitidos ou ausência de credencial mantêm o gate fechado. A operação persiste uma cópia da política usada na reserva. Raciocínio/cache são normalizados pelos adapters; categorias de cobrança desconhecidas exigem reconciliação.

Os testes entregues simulam respostas dos provedores. A ativação de uma chave não comprova custo real, capacidade, qualidade da geração ou publicação. Registre uma chamada de homologação controlada antes de liberar o uso comercial.

## Validação reproduzível

Backend, após instalar suas dependências:

```bash
cd backend
.venv/bin/python -m pip install 'pytest==9.1.1' 'ruff==0.17.2'
.venv/bin/pytest -q
.venv/bin/ruff check src tests script --select F
```

Sem `TEST_DATABASE_URL`, os testes usam SQLite temporário e o teste concorrente PostgreSQL é explicitamente ignorado. Para esse teste, forneça uma URL PostgreSQL de homologação cujo usuário possa criar schemas. Cada execução cria e remove apenas seu próprio schema de teste. Nunca use credenciais ou banco de produção.

Frontend e backoffice:

```bash
npm ci
npm run build
npm audit
```

Para as jornadas de navegador, instale Chromium no frontend com `npx playwright install chromium`. Em seguida execute, a partir de `backend`, `.venv/bin/python script/test-e2e.py`. O runner inicia APIs, worker e duas interfaces com dados temporários, executa Playwright e encerra os serviços. As portas 8000, 8001, 5173 e 5174 precisam estar livres. O runner não usa suas contas ou credenciais de IA.

## Organização e limites de entrega

O domínio puro fica em `backend/src/viracase/domain`; os casos de uso e portas em `application`; SQLAlchemy, storage, worker, conteúdo e provedores em `infrastructure`; contratos HTTP em `interfaces`. Os frontends estão organizados por funcionalidades e compartilham conceitos de contrato, sem importar o backoffice na aplicação pública.

São entregues Dockerfiles opcionais e configurações Nginx. O caminho solicitado para desenvolvimento continua sendo backend local com banco Docker. Os Compose das interfaces usam rede host, voltada a Linux; não foram executados neste ambiente.

Consulte `docs/OPERACAO.md` antes de configurar exposição pública, backup ou restore. Não exponha a porta 8001, o backoffice, o PostgreSQL ou o volume de fontes. Ainda faltam comprovação de PostgreSQL/Docker, restore e reboot reais, carga, revisão assistiva de acessibilidade e toda a cadeia isolada de publicação. Nenhum repositório remoto ou deploy foi criado nesta entrega.
