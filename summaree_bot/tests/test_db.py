from summaree_bot.models import TelegramUser

from .common import Common


class TestDb(Common):
    def test_00_session_close(self):
        with self.Session.begin() as session:
            tg_user = TelegramUser(first_name="user_old")
            session.add(tg_user)
            session.flush()
            user_id = tg_user.id
            self.assertTrue(session.is_active)

        tg_user = session.get(TelegramUser, user_id)
        tg_user.first_name = "user_new"
        session.flush()

        tg_user = session.get(TelegramUser, user_id)
        self.assertEqual(tg_user.first_name, "user_new")
