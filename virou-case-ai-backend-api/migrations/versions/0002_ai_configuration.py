from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("provider_attempts") as batch:
        batch.alter_column("model", existing_type=sa.String(80), type_=sa.String(160))
    op.create_table(
        "ai_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("auth_mode", sa.String(24), nullable=False),
        sa.Column("credential", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("takes_over", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider IN ('openai','gemini')"),
        sa.CheckConstraint("auth_mode IN ('api_key','service_account')"),
    )
    op.create_table(
        "ai_models",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("ai_connections.id"), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("verified_connection_version", sa.Integer(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("connection_id", "model"),
    )
    op.create_index("ix_ai_models_connection_id", "ai_models", ["connection_id"])


def downgrade():
    op.drop_index("ix_ai_models_connection_id", table_name="ai_models")
    op.drop_table("ai_models")
    op.drop_table("ai_connections")
    # Preserve long historical model identifiers: shrinking this column would lose data.
