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
    max_output_tokens: int = Field(ge=1, le=32000, strict=True)
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
