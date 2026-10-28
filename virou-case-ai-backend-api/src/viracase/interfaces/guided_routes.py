from fastapi import Depends

from viracase.infrastructure.guided_build import GuidedBuild
from viracase.infrastructure.recommendations import Recommendations
from viracase.interfaces.feature_schemas import BuildDraftInput, BuildSendInput, RecommendationInput
from viracase.interfaces.schemas import TransferInput


def mount_guided_routes(app, prefix, repo, learning, generation, actor, key, version):
    guided = GuidedBuild(repo, learning)

    @app.get(prefix + "/templates")
    def templates(search: str = "", category: str | None = None, who=Depends(actor)):
        return guided.templates.list(who["id"], search[:200], category)

    @app.get(prefix + "/templates/versions/{identity}")
    def template_version(identity: str, who=Depends(actor)):
        return guided.templates.version(who["id"], identity)

    @app.get(prefix + "/projects/{pid}/build-draft")
    def draft(pid: str, who=Depends(actor)):
        return guided.get(who["id"], pid)

    @app.put(prefix + "/projects/{pid}/build-draft")
    def save(
        pid: str, data: BuildDraftInput, who=Depends(actor), intention=Depends(key), current=Depends(version)
    ):
        return guided.save(who["id"], pid, intention, current, data.model_dump())

    @app.post(prefix + "/projects/{pid}/recommendations")
    def recommend(pid: str, data: RecommendationInput, who=Depends(actor), intention=Depends(key)):
        return Recommendations(guided).recommend(who["id"], pid, intention, data.model_dump())

    @app.get(prefix + "/projects/{pid}/recommendations/{identity}")
    def recommendation(pid: str, identity: str, who=Depends(actor)):
        return Recommendations(guided).get(who["id"], pid, identity)

    @app.post(prefix + "/projects/{pid}/messages", status_code=202)
    def send(pid: str, data: BuildSendInput, who=Depends(actor), intention=Depends(key)):
        return guided.send(who["id"], pid, intention, data.model_dump(), generation)

    @app.post(prefix + "/projects/{pid}/proposals/{oid}/discard")
    def discard(pid: str, oid: str, who=Depends(actor), intention=Depends(key)):
        return guided.discard(who["id"], pid, oid, intention)

    @app.post(prefix + "/learning/guides/{slug}/attach")
    def transfer(slug: str, data: TransferInput, who=Depends(actor), intention=Depends(key)):
        return guided.transfer(who["id"], slug, intention, data.model_dump())
