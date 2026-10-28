# Operação da entrega — fluxo guiado e Cloudflare

27/10/2026. Bases: backend V2.0.0.24, frontend v2.0.0.20 e backoffice v2.0.0.17 fornecidos por Felipe. O ZIP é um patch de arquivos integrais, não um repositório completo nem um deploy executado.

## Diagnóstico e alcance

Nos arquivos recebidos, o backend retornava `publication.enabled=false`/`CAPABILITY_UNVALIDATED` incondicionalmente, o comando mantinha o bloqueio e o status administrativo declarava publicação desabilitada. A tela Publicar mostrava esse bloqueio, sem integração administrativa nem pipeline operacional. Portanto, a causa demonstrada não era apenas token ausente: faltavam configuração persistida, execução e verificação. Não houve acesso à produção para confirmar qual versão está implantada nela.

A entrega acrescenta configuração privada, readiness com motivos, pipeline durável e interface visível. Mantém o erro público legado quando a capacidade não foi validada. Conexão válida não libera isolamento por si só.

**Aceite externo pendente:** não foram fornecidos credenciais, conta Cloudflare autorizada ou evidências reais de qualificação. Não houve publicação, criação/remoção de Worker/D1 ou chamada real Jev. O ambiente não oferece Docker/PostgreSQL e recusou namespaces do Bubblewrap. Build isolado no host de destino, concorrência PostgreSQL, exposição IPv4/IPv6, Hono/FastAPI remoto, D1, plano/custo e retenção permanecem não validados. Fixtures simulados não substituem esses aceites. A publicação permanece bloqueada até cumprir os requisitos.

## Aplicação do patch

1. Confira hashes de origem no `MANIFESTO_SHA256.json`. Se houver alterações locais nos mesmos arquivos, faça merge revisado; não sobrescreva base divergente automaticamente.
2. Execute o plano de backup/ensaio de `rollback/0004.md`. Preserve banco, volumes, chave, fontes e configurações existentes. O pacote não contém esses segredos ou dados.
3. Copie cada diretório do ZIP para a raiz do projeto correspondente, incluindo arquivos novos.
4. Recrie a imagem do backend. O Dockerfile instala ferramentas confiáveis e gera o vendor Python com lockfile, sem executar fonte de aluno. O host/UID do worker deve suportar Bubblewrap e namespaces não privilegiados. Não contorne falha com modo privilegiado, socket Docker ou mounts de segredos.
5. Use o inicializador existente `script/container-initialize.py`, acionado pelo fluxo Docker local, para conferir migrations e aplicar até 0004. API e worker novos exigem esse head. A migration cria quatro tabelas/índices sem alterar migrations antigas. Não foi aplicada em produção nesta sessão.
6. Suba API pública, API privada e worker da mesma entrega; valide saúde e regressões em homologação. Depois aplique os builds de frontend/backoffice. Scripts locais, portas, sessões, origens e Compose existentes foram preservados.
7. Não exponha `/admin-api/v1`, PostgreSQL ou backoffice no túnel. Teste também IPv6/rede do host; controles de aplicação não provam isolamento de rede.

## Chave, credenciais e recuperação

Cloudflare/Jev reutilizam o `CredentialVault`/Fernet existente. A chave `var/secrets/credentials.key` é bootstrap da plataforma, em mount protegido fora do banco/repositório. Nenhuma configuração de conta Cloudflare foi acrescentada a `.env`, Compose ou código. Endpoints de protocolo permanecem controlados, sem URL administrativa livre.

O inicializador não cria chave substituta se já houver credencial AI ou de integração cifrada. Faça backup protegido da chave separado dos dados. Para restaurar, recupere a chave correspondente e teste descriptografia em ambiente isolado antes de liberar integrações. Chave ausente/incorreta falha fechada sem sobrescrever ciphertext.

Rotação do token: novo valor pelo formulário → salvar versão → validar → qualificar → ativar. Preserve acesso à credencial anterior até reconciliar operações/inventário. Campo de token vazio preserva o segredo; GET retorna somente presença. Nunca registre token em logs, suporte ou relatórios.

A chave mestra é compartilhada com AI. Não foi introduzida rotação automática dessa chave. Sua rotação exige manutenção separada, recriptografia de todas as conexões e teste de restauração; substituir o arquivo isoladamente torna os segredos ilegíveis.

