# Integrações e templates: múltiplas conexões

Base desta entrega: backend V2.0.0.27 e backoffice v2.0.0.18 recebidos em 27/10/2026. O patch contém arquivos integrais alterados/criados. Não inclui frontend público nem alterações de autenticação, pagamentos, credenciais de IA, Dockerfile ou dependências.

## O que muda

O cadastro anterior reunia todos os campos na página e tratava cada provedor como uma configuração única. A nova interface apresenta tabelas com busca, filtros, paginação de 10 registros e modais próprios para cadastro, edição e gerenciamento. O catálogo de templates também usa tabela e modal; os campos pessoais são cadastrados por formulário, sem editar JSON. Estados, erros, confirmações de descarte e navegação por teclado foram implementados preservando a identidade azul existente.

Cada conexão Cloudflare/Jev tem uma identidade estável, nome único por provedor, credencial própria e versões independentes de configuração. A numeração das versões continua global por provedor para preservar os contratos antigos; por isso uma conexão pode apresentar versões não consecutivas. Salvar uma conexão invalida somente a versão ativa dessa conexão. Token vazio na edição preserva somente o segredo da própria conexão; no cadastro Cloudflare, token é obrigatório. O backoffice nunca recebe o token armazenado.

Existe uma conexão padrão por provedor. A primeira conexão se torna padrão, mas permanece rascunho até a validação/ativação. As demais só podem ser escolhidas como padrão depois de ativadas; Cloudflare exige validação vigente. A alteração é explícita, versionada e auditada. Não há alternância automática de contas.

- Cloudflare: o padrão atende a primeira publicação de novos projetos. Projetos com inventário continuam vinculados à conexão original, incluindo execução, rotação da credencial, reconciliação, suspensão e expurgo. Limites de concorrência/capacidade são considerados por conta Cloudflare. Alterar o Account ID de conexão que possui inventário é recusado; cadastre outra conexão para outra conta.
- Jev: o padrão fornece os limites locais do fluxo guiado. As chamadas ao recomendador também exigem versão ativa, token e habilitação. Salvar limites locais mantém o comportamento anterior de aplicação imediata no fluxo. A validação Jev é local e não chama o provedor.
- Templates: criar/editar produz uma nova versão; desativar bloqueia novos envios, preservando os conteúdos já enviados. O identificador permanece imutável; campos pessoais têm chave, nome exibido, obrigatoriedade e limite. Conflito de versão não sobrescreve uma edição concorrente.

Todas as configurações de conexões continuam no banco, administradas pelo backoffice. Não foram criadas variáveis de ambiente de integração, contas fixas ou segredos no código.

## Instalação na versão recebida

1. Compare os hashes anteriores do `MANIFESTO_SHA256.json` com os arquivos locais. Caso haja alterações posteriores às bases recebidas, faça merge dos arquivos completos antes de aplicar. Preserve banco, fontes, chave de credenciais e configurações de infraestrutura existentes em backup recuperável.
2. Copie o conteúdo de `backend/` e `backoffice/` do ZIP para as raízes dos respectivos projetos. Não substitua diretórios inteiros e não apague arquivos ausentes do patch.
3. Conclua publicações em andamento antes de trocar a versão. Registre operações incertas para reconciliação; não repita efeitos externos nem limpe o inventário. Não execute código antigo e novo escrevendo integrações simultaneamente durante a migração.
4. No backend, use o fluxo Docker já existente. Em janela de manutenção, depois de preparar o build, pare APIs/worker antigos e execute o inicializador com a imagem nova. Exemplo, na raiz do backend:

   ```bash
   docker compose build
   docker compose stop public-api admin-api worker
   docker compose run --rm initialize
   bash script/run-local.sh
   docker compose exec admin-api python -m alembic current
   ```

   O inicializador existente aplica migrations, permissões e bootstrap conforme a configuração já instalada. O head esperado agora é **0005**. Se a migração/inicialização falhar, mantenha as APIs paradas e trate o erro antes de continuar. Nunca use `alembic stamp` para simular migração executada. Os comandos Docker acima foram revisados contra os scripts recebidos, mas não executados neste ambiente, que não dispõe de Docker.
5. Suba public-api, admin-api e worker da mesma entrega e verifique `/health/ready`. Depois recompile/suba o backoffice pelo procedimento existente. O frontend público não precisa ser substituído por este patch.
6. Confira as conexões migradas no backoffice, os nomes/padrões, o catálogo e as permissões administrativas. Homologue no PostgreSQL/host de destino antes de liberar a operação normal.

A migration 0005 é aditiva: cria `integration_connections` e `integration_version_bindings`. Configurações existentes são agrupadas em `Cloudflare principal` e/ou `Jev principal`, sem recriptografar credenciais, modificar IDs de configuração, apagar versões ou mudar inventário. Provedor ainda não configurado permanece sem conexão. O teste de migração compara todos os registros das tabelas anteriores antes/depois, verifica integridade referencial e descriptografia com a chave original.

## Operação pelo backoffice

