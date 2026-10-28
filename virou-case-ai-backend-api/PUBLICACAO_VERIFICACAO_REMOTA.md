# Publicação Cloudflare: verificação remota e evidências

Versão de correção: 2.0.0.34. Base inspecionada: backend V2.0.0.33 enviado por Felipe.

## 1. Diagnóstico sustentado pelas evidências

O JSON fornecido mostra o executor READY, available=true, checks_completed=true,
failed_checks=[], unverified_checks=[] e can_verify=true. O único bloqueio de
publicação mostrado é CAPABILITY_UNVALIDATED. A operação remota terminou
com PUBLICATION_VERIFICATION_FAILED. Não há status HTTP ou etapa de falha remota
nesse JSON. Ele não permite atribuir o incidente a um 523 específico, nem a
permissões insuficientes.

O código da base reutiliza PUBLICATION_VERIFICATION_FAILED para falha da
leitura/escrita D1, divergência de bindings e reprovação HTTP externa. Worker.tick
substitui progress_stage por failed. O endpoint de diagnóstico retornava apenas
id, state, stage e error_code da verificação. Portanto, a causa precisa era perdida
no diagnóstico disponibilizado ao operador.

O recurso com nome <prefixo>-check-<operação> é temporário. execute_check o
remove em finally, com sucesso ou falha, e confirma a remoção. O desaparecimento
mostrado no painel Cloudflare é compatível com essa limpeza programada, não prova
um crash do Worker. O projeto do aluno ainda não foi publicado por esse teste.

## 2. Defeitos demonstrados e corrigidos

### Verificação prematura

CloudflareGateway.verify consultava /, /api/__viroucaseai_revision e /api/health
uma única vez, imediatamente após ativar workers.dev. Um 523/404/timeout
transitório encerrava a qualificação e acionava a limpeza sem nova leitura.

Reprodução controlada com HTTPX MockTransport: frontend 523 no primeiro acesso,
200 no seguinte; demais endpoints válidos. A v2.0.0.33 falhou com o código
original após três GETs. A correção passou na segunda rodada, com seis GETs,
sem repetir criação, upload ou migration. Isso comprova o defeito no algoritmo;
não recupera retroativamente o status HTTP do incidente de produção.

O novo RemoteVerifier aguarda com backoff de 1, 2, 4, 8 e depois 10 segundos,
até 16 rodadas, respeitando uma janela calculada do timeout já cadastrado:
max(90, min(180, timeout_seconds * 6)). Cada requisição usa o menor valor entre
o timeout HTTP e o tempo restante. O limite de rodadas pode encerrar antes da
janela. Não existe espera infinita nem reenvio automático de operações de escrita.

São reconsultados 404, 408, 425, 429, 5xx, falhas transitórias de transporte e
revisão antiga. Retry-After numérico de 429 é respeitado; quando excede a janela,
a tentativa termina sem insistir. 401/403, redirecionamentos e contratos JSON
inválidos são recusados sem liberar o projeto. Somente leituras GET são repetidas.

Cada rodada precisa aprovar as três respostas juntas. O frontend deve retornar
200 text/html; o endpoint reservado deve confirmar a revisão exata e database=true
booleano; /api/health precisa responder 200 application/json com objeto válido e
não pode informar explicitamente ok=false ou database=false.

### Mistura de perfis

Antes, Hono e Python reutilizavam a mesma revisão de teste. Uma resposta anterior
poderia ser confundida com a do perfil recém-enviado. Agora cada par operação/perfil
recebe uma revisão UUID determinística distinta. A checagem dos bindings também
confirma a tag dessa revisão. Resultados anteriores não aprovam o próximo perfil.
Todos os perfis selecionados continuam obrigatórios; sucesso parcial não libera
publicação automaticamente.

### Binding D1 e reconciliação

A validação exige exatamente DB/d1 e ASSETS/assets, sem bindings adicionais ou
duplicados. Aceita database_id atual e id legado, mas recusa divergência entre
os dois. A mesma compatibilidade é usada na reconciliação de uma publicação
real, que antes procurava somente id. Ausência temporária de settings/tag recebe
até cinco consultas limitadas; binding incorreto é recusado imediatamente.

### Evidência durável

Antes da limpeza, o resultado da operação persiste verification_failure com etapa,
perfil, código, mensagem controlada e detalhes técnicos permitidos. A limpeza grava
seu resultado separadamente em verification_cleanup. Falha de limpeza permanece
reconciling/VERIFICATION_CLEANUP_PENDING e não apaga a falha original.

O endpoint administrativo adiciona failure, failed_stage, stack, stack_results,
remote_verification, cleanup, activity e temporary_resources. O backoffice existente
já inclui esses campos ao copiar/exibir o JSON completo. Os campos anteriores
foram preservados; não há modificação de frontend nesta entrega.

O relatório automático passa a conter verification_format_version=2. Relatórios
automáticos antigos não são aceitos como evidência do novo procedimento; é
necessária uma nova verificação. O contrato independente de qualificação manual
foi preservado. Nenhum booleano administrativo substitui a checagem automática.

## 3. Limites de segurança e escopo

