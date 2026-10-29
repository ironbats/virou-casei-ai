from typing import Literal

from fastapi import Depends
from pydantic import Field

from viracase.infrastructure.code_analysis import CodeAnalysis
from viracase.interfaces.schemas import Input


class CodeAnalysisInput(Input):
    revision_id: str = Field(min_length=1, max_length=36)
    source: Literal["publication", "preview", "editor"]
    publication_operation_id: str | None = Field(default=None, min_length=1, max_length=36)
    runtime_error: str | None = Field(default=None, max_length=4000)
    policy_version: str = Field(min_length=1, max_length=40)


class CodeCorrectionInput(Input):
    base_project_revision: str = Field(min_length=1, max_length=36)
    policy_version: str = Field(min_length=1, max_length=40)


def mount_code_analysis_routes(app, prefix, repo, catalog, generation, actor, key):
    service = CodeAnalysis(repo, catalog)
    actor_dependency, intention_dependency = Depends(actor), Depends(key)

    @app.post(prefix + "/projects/{pid}/code-analyses", status_code=202)
    def analyze(pid: str, data: CodeAnalysisInput, who=actor_dependency, intention=intention_dependency):
        return service.start(who["id"], pid, intention, data.model_dump())

    @app.post(prefix + "/projects/{pid}/code-analyses/{oid}/correction", status_code=202)
    def correct(
        pid: str, oid: str, data: CodeCorrectionInput, who=actor_dependency, intention=intention_dependency
    ):
        return service.correct(who["id"], pid, oid, intention, data.model_dump(), generation)
