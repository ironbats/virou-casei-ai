# Especificação técnica de templates rastreabilidade e revisão de alterações do viroucaseai

Versão 1.0 | 27 de outubro de 2026 | Frontend, backend e backoffice

## 1 Objetivo e limite da entrega

Corrigir a jornada em que Jev recomenda um template, mas a construção fica bloqueada porque seus campos continuam vazios. Acrescentar preenchimento contextual por GPT/Gemini, revisão humana e rastreabilidade dos JSONs realmente enviados e recebidos nas integrações. Melhorar também a leitura e aplicação das alterações de código propostas pelo agente, eliminando rolagens concorrentes e mantendo aprovação explícita.

Este documento orienta outro agente a investigar e implementar. Não contém diagnóstico de código comprovado, implementação executada, testes realizados ou autorização de deploy. As evidências disponíveis são sete prints e o relato de Felipe. Nomes de serviços, DTOs, rotas e tabelas abaixo são contratos lógicos propostos: mapear aos componentes reais antes de alterar o sistema.

## 2 Evidências e reprodução

- Print `Captura de tela de 2026-10-27 20-22-07.png`, Library `libfile_dfa2148d59a481918e3cfdc4e1bff794`: etapa 3 de 3, Revisão, projeto `sistema-de-padaria`, pedido para adicionar botão de login na home que abra um modal.
- Há um anexo, “Autenticação, autorização e isolamento”, v1. A interface mostra: “Preencha os marcadores de Autenticação, autorização e isolamento.”
- Print `Captura de tela de 2026-10-27 20-22-26.png`, Library `libfile_af13a97ef87c81919ee72c244d06ec01`: edição do anexo mostra campos obrigatórios vazios, dois rótulos “Resultado *” e conteúdo com `{{field_1}}`, `{{field_2}}` e demais marcadores não resolvidos.
- Print `Captura de tela de 2026-10-27 20-30-38.png`, Library `libfile_7fe81888af54819197ca4ea9c797252a`: Observabilidade registra `recommendation_pending` e `recommendation_ready`; “Jev: sugestões recebidas”, 3822 ms.
- O recorte não mostra uma etapa de preenchimento por GPT/Gemini. Isso não prova que nenhuma chamada ocorreu em outra parte do backend.
- Fato observado: recomendação/anexo disponível não equivale a template preenchido; o bloqueio por marcadores está visível.
- Hipóteses a investigar: etapa inexistente, chamada não disparada, resposta não mapeada, persistência desatualizada, parser incompatível ou revisão concorrente. Nenhuma delas está confirmada.
- Não “corrigir” removendo a validação e enviando placeholders. Também não concluir, somente pela repetição do rótulo, que os IDs dos campos sejam duplicados.

## 3 Escopo preservado e precedência

- Plataforma React, backend Python/FastAPI, PostgreSQL, três projetos independentes e Docker; preservar `script/run-local.sh`, portas e operação local do backoffice.
- IDE à esquerda e conversa à direita; off-white, azul e navy; manter as três etapas Ideia, Templates e Revisão. Não aplicar identidade MIA.
- Jev recomenda templates. GPT/Gemini preenchem conteúdo e, em outra operação, constroem código. Não adicionar Claude, Grok, imagens, vídeos ou novos provedores.
- Escolha continua livre: zero, um ou vários templates autorizados, recomendados ou não. Não selecionar obrigatoriamente tudo que Jev recomendar.
- Preservar limite de dois projetos, R$75/30 dias, franquia de R$50 de custo GPT/Gemini sem acúmulo, suspensão/retenção no dia 32 e renovação manual.
- Não mostrar orçamento interno em reais ao estudante. Preenchimento por LLM consome a franquia existente e deve aparecer no uso agregado.
- Integrações, modelos, políticas de preenchimento e observabilidade devem ser administráveis pelo backoffice; não introduzir credenciais/configurações de provedor em hardcode ou `.env`.
- Preservar armazenamento seguro já existente; material criptográfico de bootstrap deve permanecer separado do dado cifrado, não exposto no backoffice.
- Publicação Cloudflare, exclusão de projetos e outros módulos não são objeto desta correção. Preservar seus contratos sem declarar suas falhas resolvidas.
- Este documento substitui a restrição INV02 da especificação de fluxo guiado anterior somente neste ponto: antes de Enviar, GPT/Gemini agora podem preencher templates mediante ação explícita de preparação/adaptação.
- Nenhum código será gerado, aplicado, executado ou publicado nessa preparação. “Enviar para construir” continua sendo o gatilho da geração de código, seguido pela revisão/aplicação prevista no fluxo vigente.

## 4 Jornada desejada

