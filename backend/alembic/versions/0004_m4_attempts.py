"""M4: per-attempt task runs, artifact index and LLM call telemetry.

* ``taskrun.attempt`` + unique (project_id, agent_name, attempt): the P5
  repair loop runs firmware/build more than once per project, and each run
  needs its own row.
* ``artifact``: index of files written to the project workspace.
* ``agentcall``: one row per LLM request, so the cost of each repair attempt
  is visible.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_m4_attempts"
down_revision = "0003_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("taskrun") as batch:
        batch.add_column(
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1")
        )

    # The worker has always kept one row per (project, agent), but nothing
    # enforced it. Number any accidental duplicates 1..n before adding the
    # constraint instead of failing the upgrade on an old database. Ordering
    # by id is arbitrary but deterministic; no real ordering existed before.
    op.execute(
        """
        UPDATE taskrun
        SET attempt = 1 + (
            SELECT COUNT(*) FROM taskrun AS earlier
            WHERE earlier.project_id = taskrun.project_id
              AND earlier.agent_name = taskrun.agent_name
              AND earlier.id < taskrun.id
        )
        """
    )

    with op.batch_alter_table("taskrun") as batch:
        batch.create_unique_constraint(
            "uq_taskrun_project_agent_attempt",
            ["project_id", "agent_name", "attempt"],
        )

    op.create_table(
        "artifact",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_run_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="source"),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
        sa.ForeignKeyConstraint(["task_run_id"], ["taskrun.id"]),
        sa.UniqueConstraint(
            "project_id", "path", "attempt", name="uq_artifact_project_path_attempt"
        ),
    )
    op.create_index("ix_artifact_project_id", "artifact", ["project_id"])

    op.create_table(
        "agentcall",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"]),
    )
    op.create_index("ix_agentcall_project_id", "agentcall", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_agentcall_project_id", table_name="agentcall")
    op.drop_table("agentcall")
    op.drop_index("ix_artifact_project_id", table_name="artifact")
    op.drop_table("artifact")
    with op.batch_alter_table("taskrun") as batch:
        batch.drop_constraint("uq_taskrun_project_agent_attempt", type_="unique")
        batch.drop_column("attempt")
