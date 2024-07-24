"""change product currency to stars

Revision ID: e14232d7f8cd
Revises: ff9d97a6a8e9
Create Date: 2024-07-24 10:58:35.564838

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e14232d7f8cd"
down_revision = "ff9d97a6a8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """UPDATE product SET
            currency = product_update.currency,
            description = product_update.description,
            price = product_update.price
        FROM (VALUES
            (1, 'Premium Subscription for 1 month (⭐)', 50, 'XTR'),
            (3, 'Premium Subscription for 1 year (⭐)', 500, 'XTR')
        ) AS product_update(id, description, price, currency)
        WHERE product.id = product_update.id;
    """
    )

    op.execute("DELETE FROM product WHERE premium_period = 'THREE_MONTHS'")
    op.execute("UPDATE product SET id = 2 WHERE id = 3")


def downgrade() -> None:
    pass
