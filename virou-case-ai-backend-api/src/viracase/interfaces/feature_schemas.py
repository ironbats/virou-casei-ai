from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictStr, StringConstraints, model_validator

from viracase.interfaces.schemas import Input, Reason


class BuildPolicy(Input):
    max_objective_chars: int = Field(default=2000, ge=100, le=12000)
    max_message_chars: int = Field(default=12000, ge=100, le=12000)
    max_attachments: int = Field(default=8, ge=1, le=20)
    max_attachment_chars: int = Field(default=12000, ge=100, le=24000)
    max_context_bytes: int = Field(default=60000, ge=10000, le=60000)
    max_generation_context_bytes: int = Field(default=252288, ge=10000, le=252288)
    max_generation_calls: int = Field(default=10, ge=1, le=10, strict=True)
    generation_model_id: str | None = Field(default=None, min_length=1, max_length=120)
    max_truncation_retries: int = Field(default=3, ge=0, le=9, strict=True)
    max_response_repairs: int = Field(default=2, ge=0, le=9, strict=True)
    max_batch_characters: int = Field(default=24000, ge=500, le=120000, strict=True)
    minimum_batch_characters: int = Field(default=500, ge=100, le=4000, strict=True)
    batch_shrink_factor: float = Field(default=0.5, ge=0.1, le=0.9)
    autosave_ms: int = Field(default=700, ge=300, le=5000)
    selection_debounce_ms: int = Field(default=500, ge=0, le=5000)
    model: str = Field(default="jev-latest", pattern=r"^jev-[a-zA-Z0-9.\-]+$", max_length=80)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    daily_requests: int = Field(default=30, ge=1, le=1000)
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    recommendation_enabled: bool = False

    @model_validator(mode="after")
    def batch_limits(self):
        if self.minimum_batch_characters > self.max_batch_characters:
            raise ValueError("O tamanho mínimo do lote não pode exceder o tamanho máximo.")
        return self


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
    fields: dict[str, StrictStr | StrictBool | list[StrictStr] | None] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def ignore_server_metadata(cls, data):
        if not isinstance(data, dict):
            return data
        read_only = {
            "title",
            "category",
            "version",
            "position",
            "edited",
            "attachment_revision",
            "attachment_instance_id",
            "schema_version",
            "field_values",
            "validation_errors",
            "fill_state",
            "fill_message",
            "fill_error_code",
            "fill_questions",
            "fill_context",
            "fill_request_id",
            "preparation_id",
            "filled_at",
            "field_generations",
        }
        return {name: value for name, value in data.items() if name not in read_only}


class BuildDraftInput(Input):
    stage: Literal["idea", "templates", "review", "conversation"]
    objective: str = Field(max_length=12000)
    message_text: str = Field(max_length=12000)
    attachments: list[AttachmentInput] = Field(default_factory=list, max_length=20)
    recommendation_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="before")
    @classmethod
    def ignore_workflow_metadata(cls, data):
        if not isinstance(data, dict):
            return data
        from viracase.infrastructure.build_workflow import SERVER_FIELDS

        return {
            key: value
            for key, value in data.items()
            if key
            not in (SERVER_FIELDS - {"correlation_id", "preparation_id"})
            | {"can_review", "can_build", "blocking_reasons"}
        }


class RecommendationInput(Input):
    expected_draft_revision: int = Field(ge=0)
    base_project_revision: str | None = Field(default=None, max_length=36)


class BuildSendInput(Input):
    expected_draft_revision: int = Field(ge=1)
    base_project_revision: str | None = Field(default=None, max_length=36)
    policy_version: str
    reply_to: str | None = Field(default=None, max_length=36)
    review_snapshot_hash: str = Field(min_length=64, max_length=64)


class BuildReviewInput(Input):
    expected_draft_revision: int = Field(ge=1)
    confirm_empty: bool = False


class ClarificationReplyInput(Input):
    message_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    base_project_revision: str | None = Field(max_length=36)
    policy_version: str


class BuildFillInput(Input):
    expected_draft_revision: int = Field(ge=1)
    attachment_ids: list[str] = Field(min_length=1, max_length=20)
    target_field_id: str | None = Field(default=None, min_length=1, max_length=60)

    @model_validator(mode="after")
    def single_field_target(self):
        if self.target_field_id is not None and len(self.attachment_ids) != 1:
            raise ValueError("A geração de um campo exige um único anexo")
        return self


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
        from viracase.domain.template_fields import validate_definition

        validate_definition(self.fields)
        return self
