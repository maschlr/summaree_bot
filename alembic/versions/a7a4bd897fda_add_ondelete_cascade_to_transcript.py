"""add ondelete=CASCADE to transcript

Revision ID: a7a4bd897fda
Revises: 5280326a34be
Create Date: 2023-08-10 07:31:05.095437

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a7a4bd897fda"
down_revision = "5280326a34be"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint("summary_transcript_id_fkey", "summary", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_summary_transcript_id_transcript"),
        "summary",
        "transcript",
        ["transcript_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f("fk_summary_transcript_id_transcript"), "summary", type_="foreignkey")
    op.create_foreign_key("fk_summary_transcript_id_transcript", "summary", "transcript", ["transcript_id"], ["id"])
    # ### end Alembic commands ###
