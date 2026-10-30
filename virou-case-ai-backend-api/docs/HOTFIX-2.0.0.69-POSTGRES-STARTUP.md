# Hotfix 2.0.0.69 — inicialização e credenciais do PostgreSQL

Base: `virou-case-ai-backend-api-v2.0.0.68.zip`.
Data: 30/10/2026.

## Diagnóstico demonstrado

O log anexado mostra build concluído, PostgreSQL saudável e falha do serviço `initialize` ao abrir a conexão administrativa usada pelo Alembic:

```text
FATAL: password authentication failed for user "postgres"
```

A falha ocorre antes de executar as migrations. A conexão chegou ao PostgreSQL, que rejeitou a credencial. O código cria o usuário da aplicação em `script/init-db.sh` na primeira inicialização do volume, mas não reconcilia as credenciais de um volume existente com a configuração efetiva do Compose nas próximas execuções. Recriar containers e alterar variáveis de ambiente não atualiza, por si só, a senha já armazenada no banco. O healthcheck existente usa `pg_isready`, sem comprovar que as credenciais das duas contas funcionam.

Não foi possível inspecionar o volume ou o `.env` do computador de origem; portanto, não é possível afirmar quando ou por qual ação surgiu a divergência. A rejeição de autenticação está comprovada pelo log, e a recuperação dessa condição foi reproduzida com PostgreSQL 17 real.

## Aplicação

1. Extraia o conteúdo deste ZIP na raiz do backend **v2.0.0.68**, substituindo os arquivos correspondentes e mantendo as pastas relativas.
2. Execute o comando habitual, na raiz do backend:

```bash
./script/run-local.sh
```

Este é um pacote incremental: não é uma distribuição completa para uma pasta vazia. Ele não inclui `.env`, senhas, volumes, fontes de projetos ou chaves de criptografia.

O script usa as credenciais efetivas do Compose, incluindo a precedência normal de variáveis exportadas sobre o `.env`. Não é necessário digitar as senhas novamente nem executar SQL manualmente.

## Comportamento após a correção

1. O build continua sendo executado antes da aplicação das imagens.
2. O launcher inicia e aguarda apenas o serviço `postgres`.
3. Dentro desse serviço, como usuário de sistema `postgres`, verifica as contas `postgres` e `viracase` por TCP.
4. Se ambas funcionarem, não altera nenhuma senha.
5. Se houver divergência, usa o socket administrativo local e uma transação para alinhar somente as credenciais necessárias. Se `viracase` estiver ausente, cria esse usuário com os mesmos privilégios básicos da inicialização original, sem superusuário.
6. Verifica novamente ambas as conexões por TCP. A falha bloqueia o restante da subida.
7. O inicializador também verifica suas próprias conexões antes do Alembic. Depois continua com as migrations, permissões e bootstrap já existentes.

Mensagens esperadas na etapa de banco:

```text
DATABASE_CREDENTIALS_OK
```

ou, quando houve recuperação:

```text
DATABASE_CREDENTIALS_RECONCILING
DATABASE_CREDENTIALS_RECONCILED
```

Essas mensagens confirmam a etapa de credenciais, não o sucesso das etapas posteriores ou da publicação na Cloudflare. A mensagem final de inicialização continua dependendo das verificações existentes.

## Diff lógico completo

