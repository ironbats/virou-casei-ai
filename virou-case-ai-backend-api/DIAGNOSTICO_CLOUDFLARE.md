# Publicacao Cloudflare - limite de processos v2.0.0.33

Base desta entrega: `virou-case-ai-backend-api-V2.0.0.32.zip` e `Texto colado.txt` enviados nesta conversa. O ZIP da base ja inclui a compatibilidade AppArmor 3.0.4. Nao reaplicar patches antigos sobre esta revisao. As secoes da v32 foram preservadas ao final como historico.

## Evidencia desta ocorrencia

O parser 3.0.4 compilou a variante legacy-no-userns, o perfil foi carregado, a imagem foi construida e os servicos ficaram healthy. O diagnostico informa `viracase-publication (enforce)` e fingerprint preenchido. A nova falha e `bwrap: Creating new namespace failed: Resource temporarily unavailable` em `isolation_probe`, nao em `sandbox_preflight`. A senha sudo, o parser e a selecao do perfil nao sao os bloqueadores demonstrados nesta execucao. Nenhuma chamada de publicacao Cloudflare aparece nesta falha.

## Defeito encontrado e reproducao

`build_limits.py` aplicava `RLIMIT_NPROC=(64,64)` antes de iniciar o Bubblewrap. O preflight simples nao aplicava esse limite. No Linux, o limite contabiliza tarefas do UID real e a hierarquia dos user namespaces; nao equivale a um limite exclusivo de um projeto. O runtime preserva o UID do operador, portanto outras tarefas desse UID podem participar da contagem externa.

Um teste C limitado a 66 threads externas reproduziu `clone(CLONE_NEWUSER)` retornando `EAGAIN` quando o limite era aplicado antes. Invertendo a ordem, a criacao passou. Dentro do novo user namespace, com capabilities removidas, o teste reaplicou o teto 64 e confirmou que uma tentativa limitada de criar descendentes era bloqueada por EAGAIN antes de 64 filhos. Todos os descendentes criados foram encerrados e coletados. O teste roda em subprocesso e nao altera os limites do processo de teste principal, sysctls ou politicas do host.

Isso confirma um defeito real no codigo, compativel com a diferenca entre as duas etapas dos logs. O log original nao inclui a quantidade de tarefas do UID ou contadores cgroup; nao demonstra que este era o unico limite atingido no servidor. A nova telemetria distingue evidencias de cgroup de um EAGAIN generico.

## Correcao aplicada

A ordem agora e `launcher com limites CPU/memoria/arquivos -> Bubblewrap com isolamento obrigatorio -> prlimit NPROC -> Node/probe/build`. O limite de processos continua 64 ou um limite herdado menor. Nao e elevado, desativado ou aplicado ao UID externo antes de criar o namespace. Nenhum codigo do projeto e executado entre a criacao do sandbox e a aplicacao do teto.

CPU 30 segundos, espaco virtual 3.000.000.000 bytes, arquivo 8.000.000 bytes, descritores 128 e core dump zero permanecem no launcher. `--disable-userns`, `--unshare-user`, `--cap-drop ALL`, ambiente limpo, fontes/toolchain somente leitura e rede isolada foram preservados. O processo de preflight continua sem executar codigo de aluno. Docker, AppArmor, seccomp, UID/GID, volumes e segredos mantem as mesmas regras. A unica mudanca de Compose e a tag da imagem 2.0.0.33.

O `probe.mjs` exige limites soft E hard, nao apenas o limite soft. O Dockerfile e o preflight verificam `/usr/bin/prlimit`, fornecido pelo util-linux ja instalado. A ausencia ou falha do prlimit continua bloqueando a publicacao.

## Diagnostico e logs

`details.resources_before` e `details.resources_after` registram os rlimits do launcher, `pids.current`, `pids.max`, `pids.events` e contadores de memoria dos cgroups v1/v2 visiveis. Os niveis ancestrais montados sao consultados ate o limite de 16 niveis. Arquivos nao visiveis aparecem como desconhecidos; nao ha inferencia de consumo zero nem afirmacao de visibilidade global do host. Nao sao registrados caminhos de cgroup, comandos de outros processos, ambiente completo ou credenciais.

`details.resource_evidence` registra eventos que aumentaram entre as amostras e saturacao observada. Contadores historicos sem aumento nao sao atribuidos a esta tentativa. Sao observacoes do grupo: outras tarefas no mesmo cgroup tambem podem contribuir. A coleta nao muda limites automaticamente.

