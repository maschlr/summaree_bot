"""add Transcript->tg_user_id relation

Revision ID: ff9d97a6a8e9
Revises: c7c98b33dcd0
Create Date: 2023-08-26 11:48:05.021242

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "ff9d97a6a8e9"
down_revision = "c7c98b33dcd0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "bot_message", "chat_id", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=False
    )
    op.alter_column("invoice", "chat_id", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=True)
    op.alter_column(
        "subscription", "chat_id", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=True
    )
    op.add_column("transcript", sa.Column("tg_user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_transcript_tg_user_id_telegram_user"), "transcript", "telegram_user", ["tg_user_id"], ["id"]
    )
    op.drop_constraint("uq_user_referral_token", "users", type_="unique")
    op.create_unique_constraint(op.f("uq_users_referral_token"), "users", ["referral_token"])
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f("uq_users_referral_token"), "users", type_="unique")
    op.create_unique_constraint("uq_user_referral_token", "users", ["referral_token"])
    op.drop_constraint(op.f("fk_transcript_tg_user_id_telegram_user"), "transcript", type_="foreignkey")
    op.drop_column("transcript", "tg_user_id")
    op.alter_column(
        "subscription", "chat_id", existing_type=sa.BigInteger(), type_=sa.INTEGER(), existing_nullable=True
    )
    op.alter_column("invoice", "chat_id", existing_type=sa.BigInteger(), type_=sa.INTEGER(), existing_nullable=True)
    op.alter_column(
        "bot_message", "chat_id", existing_type=sa.BigInteger(), type_=sa.INTEGER(), existing_nullable=False
    )
    # ### end Alembic commands ###