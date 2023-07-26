from sqlalchemy import select

from summaree_bot.models import Language, TelegramChat, TelegramUser, Token, User

from .common import Common


class TestTelegramChat(Common):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        with cls.Session.begin() as session:
            cls.tg_users = {
                TelegramUser(first_name="user1", username="user1", language_code="en"),
                TelegramUser(first_name="user2", username="user2", language_code="ru"),
            }
            session.add_all(cls.tg_users)

    def test_00_no_chat_record(self):
        stmt = select(TelegramChat).where(TelegramChat.id == 1)
        with self.Session.begin() as session:
            result = session.execute(stmt).scalar()

        self.assertIsNone(result)

    def test_01_create_chat_with_users(self):
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

    def test_02_create_user_with_token(self):
        with self.Session.begin() as session:
            for tg_user, email in zip(self.tg_users, ["user1@example.org", "user2@example.org"], strict=True):
                tg_user.user = User(email=email, token=Token())
                session.add(tg_user)

        stmt_user = select(User)
        with self.Session.begin() as session:
            users = session.scalars(stmt_user).all()
            self.assertFalse(any(user.token.active for user in users))
            self.assertTrue(all(isinstance(user.token.value, str) and len(user.token.value) for user in users))