- `CGROUP_PIDS_EXHAUSTED`: EAGAIN acompanhado de evidencia do controlador pids. Corrigir capacidade/concorrencia observada, nao remover a contencao.
- `SANDBOX_RESOURCE_EXHAUSTED`: erro generico de recursos sem evidencia suficiente para escolher um limite especifico.
- `WORKLOAD_LIMIT_FAILED`: prlimit nao aplicou o limite dentro do sandbox; build continua proibido.
- `NAMESPACE_QUOTA_EXCEEDED`: quota/profundidade de namespaces, distinta de uma negacao de AppArmor.

`failed_checks` agora lista controles que retornaram false. `unverified_checks` lista os que nao retornaram um resultado booleano. `checks_completed` so e true quando o probe retornou todos os controles requeridos como booleanos. A indisponibilidade continua determinada pelo codigo de erro e por todos os testes; listas vazias nao liberam o executor. Se o Bubblewrap nem iniciou, nao se apresentam seis controles como se tivessem sido executados e reprovados.

## Instalacao e aceite desta revisao

Preserve backup dos arquivos listados em MANIFESTO.json. Copie o conteudo de backend/ sobre a raiz do backend existente, mantendo .env, var, dados e demais arquivos. Nenhuma mudanca de frontend/backoffice e necessaria. Nao execute o processo completo como root.

```bash
bash script/restart-publication.sh
```

O script existente preserva UID/GID, reconstrui e recria worker/admin-api/public-api, sem remover volumes ou executar migrations. Pode solicitar sudo para validar/carregar o perfil AppArmor ja existente. Esta revisao nao muda esse instalador. A recriacao pode interromper requisicoes temporariamente.

```bash
docker compose exec -T worker python script/diagnose-publication.py --pretty
```

Aceite local: `available=true`, `code=READY`, fingerprint preenchido, `checks_completed=true`, `failed_checks=[]`, `unverified_checks=[]`. Em `details.resources_after.nproc_application_stage`, o valor esperado e `inside_sandbox_before_node`. Os rlimits da amostra pertencem ao launcher externo; a comprovacao do teto interno ocorre no controle `resource_limits` do probe.

Depois execute Atualizar diagnostico e Verificar publicacao na conexao ativa do backoffice. O novo fingerprint invalida a evidencia do executor anterior. O script nao cria recursos remotos por conta propria. A qualificacao remota deve aprovar build, implantacao, D1 e limpeza antes de publicar um projeto. Nao reconfigure o token ou amplie permissao como tentativa de resolver um erro local.

```bash
docker compose logs --since 15m --tail 400 --timestamps --no-color worker admin-api
```

Use diagnostic_id para correlacao. Remova quaisquer segredos antes de compartilhar logs. Se persistir EAGAIN, as amostras de recursos devem ser analisadas antes de qualquer alteracao de capacidade.

## Testes e limites da entrega

VALIDACAO.json registra comandos, resultados e versoes reais. Os testes de runtime/integrais usam SQLite e provedor HTTP simulado; os testes de scripts Docker usam simulador. A regressao de RLIMIT_NPROC utiliza syscalls reais e namespace Linux, com 66 threads externas e no maximo 70 tentativas de filhos, encerrados ao final. Ela nao equivale a uma execucao completa do Bubblewrap em Docker.

```bash
VIRA_TEST_LINUX_NPROC=1 PYTHONPATH=src pytest -q tests/test_publication_resources.py
```

O teste Linux precisa de compilador C e suporte a namespaces; retorna skip quando o host nao suporta o cenario. Fora da ativacao explicita, esse teste nao cria threads extras. Os demais testes nao exigem Docker ou Cloudflare.

Este ambiente nao possui Docker, Bubblewrap ou acesso de rede funcional para instalar dependencias ausentes. Nao houve build da imagem real, execucao do sandbox completo, Postgres real ou deploy autenticado na Cloudflare. A versao Python de teste tambem difere da imagem 3.12. Nao se declara publicacao pronta em producao sem as etapas de aceite acima.

## Rollback desta revisao

Restaure os arquivos anteriores do manifesto, remova somente os arquivos novos desta revisao e reconstrua/recrie os servicos com o script existente. Nao remova dados, perfis AppArmor ou credenciais. A versao anterior mantem o defeito de RLIMIT_NPROC e pode voltar a bloquear publicacao; rollback e reversibilidade de codigo, nao solucao operacional.

