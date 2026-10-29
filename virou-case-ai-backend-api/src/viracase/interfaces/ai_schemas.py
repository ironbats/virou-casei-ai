from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from viracase.interfaces.schemas import Input, Reason, Short

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{1,160}$")]
Price = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,7}(\.[0-9]{1,8})?$")]


class ConnectionInput(Input):
    name: Short
    provider: Literal["openai", "gemini"]
    auth_mode: Literal["api_key", "service_account"]
    credential: Annotated[str, StringConstraints(max_length=20000)] | None = None
    project_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{0,100}$")] = ""
    organization: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{0,100}$")] = ""
    location: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,50}$")] = "global"
    enabled: bool = True
    expected_version: int | None = Field(default=None, ge=1)
    reason: Reason = "Configuração administrativa de IA"


class ModelExecution(Input):
    request_timeout_seconds: int = Field(default=180, ge=10, le=600, strict=True)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=30, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    thinking_mode: Literal["auto", "provider_default", "budget", "level"] = "auto"
    thinking_budget: int | None = Field(default=None, ge=-1, le=32768, strict=True)
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def compatible_options(self):
        if self.connect_timeout_seconds > self.request_timeout_seconds:
            raise ValueError("O prazo de conexão não pode exceder o prazo da requisição.")
        if self.temperature is not None and self.top_p is not None:
            raise ValueError("Configure somente temperature ou top_p.")
        if (self.thinking_mode == "budget") != (self.thinking_budget is not None):
            raise ValueError("Informe thinking_budget somente no modo budget.")
        if (self.thinking_mode == "level") != (self.thinking_level is not None):
            raise ValueError("Informe thinking_level somente no modo level.")
        return self


class ModelInput(Input):
    model: Identifier
    name: Short
    enabled: bool = False
    expected_version: int | None = Field(default=None, ge=1)
    input_usd_per_million: Price
    cached_usd_per_million: Price
    output_usd_per_million: Price
    fx_brl_per_usd: Price
    fx_source: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
    evidence: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
    effective_at: datetime
    valid_until: datetime
    max_output_tokens: int = Field(ge=1, le=65536, strict=True)
    execution: ModelExecution | None = None
    reason: Reason = "Configuração administrativa de modelo"

    @model_validator(mode="after")
    def dates(self):
        if self.effective_at.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValueError("Informe datas com fuso horário.")
        if self.valid_until <= self.effective_at:
            raise ValueError("Validade deve ser posterior ao início.")
        return self


class ProbeInput(Input):
    expected_version: int = Field(ge=1)
    reason: Reason = "Teste explícito de geração do modelo"
