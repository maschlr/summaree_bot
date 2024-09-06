"""Add reaction_emoji to Transcript

Revision ID: 83e32f17ca75
Revises: 82501d0b6ac4
Create Date: 2024-09-06 13:04:01.086155

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "83e32f17ca75"
down_revision = "82501d0b6ac4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("transcript", sa.Column("reaction_emoji", sa.String(), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("transcript", "reaction_emoji")
    # ### end Alembic commands ###