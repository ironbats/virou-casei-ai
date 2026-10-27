from fastapi import Depends

from viracase.infrastructure.ai_registry import AIRegistry
from viracase.interfaces.ai_schemas import ConnectionInput, ModelInput, ProbeInput


def mount_ai_routes(app, prefix, repo, catalog, actor, key):
    registry = getattr(catalog, "registry", None) or AIRegistry(repo)
    actor_dependency, intention_dependency = Depends(actor), Depends(key)

    @app.get(prefix + "/ai/connections")
    def connections(who=actor_dependency):
        return registry.listing()

    @app.post(prefix + "/ai/connections", status_code=201)
    def create(data: ConnectionInput, who=actor_dependency, intention=intention_dependency):
        return registry.save_connection(who["id"], None, intention, data.model_dump())

    @app.post(prefix + "/ai/connections/{cid}")
    def update(cid: str, data: ConnectionInput, who=actor_dependency, intention=intention_dependency):
        return registry.save_connection(who["id"], cid, intention, data.model_dump())

    @app.get(prefix + "/ai/connections/{cid}/catalog")
    def local_catalog(cid: str, who=actor_dependency):
        return registry.local_catalog(cid)

    @app.post(prefix + "/ai/connections/{cid}/discover")
    def discover(cid: str, who=actor_dependency):
        repo.rate_limit("ai-discover:" + who["id"], 20, 300)
        return registry.discover(cid)

    @app.post(prefix + "/ai/connections/{cid}/models", status_code=201)
    def create_model(cid: str, data: ModelInput, who=actor_dependency, intention=intention_dependency):
        return registry.save_model(who["id"], cid, None, intention, data.model_dump(mode="json"))

    @app.post(prefix + "/ai/connections/{cid}/models/{mid}")
    def update_model(
        cid: str, mid: str, data: ModelInput, who=actor_dependency, intention=intention_dependency
    ):
        return registry.save_model(who["id"], cid, mid, intention, data.model_dump(mode="json"))

    @app.post(prefix + "/ai/connections/{cid}/models/{mid}/test")
    def test_model(
        cid: str, mid: str, data: ProbeInput, who=actor_dependency, intention=intention_dependency
    ):
        repo.rate_limit("ai-test:" + who["id"], 20, 300)
        return registry.probe(who["id"], cid, mid, intention, data.model_dump())
