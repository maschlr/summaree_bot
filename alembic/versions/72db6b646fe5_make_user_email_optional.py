"""Make User.email optional

Revision ID: 72db6b646fe5
Revises: f2e59c721760
Create Date: 2023-08-04 05:47:07.793282

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "72db6b646fe5"
down_revision = "f2e59c721760"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("subscription", sa.Column("active", sa.Boolean(), nullable=False))
    op.execute("CREATE TYPE subscriptiontype AS ENUM ('onboarding', 'referral', 'reffered', 'paid');")
    op.add_column(
        "subscription",
        sa.Column(
            "type", sa.Enum("onboarding", "referral", "reffered", "paid", name="subscriptiontype"), nullable=False
        ),
    )
    op.add_column("user", sa.Column("referrer_id", sa.Integer(), nullable=True))
    op.alter_column("user", "email", existing_type=sa.VARCHAR(), nullable=True)
    op.create_foreign_key(op.f("fk_user_referrer_id_user"), "user", "user", ["referrer_id"], ["id"])
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(op.f("fk_user_referrer_id_user"), "user", type_="foreignkey")
    op.alter_column("user", "email", existing_type=sa.VARCHAR(), nullable=False)
    op.drop_column("user", "referrer_id")
    op.drop_column("subscription", "type")
    op.drop_column("subscription", "active")
    # ### end Alembic commands ###
