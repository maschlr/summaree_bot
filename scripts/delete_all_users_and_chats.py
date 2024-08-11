from sqlalchemy import select

from summaree_bot.models import TelegramChat, TelegramUser
from summaree_bot.models.session import Session

if __name__ == "__main__":
    with Session.begin() as session:
        for Model in (TelegramUser, TelegramChat):
            records = session.scalars(select(Model))
            for record in records:
                session.delete(record)
