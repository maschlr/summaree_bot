"""Add hashtags to transcript

Revision ID: 0712891cc70a
Revises: 83e32f17ca75
Create Date: 2024-09-06 16:10:51.628543

"""
import sqlalchemy as sa

from alembic import op
from summaree_bot.models.models import JsonList

# revision identifiers, used by Alembic.
revision = "0712891cc70a"
down_revision = "83e32f17ca75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("transcript", sa.Column("hashtags", JsonList(), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("transcript", "hashtags")
    # ### end Alembic commands ###