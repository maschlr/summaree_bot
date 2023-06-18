from sqlalchemy import select

from summaree_bot.models import TelegramChat, TelegramUser, Language

from .common import Common

class TestTelegramChat(Common):
    def test_00_no_chat_record(self):
        stmt = select(TelegramChat).where(TelegramChat.id == 1)
        with self.Session.begin() as session:
            result = session.execute(stmt).scalar()

        self.assertIsNone(result)

    def test__01_create_chat_with_users(self):
        with self.Session.begin() as session:
            users = {
                TelegramUser(
                    first_name="user1", 
                    username="user1", 
                    language_code="en"
                ),
                TelegramUser(
                    first_name="user2",
                    username="user2",
                    language_code="ru"
                )
            }
            session.add_all(users)

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
