import json

import httpx

from viracase.infrastructure.providers import HttpModelGateway


REQUEST_CONTEXT = (
    "O Cliente autenticado pode criar e consultar somente os próprios pedidos. "
    "O recurso é o pedido identificado pelo id. "
    "Identificar o dono pelo usuário da sessão validada no servidor. "
    "Permitir acesso quando o cliente da sessão corresponde ao dono do pedido."
)

FIELD_VALUES = {
    "field_1": "Cliente autenticado",
    "field_2": "Criar e consultar somente os próprios pedidos",
    "field_3": "Pedido identificado pelo id",
    "field_4": "Usuário da sessão validada no servidor",
    "field_5": "Permitir acesso quando o cliente da sessão corresponde ao dono do pedido",
    "field_6": "Proposta: negar acesso quando a sessão não estiver autorizada para o pedido.",
    "field_7": "Critério proposto: testar com dois clientes e impedir acesso aos pedidos do outro cliente.",
    "field_8": "Proposta: solicitar novo login quando a sessão expirar, sem liberar o pedido.",
    "field_9": "Proposta: informar que o pedido não está disponível sem revelar dados de outro cliente.",
    "field_10": "Proposta: registrar a decisão de acesso ao pedido sem senha, token ou conteúdo privado.",
}

PROPOSAL_VALUES = {
    **FIELD_VALUES,
    "field_2": "O Cliente autenticado pode criar e consultar somente os próprios pedidos.",
    "field_3": "pedido identificado pelo id.",
    "field_4": "usuário da sessão validada no servidor.",
    "field_5": "Permitir acesso quando o cliente da sessão corresponde ao dono do pedido.",
}


def prompt_sources(prompt):
    if prompt.get("template_preparation_contract") != 3:
        return prompt["context"]
    sources = {}
    for unit in prompt["selection_catalog"]:
        source = sources.setdefault(
            unit["source_ref"],
            {"id": unit["source_ref"], "type": unit["source_type"], "content": ""},
        )
        source["content"] += " " + " ".join(token[1] for token in unit["tokens"])
    return list(sources.values())


def select_phrase(prompt, source_ref, phrase):
    target = "".join(phrase.split()).casefold()
    for unit in prompt["selection_catalog"]:
        if unit["source_ref"] != source_ref:
            continue
        tokens = unit["tokens"]
        for start in range(len(tokens)):
            text = ""
            for end in range(start, len(tokens)):
                text += "".join(tokens[end][1].split()).casefold()
                if text.rstrip(".,;!?") == target.rstrip(".,;!?"):
                    return {"unit": unit["id"], "start": tokens[start][0], "end": tokens[end][0]}
                if len(text) > len(target):
                    break
    raise AssertionError(f"Synthetic fixture phrase is absent from {source_ref}: {phrase}")


class RealisticPreparationGateway:
    def __init__(self, config):
        self.calls = {}
        self.field_calls = {}
        self.gateway = HttpModelGateway(config, transport=httpx.MockTransport(self.respond))

    def generate(self, policy, prompt):
        return self.gateway.generate(policy, prompt)

    def respond(self, request):
        body = json.loads(request.content)
        assert body["text"]["format"]["strict"] is True
        data = json.loads(body["input"])
        version = data.get("template_preparation_contract", 1)
        native_values = version == 2
        source = next(item for item in prompt_sources(data) if item["id"] == "request:message")
        message = source["content"].strip()
        self.calls[message] = self.calls.get(message, 0) + 1
        focused = {
            field["key"] for template in data["templates"] for field in template["fields"]
        } == {"field_4"}
        field_scenario = message.startswith(
            ("Gerar campo pendente no pedido atual.", "Falha ao gerar campo pendente.")
        )
        if field_scenario and focused:
            self.field_calls[message] = self.field_calls.get(message, 0) + 1
            if message.startswith("Falha ao gerar campo pendente.") and self.field_calls[message] == 1:
                return httpx.Response(
                    400,
                    json={"error": {"code": "invalid_request_error", "param": "input", "message": "Synthetic focused rejection"}},
                )
        if message.startswith("Corrigir cadastro e login de clientes na padaria") and self.calls[message] == 1:
            return httpx.Response(400, json={"error": {"code": "invalid_request_error", "param": "input", "message": "Synthetic controlled rejection"}})
        items = {} if version in {3, 4} else []
        for template in data["templates"]:
            assert template["attachment_id"] == "autorizacao"
            fields = {} if version in {3, 4} else []
            for field in template["fields"]:
                key = field["key"]
                factual = int(key.split("_")[1]) <= 5
                if version == 4:
                    fields[key] = {
                        "proposal": {"invalid": "synthetic field failure"}
                        if field_scenario and not focused and key == "field_4"
                        else PROPOSAL_VALUES[key]
                    }
                elif version == 3:
                    fields[key] = (
                        {"extracts": [select_phrase(data, source["id"], FIELD_VALUES[key])]}
                        if factual else {"proposal": FIELD_VALUES[key]}
                    )
                elif native_values:
                    fields.append({
                        "field_id": key,
                        "value": FIELD_VALUES[key],
                        "citations": [{"source_ref": source["id"], "quote": REQUEST_CONTEXT}]
                        if factual else [],
                    })
                else:
                    fields.append({
                        "field_id": key,
                        "value_json": json.dumps(FIELD_VALUES[key], ensure_ascii=False),
                        "source_type": "request" if factual else "policy_suggestion",
                        "source_refs": [source["id"]] if factual else [],
                        "evidence": REQUEST_CONTEXT if factual else "",
                    })
            if version in {3, 4}:
                items[template["attachment_id"]] = fields
            else:
                items.append({"attachment_id": template["attachment_id"], "fields": fields, "questions": []})
        value = json.dumps({"items": items}, ensure_ascii=False)
        return httpx.Response(200, json={
            "id": "synthetic-realistic-preparation",
            "status": "completed",
            "usage": {"input_tokens": 300, "output_tokens": 400},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": value}]}],
        })
