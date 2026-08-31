"""P3 generation settings and project pin policy."""

from alembic import op
import sqlalchemy as sa

revision = "0002_p3_cubemx"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(
            sa.Column(
                "pin_selection_policy",
                sa.String(length=32),
                nullable=False,
                server_default="deterministic",
            )
        )
    op.create_table(
        "generationsetting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pin_selection_policy",
            sa.String(length=32),
            nullable=False,
            server_default="deterministic",
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("generationsetting")
    with op.batch_alter_table("project") as batch:
        batch.drop_column("pin_selection_policy")