1. Usuário descreve a intenção e aciona “Preparar construção”. Jev recomenda opções do catálogo autorizado.
2. Etapa Templates mostra recomendações identificadas como sugestões, acesso ao restante do catálogo e seleção livre.
3. Após escolher anexos, usuário aciona “Preparar templates com IA”. A ajuda informa: “Preenche os templates com seu pedido e contexto. Você revisa antes de construir. Consome seu uso de IA.”
4. Sem outra tela obrigatória, o sistema preenche os anexos selecionados e mostra progresso por template. Não chamar a IA ao marcar cada checkbox, digitar ou abrir um modal.
5. O agente usa pedido, contexto permitido do projeto, versão do template e valores já informados. Retorna campos estruturados, suas fontes e dúvidas essenciais.
6. Campos preenchidos aparecem como rascunho sugerido; usuário pode abrir, editar, aceitar, remover anexo ou continuar manualmente.
7. Se houver informação essencial ausente, perguntar de maneira curta no próprio fluxo; reaproveitar a resposta em todos os campos relacionados. Não exigir que o usuário entenda o formulário técnico completo.
8. Etapa Revisão mostra a mensagem e o conteúdo final dos anexos, com pendências específicas. “Enviar para construir” só envia anexos válidos, na revisão efetivamente exibida.
9. Backend congela snapshots, reserva custo da construção e inicia operação distinta. A saída de preparação nunca deve aparecer como código já implementado.

No caminho sem templates, preservar o envio direto permitido anteriormente. Se Jev falhar, usuário pode selecionar manualmente; preencher os selecionados com GPT/Gemini continua exigindo a ação explícita acima. Não usar LLM silenciosamente como substituto pago do recomendador.

## 5 Invariantes de domínio

- Recomendação, seleção, preenchimento, revisão e construção são estados e operações distintos.
- Nenhuma recomendação adiciona anexos sem escolha do usuário; nenhum preenchimento modifica o catálogo original.
- Nenhum template privado de outro usuário entra nos candidatos, no contexto, no cache ou na telemetria desta operação.
- Nenhuma edição humana é sobrescrita por resposta tardia. Toda atualização exige revisão esperada.
- Conteúdo enviado à construção deve corresponder ao snapshot revisado, não ao template vivo relido posteriormente.
- Um template não fica pronto apenas porque seu corpo contém texto; campos obrigatórios e semântica mínima precisam ser válidos.
- Validação do frontend melhora a experiência; a validação autoritativa sempre é no backend.
- Ausência de observabilidade não pode ser substituída por JSON inventado para parecer uma chamada real.
- Cancelamento não garante interrupção do provedor nem custo zero; resultado tardio não pode ser aplicado automaticamente.

## 6 Investigação obrigatória antes de modificar código

1. Identificar commit/base, migrations, versões dos três projetos e caminho real reproduzido pelos prints.
2. Reproduzir com dados de teste: objetivo do botão/modal e template de autenticação; registrar revisões de catálogo, anexo e rascunho.
3. Rastrear handlers de Preparar, seleção, edição, Revisão e Enviar; determinar qual comando efetivamente chega à API e ao worker.
4. Examinar schema de templates, parser de marcadores, IDs de campo, renderização, ordem, labels, constraints e serialização.
5. Comparar o payload persistido e o DTO retornado com o formulário e o corpo renderizado. Verificar mapeamento por ID versus posição.
6. Localizar os pontos de integração Jev/GPT/Gemini, filas/jobs, retries, timeout, reservas e eventos já existentes.
7. Determinar onde a validação atual falha, com o campo/versão exatos, sem desabilitá-la globalmente.
8. Verificar se rascunhos legados têm placeholders sem schema completo; propor migração compatível e não destrutiva.
9. Separar no relatório final a causa reproduzida, mudanças realizadas e limitações ainda não testadas.

## 7 Modelo de campos e preenchimento

Cada versão de template deve declarar campos por ID estável e semântico, nunca por posição de tela. Para legados `field_1`, criar mapeamento explícito por versão; não renomear snapshots históricos.

Contrato lógico `TemplateFieldDefinition`:
- `id`, `label`, `description`, `type`, `required`, `constraints`, `default_value?`, `sensitivity`, `allowed_source_types`, `display_order`.
- Tipos suportados devem ser um conjunto fechado definido pelo sistema, por exemplo texto, booleano, enum e lista de textos; não aceitar tipos executáveis vindos do modelo.
- Campos com mesmo rótulo e significados distintos precisam de labels descritivas, como “Resultado esperado ao autorizar” e “Resultado esperado ao negar”, após verificar o conteúdo real.
- Campos condicionais precisam de regra determinística declarada; não tornar todos opcionais para contornar falha.
- Defaults são propostas do produto, não fatos descobertos sobre o projeto. Tornar essa diferença visível.

Contrato lógico `DraftFieldValue`:
- `field_id`, `value`, `state`, `source_type`, `source_refs[]`, `suggestion_version`, `edited_by_user`, `updated_at`.
- `state`: `missing`, `suggested`, `confirmed`, `invalid`, `not_applicable` quando o schema permitir.
- `source_type`: pedido, contexto verificado, resposta do usuário, sugestão de política ou edição manual.
- Referências apontam para trechos/revisões reais autorizados. O modelo não pode declarar uma fonte inexistente como prova.
- Não usar confiança autodeclarada do LLM como autorização ou garantia de correção.

