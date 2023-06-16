import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from summaree_bot.models import TelegramChat, Base

class TestTelegramChat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine("sqlite:///:memory:", echo=True)
        Base.metadata.create_all(engine)
        cls.Session = sessionmaker(bind=engine)

    def test_no_chat_record(self):
        stmt = select(TelegramChat).where(TelegramChat.id == 1)
        with self.Session.begin() as session:
            result = session.execute(stmt).scalar()

        self.assertIsNone(result)

