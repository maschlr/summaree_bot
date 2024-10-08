"""adapt Token<>User rel (m2o->o2o), add Subscription

Revision ID: cf3aacee6abc
Revises: 12c619b7bdc0
Create Date: 2023-07-30 16:07:41.445076

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "cf3aacee6abc"
down_revision = "12c619b7bdc0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "subscription",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.DateTime(), nullable=True),
        sa.Column("end_date", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "active", "expired", "canceled", "extended", name="subscriptionstatus"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], name=op.f("fk_subscription_user_id_user")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription")),
    )
    op.add_column("token", sa.Column("active", sa.Boolean(), nullable=False))
    op.drop_column("user", "active")
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("user", sa.Column("active", sa.BOOLEAN(), autoincrement=False, nullable=False))
    op.drop_column("token", "active")
    op.drop_table("subscription")
    # ### end Alembic commands ###
