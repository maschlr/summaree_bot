# This script is used to create the database and stamp it with the latest revision.
# python -m scrips.create_database
from pathlib import Path

# get the environment variables
import summaree_bot.integrations
# https://alembic.sqlalchemy.org/en/latest/cookbook.html#building-an-up-to-date-database-from-scratch
from summaree_bot.models import Base
from summaree_bot.models.session import engine

Base.metadata.create_all(engine)

# then, load the Alembic configuration and generate the
# version table, "stamping" it with the most recent rev:
from alembic.config import Config
from alembic import command
script_location = Path(__file__)
alembic_cfg = Config(script_location.parents[1] / "alembic.ini")
command.stamp(alembic_cfg, "head")
