from viracase.infrastructure.config import settings
from viracase.infrastructure.database import database
from viracase.infrastructure.providers import HttpModelGateway, ModelCatalog
from viracase.infrastructure.repository import Repository
from viracase.infrastructure.sources import SourceStore
from viracase.infrastructure.ai_registry import AIRegistry


def compose():
    config = settings()
    engine, sessions = database(config.database_url)
    sources = SourceStore(
        config.source_root, (config.openai_api_key, config.gemini_api_key, config.database_url)
    )
    repo = Repository(sessions, sources, config)
    registry = AIRegistry(repo)
    return (
        config,
        engine,
        repo,
        ModelCatalog(config, registry),
        HttpModelGateway(config, registry=registry),
    )
