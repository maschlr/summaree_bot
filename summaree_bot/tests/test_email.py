import unittest
from xml.etree import ElementTree

from summaree_bot.integrations import TokenEmail, is_valid_email
from summaree_bot.utils import url


class TestEmailValidation(unittest.TestCase):
    def test_valid_emails(self):
        valid_emails = [
            "user@example.com",
            "john.doe123@mail.example.co.uk",
            "name@domain.com",
            "first.last@example.org",
            "test+label@example.net",
            "12345@example.com",
            "user@99problems.com",
        ]

        for email in valid_emails:
            with self.subTest(email=email):
                self.assertTrue(is_valid_email(email, check_mx_domain_record=False), f"Expected {email} to be valid")

    def test_invalid_emails(self):
        invalid_emails = [
            "invalid_email",
            "invalid-email",
            "name@.com",
            "user@domain",
            "@example.com",
            "user@example",
            "user@example..com",
            "user@.example.com",
        ]

        for email in invalid_emails:
            with self.subTest(email=email):
                self.assertFalse(is_valid_email(email), f"Expected {email} to be invalid")


class TestEmail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = {
            "subject": "Activate your summar.ee bot account",
            "token": "1234567890",
            "bot_name": "Summaree Bot Test",
            "name": "Test User",
        }
        cls.email = TokenEmail(template_data=cls.data, email_to="user@example.com")
        cls.rendered = cls.email.render()

    def test_formatting(self):
        self.assertTrue(isinstance(self.rendered, str))

        root = ElementTree.fromstring(self.rendered)
        self.assertEqual(root.tag, "html")

    def test_data(self):
        for value in self.data.values():
            self.assertTrue(str(value) in self.rendered)

    def test_callback_data(self):
        should_be = ["activate", self.data["token"]]
        encoded = url.encode(should_be)
        self.assertEqual(self.email.template_data["start_callback"], encoded)
