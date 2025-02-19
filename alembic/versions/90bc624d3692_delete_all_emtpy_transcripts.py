"""delete all emtpy transcripts

Revision ID: 90bc624d3692
Revises: 50b6c003cdbb
Create Date: 2025-02-19 13:02:44.158435

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "90bc624d3692"
down_revision = "50b6c003cdbb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute("DELETE FROM transcript WHERE result = '';")
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###
