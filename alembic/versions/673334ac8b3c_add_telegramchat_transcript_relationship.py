"""add TelegramChat -> Transcript relationship

Revision ID: 673334ac8b3c
Revises: 5d5bcd24672e
Create Date: 2024-08-06 15:13:11.557835

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "673334ac8b3c"
down_revision = "5d5bcd24672e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("transcript", sa.Column("tg_chat_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_transcript_tg_chat_id_telegram_chat"), "transcript", "telegram_chat", ["tg_chat_id"], ["id"]
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f("fk_transcript_tg_chat_id_telegram_chat"), "transcript", type_="foreignkey")
    op.drop_column("transcript", "tg_chat_id")
    # ### end Alembic commands ###
