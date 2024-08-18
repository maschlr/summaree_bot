"""Add order to Topic

Revision ID: e1ed85923fc2
Revises: dd25645fc79f
Create Date: 2024-08-18 13:15:58.066444

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e1ed85923fc2"
down_revision = "dd25645fc79f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column("telegram_user", "referral_token_active", existing_type=sa.BOOLEAN(), nullable=False)

    op.add_column("topic", sa.Column("order", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE topic
        SET text = REPLACE(text, 'Summary', '@summaree_bot'),
            summary_id = summary.id
        FROM summary, transcript
        WHERE topic.text LIKE '%Summary%'
        AND transcript.sha256_hash = 'f5d703775735e608396db4a8bf088a4d581fcc06fda2ae38c7f0e793b9f1b6bd'
        AND summary.transcript_id = transcript.id
    """
    )
    op.execute(
        """
        WITH ordered_rows AS (
          SELECT
            id,
            summary_id,
            ROW_NUMBER() OVER (PARTITION BY summary_id ORDER BY id) AS row_num
          FROM topic
        )
        UPDATE topic
        SET "order" = ordered_rows.row_num
        FROM ordered_rows
        WHERE topic.id = ordered_rows.id;
    """
    )
    op.alter_column("topic", sa.Column("order", nullable=False))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("topic", "order")
    op.alter_column("telegram_user", "referral_token_active", existing_type=sa.BOOLEAN(), nullable=True)
    # ### end Alembic commands ###