| Arquivo | Alteração | Motivo |
| --- | --- | --- |
| `script/run-docker.sh` | Alterado: prepara o PostgreSQL e executa a reconciliação antes do `up` completo; interrompe a subida se essa etapa falhar. | Impedir que as migrations e a recriação das APIs avancem com credenciais divergentes. |
| `script/reconcile-db-credentials.sh` | Criado: valida duas contas, reconcilia senhas por socket local em transação, cria somente a conta da aplicação quando ausente e confirma autenticação TCP. | Recuperar volumes existentes preservando dados e atributos das contas já existentes. |
| `script/container-initialize.py` | Alterado: verifica as duas conexões antes das migrations e informa erros sanitizados de autenticação, banco inexistente e conectividade; normaliza a ordem de imports. | Identificar a etapa bloqueada sem depender de um traceback longo. |
| `tests/test_database_startup.py` | Criado: 12 casos de ordem de inicialização, bloqueio em falha, configuração incompleta, diagnóstico e sanitização. | Proteger o novo fluxo contra regressões. |
| `tests/test_database_startup_postgres.py` | Criado: 7 cenários com cluster PostgreSQL isolado. | Exercitar autenticação, reconciliação, dados existentes, idempotência e inicializador real. |
| `tests/publication_deployment_support.py` | Alterado: adapta o simulador de Docker à preparação separada do PostgreSQL e à execução do novo script. | Distinguir falhas de banco das falhas posteriores das APIs e do executor. |
| `tests/test_publication_deployment.py` | Alterado: mantém a proibição de executar o worker com perfil AppArmor incorreto, admitindo a preparação prévia do banco. | Preservar o controle de segurança no novo fluxo. |
| `tests/test_publication_deployment_build_guard.py` | Alterado: admite somente a reconciliação do banco antes de uma falha posterior de startup. | Manter a cobertura de build, códigos de saída, logs atuais e proteção dos serviços. |
| `docs/HOTFIX-2.0.0.69-POSTGRES-STARTUP.md` | Criado: diagnóstico, instalação, diff, evidências, limites e reversão. | Tornar a entrega verificável e aplicável com o launcher existente. |

Nenhum arquivo em `src/`, migration, Compose, Dockerfile ou lockfile foi alterado. Não há mudança de schema que exija nova migration. A atualização operacional das credenciais é transacional e utiliza a configuração existente.

## Validação executada

| Verificação | Resultado |
| --- | --- |
| Baseline focado na versão original | 73 aprovados, 3 ignorados. |
| Suíte focada após a correção | 85 aprovados, 3 ignorados. |
| Integração real com PostgreSQL 17.9 | 7 aprovados, em 252,98 segundos. |
| Suíte ampla após a correção | 2.136 aprovados, 39 ignorados e 23 falhas. |
| Reexecução dos 23 casos com falha na versão original extraída do ZIP | Os mesmos 23 casos falharam também na versão original. |
| Sintaxe Bash/POSIX e Ruff dos scripts Python/testes novos | Aprovados. |
| Comparação de conteúdo com o ZIP original | Somente os cinco arquivos existentes listados no diff foram alterados; nenhum arquivo em `src/` mudou. |

O teste real utilizou uma VM QEMU isolada, Linux 6.8, usuário não root e PostgreSQL 17.9 com autenticação TCP SCRAM. Não houve simulação do PostgreSQL nem remoção de suas proteções. Um adaptador de destino de `psql` traduziu apenas o hostname/porta/socket fixos do Compose para o cluster temporário de teste. O script de reconciliação e o inicializador foram os arquivos da entrega.

Os sete cenários cobriram:

- Divergência das duas senhas.
- Divergência somente de `postgres`.
- Divergência somente de `viracase`.
- Credenciais já corretas, sem alterações.
- Banco inexistente, sem rotação das credenciais.
- Conta da aplicação ausente, recriada sem superusuário.
- Reprodução da rejeição de senha pelo inicializador, recuperação, execução das migrations até `0007`, bootstrap e segunda inicialização bem-sucedida.

Também foram verificadas senhas com aspas e caracteres especiais, preservação de registros, preservação dos atributos das contas, estabilidade dos hashes na repetição e ausência das senhas novas nas saídas examinadas.

Comando da suíte focada:

```bash
python -m pytest -q tests/test_database_startup.py tests/test_container_migrations.py tests/test_publication_deployment_build_guard.py tests/test_publication_deployment.py tests/test_admin_env_sync.py tests/test_migrations.py
```

Para os testes de integração, usando um usuário não root e uma instalação local de PostgreSQL 17:

```bash
VIRA_TEST_POSTGRES_BIN=/usr/lib/postgresql/17/bin python -m pytest -q tests/test_database_startup_postgres.py
```

O teste cria seu próprio cluster temporário e não usa o banco da aplicação. Esse comando é para reproduzir a validação; não é necessário para aplicar o hotfix.

## Limites e falhas preexistentes

Não foi executado Docker/Compose real neste ambiente, que não disponibiliza Docker. A orquestração do launcher foi verificada com o simulador de comandos existente; a recuperação e o inicializador foram executados contra PostgreSQL real. A imagem `postgres:17-alpine`, o AppArmor e o volume do computador de origem ainda precisam passar pela execução habitual do usuário.