Renderizador determinístico combina schema + valores válidos em `resolved_content`. Tratar texto como dado, escapar no HTML e proibir execução de templates, código ou expressões do usuário. Detectar marcadores não resolvidos pelo parser real, não apenas por uma regex que rejeita qualquer uso legítimo de chaves.

## 8 Contexto e comportamento do agente de preenchimento

- Entrada: objetivo atual, mensagem do usuário, resumo autorizado de conversa pertinente, arquivos/metadados relevantes da revisão base, schemas e valores atuais dos anexos selecionados.
- Resolver arquivos no servidor, dentro do projeto autorizado; excluir segredos, `.env`, credenciais, binários e caminhos arbitrários do cliente.
- Orçamento de contexto deve reservar espaço para saída e não truncar requisitos silenciosamente; informar excesso e reduzir por seleção determinística/consentida.
- Schema, pedido e código do projeto são dados não confiáveis. Separá-los das instruções fixas do preenchimento; nenhuma instrução nesses dados concede ferramentas ou permissões.
- O agente de preenchimento não recebe ferramentas de shell, filesystem de escrita, publicação ou mutação do projeto.
- Resposta deve fornecer somente IDs de campos existentes, valores do tipo correto, origem indicada, perguntas essenciais e conflitos detectados.
- Se provedor suportar saída estruturada, usar adaptador apropriado; caso contrário, parsear e validar estritamente. Não presumir o mesmo contrato entre GPT e Gemini.
- Validar JSON, tipos, IDs desconhecidos/duplicados, tamanhos, enums, required e consistência antes de persistir como sugestão.
- Formato válido não garante semântica correta: verificar referências, escopo e incompatibilidades com contexto disponível.
- Não inventar endpoints, credenciais, regras de autorização, tenant, políticas do negócio ou evidência de testes. Ausência essencial resulta em pergunta.
- Pode sugerir uma regra segura, explicitamente como proposta a confirmar; jamais apresentá-la como regra existente do projeto.
- Reparação de saída inválida, se habilitada, deve ter limite configurado, reserva de custo e evento próprio; evitar laço infinito.

### Exemplo de comportamento para o caso dos prints

O pedido já fornece “botão de login na home” e “abrir modal”. Esses fatos podem preencher intenção e interação desejada. A existência de `login.html` ou `login.js` no print não prova contrato de autenticação, endpoint, sessão ou isolamento. O implementador deve inspecionar o contexto autorizado do projeto; se faltar decisão essencial, perguntar, por exemplo, qual fluxo de autenticação existente será reutilizado. Não obrigar o usuário a inventar controles técnicos só para remover o erro.

## 9 Estados e concorrência

Operação de preenchimento: `queued → filling → ready | needs_input | failed | cancelled`. Acrescentar `cancellation_requested` enquanto houver chamada externa incerta; marcar `stale` quando a revisão base não for mais aplicável.

- `ready` significa sugestões válidas disponíveis para revisão, não aprovação automática do usuário.
- `needs_input` preserva campos já úteis e aponta perguntas/IDs pendentes. Valores inexistentes ficam nulos/pendentes, não “a definir” para enganar required.
- Estado por anexo permite mostrar preenchimento parcial, mas o lote não pode esconder falhas de um item como sucesso total.
- Uma operação referencia `draft_revision`, `attachment_revisions`, `project_revision`, versões dos templates, política e modelo resolvido.
- Edição durante preenchimento cria nova revisão. Comparar campo a campo; preservar edição humana e oferecer aplicar sugestão somente nos campos ainda compatíveis.
- Mudar seleção/objetivo invalida resultados afetados, sem apagar o rascunho. Remover um anexo não permite que resposta tardia o reanexe.
- Uma intenção tem chave idempotente e fingerprint canônico no servidor; mesma chave/payload retorna a operação, chave reutilizada com payload diferente retorna conflito.
- Despacho durável/outbox ou mecanismo equivalente existente deve recuperar queda entre commit e chamada externa. Não manter transação de banco aberta na rede.
- Depois de timeout com efeito incerto, reconciliar antes de reenviar; não repetir automaticamente uma chamada que pode ter sido cobrada.
- Cancelamento é idempotente; ao chegar resultado tardio, conciliar custo real, descartar aplicação no rascunho e registrar o desfecho.

## 10 Contratos de backend propostos

```text
PrepareTemplatesCommand
  draft_id, expected_draft_revision, project_revision, attachment_ids[]
  expected_attachment_revisions{}, idempotency_key
TemplatePreparation
  id, project_id, draft_id, state, state_version, base_revisions
  provider_connection_ref, resolved_model, policy_version, reservation_id
  items[{attachment_id, state, suggested_fields[], questions[], errors[]}]
  correlation_id, last_event_sequence, created_at, completed_at?
PreparationAnswer
  preparation_id, expected_revision, answers[{question_id, value}]
ApplyFieldSuggestions
  preparation_id, expected_draft_revision, accepted_field_ids[]
  expected_attachment_revisions{}, idempotency_key
SendBuildCommand
  draft_id, expected_draft_revision, project_revision, idempotency_key
ResolvedAttachmentSnapshot
  source_template_id, source_version_id, draft_attachment_revision
  schema_version, field_values, resolved_content, content_hash
  preparation_id?, reviewed_at, reviewed_by, source_provenance
```

