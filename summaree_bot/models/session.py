import os
from functools import wraps

import telegram
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SqlAlchemySession
from sqlalchemy.orm import sessionmaker
from telegram.ext import ContextTypes

if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")

Session = sessionmaker(bind=engine)


# use this decorator for functions that need a database session
def session_context(fnc):
    @wraps(fnc)
    def wrapper(update: telegram.Update, context: DbSessionContext, *args, **kwargs):
        # if multiple functions are called in a row, we don't want to create a new session
        if hasattr(context, "db_session"):
            return fnc(update, context, *args, **kwargs)

        # a single function call should create a new session, commit and close it
        with Session.begin() as session:
            context.db_session = session
            result = fnc(update, context, *args, **kwargs)
        return result

    return wrapper


class DbSessionContext(ContextTypes.DEFAULT_TYPE):
    db_session: SqlAlchemySession
