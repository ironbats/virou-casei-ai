# Recuperação pública de publicação sem efeitos

## Objetivo

O desenvolvedor pode conferir e encerrar uma tentativa antiga do próprio projeto quando o servidor comprova ausência de efeitos pendentes. A ação não executa novamente a migration, não cria recibo D1, não publica uma revisão e não usa IA.

O encerramento grava a operação antiga como `failed`, o job como `done` e a recuperação como `resolved`. A prontidão da revisão salva atual é recalculada. Publicar essa revisão exige uma ação explícita posterior.

A causa original continua preservada. HTTP 400/código 7500, ausência de recibo e igualdade de schema, isoladamente, não são prova de ausência de efeitos.

## Autorização

A capacidade de domínio é `recover_own_publication_no_effect`.

- A rota pública utiliza a sessão e a audiência públicas existentes, além do CSRF existente nas mutações.
- O papel `student` é explicitamente mapeado para recuperar operações de projetos dos quais a própria conta é proprietária.
- A permissão `publish` não concede essa capacidade por implicação.
- Papéis administrativos e papéis desconhecidos não são mapeados para o comando público.
- A vigência de acesso, manutenção da plataforma, propriedade do projeto e vínculo da operação são conferidos no recebimento e novamente no worker.
- Os identificadores de ator e conta são derivados do servidor. O corpo público não aceita tenant, ator, baseline, SQL, atestação ou hashes de prova.
- O modelo atual não possui colaboradores independentes dentro da conta. Nenhum papel de colaboração foi criado ou autorizado implicitamente.
- Consultar um identificador de outro proprietário retorna `404`, sem diagnóstico ou resultado idempotente alheio.
- O caminho administrativo existente mantém suas ações e suas autorizações.

## Contrato público

### Consulta

`GET /api/v1/operations/{operation_id}/publication-recovery`

O contrato anterior foi preservado. Foram acrescentados:

| Campo | Significado |
| --- | --- |
| `capabilities.can_check` | Disponibilidade da conferência legada. |
| `capabilities.can_check_and_resolve` | Permissão atual para solicitar a avaliação resolutiva. Não significa prova suficiente. |
| `capabilities.can_close_now` | Permanece falso: o encerramento acontece dentro do comando autorizado, sem segunda etapa pública. |
| `resolution` | Modo, ação estreita, elegibilidade, motivo, evidências faltantes, provas booleanas e próxima ação. |
| `state_version` | Hash opaco do estado e do vínculo usados para controle de concorrência. |
| `recovery_request_id` | Identificador da solicitação durável. |
| `request` | Estado e horários públicos da solicitação. |
| `readiness` | Prontidão recalculada após a resolução, incluindo bloqueios independentes. |

Os valores legados `classification` e `operator_required` continuam representando a classificação existente. A nova decisão é exposta em `resolution`; a classificação histórica não é reescrita para simular uma atestação.

### Comando único

`POST /api/v1/operations/{operation_id}/publication-recovery/resolve`

Cabeçalhos obrigatórios: sessão pública existente, `X-CSRF-Token` e `Idempotency-Key`.

```json
{
  "expected_state_version": "valor de 64 caracteres recebido na consulta",
  "action": "check_and_close_if_no_effect",
  "consent": "verify_and_close_failed_attempt_when_proven_safe"
}
```

A versão do exemplo é descritiva; o cliente deve enviar o hash real recebido no GET. A resposta aceita utiliza HTTP 202 e o mesmo snapshot consultável da recuperação.

| Situação | Resposta |
| --- | --- |
| Solicitação aceita | `202`, com o identificador e o estado persistidos. |
| Mesma chave e mesmo conteúdo | Mesma solicitação, com resultado persistido; nenhuma nova execução externa. |
| Mesma chave e conteúdo diferente | Conflito de idempotência. |
| Versão de estado diferente | `412 EVIDENCE_CHANGED`. |
| Outra execução ou lease ativo | Conflito; acompanhar a solicitação em curso. |
| Cooldown ou limite da conta | `429`, sem criar nova execução. |
| Corpo expandido ou ação administrativa | Rejeição de validação. |
| CSRF inválido | Rejeição pela proteção existente. |
| Outro proprietário | `404`. |