## Referencias tecnicas usadas na investigacao

- Linux man-pages, getrlimit/RLIMIT_NPROC: https://man7.org/linux/man-pages/man2/getrlimit.2.html
- Linux 6.8, checagem de RLIMIT_NPROC no clone: https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/fork.c
- Linux 6.8, hierarquia de ucounts: https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/ucount.c
- Linux 6.8, limite herdado ao criar user namespace: https://raw.githubusercontent.com/torvalds/linux/v6.8/kernel/user_namespace.c
- Controlador pids: https://docs.kernel.org/admin-guide/cgroup-v1/pids.html

---

# Historico preservado da v2.0.0.32

# Publicacao Cloudflare - estabilizacao do AppArmor v2.0.0.32

Base: `virou-case-ai-backend-api-V2.0.0.31.zip`, enviada por Felipe. O patch nao altera frontend, backoffice, regras de acesso, tokens, banco, migrations ou contratos de publicacao. Data da investigacao: 27/10/2026, America/Sao_Paulo.

## 1. Evidencia e causa

O diagnostico fornecido mostrou `bwrap: Failed to make / slave: Permission denied`, etapa `sandbox_preflight`, exit code 1 e perfil `docker-default (enforce)`. O preflight terminou antes de executar o projeto ou iniciar a verificacao remota. O perfil AppArmor dedicado nao estava aplicado ao worker observado.

O Bubblewrap usa uma operacao `mount` para tornar recursivamente a montagem de `/` slave dentro do namespace. O perfil padrao do Docker contem `deny mount`. Isso explica a incompatibilidade observada. O log nao inclui o evento audit do kernel; nao se afirma que nenhuma politica adicional do host possa impedir etapas posteriores.

A v31 tinha `compose.publication-apparmor.yaml`, mas o Compose padrao nao carregava esse nome automaticamente. O lancador normal so selecionava o overlay quando conseguia ler o perfil em `/sys/kernel/security/apparmor/profiles`, e o script de restart exportava a selecao apenas para aquela execucao. O codigo nao verificava o perfil realmente aplicado ao container depois da recriacao. Um arquivo de politica presente nao era prova de politica ativa.

Nao ha evidencia, neste diagnostico, de recusa do token Cloudflare. `CAPABILITY_UNVALIDATED` permanece porque a verificacao remota nao pode executar sem o executor local pronto. `failed_checks: []` nao significa isolamento aprovado: a execucao nem chegou a esses testes.

O mesmo JSON mostrou `fingerprint: null`. Nao foi fornecido o arquivo inacessivel. Esta revisao informa `fingerprint_error` com nome do artefato, tipo da excecao e errno, sem caminho absoluto. A imagem normaliza a leitura da toolchain por usuarios nao root e exige fingerprint valido durante o build. Nao ha fallback para identidade inventada.

## 2. O que muda

- `compose.override.yaml` torna a selecao do perfil persistente no caminho padrao do Compose em hosts com AppArmor.
- O overlay explicito e o automatico selecionam somente `apparmor=viracase-publication` no worker. As demais restricoes continuam na base.
- Os dois lancadores compartilham `publication-runtime.sh`: consultam o daemon, selecionam o overlay, preservam o UID/GID numerico existente e inspecionam `.AppArmorProfile` apos o deploy.
- O helper nao depende da leitura do securityfs por um usuario comum para descobrir AppArmor. A instalacao/validacao do perfil no host local e feita como administrador.
- O instalador valida a sintaxe antes de carregar o perfil, confirma enforce e persiste a politica em `/etc/apparmor.d/viracase-publication`. Nao reinicia o daemon Docker nem muda sysctls globais.
- O restart faz build antes de recriar os servicos. Se o build falhar, os containers existentes nao sao recriados. Depois usa `--force-recreate --no-deps --no-build` somente em worker e APIs.
- `APPARMOR_PROFILE_MISMATCH` identifica a combinacao do erro exato com `docker-default`. Com outro perfil, `MOUNT_PROPAGATION_DENIED` evita atribuir a falha a AppArmor sem a mesma evidencia. Modo complain nao qualifica o executor como pronto.
- O monitor considera mudancas do perfil no identificador da condicao registrada, evitando correlacionar politicas diferentes como se fossem a mesma ocorrencia.

