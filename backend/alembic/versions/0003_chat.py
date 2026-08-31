"""Agentic RAG chat: conversations and messages."""

from alembic import op
import sqlalchemy as sa

revision = "0003_chat"
down_revision = "0002_p3_cubemx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
    )
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_table("message")
    op.drop_table("conversation")