O GET, o polling e o POST legado `/check` não fornecem consentimento de resolução. Ao recarregar, o frontend consulta o request existente.

## Execução e persistência

A solicitação é armazenada em `Operation.result.publication_developer_recovery`, com ator, conta, projeto, operação original, revisão original, consentimento, chave de intenção, vínculo, estado e histórico.

O worker existente de recuperação adquire o lease e encaminha somente solicitações explicitamente consentidas ao novo caso de uso:

1. Confere acesso, propriedade, configuração, binding e lease.
2. Carrega a revisão imutável e confirma seu hash.
3. Revalida token ativo, conta remota, UUID do banco e associação ao nome do projeto.
4. Coleta a evidência existente e aplica a política pública adicional.
5. Se elegível, persiste plano interno com ação única, política, hashes, vínculo e validade.
6. Repete a observação remota e a decisão técnica.
7. Sob locks, revalida vínculo, fontes, plano, validade, consentimento e lease.
8. Finaliza operação, job, inventário, recuperação, resposta idempotente e auditoria na mesma transação.
9. Recalcula a prontidão e publica os eventos de sucesso somente após a transação concluir.

Não há transação local aberta durante consultas remotas. As consultas D1 da recuperação são somente leitura. A finalização reutiliza o encerramento existente e não chama o executor de migrations.

### Limites

- Validade do plano: 15 minutos, usando a constante central existente.
- No máximo duas rodadas de planejamento/revalidação por execução.
- No máximo três retomadas da mesma solicitação após interrupção do worker.
- Cooldown de 60 segundos entre solicitações da operação.
- Limite de 10 novas solicitações por conta em 300 segundos.
- Respostas idempotentes não geram nova avaliação nem consomem esse limite.
- Observação limitada a 512 recibos, 1.024 objetos de schema e 2.000 colunas por tabela, com contagem de completude.
- Falha ou mudança de evidência invalida a prova elegível; o plano histórico é preservado.

A chave de idempotência é vinculada à conta, à operação e ao comando. Mesmo um resultado armazenado passa novamente pela autorização de acesso antes de ser devolvido por mutação.

## Política de prova

A versão inicial é `developer_no_effect_v1`.

### Condições comuns

A política exige vínculo exato da operação e da revisão, hash integral da fonte, identidade remota confirmada, recibos completos do prefixo, ausência de recibo contraditório, baseline íntegra, observação completa e checkpoint pré-upload.

O checkpoint deve registrar explicitamente a etapa de migrations e `upload_started=false`. Atividade de upload, deployment, revisão candidata ou verificação contraditória impede o encerramento, mesmo quando um marcador isolado indica ausência de envio.

Metadados históricos malformados também impedem a decisão. A evidência relevante de upload é vinculada antes de registrar progresso; o limite de tamanho do histórico de atividades não pode apagar uma contradição e tornar uma tentativa elegível. Essa preservação também vale para o `/check` consultivo e para o worker legado: quando o corte removeria um evento de upload, o histórico bruto é mantido. O worker confere novamente esse vínculo antes de coletar a prova e antes do fechamento.

Outra unidade de execução incerta ou uma gravação de recibo em andamento também impede o fechamento.

O vínculo privado inclui o hash do inventário integral e das flags de efeitos adicionais no resultado. Assim, uma alteração local entre a solicitação, as duas coletas e o commit invalida o contexto antigo; o encerramento não pode se apoiar em um snapshot anterior à nova pendência.

### A. Protocolo durável de ausência de efeito

