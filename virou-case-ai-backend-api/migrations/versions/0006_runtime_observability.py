import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_status",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_table(
        "runtime_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("service", sa.String(24), nullable=False),
        sa.Column("level", sa.String(12), nullable=False),
        sa.Column("account_id", sa.String(36)),
        sa.Column("project_id", sa.String(36)),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("stage", sa.String(60), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_events_created", "runtime_events", ["created_at", "id"])
    op.create_index("ix_runtime_events_service", "runtime_events", ["service", "id"])
    op.create_index("ix_runtime_events_correlation", "runtime_events", ["correlation_id", "id"])


def downgrade():
    op.drop_table("runtime_events")
    op.drop_table("runtime_status")