## Configurar no backoffice

Em **Integrações e templates → Cloudflare**, cadastre os valores reais:

| Campo | Critério |
|---|---|
| Account ID | Conta autorizada, 32 caracteres hexadecimais; não é Zone ID. |
| Tipo do token | Usuário ou conta; cada tipo usa sua API de verificação. |
| Ambiente/prefixo | Identificação administrativa e prefixo permitido para nomes por projeto. |
| Compatibilidade | Data validada para os perfis escolhidos. Não avança automaticamente. |
| Perfis | `hono-typescript-d1` e/ou `fastapi-python-d1`, cada um com testes reais. |
| Plano | Somente Free; sem upgrade/produto pago automático. |
| Limites | Projetos 1–10; concorrência 1–5; timeout 5–120 segundos; validade 1–168 horas. |
| SPA | Use apenas quando necessário; `/api/*` continua no Worker sem falso fallback HTML. |
| Token/motivo | Segredo somente de escrita e motivo obrigatório, com controle de versão. |

O destino inicial é o subdomínio `workers.dev` já configurado e observado na conta. Não foram incluídos domínio personalizado, DNS/zona ou outros produtos. IDs de Worker/D1 pertencem ao inventário do servidor.

O token precisa cobrir leitura da conta/subdomínio e leitura/escrita dos Workers Scripts/Assets e D1 autorizados. Confira no provedor os escopos exatos para o tipo de token e APIs; não use Global API Key como atalho. **Validar** apenas consulta token, conta, Workers, D1 e subdomínio. Não cria recurso, não comprova escrita, plano, isolamento ou consumo.

Sequência: **Salvar nova versão → Validar → registrar qualificação real → Ativar versão validada**. Ativar antes do relatório também mantém o bloqueio. Salvar desativa a versão anterior. Revalidar substitui evidências; registre novamente qualificação aplicável. **Desativar** bloqueia novos deploys, preservando reconciliação/lifecycle; não apaga recursos nem revoga token.

Em **Fluxo guiado e Jev**, configure limites de ideia/mensagem/anexos/contexto, debounce, modelo, timeout, limite diário e confiança. Limites locais salvos valem para o fluxo; chamadas Jev exigem versão ativa, token e habilitação. Validar Jev é local; somente **Preparar construção** solicita recomendação. Falha mantém catálogo manual, sem fallback GPT/Gemini nem débito dessa carteira.

Em **Templates**, salvar cria versão imutável e desativar impede novos envios. Edições do aluno continuam cópias pessoais; snapshots enviados não mudam. Atualizar uma cópia para a nova versão do catálogo exige confirmação explícita.

## Relatório de qualificação

O backoffice mostra ID/digest da configuração e digest do executor. O responsável deve executar os cenários no destino autorizado, guardar evidências e revisar os resultados. O JSON é um registro auditado; não é atestado criptográfico nem runner automático. Nunca use os fixtures sintéticos como prova de produção.

| Campo obrigatório | Tipo/conteúdo |
|---|---|
| `format_version` | Inteiro `1`. |
| `configuration_id`, `configuration_digest` | Identidade e SHA-256 desta versão. |
| `account_id` | A mesma conta configurada. |
| `tested_at`, `expires_at` | ISO-8601 com fuso; testes posteriores ao cadastro, sem futuro, validade vigente. |
| `runner_version`, `runner_sha256` | Identificação e SHA-256 do runner real. |
| `executor_digest` | SHA-256 exibido do executor implantado; precisa corresponder ao runtime. |
| `stacks` | Identificadores dos perfis efetivamente testados. |
| `checks` | Objetos com `code`, `passed` booleano, `expected`, `observed`, `evidence_sha256`. Descrições de 10–1000 caracteres; hash hexadecimal de 64. |
| `report_reference` | Referência verificável às evidências protegidas, 10–300 caracteres. |
| `reason` | Motivo da revisão administrativa, 8–300 caracteres. |

