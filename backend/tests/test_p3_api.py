from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app.api.routes import projects as projects_module
from app.api.routes.generation import (
    GenerationSettingUpdate,
    get_generation_settings,
    update_generation_settings,
)
from app.api.routes.projects import ProjectCreate, _project_summary
from app.db.models import PinSelectionPolicy, Project, TaskRun
from app.db.session import upgrade_database


def test_fresh_database_runs_baseline_and_p3(tmp_path: Path):
    path = tmp_path / "fresh.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)

    upgrade_database(engine, url)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"project", "taskrun", "agentsetting", "generationsetting"} <= tables
    columns = {column["name"] for column in inspector.get_columns("project")}
    assert "pin_selection_policy" in columns


def test_existing_pre_alembic_database_is_stamped_then_upgraded(tmp_path: Path):
    path = tmp_path / "existing.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE project (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, "
                "user_request VARCHAR NOT NULL, request_type VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, error VARCHAR, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE taskrun (id VARCHAR PRIMARY KEY, project_id VARCHAR NOT NULL, "
                "agent_name VARCHAR NOT NULL, status VARCHAR NOT NULL, result VARCHAR, "
                "error VARCHAR, started_at DATETIME, finished_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE agentsetting (agent_name VARCHAR PRIMARY KEY, model VARCHAR, "
                "enabled BOOLEAN NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO project VALUES "
                "('old', 'existing', 'request', 'full_project', 'pending', NULL, :now, :now)"
            ),
            {"now": now},
        )

    upgrade_database(engine, url)

    columns = {column["name"] for column in inspect(engine).get_columns("project")}
    assert "pin_selection_policy" in columns
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT name, pin_selection_policy FROM project WHERE id='old'")
        ).one()
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert row == ("existing", "deterministic")
    assert revision == "0003_chat"


def test_generation_setting_persists_and_project_policy_is_frozen(tmp_path: Path):
    path = tmp_path / "settings.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    upgrade_database(engine, url)

    with Session(engine) as session:
        assert get_generation_settings(session)["pin_selection_policy"] == "deterministic"
        updated = update_generation_settings(
            GenerationSettingUpdate(pin_selection_policy=PinSelectionPolicy.explicit),
            session,
        )
        assert updated["pin_selection_policy"] == "explicit"
        project = Project(
            name="frozen",
            user_request="request",
            pin_selection_policy=updated["pin_selection_policy"],
        )
        session.add(project)
        session.commit()
        update_generation_settings(
            GenerationSettingUpdate(pin_selection_policy=PinSelectionPolicy.llm),
            session,
        )
        session.refresh(project)

        assert _project_summary(project)["pin_selection_policy"] == "explicit"


def test_project_create_contract_accepts_null_and_override():
    inherited = ProjectCreate(name="a", request="b")
    overridden = ProjectCreate(
        name="a",
        request="b",
        pin_selection_policy=PinSelectionPolicy.llm,
    )

    assert inherited.pin_selection_policy is None
    assert overridden.pin_selection_policy == PinSelectionPolicy.llm


def test_project_handler_inherits_or_overrides_policy(tmp_path: Path, monkeypatch):
    path = tmp_path / "projects.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    upgrade_database(engine, url)
    monkeypatch.setattr(projects_module.run_pipeline, "delay", lambda _project_id: None)

    with Session(engine) as session:
        update_generation_settings(
            GenerationSettingUpdate(pin_selection_policy=PinSelectionPolicy.explicit),
            session,
        )
        inherited = projects_module.create_project(
            ProjectCreate(name="inherited", request="request"),
            session,
        )
        overridden = projects_module.create_project(
            ProjectCreate(
                name="overridden",
                request="request",
                pin_selection_policy=PinSelectionPolicy.llm,
            ),
            session,
        )

        assert inherited["pin_selection_policy"] == "explicit"
        assert overridden["pin_selection_policy"] == "llm"
        assert len(session.exec(select(TaskRun)).all()) == 2
