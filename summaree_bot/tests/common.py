import os
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from summaree_bot.integrations.deepl import available_target_languages
from summaree_bot.models import Base, Language, session


class Common(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine(os.getenv("TEST_DB_URL"))
        Base.metadata.create_all(engine)
        cls.Session = sessionmaker(bind=engine)
        # monkey patch to avoid writing to real database
        session.Session = cls.Session
        cls.addClassCleanup(cls.Session.close_all)
        cls.addClassCleanup(Base.metadata.drop_all, engine)
        cls._populateLanguages()

    @classmethod
    def _populateLanguages(cls):
        with cls.Session.begin() as session:
            for (
                ietf_tag,
                target_lang,
            ) in available_target_languages.ietf_tag_to_language.items():
                lang = Language(
                    name=target_lang.name,
                    ietf_tag=ietf_tag,
                    code=target_lang.code,
                )
                session.add(lang)
