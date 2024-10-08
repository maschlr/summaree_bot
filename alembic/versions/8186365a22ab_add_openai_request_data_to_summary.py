"""Add openai request data to summary

Revision ID: 8186365a22ab
Revises: 0712891cc70a
Create Date: 2024-09-07 09:58:48.662217

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "8186365a22ab"
down_revision = "0712891cc70a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("summary", sa.Column("openai_id", sa.String(), nullable=True))
    op.add_column("summary", sa.Column("openai_model", sa.String(), nullable=True))
    op.add_column("summary", sa.Column("completion_tokens", sa.Integer(), nullable=True))
    op.add_column("summary", sa.Column("prompt_tokens", sa.Integer(), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("summary", "prompt_tokens")
    op.drop_column("summary", "completion_tokens")
    op.drop_column("summary", "openai_model")
    op.drop_column("summary", "openai_id")
    # ### end Alembic commands ###
