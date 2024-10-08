"""Add paid_at field to Invoice

Revision ID: 82501d0b6ac4
Revises: badaa335306f
Create Date: 2024-09-01 15:27:31.976361

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "82501d0b6ac4"
down_revision = "badaa335306f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("invoice", sa.Column("paid_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE invoice SET paid_at = updated_at WHERE status = 'paid';")
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("invoice", "paid_at")
    # ### end Alembic commands ###