- Proposta de endpoints: `POST /api/projects/{id}/template-preparations`, `GET /.../{preparation_id}`, `POST /.../{preparation_id}/answers`, `POST /.../{preparation_id}/apply`, `POST /.../{preparation_id}/cancel`.
- Reaproveitar rotas existentes equivalentes quando possível. Envio inicial retorna `202` e ID, não afirma preenchimento concluído.
- A API deriva usuário/tenant da sessão; autoriza projeto, anexos, operação e snapshot em toda leitura/escrita.
- Enviar para construir valida conteúdo resolvido, revisão e permissões novamente; cria mensagem/snapshots/reserva de construção atomicamente.
- Erros distinguem `TEMPLATE_SCHEMA_INVALID`, `REQUIRED_FIELD_MISSING`, `UNRESOLVED_PLACEHOLDER`, `MODEL_OUTPUT_INVALID`, `CONTEXT_INSUFFICIENT`, `CONTEXT_TOO_LARGE`, `DRAFT_CONFLICT`, `INSUFFICIENT_BALANCE`, `PROVIDER_UNAVAILABLE`, `OPERATION_UNCERTAIN` e autorização.
- Erro de campo inclui `attachment_id`, `field_id`, motivo e ação possível; não retornar apenas “preencha os marcadores”.

## 11 Carteira e contabilização

- Reservar custo máximo permitido para preenchimento antes da chamada GPT/Gemini; bloquear com mensagem clara se saldo insuficiente.
- Orçamento de preenchimento e construção são operações separadas da mesma franquia, sem duas cobranças pelo mesmo evento.
- Calcular uso pelos dados efetivos do provedor e tabela/configuração versionada, não por estimativa exibida como consumo final.
- Conciliar reserva com uso real, liberar excedente e registrar falhas/uso parcial. Ausência de usage resulta em pendência de conciliação, não custo zero inventado.
- Retry e reparação de JSON precisam respeitar limite de tentativas/custo por intenção; concorrência não pode gastar acima do saldo disponível.
- Jev mantém medição/política própria. Não descontar silenciosamente sua cobrança da franquia GPT/Gemini.
- Não expor chaves, custo interno em reais ou margem ao estudante; administrador vê valores autorizados e estudante vê seu uso agregado em percentual, conforme a interface existente.

## 12 Frontend da plataforma

- Etapa Templates: lista de selecionados, ação “Preparar templates com IA”, alternativa “Preencher manualmente” e indicador de consumo de IA sem valores internos.
- Durante a operação, mostrar “Preparando 1 de 2 templates”, estados reais por item e Cancelar. Número é dado real, não animação de progresso fictícia.
- Permitir ler e editar rascunho enquanto prepara; controlar conflitos em vez de travar toda IDE.
- Editor: campos com label/ajuda, origem da sugestão e estado; oferecer aceitar/editar individualmente e “Usar sugestões válidas” sem sobrescrever edições humanas.
- Perguntas essenciais ficam perto dos campos afetados. Não renderizar dezenas de campos vazios como tarefa obrigatória do usuário.
- Corpo final do anexo deve ser legível e representar os valores atuais, sem marcadores residuais. Oferecer comparação antes/depois.
- Etapa Revisão: anexos como chips/cards com Abrir, Editar e Remover; exibir “Precisa de informação” com link direto ao campo pendente.
- Se não pronto, botão Enviar informa exatamente o impedimento. Caminho manual e remoção de anexo opcional continuam disponíveis.
- Se pronto e revisado, envio usa a revisão exibida. Voltar de etapa, refresh ou reconexão não deve perder valores confirmados.
- Após envio, mostrar preparação e construção separadas no histórico/status, sem declarar aplicação de código concluída.
- Navegação por teclado, foco no erro, `aria-live` para status sem spam, labels associadas, contraste e modal utilizável em celular/zoom.
- Não modificar layout aprovado além dos controles necessários, nem introduzir seletor público de modelos nesta mudança.

## 13 Observabilidade de Jev e do preenchimento

Mostrar a estrutura e os valores permitidos dos JSONs efetivamente transmitidos/recebidos, após proteção de dados. Não basta registrar “sugestões recebidas”. O rótulo deve dizer “Payload real com dados protegidos”, e indicar omissões/truncamento; não prometer bytes brutos íntegros quando houve redação.

Instrumentar a fronteira real do adaptador, depois da serialização da requisição e na recepção da resposta, antes do mapeamento de domínio. Capturar também o resultado normalizado em seção separada. Se SDK esconder a carga, usar hook suportado/adaptador verificável; até existir captura, mostrar “Payload não capturado”, jamais reconstruir uma requisição e apresentá-la como enviada.

