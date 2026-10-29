from copy import deepcopy

from pydantic import ValidationError

from viracase.domain.rules import RuleError
from viracase.interfaces.ai_schemas import ModelExecution


def _capability(output=32000, *, budget=None, levels=(), reasoning=(), sampling=False, sampling_none=False):
    return {
        "max_output_tokens": output,
        "thinking_modes": [
            "auto",
            "provider_default",
            *(["budget"] if budget else []),
            *(["level"] if levels else []),
        ],
        "thinking_budget_min": budget[0] if budget else None,
        "thinking_budget_max": budget[1] if budget else None,
        "thinking_budget_can_disable": bool(budget and budget[2]),
        "thinking_budget_dynamic": bool(budget),
        "thinking_levels": list(levels),
        "reasoning_efforts": list(reasoning),
        "supports_temperature": sampling,
        "supports_top_p": sampling,
        "sampling_requires_reasoning_none": sampling_none,
    }


_GEMINI_MODELS = {
    "gemini-2.5-flash": _capability(65536, budget=(1, 24576, True), sampling=True),
    "gemini-2.5-flash-lite": _capability(65536, budget=(512, 24576, True), sampling=True),
    "gemini-2.5-pro": _capability(65536, budget=(128, 32768, False), sampling=True),
    "gemini-3.5-flash-lite": _capability(65536, levels=("minimal", "low", "medium", "high")),
    "gemini-3.8-flash": _capability(65536, levels=("low", "medium", "high")),
    "gemini-4-argon": _capability(32768, levels=("low", "medium", "high")),
}
_OPENAI_MODELS = {
    "gpt-6-luna": _capability(
        65536,
        reasoning=("none", "low", "medium", "high", "xhigh", "max"),
        sampling=True,
        sampling_none=True,
    ),
    **{
        model: _capability(65536, reasoning=("low", "medium", "high", "xhigh", "max"))
        for model in ("gpt-6-astra", "gpt-6.1-sol")
    },
}
PROVIDER_CAPABILITIES = {
    "gemini": {"default": _capability(), "models": _GEMINI_MODELS},
    "openai": {"default": _capability(), "models": _OPENAI_MODELS},
}


def model_capability(provider, model):
    capabilities = PROVIDER_CAPABILITIES.get(provider)
    if not capabilities:
        raise RuleError("INVALID_AGENT_EXECUTION", "Provedor de IA não suportado.", 400)
    return deepcopy(capabilities["models"].get(model, capabilities["default"]))


def validate_model_execution(provider, model, policy):
    capability = model_capability(provider, model)
    output = policy.get("max_output_tokens")
    if type(output) is not int or not 1 <= output <= capability["max_output_tokens"]:
        raise RuleError(
            "INVALID_AGENT_EXECUTION",
            f"O limite de saída permitido para este modelo é de 1 a {capability['max_output_tokens']} tokens.",
            400,
        )
    configured = policy.get("execution")
    if configured is None:
        return None
    try:
        execution = ModelExecution.model_validate(configured).model_dump()
    except ValidationError:
        raise RuleError("INVALID_AGENT_EXECUTION", "Parâmetros de execução de IA inválidos.", 400) from None
    mode = execution["thinking_mode"]
    if mode not in capability["thinking_modes"]:
        raise RuleError("INVALID_AGENT_EXECUTION", "Modo de raciocínio incompatível com este modelo.", 400)
    budget = execution["thinking_budget"]
    if mode == "budget":
        special = budget == -1 or (budget == 0 and capability["thinking_budget_can_disable"])
        if (
            not special
            and not capability["thinking_budget_min"] <= budget <= capability["thinking_budget_max"]
        ):
            raise RuleError(
                "INVALID_AGENT_EXECUTION", "Orçamento de raciocínio incompatível com este modelo.", 400
            )
        if budget > 0 and budget >= output:
            raise RuleError(
                "INVALID_AGENT_EXECUTION",
                "O limite efetivo de saída deve ser maior que o orçamento de raciocínio para reservar espaço para a resposta.",
                400,
            )
    if mode == "level" and execution["thinking_level"] not in capability["thinking_levels"]:
        raise RuleError("INVALID_AGENT_EXECUTION", "Nível de raciocínio incompatível com este modelo.", 400)
    effort = execution["reasoning_effort"]
    if effort is not None and (provider != "openai" or effort not in capability["reasoning_efforts"]):
        raise RuleError("INVALID_AGENT_EXECUTION", "Esforço de raciocínio incompatível com este modelo.", 400)
    for option in ("temperature", "top_p"):
        if execution[option] is not None and (
            not capability["supports_" + option]
            or (capability["sampling_requires_reasoning_none"] and effort != "none")
        ):
            raise RuleError(
                "INVALID_AGENT_EXECUTION",
                f"{option} não é compatível com o modelo e o raciocínio selecionados.",
                400,
            )
    return execution


def execution_timeouts(policy, default_timeout):
    execution = policy.get("execution") or {}
    return (
        policy.get("request_timeout_seconds", execution.get("request_timeout_seconds", default_timeout)),
        policy.get("connect_timeout_seconds", execution.get("connect_timeout_seconds", 10)),
    )


def freeze_execution(policy, default_timeout):
    execution = validate_model_execution(policy["provider"], policy["model"], policy)
    return {
        **policy,
        "execution": execution,
        "request_timeout_seconds": (execution or {}).get("request_timeout_seconds", default_timeout),
        "connect_timeout_seconds": (execution or {}).get("connect_timeout_seconds", 10),
    }


def execution_fields(policy, default_timeout):
    execution = policy.get("execution") or {}
    request, connect = execution_timeouts(policy, default_timeout)
    return {
        "request_timeout_seconds": request,
        "connect_timeout_seconds": connect,
        "thinking_mode": execution.get("thinking_mode", "auto"),
        "thinking_budget": execution.get("thinking_budget"),
        "thinking_level": execution.get("thinking_level"),
        "reasoning_effort": execution.get("reasoning_effort"),
        "temperature": execution.get("temperature"),
        "top_p": execution.get("top_p"),
    }


def configuration_schema(catalog):
    from viracase.interfaces.ai_schemas import ModelInput
    from viracase.interfaces.feature_schemas import BuildPolicy
    from viracase.interfaces.preparation_schemas import PreparationPolicy

    return {
        "model": {
            "schema": ModelInput.model_json_schema(),
            "defaults": {"max_output_tokens": 16000, "execution": ModelExecution().model_dump()},
            "execution_schema": ModelExecution.model_json_schema(),
        },
        "construction": {"schema": BuildPolicy.model_json_schema(), "defaults": BuildPolicy().model_dump()},
        "preparation": {
            "schema": PreparationPolicy.model_json_schema(),
            "defaults": PreparationPolicy().model_dump(),
        },
        "provider_capabilities": deepcopy(PROVIDER_CAPABILITIES),
        "available_models": [
            {
                "id": policy.get("model_id", policy["provider"] + ":" + policy["model"]),
                "provider": policy["provider"],
                "model": policy["model"],
                "name": policy.get("name", policy["model"]),
                "ready": True,
                "max_output_tokens": policy["max_output_tokens"],
            }
            for policy in catalog.eligible()
        ],
    }
