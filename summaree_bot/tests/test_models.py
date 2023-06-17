import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from summaree_bot.models import TelegramChat, Base, Language
from summaree_bot.integrations.deepl import available_target_languages


class BaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine("sqlite:///:memory:", echo=True)
        Base.metadata.create_all(engine)
        cls.Session = sessionmaker(bind=engine)
        cls.addClassCleanup(cls.Session.close_all)
        cls.addClassCleanup(Base.metadata.drop_all, engine)

class TestTelegramChat(BaseTest):
    def test_no_chat_record(self):
        stmt = select(TelegramChat).where(TelegramChat.id == 1)
        with self.Session.begin() as session:
            result = session.execute(stmt).scalar()

        self.assertIsNone(result)


class TestLanguages(BaseTest):
    def test_language_insert(self):

        with self.Session.begin() as session:
            for ietf_tag, target_lang in available_target_languages.ietf_tag_to_language.items():
                lang = Language(
                    name=target_lang.name,
                    ietf_tag=ietf_tag,
                    code=target_lang.code,
                )
                session.add(lang)
        
        stmt = select(Language)
        languages = self.Session().scalars(stmt).all()
        self.assertEqual(len(languages), len(available_target_languages.ietf_tag_to_language))
