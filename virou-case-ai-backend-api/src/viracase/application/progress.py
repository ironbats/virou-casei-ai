from viracase.domain.rules import utcnow

STAGES = {
    "deletion_queued": "Exclusão solicitada. Aguardando o executor.",
    "deletion_remote": "Removendo a publicação e o banco do projeto.",
    "deletion_sources": "Removendo arquivos e rascunhos do projeto.",
    "deletion_complete": "Projeto excluído. A vaga foi liberada.",
    "verification_queued": "Verificação operacional agendada.",
    "verification_isolation": "Testando o isolamento do executor.",
    "verification_cloudflare": "Testando publicação temporária na Cloudflare.",
    "verification_cleanup": "Confirmando remoção dos recursos de teste.",
    "verification_complete": "Verificação operacional concluída.",
    "proposal_discarded": "Proposta descartada. Os arquivos atuais foram preservados.",
    "publication_building": "Preparando o artefato no executor isolado.",
    "publication_provisioning": "Conferindo os recursos do projeto.",
    "publication_migrating": "Aplicando migrations ao banco do projeto.",
    "publication_uploading": "Enviando o artefato à hospedagem.",
    "publication_routing": "Configurando o endereço público.",
    "publication_verifying": "Verificando frontend, API e banco.",
    "publication_verified": "Publicação verificada.",
    "publication_reconciling": "Verificando resultado da publicação. Não repita o envio.",
    "publication_failed": "Não foi possível concluir a publicação.",
    "repairing_response": "Ajustando a resposta recebida para aplicar as alterações com segurança.",
    "awaiting_answer": "Preciso confirmar um detalhe antes de alterar o código.",
    "changes_applied": "Alterações aplicadas e salvas no projeto.",
    "cancelled": "Construção cancelada. A resposta não foi aplicada ao projeto.",
    "continuing_code": "Continuando a implementação com as alterações já construídas.",
    "resizing_code": "Ajustando o tamanho das alterações para concluir os arquivos.",
    "proposal_ready": "Alterações prontas para sua revisão e aplicação.",
    "queued": "Solicitação recebida. Preparando a construção.",
    "validating": "Lendo o pedido e os arquivos da revisão atual.",
    "provider_sent": "Construindo as alterações solicitadas.",
    "validating_result": "Resposta recebida. Conferindo os arquivos gerados.",
    "saving_revision": "Arquivos conferidos. Salvando uma nova revisão.",
    "revision_ready": "Nova revisão salva.",
    "export_ready": "Download preparado.",
    "cancelled_before_dispatch": "Cancelada antes de iniciar a construção.",
    "cancelled_after_dispatch": "Cancelada. A resposta recebida não foi aplicada ao projeto.",
    "provider_result_unknown": "O resultado está em apuração. Aguarde antes de tentar novamente.",
    "reconciling": "O resultado está em apuração. Aguarde antes de tentar novamente.",
    "result_discarded_after_retention": "O resultado não foi aplicado porque o acesso aos arquivos terminou.",
    "retention_expired": "Operação encerrada porque o prazo de acesso terminou.",
}

ERRORS = {
    "PROVIDER_CONFIGURATION_INVALID": "A configuração da integração de IA foi recusada. O operador precisa verificá-la antes de uma nova tentativa. Seu código foi preservado.",
    "PROVIDER_ACCESS_DENIED": "A integração de IA está sem permissão de acesso. Solicite ao operador a revisão das credenciais e permissões.",
    "PROVIDER_MODEL_UNAVAILABLE": "A integração de IA selecionada está indisponível. Solicite ao operador a revisão da configuração.",
    "PROVIDER_CAPACITY_EXCEEDED": "A integração de IA atingiu o limite de uso ou capacidade. Aguarde antes de tentar novamente; se persistir, contate o suporte.",
    "PROVIDER_REQUEST_TOO_LARGE": "O contexto ultrapassou o tamanho aceito pela integração. Seu código foi preservado. Reduza o contexto antes de tentar novamente.",
    "GENERATION_LIMIT": "O pedido ultrapassou o limite de uma construção. Nenhuma alteração parcial foi aplicada. Divida o pedido em etapas menores.",
    "INVALID_GENERATION": "A resposta da IA não passou pela validação, mesmo após a tentativa de correção. Seu código foi preservado. Você pode tentar novamente.",
    "GENERATION_EDIT_MISMATCH": "A IA não conseguiu localizar com segurança os trechos a alterar. Seu código foi preservado. Tente novamente para reconstruir os arquivos afetados.",
    "DRAFT_NOT_SAVED": "Salve as alterações do rascunho antes de enviar o pedido.",
    "GENERATION_TRUNCATED": "A resposta atingiu o limite de tamanho antes de terminar. Sua revisão foi preservada. Divida o pedido em etapas menores ou solicite ao suporte a revisão do limite de geração.",
    "GENERATION_BLOCKED": "A solicitação não pôde ser concluída. Sua revisão foi preservada. Revise o pedido antes de tentar novamente.",
    "GENERATION_INCOMPLETE": "A construção foi interrompida antes de terminar. Sua revisão foi preservada.",
    "GENERATION_NO_CHANGES": "A resposta não trouxe alterações nos arquivos. Detalhe o comportamento esperado e tente novamente.",
    "INVALID_MANIFEST": "A resposta trouxe uma lista de arquivos inválida. Sua revisão foi preservada.",
    "STACK_MISMATCH": "Os arquivos gerados não respeitaram a estrutura do projeto. Sua revisão foi preservada.",
    "INVALID_PATH": "Um arquivo gerado possui um caminho não permitido. Sua revisão foi preservada.",
    "SECRET_DETECTED": "Foi detectada uma credencial nos arquivos gerados. A revisão não foi salva.",
    "FILE_LIMIT_EXCEEDED": "Um arquivo gerado excedeu os limites permitidos. Divida o pedido em etapas menores.",
    "PROJECT_SIZE_EXCEEDED": "O resultado ultrapassou o tamanho permitido para o projeto. Sua revisão foi preservada.",
    "PROVIDER_REJECTED": "Não foi possível iniciar a construção. Tente novamente mais tarde; se persistir, contate o suporte.",
    "PROVIDER_RESULT_UNKNOWN": STAGES["provider_result_unknown"],
    "COST_DISCREPANCY": "O consumo está em conferência. Aguarde antes de iniciar outra tentativa.",
    "CONTEXT_CHANGED": "Os arquivos mudaram antes da construção. Recarregue a revisão atual e envie o pedido novamente.",
    "COMMERCIAL_CONFIGURATION_PENDING": "A construção está temporariamente indisponível. Contate o suporte.",
    "RECOVERY_HOLD": "A construção está pausada para uma verificação operacional. Aguarde a liberação.",
}


def record_progress(operation, stage):
    result = dict(operation.result or {})
    events = list(result.get("activity", []))
    if not events or events[-1]["stage"] != stage:
        events.append({"stage": stage, "at": utcnow().isoformat()})
    operation.progress_stage = stage
    operation.result = {**result, "activity": events[-24:]}


def progress_message(operation):
    if operation.error_code:
        return ERRORS.get(
            operation.error_code,
            "Não foi possível concluir a operação. Sua revisão foi preservada. Contate o suporte se o problema persistir.",
        )
    if operation.cancel_requested and operation.state in {"running", "reconciling"}:
        return "Cancelamento solicitado. Aguardando o encerramento da construção; o consumo já realizado será apurado."
    return STAGES.get(operation.progress_stage, "Acompanhando a operação.")