A suíte ampla não está totalmente verde. Seus 23 casos com falha também falharam no pacote original, nos módulos de configuração de agentes/IA, continuação de esclarecimentos, análise/correção de código, continuação de correção de publicação e diagnóstico de compilação. Eles estão fora do escopo deste hotfix. A lista exata segue abaixo.

O caminho de recuperação exige acesso administrativo pelo socket local do serviço `postgres`, conforme a instalação local padrão. Se esse acesso tiver sido restringido, o script para com diagnóstico; ele não modifica `pg_hba.conf`, não habilita autenticação sem senha pela rede e não apaga volumes.

## Reversão

Para reverter o código, restaure os cinco arquivos alterados a partir da versão 2.0.0.68 e remova os quatro arquivos novos listados no diff. O alinhamento de senhas já concluído permanece compatível com a configuração efetiva usada na execução; não é necessário desfazer schema nem restaurar dados. Se quiser retornar às credenciais anteriores, isso é uma alteração separada de configuração e credenciais, não uma reversão automática deste pacote.

## Evolução proposta

Referências de decisão: Elon — incluir startup com volume existente como critério de release; Tony — exercitar o Compose real no CI; Bruce — cobrir rotação de credenciais e falhas do socket administrativo; Aristóteles — exigir evidência de recuperação e preservação de dados antes de encerrar incidentes de inicialização.

## Casos que também falham na versão original

- `tests/test_agent_execution_configuration.py::test_reply_after_partial_implementation_survives_two_truncations_with_checkpoint`
- `tests/test_ai_configuration.py::test_managed_generation_worker_settles_and_config_change_prevents_dispatch`
- `tests/test_clarification_continuation.py::test_legacy_reply_resumes_accepted_files_before_the_question`
- `tests/test_clarification_continuation.py::test_several_questions_keep_the_entire_origin_and_all_answers`
- `tests/test_clarification_continuation.py::test_checkpoint_stays_private_and_survives_two_questions_and_a_new_worker`
- `tests/test_clarification_continuation.py::test_a_failed_or_cancelled_reply_can_be_retried_without_replaying_its_provider_call[failed]`
- `tests/test_clarification_continuation.py::test_two_concurrent_replies_admit_only_one_child_operation`
- `tests/test_clarification_continuation.py::test_reply_context_limit_is_enforced_without_dropping_the_original_request`
- `tests/test_code_analysis.py::test_correction_requires_review_and_is_deduplicated_after_completion`
- `tests/test_code_analysis.py::test_failed_correction_can_retry_without_charging_a_new_analysis[rejected]`
- `tests/test_code_analysis.py::test_failed_correction_can_retry_without_charging_a_new_analysis[cancelled]`
- `tests/test_code_analysis.py::test_correction_is_a_readable_conversation_with_immutable_analysis_context`
- `tests/test_code_analysis.py::test_legacy_analysis_correction_is_readable_without_rewriting_stored_request`
- `tests/test_code_analysis.py::test_correction_http_rejects_cross_project_context_and_untrusted_analysis_payload`
- `tests/test_code_analysis.py::test_analysis_correction_can_remove_empty_and_obsolete_files_only_after_review`
- `tests/test_code_analysis.py::test_correction_questions_keep_analysis_context_and_deduplicate_each_answer`
- `tests/test_code_analysis.py::test_correction_question_cannot_transfer_analysis_to_another_context[other_account]`
- `tests/test_code_analysis.py::test_correction_question_cannot_transfer_analysis_to_another_context[other_project]`
- `tests/test_code_analysis.py::test_failed_correction_answer_retries_the_same_question_and_preserves_analysis[failed]`
- `tests/test_code_analysis.py::test_failed_correction_answer_retries_the_same_question_and_preserves_analysis[cancelled]`
- `tests/test_correction_followup.py::test_publication_correction_preserves_question_checkpoint_and_applies_reviewed_revision`
- `tests/test_correction_followup.py::test_publication_correction_question_rejects_revision_changes_without_reserving`
- `tests/test_publication_build_diagnostics.py::test_real_compiler_failure_is_persisted_and_available_only_to_owner_over_http`
