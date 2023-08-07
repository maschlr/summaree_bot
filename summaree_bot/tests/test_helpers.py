import unittest
from dataclasses import is_dataclass

from summaree_bot.bot.helpers import BotMessage
from summaree_bot.utils.url import decode, encode


class TestEncodeDecode(unittest.TestCase):
    def test_encode_decode(self):
        data = ["activate", "123456"]
        encoded = encode(data)
        decoded = decode(encoded)
        self.assertEqual(data, decoded)


class TestBotMessage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.kwargs = dict(chat_id=1, text="test")
        cls.msg = BotMessage(**cls.kwargs)

    def test_init(self):
        self.assertTrue(is_dataclass(self.msg))

    def test_dict(self):
        _dict = dict(**self.msg)
        self.assertTrue(isinstance(_dict, dict))
        for key, value in self.kwargs.items():
            self.assertEqual(_dict[key], value)