Cenários obrigatórios: `executor_no_network`, `executor_no_host_secrets`, `executor_no_cross_project_files`, `executor_resource_limits`, `worker_no_cross_tenant_binding`, `worker_no_platform_credentials`, `free_plan_confirmed`, `capacity_and_abuse_limits`, `frontend_api_d1_read_write`, `d1_migration_timeout_recovery`, `restart_without_duplicate_resources`, `suspension_verified`, `purge_verified`, `renewal_race_fenced`, `backup_retention_reviewed`.

O gate confere identidade/digests, conta, datas, cobertura, resultados positivos, perfil e disponibilidade real do executor. Mudanças no código coberto pelo fingerprint exigem nova qualificação. Sem prova adequada de execução multi-tenant não confiável no Free, mantenha o bloqueio. Alternativa paga ou mudança de arquitetura exige decisão de Felipe.

## Pipeline, perfis e reconciliação

O comando `/api/v1/projects/{id}/publications` mantém revisão e idempotência. Readiness não antecipa autorização: o servidor revalida no comando e antes dos efeitos. O worker registra etapas/leases, prepara fonte em Bubblewrap sem rede/segredos, cria/reutiliza D1 exclusivo, aplica migrations com recibos, envia assets/modules, ativa subdomínio e verifica frontend, revisão/D1 e `/api/health`. Leitura/escrita funcional e persistência após redeploy devem constar da qualificação real; health check não substitui esses cenários.

Ferramentas fechadas: esbuild 0.27.4/Hono 4.12.9; FastAPI 0.162.0/workers-runtime-sdk 1.9.5, com vendor produzido por workers-py 1.17.9 e uv 0.13.4. Lockfiles incluídos. Não executa scripts npm, instalação dinâmica ou imports Python de aluno no host. Python usa Workers ASGI e flags do perfil, não uvicorn convencional.

O adapter faz upload direto para nome estável do Worker. **Não há promoção canário/atômica validada:** upload pode substituir versão anterior antes do smoke test. Falha mantém reconciliação e não anuncia sucesso. Homologue rollback de código/schema. A URL anterior é a última publicação verificada, sem garantia de que uma tentativa incerta não a tenha afetado.

Timeout/5xx em escrita pode ter produzido efeito. Não há retry cego. O backoffice reconcilia consultando inventário, tags, binding D1, revisão e recibos. Confirma sucesso só com verificação; reenvio durável exige pré-requisitos válidos. Repetir a mesma intenção administrativa recupera o resultado sem novo efeito.

Migrations `migrations/*.sql` são ordenadas, conservadoras e identificadas por SHA-256. O ledger `__viroucaseai_migrations` é protegido; migration já aplicada não pode ser editada; SQL destrutivo é recusado. Se SQL produziu efeito sem recibo, o estado continua incerto: inspecione schema/dados e faça reparo/recibo consistente sob procedimento autorizado. Não apague o D1 nem limpe o bloqueio para forçar repetição. Não se promete atomicidade da API remota.

## Lifecycle e capacidade

Preserva vigência: fim do acesso bloqueia nova publicação; o corte de retenção existente suspende/expurga hospedagem sem renovação. Suspensão administrativa altera disponibilidade. Expurgo aguarda publicação em execução, usa fence/inventário, remove Worker/D1 e consulta ausência antes de confirmar.

Backups externos/Time Travel não são apagados por essa rotina: a evidência informa `requires_operator_evidence`. Não declare eliminação integral de cópias do provedor com base em 404 do recurso ativo. Mantenha hold de restauração até reconciliar vencimentos/inventário.

A admissão considera reservas, bancos observados e concorrência. Não há medição contínua de CPU/requests/storage nem promessa de teto financeiro absoluto. Não existe contratação automática. Capacidade e proteção contra abuso precisam ser verificadas na conta real.

## Referências oficiais de contrato

- https://developers.cloudflare.com/workers/static-assets/direct-upload/
- https://developers.cloudflare.com/workers/languages/python/packages/fastapi/
- https://developers.cloudflare.com/workers/languages/python/packages/
- https://developers.cloudflare.com/d1/platform/client-api/
- https://developers.cloudflare.com/d1/platform/limits/
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/cloudflare-for-platforms/workers-for-platforms/reference/worker-isolation/

Documentação não comprova aceitação da conta/host. Consulte `RELATORIO_MODIFICACOES.md` para evidências locais e pendências.
