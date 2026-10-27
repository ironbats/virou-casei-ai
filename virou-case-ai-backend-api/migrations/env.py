from alembic import context
from sqlalchemy import create_engine, text
from viracase.infrastructure.config import settings
from viracase.infrastructure.models import Base

target_metadata = Base.metadata
url = settings().migration_url()
if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_lock(73210429)"))
            connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_unlock(73210429)"))
            connection.commit()
