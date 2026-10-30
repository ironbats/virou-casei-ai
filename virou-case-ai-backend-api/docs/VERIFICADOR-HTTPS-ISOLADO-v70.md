# Verificador HTTPS isolado v2.0.0.70

## Finalidade

As verificações públicas `frontend`, `revision` e `api` permanecem implementadas em `RemoteVerifier`. Quando o backend é iniciado por `script/run-local.sh`, a implementação Docker habilita um serviço de egress independente para superar a indisponibilidade de TCP da rede bridge nos endereços públicos Cloudflare Workers.

## Fluxo

O `worker` continua nas redes `database` e `outbound`, com o perfil AppArmor dedicado. Apenas as requisições HTTP de verificação pública são encaminhadas por um socket Unix para o contêiner `verifier-egress`, que utiliza `network_mode: host` e a cadeia de confiança TLS do Ubuntu. O serviço não recebe credenciais da Cloudflare, do banco ou dos provedores de IA, não possui porta TCP de escuta nem segue redirecionamentos. Os destinos permitidos são exclusivamente URLs HTTPS da estrutura `nome.subdominio.workers.dev` e os caminhos `/`, `/api/health` e `/api/__viroucaseai_revision`, com token de prova no query string.

Os controles anteriores de lease, retries, limite de tempo, D1, validação de `revision_id`, status HTTP e formato JSON continuam no `RemoteVerifier` original. O transporte pelo socket não modifica o contrato do `CloudflareGateway`.

## Operação

Executar `bash script/run-local.sh` na raiz do backend. O script inclui automaticamente `compose.verifier-egress.yaml` em Docker local com certificado confiável em `/etc/ssl/certs/ca-certificates.crt`. Se estiver ausente, a inicialização falha antes do build com um erro explícito.

O serviço `verifier-ipc-init` prepara as permissões do volume nomeado `verification-ipc` com o mesmo UID/GID do worker. O `verifier-egress` possui healthcheck via socket Unix; o worker preserva sua inicialização independente para não bloquear geração de código ou outros trabalhos se a verificação HTTPS estiver temporariamente indisponível. O comando `run-local.sh` aguarda os serviços ficarem prontos antes de finalizar. O worker recebe `VIRA_REMOTE_VERIFIER_SOCKET=/run/viracase-verifier/http.sock` apenas nesse overlay. Nenhuma porta é publicada.

Não é necessário alterar o `.env`, e a chave privada do Cloudflare não é copiada para o serviço de verificação.

## Verificação

Após executar o `run-local.sh`, verificar o estado com os arquivos Compose efetivos mostrados na execução. Acionar a verificação operacional do Backoffice e conferir as três provas HTTP: HTML, revisão e saúde da API. O aceite exige HTTP 200, JSON válido, D1 verdadeiro e revisão correta no projeto publicado.

## Segurança e confiança TLS

O caminho `/etc/ssl/certs/ca-certificates.crt` é montado somente leitura. Deve ser revisado pelo operador se possuir autoridades certificadoras privadas ou de inspeção HTTPS. Nunca utilizar `verify=False`, nunca adicionar a chave da Cloudflare ao verificador, nem mapear o socket para a internet.

## Desativação e rollback

Para desativar o overlay em uma execução futura, utilizar `VIRA_VERIFIER_EGRESS=off bash script/run-local.sh`. Isso retorna ao transporte HTTP direto anterior e não corrige os timeouts TCP observados na rede bridge. Para rollback completo da configuração Compose, aplicar o código anterior sem `compose.verifier-egress.yaml`; os contêineres auxiliares podem ser removidos usando o mesmo conjunto Compose que os criou, sem excluir volumes do banco.

## Limitações

A correção operacional não elimina a causa-raiz da perda de retorno TCP na rede bridge. O Docker host e o pacote CA do Ubuntu foram aprovados em testes de conectividade, mas a integração completa com socket Unix ainda deve ser validada no Ubuntu com Docker e no Backoffice real antes de declarar a publicação definitivamente estabilizada.
