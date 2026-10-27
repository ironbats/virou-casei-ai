from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=300)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]
Amount = Annotated[str, StringConstraints(pattern=r"^-?[0-9]{1,7}(\.[0-9]{1,8})?$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    login: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=160)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ProjectInput(Input):
    name: Short
    objective: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=3000)]


class RevisionInput(Input):
    base_revision_id: str | None
    files: dict[str, str] | None = None
    restore_revision_id: str | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (self.files is None) == (self.restore_revision_id is None):
            raise ValueError("Informe arquivos ou revisão para restaurar.")
        return self


class GenerateInput(Input):
    brief: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=12000)]
    base_revision_id: str | None
    policy_version: str


class ConversationInput(GenerateInput):
    brief: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]
    auto_apply: bool = False
    reply_to: str | None = Field(default=None, max_length=36)


class ProjectDraftInput(Input):
    base_revision_id: str | None
    files: dict[str, str] | None = None
    prompt: str = Field(max_length=12000)


class LearningDraftInput(Input):
    fields: dict[str, str]
    prompt: str = Field(max_length=12000)
    manual: bool


class TransferInput(Input):
    project_id: str = Field(max_length=36)
    source_version: int = Field(ge=1)
    target_version: int = Field(ge=0)
    mode: Annotated[str, StringConstraints(pattern=r"^(replace|append)$")]


class RevisionReference(Input):
    revision_id: str


class AccountInput(Input):
    login: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=3, max_length=160, pattern=r"^[a-zA-Z0-9@._+-]+$"
        ),
    ]
    name: Short
    password: Password
    reason: Reason


class ResetInput(Input):
    password: Password
    reason: Reason


class ReasonInput(Input):
    reason: Reason


class PaymentInput(Input):
    amount_paid_brl: Amount
    api_credit_brl: Amount
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=160)]
    reason: Reason


class AccessInput(PaymentInput):
    starts_at: datetime
    access_days: int = Field(default=30, ge=1, le=3650, strict=True)


class AccessAdjustmentInput(Input):
    expires_at: datetime
    expected_version: int = Field(ge=1, strict=True)
    reason: Reason


class AdjustmentInput(Input):
    amount_brl: Amount
    reason: Reason


class ReconcileInput(Input):
    evidence_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)
    ]
    confirmed_no_effect: bool = False
    usage: dict[str, int] | None = None
    reason: Reason


class BackupEvidence(Input):
    fence: str
    evidence_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)
    ]
    reason: Reason
