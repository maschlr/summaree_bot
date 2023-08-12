"""bigint on chats_to_users_rel

Revision ID: 7e723906be66
Revises: c80801e43680
Create Date: 2023-08-12 17:02:20.812501

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "7e723906be66"
down_revision = "c80801e43680"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chats_to_users_rel ALTER COLUMN user_id TYPE bigint USING user_id::bigint")
    op.execute("ALTER TABLE chats_to_users_rel ALTER COLUMN chat_id TYPE bigint USING chat_id::bigint")


def downgrade() -> None:
    op.execute("ALTER TABLE chats_to_users_rel ALTER COLUMN user_id TYPE int USING user_id::int")
    op.execute("ALTER TABLE chats_to_users_rel ALTER COLUMN chat_id TYPE int USING chat_id::int")