Span de recomendação Jev:
- ID da operação, correlation/trace ID, span e parent span, tenant/projeto autorizados, versão de rascunho, timestamps UTC, duração e tentativa.
- Referência não secreta da conexão, modelo realmente usado, endpoint sem query sensível, status HTTP/código do SDK e request ID do provedor quando disponível.
- Request JSON sanitizado: estrutura/schema de saída solicitado, campos, candidatos e IDs/versões, contexto efetivamente permitido.
- Response JSON sanitizado: resposta real, erros e campos presentes. Choice/probabilidades/confidence somente quando retornados; não fabricar campos para outros tipos de resposta.
- Resultado mapeado: opções recomendadas, versões, resultado da validação e fallback. Jev recomendar não significa template preenchido.

Span de preenchimento GPT/Gemini:
- Separado do span Jev e relacionado pela mesma operação de jornada e `preparation_id`.
- Request/response sanitizados, modelo/conexão efetivos, schema solicitado, revisões/contexto de entrada e tokens/uso quando informados.
- Validação, perguntas pendentes, sugestão por campo e eventual reparação com tentativa própria.
- Mapeamento original → sugerido → edição humana → valor final enviado, incluindo IDs/versões/origem, sem revelar conteúdo protegido.
- A construção posterior tem span próprio ligado ao snapshot; deve permitir saber qual valor realmente chegou ao agente construtor.

Evento lógico `IntegrationTrace`:
- `id`, `trace_id`, `span_id`, `parent_span_id`, `sequence`, `operation_id`, `stage`, `provider`, `direction`, `attempt`, `occurred_at`, `duration_ms?`, `status`.
- `safe_request_json?`, `safe_response_json?`, `normalized_result?`, `redaction_policy_version`, `redacted_paths[]`, `truncated_paths[]`, `payload_capture_state`, `schema_version`.
- `draft_revision`, `template_versions[]`, `preparation_id?`, `snapshot_id?`, `provider_request_id?`, `error_code?`, `usage?`.
- Campos opcionais devem ser omitidos ou marcados indisponíveis quando não recebidos. Não inferir latência, tokens ou sucesso com eventos sintéticos.

## 14 Segurança e retenção dos JSONs

- A observabilidade permanece exclusiva ao backoffice autenticado/local, com permissão administrativa específica para inspecionar payloads. Não confiar somente na porta local.
- Autorizar cada consulta, expansão e cópia no backend; separar leitura de metadados de leitura de payload. Auditar acesso administrativo sem copiar o payload nesse novo log.
- Antes de persistir ou emitir em logs, remover Authorization, API keys, cookies, tokens, senhas, segredos em JSON, URLs e texto livre; aplicar allowlist de campos e redação recursiva.
- Prompts/contextos podem conter dados pessoais ou de negócio. Preservar estrutura e valores técnicos permitidos; substituir conteúdo sensível por marcador e motivo, sem guardar uma cópia “bruta” escondida.
- Uma regex isolada não basta: schema/classificação/allowlist e testes adversariais devem proteger strings, arrays, campos desconhecidos, erros do SDK e respostas ecoadas.
- Payload desconhecido ou impossível de sanitizar falha fechado para conteúdo: armazenar somente metadados e motivo da indisponibilidade.
- Redação para observabilidade não deve alterar o payload operacional enviado ao provedor; redução de dados operacionais é responsabilidade própria do construtor de contexto.
- Não transmitir segredos encontrados nos arquivos ao provedor para obter preenchimento. Não registrar headers completos nem corpo de erro sem sanitização.
- No MVP, propor retenção de payloads de 7 dias, alinhada ao período mostrado no print, com limite de bytes por payload/operação e teto total configuráveis no backoffice.
- A política deve ser efetiva no banco/armazenamento/backups conforme retenção aprovada; não basta ocultar registros na UI. Alteração de retenção não altera automaticamente histórico já eliminado.
- Limites devem cortar somente a cópia de observabilidade, marcar truncamento e preservar JSON parseável. Não truncar o request de negócio silenciosamente.
- Ao expirar/excluir projeto, cumprir a política existente para conteúdo e snapshots; manter somente registros financeiros/metadados cuja retenção esteja prevista, sem preservar prompts indefinidamente.

## 15 Backoffice de observabilidade

- Preservar filtros Serviço, Nível, ID da operação e Projeto, atualização a cada 3 segundos, Pausar e Atualizar monitor; não duplicar pollers ao navegar.
- Adicionar filtro Etapa: Recomendação Jev, Preenchimento, Revisão e Construção, além de status e intervalo temporal.
- Linha mostra duração, estado e indicação Request/Response disponíveis, protegidos, truncados ou não capturados.
- Expansão: abas “Request”, “Response”, “Resultado mapeado” e “Campos”, com JSON formatado, busca e botão Copiar apenas o conteúdo sanitizado exibido.
- Aba Campos compara por ID estável: valor inicial, sugestão, origem, edição do usuário, pendência e versão final; não comparar por posição da lista.
- Linha do tempo liga Jev → preparação GPT/Gemini → revisão → construção. Tentativas ficam agrupadas, sem aparentar múltiplos pedidos do usuário.
- JSON longo usa carregamento sob demanda/paginação ou virtualização; manter filtros e rolagem ao atualizar. Pausar interrompe polling, não execução do projeto.
- Falha, timeout e resposta inválida são visíveis. Não trocar erro por “ready” somente porque HTTP retornou 200.
- Exibir horários locais com referência ao fuso e duração calculada consistentemente; persistir timestamps em UTC.
- Configurações administrativas: captura sanitizada, limites, retenção, política de campos permitidos e provedor/modelo de preenchimento; alterações auditadas e versionadas.

