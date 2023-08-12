"""make bigint for telegram_user explicit

Revision ID: c80801e43680
Revises: e619dc0a2844
Create Date: 2023-08-12 05:56:52.707601

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c80801e43680"
down_revision = "e619dc0a2844"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE telegram_user ALTER COLUMN id TYPE bigint USING id::bigint")
    op.execute("ALTER TABLE telegram_chat ALTER COLUMN id TYPE bigint USING id::bigint")


def downgrade() -> None:
    op.execute("ALTER TABLE telegram_user ALTER COLUMN id TYPE int USING id::int")
    op.execute("ALTER TABLE telegram_chat ALTER COLUMN id TYPE int USING id::int")
