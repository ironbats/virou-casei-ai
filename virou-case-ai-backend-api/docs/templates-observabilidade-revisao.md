# Templates, observabilidade e revisão de propostas

## Base e escopo

Implementação sobre os arquivos recebidos em `virou-case-ai-backend-api-V2.0.0.36(1).zip`, `virou-case-ai-front-end-app-v2.0.0.27.zip` e `virou-case-ai-front-end-backoffice-v2.0.0.24.zip`, conforme a especificação de 27 de outubro de 2026. A base entregue em ZIP não identifica um commit Git. O schema original termina em `0006_runtime_observability.py`; esta entrega acrescenta `0007_template_preparation_traces.py`.

O pacote de atualização contém arquivos completos, separados por projeto. A atualização deve ser sobreposta à mesma base, preservando os caminhos internos. Os arquivos que não aparecem no pacote permanecem na instalação existente. Não há atualização de dependências, de lockfiles, de credenciais, de portas ou de scripts de inicialização.

## Organização do ZIP

O ZIP contém **83 arquivos completos: 47 criados e 36 alterados**, sem arquivos removidos. As pastas externas identificam o projeto de destino; o conteúdo de cada uma deve ser aplicado na raiz correspondente da base recebida.

| Pasta no ZIP | Projeto de destino | Criados | Alterados | Total |
| --- | --- | ---: | ---: | ---: |
| `backend/` | Backend API V2.0.0.36 | 15 | 21 | 36 |
| `plataforma-web/` | Frontend app v2.0.0.27 | 21 | 12 | 33 |
| `backoffice/` | Frontend backoffice v2.0.0.24 | 11 | 3 | 14 |

A conferência do pacote compara cada arquivo com a base recebida e verifica os bytes de cada entrada após a compressão. Dependências instaladas, resultados temporários de testes e arquivos gerados de build não fazem parte da atualização.

## Diagnóstico comprovado na base recebida

### Recomendação e campos vazios

Em `src/features/GuidedBuildPanel.tsx`, o comando original `prepare()` envia a revisão do rascunho para `/projects/{pid}/recommendations`. A seleção cria o anexo com `fields: {}` e com o conteúdo original do template. Não havia comando de preparação GPT/Gemini entre a seleção e a revisão. O Jev devolvia recomendações; isso não preenchia os campos do anexo.

A validação original de `src/viracase/infrastructure/guided_build.py` bloqueava o envio com marcadores ainda presentes. O bloqueio não deve ser removido: faltava a operação que propõe valores e permite a aprovação humana. O novo fluxo mantém a validação autoritativa no servidor e identifica o anexo e o campo pendentes.

No template legado `autorizacao`, os dois rótulos “Resultado” pertencem a IDs diferentes: `field_8` corresponde a “Sessão expirada” e `field_9` a “Recurso inexistente”. O mapeamento usa o ID e a estrutura da versão autorizada; os IDs e snapshots históricos não são renomeados.

### Observabilidade

Os eventos `recommendation_pending` e `recommendation_ready` informavam o andamento, mas não demonstravam o conteúdo efetivamente transmitido. A nova captura ocorre no adaptador HTTP, sobre o corpo já serializado e a resposta recebida, antes do mapeamento de domínio. O resultado normalizado ocupa uma seção separada. Chamadas sem captura real permanecem identificadas como não capturadas ou indisponíveis.

### Revisão da proposta

O `WorkspaceDialogs.tsx` original renderizava um `<details>` por arquivo, contendo dois `<pre class="source-preview">`. Cada `.source-preview` tinha `max-height: 400px` e `overflow: auto`, dentro de um `dialog` limitado a `90dvh`. O rodapé vinha depois da lista inteira. Essa árvore produzia rolagem do modal e rolagens próprias dos pares de código.

A revisão agora carrega um arquivo por vez em um diálogo dedicado. O conteúdo do diff tem um único contêiner vertical; a navegação lateral possui rolagem separada quando necessária. Cabeçalho e ações ocupam linhas próprias do layout. A revisão base é a versão imutável ligada à proposta, não uma cópia do editor corrente.

### Expurgo em PostgreSQL

O teste real de retenção encontrou um erro também presente na base original: `Worker.purge()` usa o estado `requires_operator_evidence`, de 25 caracteres, mas `purge_inventory.state` era `VARCHAR(24)`. O PostgreSQL recusava a gravação e interrompia essa etapa; o SQLite não reproduz a restrição de comprimento. A coluna passa a comportar 32 caracteres. O nome do estado, a exigência de evidência de backups, os prazos e as regras financeiras permanecem os mesmos.

## Fluxo funcional

1. “Preparar construção” solicita recomendações do Jev. Selecionar zero, um ou vários templates continua sendo uma decisão do usuário.
2. “Preparar templates com IA” salva a revisão atual e solicita uma operação própria de preparação. Abrir um editor, digitar e marcar um template não acionam o provedor de preenchimento.
3. O worker usa o modelo elegível e a política administrativa, reserva o consumo na carteira existente e produz sugestões estruturadas. Essa operação não grava arquivos de projeto nem executa geração, publicação ou execução de código.
4. Sugestões, origens e perguntas aparecem no fluxo atual. Aceitar uma sugestão atualiza apenas campos ainda compatíveis com a revisão e com a instância do anexo. Edições humanas e anexos removidos não são substituídos por respostas tardias.
5. A revisão mostra o conteúdo resolvido. “Enviar para construir” valida novamente campos, permissões e revisão e congela os snapshots efetivamente enviados. A construção possui operação e reserva próprias na mesma franquia.
6. A proposta de código é consultada e revisada antes da aplicação. Selecionar outro arquivo muda a leitura; aplicar confirma o conjunto completo. Fechar preserva a proposta; descartar exige confirmação explícita.

As três etapas existentes, a identidade off-white/azul/navy e o layout IDE/conversa foram mantidos. O estudante não recebe seletor público de modelos nem valores do orçamento interno em reais.

## Contratos efetivos

Os contratos novos usam a autenticação, a sessão, o CSRF e a idempotência existentes. Nenhuma identidade de usuário enviada no corpo substitui a identidade derivada da sessão.