## 16 Persistência e evolução compatível

- Reutilizar rascunhos, anexos, operações, carteira e eventos existentes. Criar apenas o necessário para valores/proveniência/revisões/preparações e payloads sanitizados.
- Tabelas lógicas possíveis: `template_preparations`, `template_preparation_items`, `draft_field_values`, `integration_trace_payloads`; mapear ou estender estruturas reais.
- Unicidade de idempotência por usuário/projeto/intenção, foreign keys, integridade de versão e índices por projeto/operação/etapa/data para consulta paginada.
- Payloads não precisam ficar no mesmo registro quente dos eventos. Definir acesso e retenção independentes; não duplicar JSON em múltiplas tabelas/logs.
- Migração deve partir da última versão realmente instalada. Há relato de schema `0006_runtime_observability.py` em outra entrega, mas confirmar a base e não reutilizar/alterar número cegamente.
- Adotar mudanças aditivas, compatibilidade temporária com clientes antigos, preenchimento de dados legado por versão e rollback do código sem apagar anexos ou registros financeiros.
- Flags de liberação no backoffice separam preenchimento e captura de payload. Desligar captura interrompe nova persistência; não apaga dados sem política.
- Rollback mantém leitura de rascunhos anteriores e caminho manual. Não executar migração destrutiva para “limpar” inconsistências.

## 17 Critérios de aceite e testes a executar

Todos os itens abaixo são exigências de validação futura, não testes já executados nesta especificação.

1. Reproduzir erro dos prints e demonstrar causa com trace e código antes da correção.
2. Pedido do botão/modal com contexto suficiente preenche template e permite revisão/envio sem preenchimento manual de todos os campos.
3. Jev recomenda, mas somente templates escolhidos são preenchidos; seleção de zero, um e vários permanece funcional.
4. Template não recomendado pode ser escolhido/preenchido; falha do Jev mantém catálogo manual.
5. Preparação não gera código nem toca arquivos, publicação ou execução do projeto.
6. Falta essencial gera pergunta objetiva; resposta conhecida é reutilizada e nenhum endpoint/política é inventado.
7. Placeholder obrigatório remanescente bloqueia envio com campo específico; texto literal legítimo com chaves não é bloqueado indevidamente.
8. IDs/labels repetidos e schemas legados são tratados explicitamente; fixtures cobrem dois campos distintos antes chamados Resultado.
9. JSON inválido, ID desconhecido, tipo errado, campo duplicado e saída excessiva são rejeitados sem apagar rascunho.
10. Edição humana durante preenchimento, troca de objetivo e remoção de anexo não são sobrescritas por resposta tardia.
11. Duplo clique/retry de rede cria uma operação e uma reserva; chave com payload diferente retorna conflito.
12. Queda após commit, timeout incerto e cancelamento tardio reconciliam operação/custo sem chamada ou cobrança duplicada.
13. Saldo insuficiente impede chamada paga; custo real de preparação e construção é separado e conciliado.
14. Snapshot enviado corresponde à versão revisada; edição posterior do catálogo não altera mensagem histórica.
15. Cross-tenant em projeto, template privado, operação, trace, cache, cópia e snapshot é negado no backend.
16. Request/response Jev capturados correspondem ao tráfego de teste conhecido; resultado normalizado aparece separado e não substitui a captura real.
17. Trace mostra Jev e preenchimento GPT/Gemini como spans distintos e vincula valores finais ao snapshot de construção.
18. Segredos semeados em headers, URLs, campos aninhados, texto livre e erros não aparecem no banco, stdout, resposta admin ou clipboard.
19. Payload não capturado, truncado, redigido ou inválido é explicitamente identificado; sem JSON fictício.
20. Filtros, pausas, reconexão, paginação e payloads grandes não perdem estado nem multiplicam polling.
21. Navegação por teclado, foco de erro, leitor de tela e celular permitem revisar/editar/confirmar sem modal inacessível.
22. Migração em PostgreSQL real, concorrência de saldo/revisões e rollback preservam dados e contratos.
23. Executar regressão de login, dois projetos, renovação/vencimento, download, catálogo, chat, publicação e scripts Docker sem alegar validar Cloudflare real por mock.
24. Demonstrar pelo menos uma integração autorizada real de Jev e do provedor de preenchimento configurado; para adaptador não exercitado, declarar limitação separadamente.

## 18 Evidências da revisão de código

