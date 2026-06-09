"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-09

"""
from typing import Sequence, Union
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


difficulty_enum = postgresql.ENUM("easy", "medium", "hard", name="difficulty_enum")


def upgrade() -> None:
    difficulty_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("premium_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_users_device_id", "users", ["device_id"], unique=True)

    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", postgresql.JSON(), nullable=False),
        sa.Column("description", postgresql.JSON(), nullable=True),
        sa.Column("is_premium", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cover_image_url", sa.String(512), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_categories_slug", "categories", ["slug"], unique=True)

    op.create_table(
        "word_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("civilian_word", sa.String(255), nullable=False),
        sa.Column("impostor_word", sa.String(255), nullable=True),
        sa.Column("difficulty", postgresql.ENUM("easy", "medium", "hard", name="difficulty_enum", create_type=False), nullable=False),
        sa.Column("tags", postgresql.JSON(), nullable=True),
    )
    op.create_index("ix_word_packs_category_id", "word_packs", ["category_id"])
    op.create_index("ix_word_packs_locale", "word_packs", ["locale"])

    op.create_table(
        "feature_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_feature_configs_key", "feature_configs", ["key"], unique=True)

    op.create_table(
        "ai_topic_request_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("sanitized_prompt", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("was_rejected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_ai_logs_device_id", "ai_topic_request_logs", ["device_id"])
    op.create_index("ix_ai_logs_created_at", "ai_topic_request_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("ai_topic_request_logs")
    op.drop_table("feature_configs")
    op.drop_table("word_packs")
    op.drop_table("categories")
    op.drop_table("users")
    difficulty_enum.drop(op.get_bind(), checkfirst=True)