### Plataforma: prefixo `/api/v1`

| Recurso | Contrato implementado |
| --- | --- |
| Preparar | `POST /projects/{pid}/template-preparations`, retorno `202`; exige `expected_draft_revision`, `base_project_revision`, `attachment_ids` e `expected_attachment_revisions`. |
| Consultar | `GET /projects/{pid}/template-preparations` e `GET /projects/{pid}/template-preparations/{id}`; estados e itens pertencem à operação persistida. |
| Responder | `POST /projects/{pid}/template-preparations/{id}/answers`; exige revisão do rascunho, versão do estado e pares `question_id`/`value`. Cria preparação vinculada à anterior, mediante ação explícita e reserva própria. |
| Aceitar campos | `POST /projects/{pid}/template-preparations/{id}/apply`; exige versões esperadas e pares `attachment_id`/`field_id`. Retorna campos aplicados, conflitos e rascunho atualizado. |
| Cancelar | `POST /projects/{pid}/template-preparations/{id}/cancel`; não afirma que uma chamada já enviada deixou de consumir. |
| Rascunho | Rotas existentes de `build-draft`, com metadados aditivos de schema, revisão do anexo, proveniência e erros por campo. |
| Construir | Rota existente de mensagens/construção; snapshots contêm conteúdo resolvido, valores, origem, versão e hash. |
| Ler proposta | `GET /projects/{pid}/proposals/{id}`; retorna versão, base, manifesto, estado, conflitos e verificações efetivamente registradas. |
| Ler arquivo | `GET /projects/{pid}/proposals/{id}/files/{file_id}?proposal_version=...`; o ID é resolvido no manifesto autorizado. Não é um caminho arbitrário de filesystem. |
| Aplicar proposta | Rota existente `POST /projects/{pid}/proposals/{id}/apply`, com `If-Match` e corpo aditivo `expected_proposal_version`/`expected_project_revision`. O corpo legado vazio continua aceito. |

O DTO de preparação distingue `queued`, `filling`, `ready`, `needs_input`, `failed`, `cancelled`, `cancellation_requested`, `reconciling` e `stale`. A infraestrutura reutiliza `Operation`, `Job`, `Attempt`, reservas e ledger existentes; não cria uma segunda carteira ou uma fila independente.

Os campos aceitam exclusivamente texto, booleano, enumeração e lista de textos. A resposta do modelo é validada por IDs, tipos, limites, origens e evidências presentes no contexto autorizado. Marcadores conhecidos ainda não resolvidos bloqueiam o envio; chaves literais que não pertencem ao schema não são tratadas como campos obrigatórios.

Clientes anteriores que salvam o DTO recebido de `GET build-draft` continuam aceitos. Os metadados conhecidos de leitura do anexo são descartados na entrada e recompostos pelo servidor; campos desconhecidos continuam inválidos. As correlações internas persistem no rascunho, mas não são devolvidas como campos editáveis do DTO público. O roundtrip literal GET → PUT, com e sem anexo, e a tentativa de adulterar esses metadados foram testados.

O contexto inclui o pedido, os arquivos permitidos da revisão base, respostas já conhecidas e trechos pertinentes da conversa do mesmo projeto. Descrições anteriores do assistente permanecem sugestões; a aplicação de uma proposta não transforma sua explicação em prova de uma regra de autorização. A seleção de contexto é determinística e limitada. O DTO `context_summary` informa arquivos e mensagens incluídos ou omitidos, e a interface avisa quando o contexto é parcial. Um pedido essencial que exceda o limite é recusado antes da chamada paga.

### Backoffice: prefixo `/admin-api/v1`

| Recurso | Contrato implementado |
| --- | --- |
| Política de preenchimento | `GET` e `PUT /template-preparation-policy`; atualização com versão esperada e justificativa. |
| Captura e retenção | `GET` e `PUT /observability/policy`; política versionada e auditada. |
| Permissão específica | `GET` e `PUT /observability/payload-access`; lista explícita de administradores autorizados, inicialmente vazia. |
| Metadados | `GET /observability/traces`; filtros, cursor cronológico e seções disponíveis, sem carregar todos os payloads. |
| Conteúdo protegido | `GET /observability/traces/{id}/payload?section=request|response|normalized|fields`; reautoriza a leitura. |
| Copiar | `POST /observability/traces/{id}/copy`; reautoriza e registra a cópia separadamente, retornando somente conteúdo sanitizado. |
| Linha do tempo | `GET /observability/traces/{id}/timeline`; liga as etapas pela correlação da jornada. |

O monitor mantém os filtros anteriores, o ciclo único de três segundos, Pausar e Atualizar. A nova visualização de integrações permite consulta por etapa, estado e intervalo. Os JSONs são buscados sob demanda; o payload operacional do provedor não é modificado pelo mecanismo de observabilidade.

## Aplicação de propostas e concorrência

O armazenamento já cria cada revisão como um objeto JSON imutável, com escrita temporária, sincronização e publicação do objeto completo. A aplicação não grava os arquivos individualmente: valida a integridade da revisão candidata e muda o ponteiro da revisão atual dentro da transação existente. A atualização do estado da proposta ocorre na mesma transação.

Para o cliente novo, o rascunho limpo também avança para a revisão aplicada, preservando o próximo pedido e sua proveniência. Um rascunho alterado ou uma revisão concorrente produz conflito, preservando ambos os lados. A revisão identifica os caminhos afetados e oferece a comparação explícita com o estado atual.

O frontend confirma o resultado com o servidor antes de atualizar IDE e histórico. Se a resposta de aplicação for perdida, consulta o estado; não faz um segundo POST automático. A recuperação após recarga lê a revisão persistida. A aplicação continua protegida por idempotência e por verificação do estado já aplicado.

O cálculo do diff ocorre em worker, com limites de trabalho e paginação. O fallback para arquivos extensos mostra blocos completos e informa a opção usada. Nenhuma linha é eliminada silenciosamente. O realce reutiliza os parsers já instalados; JS/TS, Python, HTML, CSS, SQL e JSON têm realce, e outros arquivos permanecem texto. Código exibido é texto escapado e não é executado.