- Prints `Captura de tela de 2026-10-27 20-35-19.png`, `20-35-35.png`, `20-35-52.png` e `20-36-04.png`: fluxo após a proposta de alterações do agente.
- O chat mostra “Alterações prontas”, indica estrutura conferida e ressalva que testes/execução ainda precisam ser realizados; oferece Descartar proposta, Ver revisão/histórico e Revisar alterações.
- Modal “Revise as alterações propostas” empilha pares “Revisão atual” e “Proposta” por arquivo. As caixas têm barras verticais próprias, além da rolagem externa do modal.
- Arquivos visíveis incluem `public/app.js`, `public/index.html`, `public/modal.js` criado, `public/style.css` e `src/index.ts`. São dados do exemplo, não lista fixa de arquivos da plataforma.
- “Aplicar alterações revisadas” aparece somente ao fim do conteúdo. Nas partes vistas, há blocos antes/depois redundantes, sem realce de diff que facilite localizar alterações.
- Fato visual: múltiplas rolagens e comparação extensa dificultam ler e alcançar a ação. Causa de CSS/DOM, acessibilidade e comportamento de aplicação ainda precisam de inspeção do implementador.

## 19 UX da revisão e aplicação das alterações

Esta mudança organiza a proposta já gerada. Não altera silenciosamente a lógica de geração nem autoriza aplicação automática.

1. No chat, manter um resumo compacto: arquivos alterados/criados/removidos, estado da proposta, verificação efetivamente executada e botão “Revisar alterações”. Não renderizar todos os blocos de código dentro da conversa.
2. Ao revisar, abrir painel ampliável na área de trabalho ou overlay amplo, preservando contexto e rascunho do chat. Reutilizar o componente compatível com a arquitetura real da interface.
3. Cabeçalho estável: título, identificação da proposta/revisão base, resumo de alterações e aviso real de testes. Fechar retorna ao chat sem aplicar nem descartar.
4. Navegação por arquivos à esquerda no desktop; lista compacta/seletor no celular. Selecionar arquivo muda apenas a leitura, não o conjunto a aplicar.
5. Área central mostra um arquivo por vez, com diff unificado por padrão, números de linha, adições/remoções identificadas por símbolo e cor e contexto suficiente.
6. Oferecer comparação lado a lado somente em largura adequada e como alternativa; nunca empilhar dois editores altos para cada arquivo de todo o lote.
7. Arquivo novo: indicar “Novo arquivo” e adições. Exclusão: indicar remoção e conteúdo afetado. Renomeação/binário/tamanho não suportado: explicar a operação sem fingir diff textual.
8. Trechos não alterados podem ser recolhidos com ação para expandir contexto. Busca por arquivo/texto e navegação entre mudanças ajudam a revisar diffs extensos.
9. Rodapé fixo e visível: “Voltar”, “Descartar proposta” e “Aplicar alterações revisadas”. Não colocar a única ação de aplicar depois de milhares de linhas.
10. Antes da aplicação, mostrar claramente que o botão aplica o conjunto validado da proposta. Não fazer o usuário acreditar que somente o arquivo aberto será aplicado.

### Rolagem e leitura

- Definir um único proprietário da rolagem vertical do conteúdo de diff; evitar body, modal, bloco antes e bloco depois rolando verticalmente ao mesmo tempo.
- Lista lateral pode ter sua rolagem independente somente quando necessária, claramente separada; não capturar a roda de maneira imprevisível sobre o diff.
- Cabeçalho e rodapé permanecem acessíveis sem cobrir linhas; reservar espaço real para ambos e respeitar teclado virtual/safe area no celular.
- No celular, usar revisão em tela inteira com um arquivo por vez; não comprimir duas colunas de código lado a lado.
- Código pode ter rolagem horizontal própria, mas nunca provocar overflow horizontal da página inteira. Oferecer quebra de linha opcional sem alterar o arquivo.
- Preservar posição e arquivo ao voltar de um detalhe ou atualizar status. Fechar/abrir revisão não perde a proposta.
- Diff grande deve usar cálculo/paginação/virtualização compatível, sem congelar a tela. Informar se a visualização está parcial e permitir acessar o restante; truncamento silencioso é proibido.
- Aplicar syntax highlighting como uma IDE: cores semânticas por linguagem suportada, incluindo JS/TS, Python, HTML, CSS, SQL e JSON, com indentação e números de linha preservados.
- Cores dos tokens (palavras-chave, strings, comentários e números) devem coexistir com fundo/marcadores de adição e remoção; diff não pode apagar legibilidade da sintaxe.
- Reutilizar editor/highlighter existente se adequado, com carregamento controlado de linguagens; arquivo desconhecido tem fallback explícito para texto, sem falhar a revisão.
- Realce de sintaxe não equivale a LSP, compilação, análise semântica ou execução. Não declarar código correto somente porque foi colorido.
- Escapar conteúdo, não executar HTML/JS do diff e não usar realce que interprete o código como markup ativo.
- Foco inicial no título, ordem de tabulação previsível, Escape apenas fecha a revisão quando seguro e retorno de foco ao botão que a abriu.
- Não comunicar adição/remoção somente por cor. Leitor de tela deve identificar arquivo, tipo de alteração e estado da aplicação.

### Avisos que precisam continuar visíveis

