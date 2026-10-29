from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict
    external_id: str | None
    output_error: str | None = None
    diagnostics: dict = field(default_factory=dict)


class ModelGateway(Protocol):
    def generate(self, policy: dict, prompt: str) -> ModelResult: ...


class ProjectRepository(Protocol):
    def project(self, actor: str, project_id: str) -> dict: ...
    def create_operation(
        self,
        actor: str,
        project_id: str,
        kind: str,
        key: str,
        payload: dict,
        policy: dict | None = None,
        ceiling=None,
    ) -> dict: ...


class Catalog(Protocol):
    def resolve(self, prompt: str) -> tuple[dict, object]: ...