O protocolo existente de migrations pode comprovar a fase `prepared/not_sent`, uma falha de conexão anterior ao envio ou uma rejeição de compilação conhecida antes de qualquer efeito. A decisão exige versão suportada, identidade, hash, contagem de statements e vínculo preservados. Falhas de transporte ou compilação precisam estar vinculadas ao método POST e à rota exata de consulta D1; resposta HTTP e confirmação de execução contraditórias invalidam a alegação de não envio.

Um erro genérico HTTP 400/7500 não atende a essa regra. Mensagem fornecida pelo usuário, booleano de confirmação ou atestação pública não são aceitos.

A reconstrução de um registro legado nessa alternativa é limitada às informações já persistidas do protocolo suportado. Ela não cria retroativamente um registro de envio desconhecido.

### B. ADD COLUMN estrutural estritamente permitido

A política interpreta tokens e uma gramática fechada. Aceita somente um comando `ALTER TABLE ... ADD [COLUMN] ...` de tabela comum, com tipos escalares explicitamente permitidos e, quando presente, default constante permitido.

São rejeitados comandos múltiplos, DML, expressões, funções, consultas, referências externas, reconstrução de tabela, constraints não suportadas, colunas geradas, tabelas virtuais e execuções parcialmente conhecidas.

O nome da migration e seus comentários não classificam seu efeito. O arquivo do incidente que menciona atualização de administrador somente é tratado como ADD COLUMN se seu SQL efetivo corresponder ao subconjunto.

Além das condições comuns, são exigidos:

- Registro persistido da unidade enviada, anterior ao envio.
- Hash do SQL completo e hashes individuais dos statements.
- Identidade do banco e ausência de parâmetros adicionais.
- Hash do prefixo confirmado e da baseline reconstruída.
- Semântica de comando único compatível com o executor D1.
- Schema observado completo igual à baseline.
- Coluna exata ausente no inventário completo da tabela.
- Ausência de confirmação contraditória do provedor.
- Nova leitura do schema após observar a coluna, para detectar mudança durante a coleta.
- Leitura independente e completa dos recibos antes e depois da observação estrutural.

A contagem de recibos, objetos e colunas é comparada ao número de linhas retornado. Os sete campos do inventário de colunas são validados. Resposta com sucesso falso, truncamento, indicação de mais páginas, lista incompleta ou schema divergente não comprova ausência de efeito.

A justificativa técnica usa o subconjunto de adição de coluna constante que altera o schema sem atualizar os registros existentes. SQLite executa comandos dentro de transações; a implementação não estende essa propriedade a lotes ou a efeitos que não foram analisados. Referências primárias:

