from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _engine_options(database_url: str) -> dict[str, object]:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        options: dict[str, object] = {
            "connect_args": {"check_same_thread": False},
        }
        if url.database not in (None, "", ":memory:"):
            options.update(
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
            )
        return options
    if url.get_backend_name() == "postgresql":
        return {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
            "pool_recycle": 1800,
        }
    return {}


@lru_cache
def get_engine() -> Engine:
    database_url = get_settings().database_url
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        **_engine_options(database_url),
    )

    if make_url(database_url).get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