## 3. Aplicar na instalacao existente

Guarde os arquivos anteriores listados em `MANIFESTO.json`. Copie o conteudo de `backend/` do ZIP para a raiz do backend existente. Nao remova a pasta do projeto, nao copie `.env` de outra instalacao e nao substitua `var/`.

O ZIP da base nao possuia `compose.override.yaml`. Se houver um override criado localmente e nao enviado para analise, preserve-o: incorpore somente a entrada AppArmor do worker, mantendo as demais opcoes locais. O script tambem adiciona o overlay explicito ao final da lista; selecoes conflitantes precisam ser resolvidas, nao ocultadas.

Na raiz do backend:

```bash
bash script/restart-publication.sh
```

O script pode pedir sudo para carregar/atualizar o perfil dedicado. Execute como seu usuario normal; nao rode todo o deploy com sudo sem necessidade. O UID/GID numerico do worker existente e preservado, a menos que haja uma selecao explicita nas variaveis de runtime. Identidades root ou nao numericas sao recusadas para evitar troca silenciosa da identidade de acesso aos volumes.

Esta acao reconstrui e recria `worker`, `admin-api` e `public-api`. Pode haver interrupcao temporaria de requisicoes. Nao executa `down`, nao remove volumes, nao executa migrations e nao cria recursos Cloudflare. Jobs permanecem sob os mecanismos existentes de lease e reconciliacao.

Se `apparmor_parser` estiver ausente, instale o pacote apropriado do sistema (normalmente apparmor-utils) no host do daemon. Se a politica nao compilar, a execucao para: nao remova regras de seguranca para forcar a inicializacao.

Para proximas inicializacoes use o `script/run-local.sh` existente, que encaminha para o lancador corrigido. Ele tambem executa o diagnostico de publicacao depois de verificar as credenciais administrativas; container healthy nao e tratado como prova de executor pronto.

## 4. Ambientes e selecao do Compose

No host analisado, o daemon usa AppArmor. `compose.override.yaml` e carregado automaticamente por um `docker compose` sem selecao explicita de arquivos. O uso de `-f compose.yaml` sozinho ou `COMPOSE_FILE` externo pode omitir qualquer override automatico; prefira os scripts fornecidos.

Em hosts cujo daemon nao anuncia AppArmor, os scripts usam a base sem o overlay dedicado. Nao adicionam `apparmor=unconfined`. O comando Compose padrao com o novo override exige AppArmor; portanto, nesses hosts, use `script/run-local.sh` ou `script/restart-publication.sh` em vez do Compose cru.

Uma lista COMPOSE_FILE exportada no terminal e preservada; o perfil dedicado e acrescentado quando aplicavel. Se COMPOSE_FILE estiver somente dentro do .env, o script para com uma orientacao para exportar a mesma lista, em vez de ignorar personalizacoes. O .env nao e reescrito nem executado como shell.

Em Docker remoto, o script nao carrega perfil no computador cliente. O perfil precisa ser provisionado no host do daemon. O deploy especifica o nome esperado e o Docker deve recusar a criacao se nao houver esse perfil. A inspecao posterior tambem exige a identidade correta. DOCKER_CONTEXT tem precedencia sobre DOCKER_HOST na deteccao do endpoint.

## 5. Criterios de aceite

Apos o script, confirme o perfil efetivo:

```bash
docker inspect --format '{{.AppArmorProfile}}' "$(docker compose ps -q worker)"
```

Resultado esperado para esta instalacao: `viracase-publication`.

Confira o diagnostico real dentro do worker:

```bash
docker compose exec -T worker python script/diagnose-publication.py --pretty
```

O resultado deve ter `available: true`, `code: READY`, fingerprint nao nulo, `fingerprint_error: null` e nenhum controle de isolamento reprovado. No host analisado, `environment.apparmor_profile` deve ser `viracase-publication (enforce)`. A disponibilidade depende da execucao real dos testes; nao e liberada pelo nome do perfil sozinho.

Exit code 0 do diagnostico significa aprovacao local. Exit code 2 significa bloqueio. Falha anterior no Python, build, Docker ou script tambem exige correcao; nao e sucesso.

