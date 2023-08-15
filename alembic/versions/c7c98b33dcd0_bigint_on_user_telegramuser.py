"""bigint on user->TelegramUser

Revision ID: c7c98b33dcd0
Revises: 7e723906be66
Create Date: 2023-08-15 13:09:40.060672

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c7c98b33dcd0"
down_revision = "7e723906be66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN telegram_user_id TYPE bigint USING telegram_user_id::bigint;")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN telegram_user_id TYPE int USING telegram_user_id::int;")