- [SQLite — ALTER TABLE ADD COLUMN](https://www.sqlite.org/lang_altertable.html)
- [SQLite — Transactions](https://www.sqlite.org/lang_transaction.html)
- [Cloudflare — D1 Query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
- [Cloudflare — SQL statements e table_xinfo](https://developers.cloudflare.com/d1/sql-api/sql-statements/)

### Telemetria de novas tentativas

O executor continua enviando o mesmo SQL, pelo mesmo gateway e pelo protocolo de migrations existente. Foi acrescentado `execution_unit` ao marcador persistido antes do envio.

A baseline adicional é reconstruída isoladamente. Se um prefixo não puder ser reconstruído para essa telemetria, isso não transforma uma publicação previamente válida em falha: o registro fica sem a prova adicional e a recuperação pública permanece inelegível. A validação normal do plano completo continua obrigatória.

### Registros antigos e incidente informado

A migration citada na especificação é candidata à gramática B, mas seu diagnóstico isolado não comprova todos os requisitos. Registros legados sem a unidade de execução necessária recebem `PROOF_INCOMPLETE`, com a prova ausente explicitada.

A implementação não afirma que a operação `ce81e74c-8604-477b-95ff-ff2f703bf730` foi encerrada no ambiente do usuário. Esse resultado somente pode ser confirmado observando a execução no ambiente real. A causa original do HTTP 400 também permanece independente do defeito de recuperação.

## Interface V3

A página mantém o shell, a sidebar, acesso, quota e perfil. No desktop, recuperação, diagnóstico e IA ficam na coluna principal; etapas, revisão atual e último link verificado ficam na coluna de acompanhamento. No mobile, os cards são empilhados.

O CTA é `Conferir e resolver`. Antes dele, o efeito é explicado: “Ao continuar, verificamos as evidências e resolvemos a pendência quando for seguro. Nenhum comando de banco será repetido.”

Os três indicadores são estados informativos, sem wizard ou confirmação adicional. O progresso usa estados reais do request; não há porcentagem simulada.

O diagnóstico diferencia erro inicial, impedimento atual, revisão original, migration e resultado da recuperação. Evidências, exportação e dados técnicos continuam acessíveis.

A Análise IA permanece no cabeçalho e no card existente. A navegação pelo cabeçalho apenas focaliza a funcionalidade. A análise continua opcional, usa a própria quota e mantém a revisão antes da aplicação de código.

A recuperação não marca upload/verificação como concluídos. O link anterior, seus metadados, copiar/abrir e o aviso de retenção até o dia 32 permanecem disponíveis.

## Compatibilidade e aplicação

Nenhuma configuração de Cloudflare, credencial, variável de ambiente ou schema de infraestrutura foi alterado. Não foi necessário criar migration Alembic: a persistência existente comporta a nova solicitação, os planos e a auditoria.

Aplicar os arquivos completos nas respectivas raízes do backend e frontend. Reiniciar/reconstruir cada projeto pelo `./script/run-local.sh` já existente, mantendo o modo de execução habitual.

A API e o worker precisam usar a mesma versão do backend. Em seguida, carregar o frontend atualizado. A configuração Cloudflare existente continua centralizada no backoffice; a recuperação pública não exige nova configuração nem login administrativo.

O frontend novo mantém leitura compatível com o contrato antigo e conserva a conferência legada quando o backend não oferece a capacidade nova. O comando consultivo antigo não adquire efeito resolutivo.

Para rollback, repor os arquivos anteriores e reiniciar API/worker/frontend juntos. Solicitações públicas que ainda não finalizaram permanecem pendentes no comportamento antigo; rollback não é mecanismo de desbloqueio. Operações já finalizadas usam os estados tradicionais existentes.

## Testes reproduzíveis

Executar os comandos a partir da raiz de cada projeto, com suas dependências instaladas.

Backend, incluindo a cobertura pública e as regressões administrativas:

```bash
python -m pytest -q tests/test_developer_recovery_proof.py tests/test_developer_recovery_acceptance.py tests/test_developer_recovery_service.py tests/test_publication_resolution_http.py tests/test_publication_resolution_security.py tests/test_publication_recovery_evidence.py tests/test_publication_recovery_projection.py
```

Os testes de concorrência exigem `TEST_DATABASE_URL` apontando para um PostgreSQL isolado. Cada fixture usa um schema exclusivo. Não apontar a suíte para um banco de produção.

Frontend:

```bash
npm ci
npm run build
npx playwright test tests/publication.spec.ts tests/publication-analysis.spec.ts tests/publication-recovery.spec.ts
```

Jornada integrada em laboratório:

```bash
VIRA_E2E_ISOLATED=true PYTHONPATH=src:tests python tests/developer_recovery_server.py
```

O comando acima é exclusivo de testes: cria API, worker e dados sintéticos, bloqueia uso de banco externo e substitui somente a Cloudflare por transporte controlado com banco em memória. O servidor deve ser iniciado a partir do backend, sem `TEST_DATABASE_URL`.

Com o frontend atendendo em 127.0.0.1:5173 e API em 127.0.0.1:8000:

```bash
VIRA_E2E_DEVELOPER_RECOVERY=true npx playwright test tests/publication-developer-recovery.integration.spec.ts
```

A jornada usa login, endpoints, persistência e worker reais. O laboratório deve ser reiniciado antes de repetir a jornada, pois não apaga ou reabre artificialmente uma operação resolvida.

Os resultados efetivamente executados e os limites ambientais constam a seguir.

## Relatório de validação — 29 de outubro de 2026

A rodada final foi executada após o congelamento do código de produto e dos testes entregues. As dependências da aplicação seguiram as versões e hashes do `requirements.lock` fornecido; o PostgreSQL utilizado foi o 16.15, em instância isolada. A toolchain de frontend e compilação utilizou os lockfiles originais.

| Verificação | Resultado observado | Escopo |
| --- | --- | --- |
| Backend completo, 92 módulos, dividido em três grupos sem sobreposição | **2.161 aprovados, 4 ignorados por condição ambiental, 0 falhas** | 2.165 casos coletados na versão final. Grupos: 720/2, 721/1 e 720/1 aprovados/ignorados. |
| Política determinística de prova | **96 aprovados** | Subconjunto do total do backend; inclui evidência completa/incompleta, protocolo, SQL, truncamento, metadados e observação concorrente. |
| Serviço público de recuperação | **56 aprovados** | Subconjunto do total do backend; inclui admissão, replay, TTL, binding integral, preservação pelo `/check` e alterações depois da segunda prova. |
| Aceitação pública HTTP | **34 aprovados** | Subconjunto do total do backend; API, sessão, CSRF, proprietário, prova do executor, worker e concorrência PostgreSQL. |
| Recuperação legada e administrativa, rodada direcionada | **70 aprovados** | Regressão adicional anterior à rodada final completa; não deve ser somada novamente ao total de casos distintos. |
| Interface de publicação, recuperação e Análise IA | **83 aprovados, 0 falhas** | Uma execução final conjunta das três suites Playwright. |
| Jornada integrada final no navegador | **1 aprovado, 0 falhas** | Login público, API e worker reais do laboratório; transporte Cloudflare substituído por D1 controlado em SQLite. |
| Build e tipos frontend | **Aprovados** | `npm run build`, incluindo TypeScript, bundle da aplicação e bundle da prévia; verificação explícita de `tsc --noEmit`. |
| Formatação frontend | **Aprovada** | Prettier nos 11 arquivos entregues do frontend. |
| Checagem estática Python | **Aprovada no escopo E4/E7/E9/F** | Todos os arquivos Python criados/alterados. Regras adicionais globais já apontam padrões do projeto original, como `Depends` em assinaturas FastAPI e tratamento genérico na fronteira do worker; esta entrega não refatora esses padrões. |

Os quatro testes condicionais da rodada ampla foram:

1. Permissões de migration ilegível: ignorado porque a rodada ampla executa com privilégio. O mesmo caso foi executado separadamente como `nobody` em cópia isolada e **passou**.
2. Imagem Docker real: depende de Docker e da habilitação explícita prevista na suite existente.
3. Sandbox real do worker implantado: depende do ambiente de implantação.
4. Pressão de processos e namespace Linux: depende das capacidades do host e da habilitação opt-in existente.

Assim, a rodada ampla somada somente à verificação distinta de permissões cobre **2.162 casos de backend aprovados**, com três verificações de ambiente sem execução. Essa soma não representa uma única execução de 2.162 testes. Nenhum desses três testes de ambiente é apresentado como aprovado.

A validação visual e automatizada cobriu teclado, foco, Axe, redução de movimento, larguras de 320, 375, 768, 1024 e 1440 pixels e zoom de 200%.

A jornada integrada final confirmou:

- Migration, HTTP 400 e código 7500 visíveis no diagnóstico, com causa original indeterminada.
- Um único POST resolutivo aceito com HTTP 202 e acompanhamento restaurado após reload.
- Operação antiga `failed`, job `done` e recuperação `resolved` após a prova e o commit.
- Nenhuma escrita SQL/recibo D1, chamada de IA ou publicação automática no fluxo determinístico.
- Revisão atual diferente da revisão da tentativa, histórico, último link verificado e quota preservados.
- Readiness recalculada e nova publicação dependente de ação explícita.

Na preparação inicial, a ausência de `esbuild` no ambiente produziu a mesma falha no código original e no alterado. A instalação temporária da toolchain já fixada no projeto permitiu executar os testes de compilador; a rodada final acima já inclui a correção do ambiente. Nenhum manifesto de dependências foi alterado.

A implementação e o laboratório não comprovam o encerramento da operação real informada nos anexos. Tentativas antigas sem os registros exigidos continuam inelegíveis, com evidência faltante explicitada. A causa original da recusa D1 permanece dependente de evidência do ambiente real.

## Diff lógico dos arquivos entregues

Os caminhos abaixo são relativos à raiz de cada projeto. Cada arquivo do pacote contém seu conteúdo integral; aplicar sobre as mesmas versões de base fornecidas: backend `V2.0.0.56` e frontend `v2.0.0.43`.

### Backend

| Arquivo | Situação | O que mudou e por quê |
| --- | --- | --- |
| `src/viracase/domain/publication_recovery_access.py` | Criado | Capacidade e autorização explícitas do proprietário; evita derivar recuperação de permissão de publicação ou de papel administrativo. |
| `src/viracase/domain/publication_no_effect.py` | Criado | Gramática estrutural fechada, catálogo de provas e decisão de domínio; impede encerrar DML, lotes, efeitos desconhecidos ou prova incompleta por simples igualdade de schema. |
| `src/viracase/infrastructure/publication_developer_proof.py` | Criado | Coleta determinística independente de recibos, schema, coluna e protocolo; exige completude, identidade, baseline e ausência de contradições. |
| `src/viracase/infrastructure/publication_developer_recovery.py` | Criado | Caso de uso público completo: admissão, idempotência, limite, persistência, lease, plano interno, revalidação, fechamento transacional, prontidão e auditoria. Preserva evidência histórica antes de registrar progresso. |
| `src/viracase/infrastructure/publication_migrations.py` | Alterado | Grava a unidade exata de execução no checkpoint anterior ao envio. Mantém SQL, gateway e protocolo existentes; indisponibilidade da baseline adicional não invalida publicação normal. |
| `src/viracase/infrastructure/publication_recovery.py` | Alterado | Encaminha solicitações consentidas ao novo caso de uso no worker existente e evita modificar atividade histórica antes de validar seu vínculo. Preserva o caminho consultivo legado. |
| `src/viracase/infrastructure/publication_resolution.py` | Alterado | Acrescenta a projeção pública ao snapshot, permite a capacidade específica na configuração e impede concorrência administrativa enquanto a solicitação pública está ativa. Mantém ações administrativas existentes. |
| `src/viracase/infrastructure/repository.py` | Alterado | Expõe a capacidade nova somente para o papel de proprietário suportado, sujeita à vigência normal do acesso. |
| `src/viracase/infrastructure/repository_dto.py` | Alterado | Remove planos e histórico internos do DTO genérico público. O cliente recebe somente a projeção saneada e autorizada. |
| `src/viracase/interfaces/publication_recovery_routes.py` | Alterado | Acrescenta POST público `/resolve` e corpo estrito de intenção, consentimento e versão; preserva GET, `/check` e rotas administrativas. |
| `tests/test_developer_recovery_proof.py` | Criado | Exercita prova real do executor, gramática, protocolo durável, legacy, DML, lotes, metadados malformados, respostas parciais e mudanças durante a coleta. |
| `tests/test_developer_recovery_service.py` | Criado | Exercita orquestração, replay, estado, concorrência, validade, revalidação, interrupção, binding e preservação de contradições históricas. |
| `tests/test_developer_recovery_acceptance.py` | Criado | Exercita HTTP, autorização, CSRF, isolamento, prova real, worker, idempotência e persistência; inclui concorrência PostgreSQL e ausência de escrita D1/IA. |
| `tests/developer_recovery_server.py` | Criado | Disponibiliza laboratório opt-in para o navegador usando API e worker reais, dados sintéticos e apenas o provedor externo substituído. |
| `docs/publication-developer-recovery.md` | Criado | Documenta contrato, política, operação, aplicação, rollback, reprodução dos testes, diff lógico e limites da entrega. |

### Frontend

| Arquivo | Situação | O que mudou e por quê |
| --- | --- | --- |
| `src/features/publicationRecovery.ts` | Alterado | Tipos, leitura estrita e exportação saneada dos campos aditivos; mantém compatibilidade com snapshots legados. |
| `src/features/usePublicationRecovery.ts` | Alterado | Comando resolutivo único, acompanhamento por GET, reload, resposta perdida, cooldown e proteção contra troca de contexto; impede duplicação e mistura de operações. |
| `src/features/PublicationRecoveryPanel.tsx` | Alterado | Card V3 com consentimento contextual, CTA, indicadores reais, provas faltantes e evidências acessíveis; elimina a necessidade de confirmação administrativa nessa jornada. |
| `src/features/PublicationPage.tsx` | Alterado | Duas colunas V3, banner único e atualização de prontidão; mantém revisão atual, tentativa histórica, IA, etapas, último link e retenção. |
| `src/features/PublicationOperationDiagnostics.tsx` | Alterado | Usa o snapshot autorizado como fallback de fase/migration/provedor; separa erro original, impedimento atual e resultado da recuperação, preservando HTTP 400/7500. |
| `src/features/publicationDiagnostic.ts` | Alterado | Aceita fallback opcional da fase comprovada, mantendo os consumidores anteriores compatíveis. |
| `src/features/publication.css` | Alterado | Grid, hierarquia, aviso âmbar e responsividade V3, preservando azul/navy e redução de movimento. |
| `tests/publication-recovery.spec.ts` | Alterado | Dez cenários resolutivos novos, acessibilidade e responsividade; corrige fixture 404 que falhava de modo intermitente com StrictMode. |
| `tests/publication.spec.ts` | Alterado | Atualiza expectativas de banner e ordem semântica mobile para a V3; conserva as demais verificações. |
| `tests/publication-analysis.spec.ts` | Alterado | Verifica o aviso aprovado sobre quota e limites da IA, mantendo o fluxo opcional existente. |
| `tests/publication-developer-recovery.integration.spec.ts` | Criado | Jornada opt-in com login, API e worker reais do laboratório, reload, um único comando e preservação de revisão, quota e link anterior. |

### Critérios de aceite e pontos de verificação

| Critérios da especificação | Evidência de implementação e testes |
| --- | --- |
| 1–7, 43, 45 | Autorização de domínio, rotas públicas e suite de aceitação HTTP: sessão, propriedade, papel, CSRF, corpo estrito, saneamento e configuração existente. |
| 8–19, 26–28, 36–37, 47 | Política e suite de prova: unidade exata, baseline, prefixo, schema, protocolo, DML, multicomando, legacy, efeitos adicionais e falhas de observação. |
| 20–24, 29–31, 38, 48, 60 | Serviço, aceitação e regressões administrativas: plano, expiração, binding, lease, replay, concorrência PostgreSQL, encerramento e retomada. |
| 25, 32–35, 40–42, 44, 49–59 | Suites Playwright e jornada integrada: clique único, prontidão, revisão atual, IA, V3, acessibilidade, mobile, reload, ausência de envio automático e preservação do histórico. |
| 39, 46 | Regressão backend ampla e comparação com os arquivos originais; nenhuma migration de infraestrutura ou dependência de produto foi alterada. |

No modelo fornecido, projeto e operação pertencem à conta do usuário; não existe colaborador independente dentro da mesma conta. A verificação de proprietário não concede capacidade implícita a colaboradores futuros. O teste de isolamento usa as identidades e relações que o modelo atual realmente suporta.

A jornada integrada substitui o transporte externo D1 por SQLite controlado. As provas de concorrência usam PostgreSQL real. Nenhum desses resultados substitui a observação do incidente e de uma nova publicação verificada na conta Cloudflare do usuário.

