import random

from sqlalchemy import select

from summaree_bot.models import Language, TelegramChat, TelegramUser

from .common import Common


class TestTelegramChat(Common):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def _generate_tg_users(self, count: int) -> None:
        language_codes = ["en", "ru", "de", "fr", "es", "it", "pt", "zh", "ja", "ko"]
        with self.Session.begin() as session:  # type: ignore
            self.tg_users = {
                TelegramUser(first_name=f"user{i}", username="user{i}", language_code=random.choice(language_codes))
                for i in range(count)
            }
            session.add_all(self.tg_users)

    def _generate_users(self, count: int) -> None:
        self._generate_tg_users(count)
        with self.Session.begin() as session:  # type: ignore
            for tg_user in self.tg_users:
                session.add(tg_user)

    def test_00_no_chat_record(self):
        stmt = select(TelegramChat).where(TelegramChat.id == 1)
        with self.Session.begin() as session:
            result = session.execute(stmt).scalar()

        self.assertIsNone(result)

    def test_01_create_chat_with_users(self):
        self._generate_tg_users(2)
        with self.Session.begin() as session:
            users = self.tg_users

            language = Language.get_default_language(session=session)
            self.assertEqual(language.ietf_tag, "en")
            chat = TelegramChat(type="private", users=users, language=language)
            session.add(chat)

        stmt_chat = select(TelegramChat)
        stmt_users = select(TelegramUser)
        with self.Session.begin() as session:
            result_chat = session.scalars(stmt_chat).one_or_none()
            result_users = session.scalars(stmt_users).all()

            self.assertEqual(result_chat.type, "private")
            user_ids_in_chat = {user.id for user in result_chat.users}
            for user in result_users:
                self.assertIn(user.id, user_ids_in_chat)
