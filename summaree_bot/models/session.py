import os
from functools import wraps

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SqlAlchemySession
from sqlalchemy.orm import sessionmaker
from telegram.ext import ContextTypes

if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")

Session = sessionmaker(bind=engine)


# use this decorator for functions in bot.py
def add_session(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        context = kwargs.get("context", args[1])
        if hasattr(context, "db_session"):
            return fnc(*args, **kwargs)

        with Session.begin() as session:
            context.db_session = session
            result = fnc(*args, **kwargs)
        return result

    return wrapper


class DbSessionContext(ContextTypes.DEFAULT_TYPE):
    db_session: SqlAlchemySession