Não foram alterados AppArmor, seccomp, namespaces, UID/GID, Dockerfile, limites de
processos, construção isolada, configuração de tokens, migrations, banco ou login.
O Compose muda apenas a tag da imagem para 2.0.0.34.

As sondagens públicas só aceitam HTTPS no formato worker.conta.workers.dev, sem
credenciais na URL, portas alternativas, query fornecida ou redirecionamentos.
Não recebem o token Cloudflare e descartam cookies entre consultas. As leituras
JSON são limitadas a 64 KiB. Corpos, valores de respostas, cookies, URLs de login
e cabeçalhos de autenticação não são gravados no diagnóstico. HTTP status,
CF-Ray válido, tipo de resposta normalizado, duração e resultados booleanos
permitem investigar sem copiar respostas potencialmente sensíveis.

A cada consulta externa, o callback existente verifica/renova o lease da operação.
Perda de lease interrompe a execução. A prova HTTP e a limpeza continuam anteriores
à liberação. Worker e D1 de outros projetos não são apagados pelos testes.

## 4. Aplicação e aceite

1. Preserve uma cópia dos arquivos substituídos da v2.0.0.33.
2. Copie backend/ do ZIP para a raiz do backend existente. Não substitua .env,
   var/, volumes ou outros arquivos não presentes no pacote.
3. Na raiz do backend, execute como seu usuário normal:

```bash
bash script/restart-publication.sh
```

O script existente preserva UID/GID e recria worker, admin-api e public-api. Pode
haver interrupção breve de requisições. O script de preparo de AppArmor existente
pode solicitar sudo; este patch não altera o perfil nem introduz nova elevação.
Não execute todo o inicializador como root. Não execute down -v, não edite a base
para marcar verificação como aprovada e não desative controles de isolamento.

Confirme o diagnóstico local:

```bash
docker compose exec -T worker python script/diagnose-publication.py --pretty
```

O executor precisa continuar READY. No backoffice, abra a conexão ativa, atualize
o diagnóstico e execute Verificar publicação com um motivo. Um teste antigo com
state=failed não é reaprovado retroativamente. Se houver cleanup pendente, retome
a verificação/limpeza existente antes de solicitar outra.

Espere a operação terminar. O estado correto é verification.state=succeeded,
cleanup.state=succeeded e can_publish=true, respeitadas as demais condições de
acesso, capacidade, revisão e validade da configuração. Em Meus projetos, clique
Verificar novamente antes de Publicar. O Worker temporário desaparecerá mesmo
quando o teste for aprovado; o Worker permanente é criado na publicação do projeto.

Se ainda falhar, o diagnóstico atual terá a causa em verification.failure,
o resultado da limpeza em verification.cleanup e os testes HTTP em
verification.remote_verification. Não será necessário atribuir a falha por
suposição a token, isolamento ou DNS.

Para correlacionar os logs da nova tentativa:

```bash
docker compose logs --since 15m --timestamps --no-color worker admin-api
```

Os eventos publication_check_stage, publication_bindings_observed,
publication_http_probe, publication_http_wait, publication_http_verified,
publication_check_failed e publication_cleanup_completed usam o operation_id
como correlation_id durante a execução normal do worker.

## 5. Rollback

Aguarde ou conclua a limpeza de operações em andamento. Restaure somente os arquivos
substituídos pela versão 2.0.0.33 e a tag anterior no Compose; remova os arquivos
novos deste pacote apenas depois de restaurar seus importadores. Reexecute o
script de reinício. Não remova volumes, configurações Cloudflare ou registros
de operação. Os campos JSON adicionais são expansivos e não exigem rollback SQL.
O rollback recupera também o comportamento de verificação prematura antigo;
não deve ser usado para forçar uma aprovação.

## 6. Evidências e limites da validação

VALIDACAO.json informa comandos, contagens, dependências e resultados reais.
Os testes novos usam HTTPX MockTransport para os endpoints Cloudflare, SQLite
para operações/jobs e um builder controlado. Eles exercitam o gateway real,
multipart de upload, publicação temporária, backoff, leituras públicas, limpeza,
diagnóstico administrativo e liberação do projeto, mas não constituem um deploy
real nem execução de Hono/FastAPI no runtime Cloudflare.

Não há Docker nem credenciais Cloudflare neste ambiente de validação. O teste
final no servidor de Felipe permanece necessário. O ambiente de testes possui
versões de bibliotecas distintas do requirements.lock; as versões utilizadas
estão registradas. A dependência google-auth não está instalada e sua instalação
falhou por indisponibilidade de DNS/rede do ambiente; não alteramos o lockfile.

## 7. Documentação externa consultada

- Cloudflare Get started: descreve 523 temporário no primeiro acesso workers.dev:
  https://developers.cloudflare.com/workers/get-started/guide/
- Contrato dos bindings D1, database_id atual e id legado:
  https://developers.cloudflare.com/api/resources/workers/subresources/scripts/
- Roteamento e publicação de arquivos estáticos:
  https://developers.cloudflare.com/workers/static-assets/
- Distinção entre versão enviada e deployment que atende tráfego:
  https://developers.cloudflare.com/workers/versions-and-deployments/
