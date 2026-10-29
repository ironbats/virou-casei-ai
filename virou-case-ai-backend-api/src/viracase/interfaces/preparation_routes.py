from datetime import datetime
from typing import Literal

from fastapi import Depends, Query

from viracase.infrastructure.feature_policies import FeaturePolicies
from viracase.infrastructure.integration_traces import TraceStore
from viracase.infrastructure.proposal_review import ProposalReviews
from viracase.infrastructure.template_preparations import TemplatePreparations
from viracase.interfaces.preparation_schemas import (
    CapturePolicyInput,
    PayloadAccessInput,
    PayloadCopyInput,
    PreparationAnswersInput,
    PreparationApplyInput,
    PreparationInput,
    PreparationPolicyInput,
)


def mount_preparation_routes(app, prefix, repo, learning, catalog, actor, key):
    service = TemplatePreparations(repo, learning, catalog)
    reviews = ProposalReviews(repo)

    @app.post(prefix + "/projects/{pid}/template-preparations", status_code=202)
    def start(pid: str, data: PreparationInput, who=Depends(actor), intention=Depends(key)):
        return service.start(who["id"], pid, intention, data.model_dump())

    @app.get(prefix + "/projects/{pid}/template-preparations")
    def listing(pid: str, who=Depends(actor)):
        return service.listing(who["id"], pid)

    @app.get(prefix + "/projects/{pid}/template-preparations/{identity}")
    def get(pid: str, identity: str, who=Depends(actor)):
        return service.get(who["id"], pid, identity)

    @app.post(prefix + "/projects/{pid}/template-preparations/{identity}/answers", status_code=202)
    def answers(
        pid: str, identity: str, data: PreparationAnswersInput, who=Depends(actor), intention=Depends(key)
    ):
        return service.answer(who["id"], pid, identity, intention, data.model_dump())

    @app.post(prefix + "/projects/{pid}/template-preparations/{identity}/apply")
    def apply(
        pid: str, identity: str, data: PreparationApplyInput, who=Depends(actor), intention=Depends(key)
    ):
        return service.apply(who["id"], pid, identity, intention, data.model_dump())

    @app.post(prefix + "/projects/{pid}/template-preparations/{identity}/cancel")
    def cancel(pid: str, identity: str, who=Depends(actor), intention=Depends(key)):
        return service.cancel(who["id"], pid, identity, intention)

    @app.get(prefix + "/projects/{pid}/proposals/{identity}")
    def proposal(pid: str, identity: str, who=Depends(actor)):
        return reviews.get(who["id"], pid, identity)

    @app.get(prefix + "/projects/{pid}/proposals/{identity}/files/{file_id}")
    def proposal_file(
        pid: str,
        identity: str,
        file_id: str,
        proposal_version: str = Query(..., min_length=1, max_length=64),
        who=Depends(actor),
    ):
        return reviews.file(who["id"], pid, identity, file_id, proposal_version)


def mount_trace_routes(app, prefix, repo, catalog, actor, key, version):
    policies, traces = FeaturePolicies(repo, catalog), TraceStore(repo)

    @app.get(prefix + "/template-preparation-policy")
    def preparation_policy(who=Depends(actor)):
        return policies.get(who["id"], "template_preparation")

    @app.put(prefix + "/template-preparation-policy")
    def save_preparation_policy(
        data: PreparationPolicyInput, who=Depends(actor), intention=Depends(key), current=Depends(version)
    ):
        return policies.save(who["id"], "template_preparation", intention, current, data.model_dump())

    @app.get(prefix + "/observability/policy")
    def capture_policy(who=Depends(actor)):
        return policies.get(who["id"], "integration_capture")

    @app.put(prefix + "/observability/policy")
    def save_capture_policy(
        data: CapturePolicyInput, who=Depends(actor), intention=Depends(key), current=Depends(version)
    ):
        return policies.save(who["id"], "integration_capture", intention, current, data.model_dump())

    @app.get(prefix + "/observability/payload-access")
    def payload_access(who=Depends(actor)):
        return policies.get(who["id"], "payload_access")

    @app.put(prefix + "/observability/payload-access")
    def save_payload_access(
        data: PayloadAccessInput, who=Depends(actor), intention=Depends(key), current=Depends(version)
    ):
        return policies.save(who["id"], "payload_access", intention, current, data.model_dump())

    @app.get(prefix + "/observability/traces")
    def trace_list(
        service: str | None = Query(None, pattern="^(jev|agents|cloudflare|platform)$"),
        level: str | None = Query(None, pattern="^(info|warning|error)$"),
        correlation: str | None = Query(None, max_length=36),
        project: str | None = Query(None, max_length=36),
        before: str | None = Query(None, max_length=36),
        limit: int = Query(100, ge=1, le=200),
        phase: Literal["recommendation", "preparation", "review", "construction"] | None = None,
        state: str | None = Query(None, max_length=24),
        since: datetime | None = None,
        until: datetime | None = None,
        who=Depends(actor),
    ):
        return traces.listing(
            who["id"], service, level, correlation, project, before, limit, phase, state, since, until
        )

    @app.get(prefix + "/observability/traces/{identity}/payload")
    def trace_payload(
        identity: str, section: Literal["request", "response", "normalized", "fields"], who=Depends(actor)
    ):
        return traces.payload(who["id"], identity, section)

    @app.post(prefix + "/observability/traces/{identity}/copy")
    def trace_copy(identity: str, data: PayloadCopyInput, who=Depends(actor), intention=Depends(key)):
        return traces.payload(who["id"], identity, data.section, copying=True)

    @app.get(prefix + "/observability/traces/{identity}/timeline")
    def trace_timeline(identity: str, who=Depends(actor)):
        return traces.timeline(who["id"], identity)
