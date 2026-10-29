from typing import Annotated, Literal

from fastapi import Depends
from pydantic import Field, StrictInt, StringConstraints

from viracase.infrastructure.publication_resolution import (
    prepare_recovery_plan,
    recovery_snapshot,
    request_recovery_check,
    resolve_recovery_plan,
)
from viracase.interfaces.schemas import Input, Reason, ReasonInput

Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class RecoveryCheck(Input):
    pass


class DeveloperRecoveryResolution(Input):
    expected_state_version: Digest
    action: Literal["check_and_close_if_no_effect"]
    consent: Literal["verify_and_close_failed_attempt_when_proven_safe"]


class StructuralAttestation(Input):
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]
    evidence_sha256: Digest
    statement_indices: Annotated[list[Annotated[StrictInt, Field(ge=1, le=2000)]], Field(min_length=1, max_length=2000)]
    observed_outcome: Literal["schema_applied", "schema_unchanged"]


class RecoveryResolution(Input):
    plan_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,120}$")]
    plan_sha256: Digest
    action: Literal["record_receipt", "close_no_effect", "adopt_schema"]
    reason: Reason
    attestation: StructuralAttestation | None = None


def mount_publication_recovery_routes(app, prefix, repo, actor, key, *, admin):
    if admin:
        @app.get(prefix + "/publications/{oid}/recovery")
        def inspect(oid: str, who=Depends(actor)):
            return recovery_snapshot(repo, who["id"], oid, admin=True)

        @app.post(prefix + "/publications/{oid}/recovery-plan")
        def plan(oid: str, data: ReasonInput, who=Depends(actor), intention=Depends(key)):
            return prepare_recovery_plan(repo, who["id"], oid, intention, data.reason)

        @app.post(prefix + "/publications/{oid}/resolve-recovery")
        def resolve(oid: str, data: RecoveryResolution, who=Depends(actor), intention=Depends(key)):
            return resolve_recovery_plan(repo, who["id"], oid, intention, data.model_dump(exclude_none=True))
    else:
        @app.get(prefix + "/operations/{oid}/publication-recovery")
        def inspect(oid: str, who=Depends(actor)):
            return recovery_snapshot(repo, who["id"], oid)

        @app.post(prefix + "/operations/{oid}/publication-recovery/resolve", status_code=202)
        def resolve(oid: str, data: DeveloperRecoveryResolution, who=Depends(actor), intention=Depends(key)):
            from viracase.infrastructure.publication_developer_recovery import request_developer_recovery

            return request_developer_recovery(repo, who["id"], oid, intention, data.model_dump())

        @app.post(prefix + "/operations/{oid}/publication-recovery/check", status_code=202)
        def check(oid: str, data: RecoveryCheck, who=Depends(actor), intention=Depends(key)):
            return request_recovery_check(repo, who["id"], oid, intention)

