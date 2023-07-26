import os
from functools import wraps

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")


# use this decorator for functions in bot.py
def add_session(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        context = kwargs.get("context", args[1])
        if hasattr(context, "db_session"):
            return fnc(*args, **kwargs)

        Session = sessionmaker(bind=engine)
        with Session.begin() as session:
            context.db_session = session
            result = fnc(*args, **kwargs)
        return result

    return wrapper
