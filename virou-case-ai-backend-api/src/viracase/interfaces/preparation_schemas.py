from typing import Literal

from pydantic import Field, model_validator

from viracase.interfaces.schemas import Input, Reason


class PreparationInput(Input):
    expected_draft_revision: int = Field(ge=1)
    base_project_revision: str | None = Field(default=None, min_length=1, max_length=36)
    attachment_ids: list[str] = Field(min_length=1, max_length=20)
    expected_attachment_revisions: dict[str, int]


class AcceptedField(Input):
    attachment_id: str = Field(min_length=1, max_length=80)
    field_id: str = Field(min_length=1, max_length=60)


class PreparationApplyInput(Input):
    expected_state_version: int = Field(ge=1)
    expected_draft_revision: int = Field(ge=1)
    expected_attachment_revisions: dict[str, int]
    accepted_fields: list[AcceptedField] = Field(min_length=1, max_length=400)


class QuestionAnswer(Input):
    question_id: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=4000)


class PreparationAnswersInput(Input):
    expected_state_version: int = Field(ge=1)
    expected_draft_revision: int = Field(ge=1)
    answers: list[QuestionAnswer] = Field(min_length=1, max_length=100)


class PreparationPolicy(Input):
    enabled: bool = True
    model_id: str | None = Field(default=None, min_length=1, max_length=120)
    max_context_bytes: int = Field(default=32000, ge=4000, le=60000)
    max_output_tokens: int = Field(default=4000, ge=256, le=65536, strict=True)
    max_repairs: int = Field(default=0, ge=0, le=3, strict=True)


class CapturePolicy(Input):
    capture_enabled: bool = False
    retention_days: int = Field(default=7, ge=1, le=30)
    max_payload_bytes: int = Field(default=32768, ge=1024, le=262144)
    max_operation_bytes: int = Field(default=262144, ge=4096, le=2097152)
    max_total_bytes: int = Field(default=16777216, ge=1048576, le=268435456)
    allowed_categories: list[Literal["technical_metadata", "schemas", "field_identifiers", "usage"]] = Field(
        default_factory=lambda: ["technical_metadata", "schemas", "field_identifiers", "usage"], max_length=4
    )

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.max_operation_bytes < self.max_payload_bytes
            or self.max_total_bytes < self.max_operation_bytes
        ):
            raise ValueError("A quota deve comportar uma carga e uma operação")
        if len(set(self.allowed_categories)) != len(self.allowed_categories):
            raise ValueError("Categorias duplicadas")
        return self


class PreparationPolicyInput(Input):
    settings: PreparationPolicy
    reason: Reason


class CapturePolicyInput(Input):
    settings: CapturePolicy
    reason: Reason


class PayloadAccessInput(Input):
    admin_ids: list[str] = Field(max_length=200)
    reason: Reason


class PayloadCopyInput(Input):
    section: Literal["request", "response", "normalized", "fields"]


class ProposalApplyInput(Input):
    expected_proposal_version: str | None = Field(default=None, min_length=1, max_length=64)
    expected_project_revision: str | None = Field(default=None, min_length=1, max_length=36)