1. Abra **Integrações e templates**, selecione Cloudflare ou Fluxo guiado e Jev e use **Nova conexão**.
2. Preencha o nome, os parâmetros reais, a credencial e o motivo. O cadastro permanece rascunho.
3. Na linha da conta, use **Gerenciar**. Selecione **Validar conexão**, informe o motivo e execute. Cloudflare faz consultas de verificação; Jev valida os parâmetros locais.
4. Selecione **Ativar conexão** após validação. Para outra conta receber novas operações, escolha **Definir como padrão** e registre o motivo.
5. Cloudflare mantém os requisitos existentes de publicação: a ativação sozinha não libera deploy. O relatório de qualificação da versão fica em **Gerenciar → Relatório de qualificação da publicação**. Os identificadores/digests exibidos pertencem à conexão selecionada. Siga `PUBLICACAO.md` para evidências e critérios.
6. Use **Editar** para alterar nome/parâmetros/credencial. Salve a nova versão e valide/ative novamente; quando aplicável, registre a qualificação correspondente. **Desativar conexão** bloqueia novas operações dessa conexão sem apagar projetos hospedados.
7. Em **Templates → Novo template**, preencha identificação, conteúdo e campos pessoais opcionais. Use `{{chave}}` no conteúdo. A edição de um template existente cria a próxima versão e preserva o identificador.

**Qualificação após esta atualização:** o código de seleção da conexão no worker/lifecycle mudou, alterando o fingerprint do executor coberto pela política existente. Relatórios anteriores não comprovam o executor novo. A atualização não apaga nem suspende publicações remotas existentes, mas novos deploys/atualizações precisam de qualificação válida para a versão implantada. Não foram afrouxados os bloqueios de segurança nem produzidas evidências reais de Cloudflare.

## Compatibilidade das APIs

Todos os endpoints anteriores `/admin-api/v1/integrations/{provider}` e suas ações continuam disponíveis e operam a conexão padrão. Hashes de idempotência dos comandos legados foram preservados. Templates conservam as mesmas rotas/contratos.

| Método | Nova rota administrativa | Função |
|---|---|---|
| GET / POST | `/integrations/{provider}/connections` | Listar / cadastrar conexão |
| GET / PUT | `/integrations/{provider}/connections/{id}` | Consultar / criar nova versão da conexão |
| POST | `/integrations/{provider}/connections/{id}/validate` | Validar versão selecionada |
| POST | `/integrations/{provider}/connections/{id}/activate` | Ativar versão validada |
| POST | `/integrations/{provider}/connections/{id}/disable` | Desativar a conexão selecionada |
| POST | `/integrations/{provider}/connections/{id}/default` | Escolher a conexão padrão |
| POST | `/integration-connections/{id}/qualification` | Registrar evidência da versão selecionada |

Prefixo de todas as rotas: `/admin-api/v1`. `provider` admite `cloudflare` e `jev`. Mantidos sessão administrativa, verificação de origem, CSRF, motivo, controle de versão e idempotência nos comandos aplicáveis. As rotas não são montadas na API pública. Consulta/listagem não retorna credencial. O GET de uma conexão de outro provedor é recusado.

## Evidências e limites

- Backend: **335 testes aprovados, 3 ignorados condicionalmente**, com `VIRA_TEST_BUILD_TOOLCHAIN=1 python -m pytest -q -rs`. Inclui instalação limpa real do toolchain npm já existente, preservação da migração, múltiplas credenciais, atualização isolada, escolha do padrão, limites por conta, execução/reconciliação/lifecycle vinculados e compatibilidade das rotas antigas.
- Backoffice: **12 testes Playwright aprovados** com APIs reais em SQLite temporário: 8 jornadas anteriores e 4 cenários novos de integrações/templates. Chamadas externas de Cloudflare/Jev não foram realizadas. O teste de erro de edição usa resposta HTTP 412 simulada; gravação e recarga dos cadastros usam o backend real.
- Build TypeScript/Vite de produção aprovado; Ruff aprovado nos arquivos Python alterados. Inspeção visual interna de tabela e modais desktop/mobile concluída. Verificados larguras de 320, 375, 640, 768 e 1440 pixels, foco inicial/restrito ao modal, retorno de foco, Escape e preservação de dados no cancelamento. O teste em 640 pixels verifica reflow equivalente à largura útil de uma tela de 1280 pixels a 200%; não é uma certificação de acessibilidade nem ensaio de zoom nativo em todos os navegadores.
- A suíte recebida possuía uma asserção intermitente: procurava uma operação específica apenas na primeira página administrativa ordenada por UUID. O teste foi ajustado para filtrar as operações ativas antes de verificar o rótulo; a lógica de produção de operações não foi modificada.
- Pendentes no destino: imagem Docker real, concorrência/constraints no PostgreSQL real, integração real com os provedores e publicação/qualificação no host. A migration foi executada em SQLite e teve SQL PostgreSQL gerado em modo offline; isto não substitui execução em PostgreSQL.
- Permanecem os avisos preexistentes de depreciação Starlette/httpx nos testes e diretivas `use client` de lucide-react no build; eles não impediram as verificações.

Não há garantia matemática de regressão zero. A evidência acima delimita o que foi executado; não foi feito deploy na instalação do usuário.

## Reversão

Não reverta o backend para a versão anterior após cadastrar múltiplas conexões: o código antigo escolheria a última versão global e não compreenderia os vínculos. O downgrade automático da migration é bloqueado para não apagar conexões ou produzir divergência de schema.

Para recuar apenas a interface, mantenha backend/worker/schema 0005 e use o backoffice anterior somente após homologação; ele gerencia exclusivamente a conexão padrão. Para reverter backend/schema, interrompa novas escritas e restaure o conjunto coerente de banco, chave, fontes e imagens do backup anterior, reconciliando separadamente os efeitos externos posteriores. Não baixe apenas o marcador Alembic nem exclua tabelas manualmente.

## Próximo aprimoramento possível

Fora deste patch: automatizar em CI a migração e os cenários de concorrência com PostgreSQL/Docker reais. Isso acrescentaria evidência ao ambiente de destino sem ampliar as funcionalidades solicitadas aqui.
