from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect
from sqlmodel import Session, create_engine

from alembic import command
from app.core.config import settings

engine = create_engine(settings.database_url, echo=False)


def init_db() -> None:
    """Upgrade a fresh or pre-Alembic database to the current schema.

    Existing installations already have the baseline tables but no
    ``alembic_version`` row. Stamp those at the baseline before applying P3;
    an empty database runs the baseline migration normally.
    """
    upgrade_database(engine, settings.database_url)


def upgrade_database(database_engine, database_url: str) -> None:
    """Apply migrations to one engine; separated for migration tests."""
    config = _alembic_config(database_url)
    tables = set(inspect(database_engine).get_table_names())
    baseline_tables = {"project", "taskrun", "agentsetting"}
    if "alembic_version" not in tables:
        if baseline_tables <= tables:
            command.stamp(config, "0001_baseline")
        elif baseline_tables & tables:
            missing = ", ".join(sorted(baseline_tables - tables))
            raise RuntimeError(
                f"database has a partial pre-Alembic schema; missing baseline tables: {missing}"
            )
    command.upgrade(config, "head")


def _alembic_config(database_url: str) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session."""
    with Session(engine) as session:
        yield session
