import re
from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from viracase.interfaces.schemas import Input, Reason


class BuildPolicy(Input):
    max_objective_chars: int = Field(default=2000, ge=100, le=12000)
    max_message_chars: int = Field(default=12000, ge=100, le=12000)
    max_attachments: int = Field(default=8, ge=1, le=20)
    max_attachment_chars: int = Field(default=12000, ge=100, le=24000)
    max_context_bytes: int = Field(default=60000, ge=10000, le=60000)
    autosave_ms: int = Field(default=700, ge=300, le=5000)
    model: str = Field(default="jev-latest", pattern=r"^jev-[a-zA-Z0-9.\-]+$", max_length=80)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    daily_requests: int = Field(default=30, ge=1, le=1000)
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    recommendation_enabled: bool = False


class CloudflarePolicy(Input):
    account_id: str = Field(pattern=r"^[a-fA-F0-9]{32}$")
    token_type: Literal["user", "account"]
    environment: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9-]+$")
    worker_prefix: str = Field(min_length=2, max_length=18, pattern=r"^[a-z][a-z0-9-]+$")
    compatibility_date: date
    allowed_stacks: list[Literal["hono-typescript-d1", "fastapi-python-d1"]] = Field(
        min_length=1, max_length=2
    )
    max_projects: int = Field(ge=1, le=10)
    validation_hours: int = Field(ge=1, le=168)
    timeout_seconds: int = Field(ge=5, le=120)
    max_concurrent: int = Field(ge=1, le=5)
    spa: bool
    plan: Literal["free"]
    allow_paid: Literal[False]


class ConfigInput(Input):
    settings: dict
    token: str | None = Field(default=None, min_length=10, max_length=4096)
    reason: Reason


class ConfigAction(Input):
    reason: Reason


class ConnectionInput(ConfigInput):
    name: str = Field(min_length=3, max_length=80)

    @model_validator(mode="after")
    def normalize_name(self):
        self.name = self.name.strip()
        if len(self.name) < 3:
            raise ValueError("Informe um nome para a conexão")
        return self


class AttachmentInput(Input):
    template_id: str = Field(max_length=80)
    source_version_id: str = Field(max_length=36)
    edited_content: str = Field(max_length=24000)
    fields: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class BuildDraftInput(Input):
    stage: Literal["idea", "templates", "review", "conversation"]
    objective: str = Field(max_length=12000)
    message_text: str = Field(max_length=12000)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=20)
    recommendation_id: str | None = Field(default=None, max_length=36)


class RecommendationInput(Input):
    expected_draft_revision: int = Field(ge=0)


class BuildSendInput(Input):
    expected_draft_revision: int = Field(ge=1)
    base_project_revision: str = Field(max_length=36)
    policy_version: str
    reply_to: str | None = Field(default=None, max_length=36)


class TemplateInput(Input):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    expected_version: int = Field(ge=0)
    title: str = Field(min_length=3, max_length=100)
    category: Literal["planning", "context", "quality", "security"]
    summary: str = Field(min_length=10, max_length=500)
    content: str = Field(min_length=10, max_length=24000)
    fields: list[dict] = Field(default_factory=list, max_length=20)
    available: bool = True
    reason: Reason

    @model_validator(mode="after")
    def valid_fields(self):
        keys = set()
        for field in self.fields:
            if set(field) != {"key", "label", "required", "max_length"}:
                raise ValueError("Campo inválido")
            if (
                not isinstance(field["key"], str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,49}", field["key"])
                or field["key"] in keys
            ):
                raise ValueError("Campo duplicado")
            if (
                not isinstance(field["label"], str)
                or not 1 <= len(field["label"].strip()) <= 120
                or type(field["required"]) is not bool
            ):
                raise ValueError("Campo inválido")
            if type(field["max_length"]) is not int or not 1 <= field["max_length"] <= 12000:
                raise ValueError("Limite inválido")
            keys.add(field["key"])
        return self
