import unittest

from summaree_bot.utils.url import decode, encode


class TestEncodeDecode(unittest.TestCase):
    def test_encode_decode(self):
        data = ["activate", "123456"]
        encoded = encode(data)
        decoded = decode(encoded)
        self.assertEqual(data, decoded)
