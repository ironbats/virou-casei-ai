# P0/P1 — Persistência, exclusão e recuperação de projetos

## Escopo

Correção incremental sobre v2.0.0.71 do backend e frontend, preservando o verificador HTTPS isolado da v2.0.0.70. Não existem migrations de schema nem alterações em tokens Cloudflare, autenticação ou rotas públicas.

## P0 — Persistência

`script/run-local.sh` em modo Docker prepara automaticamente o armazenamento de fontes em `$XDG_DATA_HOME/viroucaseai/sources` ou `$HOME/.local/share/viroucaseai/sources`, a menos que `VIRA_SOURCE_ROOT` já indique um destino não padrão. O script importa as revisões de `var/sources`, da montagem do worker anterior e de diretórios irmãos nomeados `virou-case-ai-backend-api*`.

A importação é conservadora: aceita apenas caminhos UUID/UUID/UUID.json e JSON válido, preserva o original, não substitui conteúdo existente, rejeita links simbólicos e interrompe a inicialização diante de colisões divergentes. Também é possível declarar diretórios históricos adicionais por `VIRA_SOURCE_IMPORT_ROOTS`, separados por `:`. O comando exibe `SOURCE_STORAGE_READY` antes do build.

O processo gera uma auditoria de integridade após a inicialização. `SOURCE_STORAGE_AUDIT_WARNING` significa que o PostgreSQL ainda conhece revisões cujos arquivos não foram encontrados ou falharam na validação; não significa que o banco esteja vazio. Não se pode recuperar fontes ausentes sem uma cópia válida de backup/versão anterior. Não remova os diretórios antigos até conferir essa auditoria.

Quando um projeto é excluído, o armazenamento registra um marcador para impedir que futuras atualizações reimportem as revisões apagadas. O marcador é interno, não contém código.

## P0 — Exclusão

DELETE continua exigindo sessão, confirmação do nome, versão atual e chave de idempotência. O usuário proprietário também pode solicitar exclusão após vencimento do acesso. A requisição bloqueia novos trabalhos do projeto e cancela os que ainda não começaram, sem apagar tarefas em execução nem operações de cobrança incerta. O worker aguarda o encerramento seguro, com estado `deletion_waiting_operations`, antes de apagar recursos Cloudflare e arquivos. Operações Cloudflare de publicação abandonadas com inventário registrado e lease expirado são terminadas pelo fluxo de limpeza.

A limpeza remota acontece antes da limpeza local. A limpeza dos dados de revisão é confirmada no banco antes do filesystem; em caso de erro, o projeto fica em `deleting` e pode ser retomado. Dados de consumo, auditoria e referências mínimas de operações são retidos por necessidade de rastreabilidade, mas nomes, objetivos, arquivos, rascunhos e entradas de idempotência do projeto são apagados ou expirados.

Uma operação `reconciling` com efeito externo ou faturamento não resolvido pode deixar a exclusão em espera. Isso é deliberado: não é seguro descartar uma transação incerta e declarar que a limpeza está concluída. O Backoffice deve usar os mecanismos de reconciliação existentes para eliminar esse bloqueio. O frontend mostra o andamento.

## P1 — Experiência

A IDE preserva identificação, navegação e acesso ao histórico quando a revisão não está disponível. Não libera edição, publicação ou gravação de dados inventados durante um `SOURCE_UNAVAILABLE` / `SOURCE_INTEGRITY_FAILED`. Há retentativa manual e identificação de requisição. A página de projetos acompanha as operações de exclusão por API existente.

## Aplicação

1. Faça um backup do PostgreSQL e preserve as pastas `var/sources` de versões anteriores, antes de atualizar.
2. Aplique os arquivos integrais do pacote nas pastas de backend e frontend correspondentes à v2.0.0.71.
3. Execute `./script/run-local.sh` no backend e confira `SOURCE_STORAGE_READY`, `SOURCE_STORAGE_AUDIT` e `VERIFIER_EGRESS_ENABLED`.
4. Execute `./script/run-local.sh` no frontend.
5. Confira leitura das revisões, descarte de operação pendente, exclusão idempotente e publicação/verificação Cloudflare.

Não execute `docker compose down -v`, não apague `var/sources` antigo, não desative o verificador HTTPS e não execute comandos SQL destrutivos para corrigir arquivos ausentes.

## Rollback

Antes de restaurar o código anterior, preserve o banco e copie as revisões novas da localização estável para a pasta de fontes que a versão anterior utilizará:

`python3 script/source-storage.py --root . --rollback-to ./var/sources`

O comando não sobrescreve revisões divergentes. Em seguida, restaure os arquivos de aplicação anteriores. O rollback de código não pode desfazer uma exclusão já concluída; restauração de dados exige backups, inventário remoto e uma avaliação de consistência específica. Execute o rollback apenas com processos de escrita parados, para não copiar dados concorrentes.

## Limitações da validação de laboratório

Os testes Python de exclusão, migração, integridade e verificador isolado foram executados em banco SQLite local. A suíte ampliada de backend depende de `google.auth`, indisponível no ambiente de validação. O frontend não tinha todas as dependências do lockfile disponíveis para build completo e E2E. Esses testes devem ser executados no Ubuntu do usuário antes de declarar validação integral.