No Backoffice, atualize o diagnostico e execute **Verificar publicacao** na conexao ativa, com motivo preenchido. Essa acao existente cria recursos temporarios e testa build, deploy, D1, isolamento e limpeza. O restart nao executa essa acao automaticamente. O novo fingerprint exige qualificacao operacional nova.

Heartbeats anteriores podem aparecer por ate 60 segundos, conforme a janela existente. Se houver outros workers atuais ainda bloqueados, eles tambem precisam ser tratados. Nao apague registros do banco nem marque qualificacao manualmente como aprovada para contornar o bloqueio.

## 6. Logs e falhas subsequentes

```bash
docker compose logs --since 15m --tail 400 --timestamps --no-color worker admin-api public-api
```

Para acompanhar:

```bash
docker compose logs -f --since 5m --timestamps --no-color worker admin-api
```

Use `diagnostic_id` para correlacionar o JSON do Backoffice com os logs. `expected_apparmor_profile` e `observed_apparmor_profile` aparecem no erro de perfil. `fingerprint_error` aponta falhas independentes da identidade da toolchain. Nunca compartilhe tokens, cookies, Authorization, senhas ou o .env.

Se o perfil ja estiver correto mas a montagem continuar recusada, consulte o audit do host para a mesma tentativa:

```bash
sudo journalctl -k --since '10 minutes ago' --no-pager | grep -Ei 'apparmor|audit|denied|seccomp'
```

Esse comando e somente de leitura; os registros podem incluir detalhes de outros processos. Selecione apenas o trecho relacionado ao worker antes de compartilhar. `Seccomp: 2` comprova modo de filtro, nao qual arquivo de politica foi aplicado. Nao atribua um novo erro a seccomp apenas por esse numero.

## 7. Seguranca e limites

Permanecem usuario nao root, cap_drop ALL, no-new-privileges, root filesystem somente leitura, seccomp dedicado e testes do Bubblewrap. Nenhum socket Docker e montado no worker. Nao foram adicionados SYS_ADMIN, privileged, seccomp=unconfined, apparmor=unconfined ou mudancas globais de user namespaces.

O perfil dedicado ja existente autoriza operacoes de montagem necessarias ao Bubblewrap; aplicar esse perfil e uma alteracao real de politica em relacao a docker-default. A excecao e exclusiva do worker. `systempaths=unconfined` ja estava na v31 e nao foi ampliado para APIs ou outros servicos. O worker continua compartilhando o kernel do host: este patch nao e uma certificacao de isolamento contra toda classe de ataque.

Nao houve acesso ao Docker do usuario nem deploy autenticado na Cloudflare. A validacao local em pytest usa simulacoes de Docker/Bubblewrap e SQLite para os contratos que usam banco. O perfil foi compilado pelo apparmor_parser disponivel, sem ser carregado no kernel deste ambiente.

## 8. Rollback

Restaure os arquivos anteriores conforme os hashes de origem do manifesto. Remova apenas os arquivos novos indicados ali que nao existiam antes do patch, incluindo o override criado por esta entrega, se nao contiver customizacoes locais. Nao remova .env, var, volumes ou credenciais.

Restaure e recrie os servicos com a versao e a selecao de Compose anteriores, preservando UID/GID. Nao execute `docker compose down -v`. O perfil dedicado ja existia no projeto: nao o descarregue enquanto algum container depender dele. Se um perfil local personalizado foi substituido pelo instalador, restaure a copia aprovada e recarregue somente esse perfil como administrador.

Nao ha rollback SQL. Retornar a v31 pode restabelecer o bloqueio conhecido sob docker-default; rollback nao e prova de publicacao funcionando.

## 9. Referencias primarias consultadas

- Docker/Moby: template AppArmor padrao, contendo `deny mount`.
  https://raw.githubusercontent.com/moby/profiles/main/apparmor/template.go
- Bubblewrap v0.11.0: chamada mount MS_SLAVE e mensagem Failed to make / slave.
  https://raw.githubusercontent.com/containers/bubblewrap/v0.11.0/bubblewrap.c
- Docker: carregamento e selecao de perfis AppArmor.
  https://docs.docker.com/engine/security/apparmor/
- Docker Compose: carregamento padrao de compose.override.yaml e selecao por -f/COMPOSE_FILE.
  https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/
- Docker Compose: restart nao aplica mudancas de configuracao; up pode recriar containers.
  https://docs.docker.com/reference/cli/docker/compose/restart/
  https://docs.docker.com/reference/cli/docker/compose/up/
