"""create google_cookies table

Revision ID: 4f1a713dd532
Revises: a1d2cac2a8d2
Create Date: 2025-05-12 01:29:05.327557

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4f1a713dd532'
down_revision = 'a1d2cac2a8d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "google_cookies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("cookie_json_encrypted", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade():
    op.drop_table("google_cookies")