## Configuração administrativa

A preparação fica disponível quando existe modelo elegível na configuração de IA e a política permite a função. O padrão é `enabled=true`, modelo automático entre os elegíveis, contexto máximo de 32.000 bytes, saída máxima de 4.000 tokens e nenhuma reparação automática. O operador pode escolher um modelo já configurado e ajustar os limites pelo backoffice. A ação do usuário continua obrigatória mesmo com a política habilitada.

A captura de payload vem desabilitada. Para utilizá-la:

1. Abrir as configurações de observabilidade no backoffice.
2. Habilitar captura sanitizada e revisar categorias, quotas e retenção.
3. Conceder a permissão de inspeção aos administradores necessários.
4. Salvar com justificativa; a política recebe nova versão.
5. Fazer uma nova operação autorizada. A mudança não recupera corpos de chamadas antigas que não foram capturados.

Os padrões de retenção e volume são sete dias, 32 KiB por payload, 256 KiB por operação e 16 MiB no total. O teto total inclui as seções armazenadas. A sanitização ocorre antes da persistência e mantém somente categorias técnicas permitidas. Valores protegidos, campos desconhecidos e segredos não são gravados como cópia bruta oculta.

A permissão de payload é independente da leitura de metadados. Cada leitura e cópia é auditada sem replicar o conteúdo no registro de auditoria. Desabilitar captura interrompe novas capturas; a eliminação de conteúdo existente obedece à política. Reduzir retenção pode eliminar conteúdo antes disponível; aumentar o prazo não recupera conteúdo eliminado.

Payloads vencidos são removidos fisicamente nas rotinas de limpeza. O monitor existente do worker executa a limpeza periódica a cada hora, inclusive sem consulta administrativa; leituras e novas gravações também verificam expiração. Portanto, a limpeza periódica pode ocorrer até o próximo ciclo após o vencimento. O expurgo de projeto ou conta também remove o conteúdo associado, e uma resposta tardia não pode recriá-lo após a exclusão.

## Migração e atualização

`0007` acrescenta três tabelas: `feature_policies`, `integration_traces` e `integration_trace_payloads`, com unicidade de versão, índices cronológicos e vínculo entre metadados e payloads. Também amplia `purge_inventory.state` de 24 para 32 caracteres para aceitar o estado já utilizado pelo sistema. Não altera tabelas comerciais existentes nem reescreve snapshots históricos. As informações de preparação são persistidas nas operações e rascunhos já existentes.

Ordem de atualização:

1. Sobrepor os arquivos completos em cada um dos três projetos correspondentes.
2. Aplicar as migrations com o procedimento existente. O inicializador Docker já executa `alembic upgrade head`; em um ambiente Python configurado, o comando equivalente é `python -m alembic upgrade head`, na raiz do backend.
3. Atualizar API pública, API administrativa e worker para o mesmo conjunto de arquivos.
4. Construir e atualizar os dois frontends pelos processos já existentes.
5. Conferir a política de preenchimento e habilitar captura/permissões de payload apenas quando desejado.

As credenciais continuam na configuração segura já existente. O pacote não contém chaves de provedor, senhas de usuários, banco de dados, fontes de projetos de usuários ou configuração de infraestrutura de produção.

## Reversão operacional

Primeiro desabilitar novas preparações e novas capturas no backoffice. O preenchimento manual e a construção existente continuam disponíveis. Operações em andamento ou com resultado incerto precisam concluir ou ser conciliadas antes de retirar um worker que compreende `template_preparation`; não reenviar chamadas incertas para tentar encerrar a fila.

Preservar o banco em `0007` e os dados de rascunhos, operações, snapshots e ledger. O arquivo da migration deve permanecer no pacote atual. Reverter o frontend é independente de apagar as tabelas novas. O procedimento recomendado para interromper as funções novas é a reversão por política, mantendo a camada de compatibilidade do backend.

A API da base anterior exige literalmente a versão `0006` em sua verificação de prontidão. Portanto, simplesmente restaurar o binário antigo sobre o banco em `0007` não é um rollback de backend validado. A reversão por política descrita aqui é imediata e preserva os dados; uma reversão integral do binário exige um pacote anterior compatibilizado com o schema ou um plano de restauração previamente homologado. Não alterar a versão registrada no banco para contornar a verificação.

O `downgrade` de `0007` existe para validar o ciclo de migration em laboratório, mas remove as três tabelas novas. Não deve ser usado para uma reversão que precise preservar políticas e rastros. A ampliação de `purge_inventory.state` é mantida no downgrade para não truncar nem impedir a leitura de um estado já registrado. O teste de migration valida que os dados anteriores de conta, carteira, projeto, revisão, rascunho e snapshot permanecem iguais durante upgrade/downgrade/upgrade em um schema PostgreSQL isolado.

Backups e réplicas precisam cumprir a mesma retenção definida pelo operador. O código da aplicação não apaga cópias externas administradas fora deste projeto; a expiração dessas cópias precisa ser configurada no serviço responsável.

## Reprodução e validação

Os testes novos de backend ficam em `tests/test_template_preparations.py`, `tests/test_integration_traces.py`, `tests/test_proposal_review.py` e `tests/test_template_preparation_migrations.py`. A execução com `TEST_DATABASE_URL` apontando para PostgreSQL de teste habilita os casos de concorrência e migration real; nunca usar um banco de produção para essa suíte.

```bash
python -m pytest -q tests/test_template_preparations.py tests/test_integration_traces.py tests/test_proposal_review.py tests/test_template_preparation_migrations.py
```

Os testes de interface incluem preparação, observabilidade, diff, recuperação de carregamento e aplicação da proposta. A suíte de laboratório de construção já existente pode incluir os casos novos sem alterar suas portas:

```bash
python script/test-workspace.py --frontend CAMINHO_DO_FRONTEND tests/template-preparation.integration.spec.ts tests/proposal-apply.spec.ts
```

As fixtures de interface também podem ser executadas separadamente. Na plataforma web, iniciar `npm run dev` em um terminal e executar, em outro terminal na mesma raiz:

