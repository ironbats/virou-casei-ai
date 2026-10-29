def managed_publication_guidance(stack):
    if stack not in {"hono-typescript-d1", "fastapi-python-d1"}:
        return ""
    return (
        "Na publicação gerenciada pela plataforma, o servidor provisiona ou reutiliza o banco D1 autorizado "
        "e associa o binding DB; os arquivos públicos usam o binding ASSETS. "
        "O servidor determina database_id, account_id e nome do Worker a partir da configuração e do recurso do projeto. "
        "Esses valores não precisam ser preenchidos no wrangler.jsonc para esta publicação; "
        "um identificador vazio ou ilustrativo nesse arquivo, isoladamente, não é defeito do código. "
        "Não solicite esses identificadores nem credenciais para corrigir o projeto, mesmo se o diagnóstico "
        "ou uma pergunta anterior os tiver solicitado. Prossiga com as correções demonstradas nas fontes. "
        "Preserve o wrangler.jsonc e a configuração de exportação quando não houver mudança necessária. "
        "Confira e corrija o uso incorreto dos bindings no código quando houver evidência; "
        "a gestão automática de identificadores não torna um binding incorreto válido.\n"
    )
