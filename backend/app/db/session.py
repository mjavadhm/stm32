from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings

engine = create_engine(settings.database_url, echo=False)


def init_db() -> None:
    """Create tables. Simple create_all for now; switch to Alembic when the
    schema starts changing between releases."""
    from app.db import models  # noqa: F401  (register tables on metadata)

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session."""
    with Session(engine) as session:
        yield session
