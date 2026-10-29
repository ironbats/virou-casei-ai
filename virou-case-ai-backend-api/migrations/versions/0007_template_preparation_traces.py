import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("purge_inventory") as batch:
        batch.alter_column("state", existing_type=sa.String(24), type_=sa.String(32), existing_nullable=False)
    op.create_table(
        "feature_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version"),
    )
    op.create_table(
        "integration_traces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trace_id", sa.String(36), nullable=False),
        sa.Column("parent_span_id", sa.String(36)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36)),
        sa.Column("account_id", sa.String(36)),
        sa.Column("project_id", sa.String(36)),
        sa.Column("service", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("capture_state", sa.String(24), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    for name in ("trace_id", "operation_id", "project_id"):
        op.create_index("ix_integration_traces_" + name, "integration_traces", [name])
    op.create_index("ix_integration_traces_created", "integration_traces", ["created_at", "id"])
    op.create_index("ix_integration_traces_phase", "integration_traces", ["stage", "created_at", "id"])
    op.create_index("ix_integration_traces_expires", "integration_traces", ["expires_at"])
    op.create_table(
        "integration_trace_payloads",
        sa.Column("trace_id", sa.String(36), sa.ForeignKey("integration_traces.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("section", sa.String(16), primary_key=True),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_integration_trace_payloads_expires_at", "integration_trace_payloads", ["expires_at"])


def downgrade():
    """Retain VARCHAR(32): existing lifecycle states exceed 24 characters and must not be truncated."""
    op.drop_table("integration_trace_payloads")
    op.drop_table("integration_traces")
    op.drop_table("feature_policies")
