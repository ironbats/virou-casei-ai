import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "integration_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("credential", sa.Text(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "version"),
    )
    op.create_index("ix_integration_configs_provider", "integration_configs", ["provider"])
    op.create_table(
        "template_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("accounts.id")),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug", "version"),
    )
    op.create_index("ix_template_versions_slug", "template_versions", ["slug"])
    op.create_table(
        "recommendation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("request_key", sa.String(120), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("telemetry", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "project_id", "request_key"),
    )
    op.create_index("ix_recommendation_runs_account_id", "recommendation_runs", ["account_id"])
    op.create_index("ix_recommendation_runs_project_id", "recommendation_runs", ["project_id"])
    op.create_table(
        "publication_resources",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("config_id", sa.String(36), sa.ForeignKey("integration_configs.id"), nullable=False),
        sa.Column("remote_account_id", sa.String(32), nullable=False),
        sa.Column("worker_name", sa.String(63), nullable=False),
        sa.Column("database_id", sa.String(36)),
        sa.Column("inventory", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.UniqueConstraint("remote_account_id", "worker_name"),
    )
    op.create_index("ix_publication_resources_account_id", "publication_resources", ["account_id"])


def downgrade():
    raise RuntimeError(
        "Downgrade automático bloqueado para preservar credenciais, inventário e rascunhos. Consulte rollback/0004.md."
    )
