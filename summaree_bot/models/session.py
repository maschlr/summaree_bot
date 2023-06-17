import os
from functools import wraps
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base

if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")

# use this decorator for functions in bot.py
def add_session(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        with sessionmaker(engine).begin() as session:
            kwargs["session"] = session
            return fnc(*args, **kwargs)
    return wrapper
