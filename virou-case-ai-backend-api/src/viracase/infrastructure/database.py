from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def database(url):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

    from viracase.infrastructure.observability import install_events

    sessions = sessionmaker(engine, expire_on_commit=False)
    install_events(sessions)
    return engine, sessions