```bash
npx playwright test tests/template-preparation.spec.ts tests/proposal-review.spec.ts tests/proposal-diff.spec.ts
```

No backoffice, iniciar o Vite na porta já esperada pela configuração de testes, `npx vite --host 127.0.0.1 --port 5175 --strictPort`, e executar em outro terminal na mesma raiz:

```bash
npx playwright test tests/observability-policy.spec.ts tests/observability-traces.spec.ts
```

Esses comandos pressupõem as dependências e o Chromium do Playwright instalados conforme os projetos existentes. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` continua disponível para apontar para um Chromium já instalado. Não executar dois laboratórios na mesma porta ou sobre o mesmo banco temporário. Os arquivos `*.fixture.html` pertencem aos testes e não são entradas do build de produção.

Chamadas de provider nesses laboratórios usam respostas sintéticas controladas. Os testes da fronteira HTTP exercitam os adaptadores de verdade com transporte HTTP simulado, para comparar o JSON conhecido com o material sanitizado. Isso não constitui uma chamada externa autorizada a Jev, OpenAI, Gemini ou Cloudflare.

Uma verificação real de provedor depende da conexão autorizada e das credenciais da instalação. Nenhuma credencial de produção foi fornecida para esta entrega; esse teste externo não foi executado e não é apresentado como aprovado. A aparência de sintaxe na revisão também não representa compilação ou execução dos projetos gerados; esses estados permanecem explicitamente separados na interface.

## Resultados de validação

Execuções de 28 de outubro de 2026, sobre as bases e a implementação descritas neste documento. As suítes têm escopos sobrepostos: **as contagens não devem ser somadas**. As verificações finais específicas complementam a regressão ampla executada antes dos últimos ajustes de contrato e casos adversariais.

| Escopo | Resultado observado | Alcance |
| --- | --- | --- |
| Backend original imutável, sem testes de container | 582 aprovados; 9 ignorados | Referência da base recebida. |
| Regressão ampla do backend implementado | 615 aprovados; 12 ignorados | Rodada anterior aos últimos ajustes específicos; não apresentada como nova execução integral posterior a eles. |
| Preparação e construção guiada finais no PostgreSQL | 34 aprovados | Inclui GET → PUT com e sem anexo, revisão interna preservada e rejeição de metadados adulterados. |
| Fluxos legados, autenticação e bootstrap no PostgreSQL | 40 aprovados | Preservação dos contratos anteriores. |
| Preparação, migration, concorrência e expurgo no PostgreSQL | 24 aprovados | Inclui o ciclo 0006 → 0007 → 0006 → 0007 e o estado de expurgo com 25 caracteres. |
| Propostas e migration no PostgreSQL | 15 aprovados | Manifesto, integridade, aplicação, conflitos, concorrência, contrato HTTP e preservação de dados. Há sobreposição com a migration da rodada posterior de 24. |
| Traces finais no PostgreSQL | 17 aprovados | Jev, OpenAI e Gemini com adaptadores reais e transporte HTTP simulado; sanitização, autorização, auditoria e retenção. |
| Scripts, migrations e estrutura de Compose | 30 aprovados; 2 ignorados | Imagem Docker real e teste de permissão Unix como usuário não root permanecem fora desta execução. |
| Compilação Python e Ruff seleção F | Aprovados nos 35 arquivos Python da entrega | O arquivo de traces foi recompilado e reverificado após o último teste Jev. Não representa aprovação de todas as regras de lint. |
| Builds da plataforma e do backoffice | Aprovados | Incluem TypeScript e build Vite; dependências e lockfiles preservados. |
| Preparação na plataforma | 19 aprovados | 17 jornadas de interface e 2 verificações diretas de merge/renderização com fixtures. |
| Preparação integrada | 1 aprovado | Catálogo, API, worker sintético, aceite, recarga e snapshot de construção. |
| Diff e interface da revisão | 11 aprovados | 3 testes do algoritmo e 8 testes de interface. O caso reforçado de reflow também passou isoladamente: 1/1. |
| Regressão principal da plataforma | 28/29 aprovados na rodada; caso restante aprovado em repetição isolada 1/1 | Os 17 cenários de publicação passaram na rodada. O teste existente de Aprender encontrou vários links durante a atualização do filtro e passou sem alteração na repetição isolada. |
| Workspace integrado, rodada final limpa | 25 aprovados; nenhum ignorado ou intermitente | Construção guiada (1), prévia/CSP (4), recuperação de aplicação (2) e workspace (18), com API e worker locais da entrega e provedor sintético. |
| Backoffice, rodada final de funcionalidades novas e CSP | 27 aprovados | 26 cenários de interface e 1 verificação sob a política CSP do build. |
| Backoffice, suíte ampla anterior | 57/58 aprovados; caso restante aprovado em repetição isolada 1/1 | A falha foi a espera inicial de um teste de login em 320 px. Não houve alteração da autenticação nem do limite de espera para a repetição. |
| Contratos HTTP administrativos | 16 verificações aprovadas | Leitura/gravação de políticas, conflito de versão, concessão, revogação, recusa de cópia e restauração das configurações do laboratório. |

A verificação adicional de imports registra sete observações I001, quatro já existentes na base. A entrega não inclui reorganização apenas cosmética de imports. A compilação e a seleção F foram concluídas; não há declaração de lint geral aprovado.

Os testes PostgreSQL usam um servidor local real isolado; os testes de frontend usam Chromium. Os cenários de provider não utilizam credenciais de produção. Docker real, limites nativos de sandbox dependentes do host e a integração externa autorizada com Jev/OpenAI/Gemini/Cloudflare permanecem explicitamente não exercitados. Ignorados não são contabilizados como aprovados.

As primeiras tentativas de um laboratório temporário foram interrompidas por configuração de importação e de portas na cópia local do ambiente. Esses ajustes não foram incorporados às fontes de produção; a integração e as jornadas definitivas foram retomadas em bancos temporários limpos. As falhas de contrato efetivamente encontradas, como o roundtrip do DTO de rascunho e o comprimento do estado de expurgo, foram corrigidas e receberam testes específicos.

## Evidências de interface e captura

As evidências abaixo usam dados sintéticos. Os testes e fixtures citados estão integralmente no ZIP, e seus comandos de reprodução constam acima.

| Área | Evidência observada | Teste de reprodução |
| --- | --- | --- |
| Preparação explícita | A seleção de template não emite preparação; as sugestões aparecem separadas dos valores aceitos, e o envio usa o conteúdo revisado. | `plataforma-web/tests/template-preparation.spec.ts` |
| Preparação integrada | API, worker e catálogo locais prepararam os dez campos do anexo de autorização; o aceite persistiu após recarga e o snapshot de construção correspondeu ao anexo revisado. O provedor foi um gateway sintético. | `plataforma-web/tests/template-preparation.integration.spec.ts` |
| Comparação de código | Um arquivo por vez, destaque de sintaxe e diff, arquivo criado/excluído, busca e navegação em arquivo de 1.600 linhas; conteúdo hostil permaneceu texto. | `plataforma-web/tests/proposal-review.spec.ts` |
| Responsividade da revisão | Capturas e verificações em desktop, celular e reflow equivalente a 200% mantiveram o diff e as ações acessíveis. O caso de reflow usou viewport CSS de 720 × 480 e densidade 2; não representa acionamento manual do zoom do navegador. | `plataforma-web/tests/proposal-review.spec.ts` |
| Confirmação de aplicação | Resposta de aplicação perdida depois do commit e recarga durante a confirmação foram recuperadas por consulta, com um único POST e a revisão correta. | `plataforma-web/tests/proposal-apply.spec.ts` |
| Inspeção administrativa | Request, Response, Resultado mapeado e Campos foram consultados separadamente. A linha do tempo manteve a correlação de operação, etapa e tentativa; texto protegido apareceu como conteúdo protegido. | `backoffice/tests/observability-traces.spec.ts` |
| Cópia protegida | A revogação entre leitura e cópia impediu o clipboard; conteúdo alterado após a leitura também não foi copiado como se fosse o exibido. | `backoffice/tests/observability-traces.spec.ts` |
| Fronteira HTTP Jev | O adaptador real foi exercitado com transporte simulado, comparando os bytes enviados e a resposta conhecida com suas capturas protegidas; o resultado normalizado ocupou outro registro. A repetição da mesma intenção manteve uma chamada. | `backend/tests/test_integration_traces.py` |
| Fronteira HTTP OpenAI/Gemini | O transporte simulado recebeu o corpo efetivamente serializado pelo adaptador; a captura preservou os metadados permitidos e o uso de teste, protegendo o texto privado antes de persistir. | `backend/tests/test_integration_traces.py` |

## Relatório de modificações por arquivo

Os caminhos são relativos à pasta de cada projeto no ZIP. Todos os arquivos listados são entregues na íntegra.


### Backend — `backend/`

| Arquivo | Situação | O que foi alterado | Por que foi alterado |
| --- | --- | --- | --- |
| `docs/templates-observabilidade-revisao.md` | Criado | Documenta diagnóstico, fluxos, contratos, políticas, migração, reversão e comandos de validação da entrega. | Orientar implantação, administração e conferência dos novos recursos e de seus limites. |
| `migrations/versions/0007_template_preparation_traces.py` | Criado | Cria políticas versionadas, metadados de integração e payloads separados com índices; amplia o estado de expurgo para 32 caracteres. | Persistir captura e configuração e acomodar estados existentes de retenção sem truncar dados na reversão. |
| `src/viracase/application/progress.py` | Alterado | Adiciona mensagens de preparação, reparo, revisão, perguntas, falha e cancelamento. | Apresentar o progresso real da preparação sem confundi-lo com geração ou aplicação de código. |
| `src/viracase/application/template_preparation.py` | Criado | Define schema, prompt e parser estritos para sugestões, evidências, origens e perguntas por campo. | Aceitar somente campos e fontes autorizados pelo contexto, preservando valores preenchidos e exigindo revisão humana. |
| `src/viracase/domain/rules.py` | Alterado | Acrescenta detalhes estruturados opcionais a RuleError. | Transportar pendências precisas por anexo e campo sem mudar os códigos e mensagens existentes. |
| `src/viracase/domain/template_fields.py` | Criado | Centraliza normalização de campos legados, tipos, limites, marcadores declarados, renderização e proveniência. | Dar identidade semântica aos campos e validar anexos de modo consistente sem bloquear chaves literais. |
| `src/viracase/infrastructure/conversation_generation.py` | Alterado | Envolve chamadas de construção em captura observada e registra resultados normalizados por lote. | Relacionar intercâmbios reais do provedor e seus resultados à operação de construção correspondente. |
| `src/viracase/infrastructure/drafts.py` | Alterado | Valida integridade e versões antes de aplicar propostas e atualiza o rascunho na mesma transação, mantendo idempotência. | Evitar aplicação sobre edições concorrentes, duplicação de efeitos e dessincronização do rascunho. |
| `src/viracase/infrastructure/feature_policies.py` | Criado | Implementa leitura e gravação versionada de preparação, captura e concessões de payload, com validação, bloqueio e auditoria. | Administrar modelos, limites e permissões explícitas sem habilitar captura ou acesso sensível por padrão. |
| `src/viracase/infrastructure/guided_build.py` | Alterado | Valida campos tipados, preserva identidade e revisão de anexos, registra edições e congela valores e proveniência no envio. | Construir a partir do conteúdo efetivamente revisado e apontar pendências sem perder alterações do usuário. |
| `src/viracase/infrastructure/integration_traces.py` | Criado | Captura intercâmbios HTTP reais e resultados em seções separadas, com correlação, proteção, quotas, expurgo e leitura/cópia auditadas. | Oferecer diagnóstico verificável sem reconstruir payloads ausentes, expor segredos ou contornar permissões revogadas. |
| `src/viracase/infrastructure/models.py` | Alterado | Adiciona modelos de políticas, traces e payloads e amplia o campo de estado do inventário de expurgo. | Alinhar o ORM à migração 0007 e separar metadados consultáveis do conteúdo protegido. |
| `src/viracase/infrastructure/observability.py` | Alterado | Inclui preparação nos eventos, filtros por etapa, estado e período e expurgo periódico de payloads vencidos. | Preservar o monitor atual enquanto permite acompanhar a nova jornada e cumprir retenção sem acesso administrativo. |
| `src/viracase/infrastructure/preparation_worker.py` | Criado | Executa preparação com política e contexto congelados, reparo limitado, consumo real, cancelamento e reconciliação de incerteza. | Produzir sugestões revisáveis sem gerar código, repetir chamadas incertas ou aplicar respostas tardias automaticamente. |
| `src/viracase/infrastructure/project_deletion.py` | Alterado | Inclui o expurgo de traces e payloads no encerramento da exclusão do projeto. | Impedir que conteúdo de observabilidade permaneça disponível após a exclusão correspondente. |
| `src/viracase/infrastructure/proposal_review.py` | Criado | Fornece manifesto autoritativo, arquivos sob demanda, versão da proposta, conflitos e validação de integridade para aplicação. | Comparar as revisões corretas e impedir aplicação quando a base, o rascunho ou os arquivos mudaram. |
| `src/viracase/infrastructure/providers.py` | Alterado | Acrescenta o contrato estruturado de preparação e hooks de request, response, falha e tentativa de compatibilidade. | Reutilizar os provedores existentes e registrar o tráfego efetivamente enviado e recebido em cada tentativa. |
| `src/viracase/infrastructure/recommendations.py` | Alterado | Instrumenta o HTTP real do Jev, limita a leitura da resposta e registra resultado mapeado com telemetria validada. | Diagnosticar recomendações e falhas sem expor valores arbitrários retornados como metadados. |
| `src/viracase/infrastructure/repository.py` | Alterado | Integra preparação à reserva e fila idempotentes e registra os campos do snapshot ao iniciar construção. | Reutilizar controles financeiros e de concorrência e correlacionar o conteúdo aceito com a geração. |
| `src/viracase/infrastructure/repository_dto.py` | Alterado | Resume operações de preparação em estado e quantidade de anexos no DTO geral. | Evitar duplicar sugestões e conteúdo sensível fora do endpoint específico de preparação. |
| `src/viracase/infrastructure/template_catalog.py` | Alterado | Publica schema versionado, campos normalizados e mapeamento semântico dos identificadores legados. | Permitir edição e preenchimento tipados mantendo compatibilidade com templates já cadastrados. |
| `src/viracase/infrastructure/template_preparations.py` | Criado | Implementa capacidade, início, consultas, perguntas, aceite por campo e cancelamento com snapshots, versões e verificação de contexto. | Separar preparação da construção e preservar edições humanas, isolamento de conta e intenção explícita em cada aplicação. |
| `src/viracase/infrastructure/worker.py` | Alterado | Encaminha operações de preparação ao executor, instrumenta geração e inclui traces no expurgo de conta. | Integrar o novo fluxo ao ciclo de execução e retenção já existente. |
| `src/viracase/interfaces/api.py` | Alterado | Monta rotas novas, expõe capacidade de preparação, aceita guardas opcionais de proposta, transmite detalhes e exige schema 0007. | Disponibilizar os contratos de ponta a ponta preservando autenticação e compatibilidade das chamadas existentes. |
| `src/viracase/interfaces/feature_schemas.py` | Alterado | Aceita valores estritamente tipados, ignora metadados de anexo derivados pelo servidor e delega validação do schema ao domínio. | Evitar coerções e permitir salvar rascunhos retornados pela API sem aceitar metadados como autoridade do cliente. |
| `src/viracase/interfaces/preparation_routes.py` | Criado | Adiciona rotas públicas de preparação e revisão e rotas administrativas de políticas, traces, timeline, payload e cópia. | Separar ações de usuário, configuração e inspeção protegida usando os controles existentes de acesso e comandos. |
| `src/viracase/interfaces/preparation_schemas.py` | Criado | Define entradas restritas para preparação, respostas, aceites, políticas, permissões, cópia e guardas de proposta. | Validar identificadores, versões, motivos e limites antes de executar as operações. |
| `tests/conftest.py` | Alterado | Atualiza a versão de schema declarada no banco de testes para 0007. | Manter o ambiente de testes coerente com os modelos e a verificação de prontidão da API. |
| `tests/test_container_migrations.py` | Alterado | Atualiza a revisão esperada para 0007 e restringe a inspeção de chmod ao comando de permissões de /app/src. | Verificar a migração atual e evitar confundir a preparação de diretórios com a regra de acesso ao código. |
| `tests/test_guided_build.py` | Alterado | Usa um marcador declarado no cenário de anexo incompleto. | Verificar a pendência real do schema sem tratar qualquer conteúdo entre chaves como campo obrigatório. |
| `tests/test_integration_migration.py` | Alterado | Atualiza para 0007 a revisão esperada após a migração das integrações. | Continuar verificando preservação das integrações com a nova revisão do banco. |
| `tests/test_integration_traces.py` | Criado | Cobre fronteiras HTTP de Jev, OpenAI e Gemini, proteção recursiva, permissão e cópia, auditoria, quotas, expurgo, respostas inválidas e tentativas. | Verificar a fidelidade dos registros e os controles que impedem vazamento ou recriação de conteúdo eliminado. |
| `tests/test_proposal_review.py` | Criado | Cobre manifesto, leitura sob demanda, integridade, isolamento, conflitos, aplicação atômica e idempotente e contrato HTTP. | Proteger revisões e rascunhos contra aplicação incorreta, concorrência e acesso entre contas. |
| `tests/test_template_preparation_migrations.py` | Criado | Adiciona teste de upgrade e downgrade em PostgreSQL com projetos, rascunhos e histórico financeiro existentes. | Verificar preservação dos dados anteriores durante instalação e reversão da migração. |
| `tests/test_template_preparations.py` | Criado | Cobre preparação, cobrança, perguntas, aceite, concorrência, cancelamento, respostas tardias, schemas e contratos HTTP. | Verificar o ciclo completo sem aplicação automática e sem perder dados, permissões ou controle de consumo. |
| `tests/workspace_server.py` | Alterado | Atualiza o laboratório para schema 0007 e acrescenta resposta sintética específica para preparação. | Executar jornadas integradas locais de preparação mantendo os cenários existentes de construção. |


### Plataforma web — `plataforma-web/`

| Arquivo | Situação | O que foi alterado | Por que foi alterado |
| --- | --- | --- | --- |
| `src/features/BuildAttachments.tsx` | Alterado | Exibe pendências por campo com ação para edição focada, além do estado pronto para revisão. | Identificar exatamente o que impede construir e como corrigir. |
| `src/features/BuildConversation.tsx` | Alterado | Resume arquivos criados, alterados e excluídos, mostra descarte e recolhe explicações longas, direcionando código para a revisão. | Manter a conversa legível e tornar o estado e o alcance da proposta claros antes da inspeção. |
| `src/features/GuidedBuildPanel.tsx` | Alterado | Integra preparação explícita ao fluxo de 3 etapas; carrega schema da versão anexada, valida campos antes do envio, integra merge por campo e mantém modais fora do painel ocultável. | Preparar, revisar e editar sem gerar código ou perder estado ao mudar de viewport. |
| `src/features/GuidedTemplateCard.tsx` | Alterado | Atualiza apresentação dos anexos adaptados sem atribuir automaticamente autoria humana. | Distinguir conteúdo selecionado/adaptado mantendo o checkbox sem chamada de IA. |
| `src/features/ProjectWorkspace.tsx` | Alterado | Carrega o manifesto de proposta, coordena revisão e descarte e confirma aplicação com versões, consulta de estado e sincronização do rascunho. | Preservar edições concorrentes e recuperar confirmações perdidas sem repetir aplicação ou misturar projetos. |
| `src/features/ProposalDiffView.tsx` | Criado | Exibe diff unificado ou lado a lado, paginado, pesquisável e navegável por alteração, com cálculo em worker. | Revisar arquivos extensos com conteúdo preservado, leitura responsiva e menor bloqueio da interface. |
| `src/features/ProposalReview.tsx` | Criado | Cria revisão com lista pesquisável de arquivos, carregamento sob demanda, verificações, conflitos e confirmação de aplicar ou descartar. | Permitir inspeção focada de um arquivo por vez e aprovação explícita de toda a proposta, preservando foco e posição. |
| `src/features/TemplateEditor.tsx` | Alterado | Editor tipado com origens, sugestões individuais, comparação, corpo manual preservado e foco inicial acessível. | Revisar e aceitar sugestões explicitamente, mantendo alterações próprias e atualização correta ao reabrir. |
| `src/features/TemplateFieldInput.tsx` | Criado | Renderiza controles para texto, booleano, enum e lista com labels e limites do schema. | Aceitar tipos reais, inclusive booleano falso, sem coerção indevida. |
| `src/features/TemplatePreparationPanel.tsx` | Criado | Painel com ação explícita, estados reais por item, origem, comparação, perguntas, aceites válidos, cancelamento/refresh e aviso de contexto parcial. | Tornar a preparação revisável, transparente e separada da geração/aplicação de código. |
| `src/features/WorkspaceDialogs.tsx` | Alterado | Substitui a comparação expandida de todos os arquivos pelo componente de revisão e guarda a posição por proposta. | Integrar a revisão ao workspace e reabrir no mesmo ponto sem descartar a proposta ao fechar. |
| `src/features/guided.css` | Alterado | Estilos isolados para preparação/editor, ações legíveis, modal responsivo e rodapé acessível. | Manter identidade visual existente e leitura/uso em coluna estreita e celular. |
| `src/features/guidedTypes.ts` | Alterado | Acrescenta schemas e valores tipados/proveniência opcionais; payload de autosave explicita somente os campos editáveis. | Preservar o contrato legado e deixar o servidor derivar metadados. |
| `src/features/mergeBuildDraft.ts` | Criado | Merge de três vias por campo e por anexo com conflito explícito em alterações incompatíveis. | Combinar edições distintas sem sobrescrever o mesmo campo ou corpo manual. |
| `src/features/proposalDiff.ts` | Criado | Implementa diff por linhas, contagens, contexto recolhível e cálculo limitado com alternativa por blocos. | Preservar conteúdo e numeração de ambas as revisões sem custo de cálculo ilimitado. |
| `src/features/proposalDiff.worker.ts` | Criado | Calcula diferenças e realce sintático com parsers existentes fora da thread principal, com retorno textual em falhas de realce. | Manter a interface responsiva e apresentar código sem executar HTML ou scripts do arquivo. |
| `src/features/proposalReview.css` | Criado | Define layout, cores de diff, navegação, área de código e ações do diálogo em desktop e celular. | Manter a identidade visual, leitura e ações acessíveis com uma área principal de rolagem. |
| `src/features/proposalTypes.ts` | Criado | Tipa manifesto, conteúdo por arquivo, estados, verificações, conflitos e posição da revisão. | Alinhar a interface ao contrato autoritativo e preservar a navegação durante a inspeção. |
| `src/features/templateFields.ts` | Criado | Validação e renderização determinísticas de marcadores declarados e valores tipados. | Bloquear pendências reais e preservar conteúdo literal/whitespace conforme backend. |
| `src/features/templatePreparationTypes.ts` | Criado | DTOs tipados para estados, sugestões, perguntas, aplicação e contexto parcial. | Manter alinhamento com contrato backend e compatibilidade de respostas antigas. |
| `src/features/useTemplatePreparation.ts` | Criado | Consulta retomável com polling de estados ativos, retenção em falhas transitórias e guardas de projeto/versão/desmontagem. | Acompanhar estado real após refresh sem duplicar chamadas pagas ou misturar projetos. |
| `src/features/useTemplatePreparationFlow.ts` | Criado | Orquestra ações explícitas com snapshot atual, versões esperadas, flush, proteção de escopo e recuperação de confirmação incerta. | Impedir respostas tardias de substituir edição e evitar reenvio automático em falha de rede. |
| `src/shared/api.ts` | Alterado | Acrescenta details opcional em ApiError, preservando transporte e comandos existentes. | Permitir pendências autoritativas por campo sem alterar autenticação, CSRF ou idempotência. |
| `src/shared/types.ts` | Alterado | Acrescenta capacidade opcional de preparação e alteração de arquivo do tipo excluído. | Representar de forma aditiva os contratos de preparação e revisão de propostas. |
| `tests/proposal-apply.spec.ts` | Criado | Adiciona cenários de resposta perdida após commit e recarga durante a confirmação da aplicação. | Verificar recuperação pelo estado do servidor sem um segundo comando de aplicação. |
| `tests/proposal-diff.spec.ts` | Criado | Cobre reconstrução integral das revisões, numeração, alterações pequenas e limite de cálculo em arquivos extensos. | Verificar que otimizações e comparação por blocos não omitem conteúdo. |
| `tests/proposal-review.fixture.html` | Criado | Adiciona a página de entrada isolada do laboratório de revisão. | Carregar o componente para testes de interface sem modificar a aplicação de produção. |
| `tests/proposal-review.fixture.tsx` | Criado | Monta propostas sintéticas com múltiplas linguagens, arquivos longos, estados e ações controláveis. | Reproduzir navegação, conflitos e falhas da revisão sem dependência de geração externa. |
| `tests/proposal-review.spec.ts` | Criado | Cobre revisão por arquivo, fechamento e foco, realce seguro, busca, paginação, responsividade, conflitos e recuperação de leitura. | Verificar a usabilidade e os bloqueios de aplicação diante de conteúdo hostil ou consulta incompleta. |
| `tests/template-preparation.integration.spec.ts` | Criado | Jornada local real de catálogo→worker sintético→aceite→recarga→snapshot de construção. | Verificar integração frontend/backend e separação entre preparação, geração e aplicação. |
| `tests/template-preparation.spec.ts` | Criado | Cobre preparo e aceite explícitos, edição manual, tipos, perguntas, concorrência, falhas, recarga e acessibilidade. | Verificar os novos riscos do fluxo e preservar os caminhos manuais e sem anexos. |
| `tests/templatePreparationFixture.ts` | Criado | Fixture de API estrita com estados controláveis, metadados e falhas de rede/concorrência. | Validar o contrato da interface deterministicamente sem provedor externo. |
| `tests/workspace.spec.ts` | Alterado | Ajusta o botão de fechamento e o cenário de falha transitória para a revisão com manifesto e arquivos sob demanda. | Manter a cobertura da recuperação da IDE no novo fluxo, sem depender da antiga leitura antecipada de toda a revisão. |


### Backoffice — `backoffice/`

| Arquivo | Situação | O que foi alterado | Por que foi alterado |
| --- | --- | --- | --- |
| `src/admin-policy-editor.ts` | Criado | Centraliza rascunho, motivo, versão, gravação idempotente e preservação de edições em conflitos de política. | Compartilhar controles de configuração sem perder alterações nem duplicar envios. |
| `src/integration-trace-detail.tsx` | Criado | Mostra timeline e contexto e carrega Request, Response, Resultado e Campos sob demanda, correlacionando somente registros da mesma tentativa. | Inspecionar dados realmente disponíveis sem misturar operações ou esconder a separação entre HTTP e resultado normalizado. |
| `src/main.tsx` | Alterado | Inclui a configuração de preparação junto à configuração de IA existente. | Disponibilizar a nova política no backoffice sem alterar navegação, autenticação ou integrações atuais. |
| `src/observability-policy.tsx` | Criado | Adiciona editores versionados de captura, retenção, categorias, quotas e concessão explícita de acesso a payloads. | Administrar exposição e armazenamento de dados com motivo e preservação dos rascunhos entre abas. |
| `src/observability-types.ts` | Criado | Define DTOs e rótulos de traces, fases, seções, campos, estados de captura e motivos controlados de indisponibilidade. | Renderizar o contrato real sem inferir dados ausentes, truncados, expirados ou não capturados. |
| `src/observability.css` | Alterado | Acrescenta estilos do monitor, detalhe, timeline, JSON protegido, comparação de campos e formulários de política. | Organizar a informação em telas estreitas preservando a identidade visual e os estilos existentes. |
| `src/observability.tsx` | Alterado | Adiciona modo de integrações e payloads, filtros de etapa, estado e período, paginação e acesso aos detalhes e políticas. | Acompanhar a jornada completa preservando eventos, filtros anteriores, pausa e atualização a cada três segundos. |
| `src/preparation-settings.tsx` | Criado | Adiciona painel e editor da flag de preparação, modelo habilitado, conexão informativa e limites de contexto, saída e reparo. | Controlar a preparação por configuração versionada sem expor escolha de provedor ao usuário final. |
| `src/protected-json.tsx` | Criado | Renderiza JSON como texto, com busca e navegação, e copia somente conteúdo fresco autorizado e igual ao exibido. | Facilitar inspeção sem executar conteúdo ou copiar dados após revogação, alteração ou falha de autorização. |
| `tests/observability-policy.spec.ts` | Criado | Cobre políticas, concessão e revogação, versões, limites, edições preservadas, bloqueio de duplicidade e acessibilidade. | Verificar a configuração administrativa e seus estados de conflito e navegação. |
| `tests/observability-test-data.ts` | Criado | Fornece fixtures de metadados, timelines, payloads protegidos, políticas e rotas de teste. | Reproduzir contratos e estados do monitor sem consultar provedores externos. |
| `tests/observability-traces.spec.ts` | Criado | Cobre filtros, pausa, carregamento sob demanda, cópia autorizada, estados de captura, correlação, falhas e renderização segura. | Verificar fidelidade dos dados, isolamento entre tentativas e preservação do comportamento do monitor. |
| `tests/observability.fixture.html` | Criado | Adiciona a página isolada de entrada do laboratório de observabilidade. | Executar testes da interface sem incluir o laboratório na aplicação de produção. |
| `tests/observability.fixture.tsx` | Criado | Monta monitor ou configuração de preparação com sessão sintética conforme o cenário. | Exercitar os componentes administrativos e seus estados sem alterar autenticação real. |

