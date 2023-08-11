import datetime

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

    def test_01_big_int(self):
        vals = {
            "id": 6633528690,
            "is_bot": False,
            "first_name": "Shiva",
            "last_name": "Schiff",
            "username": None,
            "language_code": "en",
            "is_premium": None,
            "created_at": datetime.datetime(2023, 8, 10, 20, 6, 0, 314647),
            "updated_at": datetime.datetime(2023, 8, 10, 20, 6, 0, 314706),
        }
        with self.Session.begin() as session:
            tg_user = TelegramUser(**vals)
            session.add(tg_user)

        tg_user = session.get(TelegramUser, vals["id"])
        self.assertEqual(tg_user.id, vals["id"])
