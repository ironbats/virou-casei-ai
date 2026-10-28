import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "integration_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("name_key", sa.String(160), nullable=False),
        sa.Column("default_for", sa.String(24), nullable=True, unique=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "name_key"),
        sa.CheckConstraint("default_for IS NULL OR default_for = provider"),
    )
    op.create_index("ix_integration_connections_provider", "integration_connections", ["provider"])
    op.create_table(
        "integration_version_bindings",
        sa.Column("config_id", sa.String(36), sa.ForeignKey("integration_configs.id"), primary_key=True),
        sa.Column(
            "connection_id", sa.String(36), sa.ForeignKey("integration_connections.id"), nullable=False
        ),
    )
    op.create_index(
        "ix_integration_version_bindings_connection_id", "integration_version_bindings", ["connection_id"]
    )
    for provider, identity, name in (
        ("cloudflare", "b3294d6a-d909-49e5-a6ae-77ad226e9ae1", "Cloudflare principal"),
        ("jev", "41555b02-7edc-4e01-b582-125834646924", "Jev principal"),
    ):
        op.execute(
            sa.text(
                "INSERT INTO integration_connections (id, provider, name, name_key, default_for, created_by, created_at) "
                "SELECT :identity, provider, :name, :name_key, provider, created_by, created_at "
                "FROM integration_configs WHERE provider = :provider ORDER BY version DESC LIMIT 1"
            ).bindparams(identity=identity, name=name, name_key=name.casefold(), provider=provider)
        )
        op.execute(
            sa.text(
                "INSERT INTO integration_version_bindings (config_id, connection_id) "
                "SELECT id, :identity FROM integration_configs WHERE provider = :provider"
            ).bindparams(identity=identity, provider=provider)
        )


def downgrade():
    raise RuntimeError(
        "Reversão automática bloqueada: preserve conexões e seus vínculos. Consulte MULTIPLAS_INTEGRACOES.md."
    )
