from sqlalchemy.orm import Session
from telegram.ext import ContextTypes


class DbSessionContext(ContextTypes.DEFAULT_TYPE):
    db_session: Session