“Estrutura conferida” não significa “testado”, “seguro” ou “funcionando”. Mostrar verificações efetivas com estados separados: não executada, em execução, aprovada ou falhou. Não remover a ressalva atual sobre testes/execução apenas para simplificar a tela. O ato de o usuário revisar não transforma teste pendente em teste aprovado.

## 20 Contrato da proposta e aplicação

- Reutilizar a proposta existente, com ID, versão, revisão base do projeto, manifesto de arquivos e estado. Backend é a fonte autoritativa.
- Estados lógicos: `generated`, `reviewing`, `applying`, `applied`, `failed`, `conflict`, além de descartada/cancelada se já previstas. `reviewing` não concede lock permanente nem altera arquivos.
- A abertura do painel consulta a versão atual e sua revisão base; versão nova não pode substituir silenciosamente a que o usuário está lendo.
- Contrato lógico de leitura: `ProposalReview {proposal_id, proposal_version, base_project_revision, state, files[], verification_results[], warnings[]}`.
- Cada arquivo inclui caminho normalizado, operação, hashes/revisões de origem e destino quando disponíveis, diff e estado de carregamento. Validar caminhos no servidor e impedir traversal.
- Contrato lógico de aplicação: `ApplyProposal {proposal_id, expected_proposal_version, expected_project_revision, idempotency_key}`.
- Seleção de arquivo serve para navegação. Não introduzir aplicação parcial, checkboxes por hunk ou seleção granular sem contrato já existente e validado; esta especificação não pede tal funcionalidade.
- Aplicar o conjunto validado como unidade atômica na arquitetura real, usando staging/commit e recuperação apropriados ao armazenamento. Não prometer atomicidade de vários arquivos apenas com uma transação SQL.
- Revalidar acesso, estado, revisão base, manifesto e integridade no momento de aplicar. Mudança concorrente do usuário/agente deve retornar `conflict`, sem sobrescrever a revisão atual.
- Em conflito, preservar proposta e edições, explicar quais arquivos/revisões mudaram e oferecer comparar ou gerar nova proposta explicitamente. Não fazer merge inseguro oculto.
- Duplo clique e retry retornam o mesmo resultado para a mesma chave; não criar nova revisão ou aplicar patch duas vezes.
- Durante `applying`, desabilitar ações incompatíveis e mostrar status real; em falha incerta, consultar/reconciliar o resultado antes de permitir repetir.
- Atualizar arquivos/preview da IDE e histórico somente após confirmação do backend. Não mostrar “Aplicado” por timeout ou por mero aceite `202`.
- Falha recuperável mantém a proposta e não deixa lote parcialmente aplicado sem indicação/recuperação. Logs registram fases e revisão final, sem conteúdo sensível desnecessário.
- Descartar exige ação explícita com consequência clara; não apagar histórico/snapshots necessários para rastreabilidade. Fechar o painel não equivale a descartar.

## 21 Critérios adicionais de aceite da revisão

25. Reproduzir o caso dos quatro prints e documentar a árvore de rolagem real antes da alteração.
26. Desktop, celular e zoom de 200% permitem escolher arquivo, ler diff e acessar ações sem rolagens verticais aninhadas concorrentes ou conteúdo coberto.
27. Diff unificado destaca adições/remoções, arquivos novos/excluídos e contexto; aviso de testes pendentes permanece visível e correto.
28. Cinco arquivos e um diff extenso são revisáveis sem empilhamento de todos os pares antes/depois, congelamento ou truncamento silencioso.
29. Abrir outro arquivo não muda o conjunto aplicado; aplicação parcial não aparece por acidente.
30. Edição concorrente após gerar proposta produz conflito e preserva ambas as versões; nunca sobrescreve trabalho silenciosamente.
31. Duplo clique, refresh durante aplicação, resposta tardia e falha entre escritas não duplicam aplicação nem anunciam sucesso incerto.
32. Teclado, foco, leitor de tela, quebra de linha e conteúdo hostil no diff são cobertos por testes; realce não executa código.
33. Aplicação confirmada atualiza IDE e histórico para a revisão correta; fechar a revisão mantém proposta; descarte explícito não afeta arquivos atuais.
34. JS/TS, Python, HTML, CSS, SQL e JSON têm realce legível de sintaxe e diff ao mesmo tempo; linguagem desconhecida usa texto seguro; contraste, indentação e números de linha são preservados.

## 22 Entrega exigida do implementador

Entregar diagnóstico reproduzível, mapa dos contratos reais versus propostos, mudanças por projeto, migrations/rollback, configuração no backoffice, testes e evidências de UI/trace sanitizados. Separar testes unitários com mocks, integração local e chamadas reais autorizadas. Não incluir credenciais, conteúdo privado de usuários ou afirmar segurança/publicação validadas sem os respectivos testes.

A mudança só está concluída quando o usuário consegue preparar, revisar e enviar templates preenchidos com controle, o administrador consegue rastrear o request, o response e a transformação dos campos sem exposição indevida de dados, e a revisão de código permite ler e aplicar a proposta com clareza, sem sobrescrita indevida.
