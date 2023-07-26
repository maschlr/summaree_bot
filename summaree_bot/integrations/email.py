import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

from dns.name import EmptyLabel
from dns.resolver import NXDOMAIN, resolve

from ..templates import get_template
from ..utils.url import encode

_logger = logging.getLogger(__name__)


@dataclass
class EmailConfig:
    SMTP_SERVER: str = field(init=False)
    SMTP_PORT: int = field(init=False)
    EMAIL_ADDRESS: str = field(init=False)
    EMAIL_PASSWORD: str = field(init=False)

    def __post_init__(self):
        for key in ("SMTP_SERVER", "SMTP_PORT", "EMAIL_ADDRESS", "EMAIL_PASSWORD"):  # no-reply@summar.ee
            setattr(self, key.lower(), os.getenv(key))


class Email:
    def __init__(self, template_name, template_data, email_to, email_from=None):
        self.config = EmailConfig()

        msg = MIMEMultipart()
        msg["From"] = email_from or self.config.email_address
        msg["To"] = email_to
        msg["Subject"] = template_data["subject"]
        self.template = get_template(template_name)
        self.template_data = template_data
        # Attach the email content as HTML
        self.msg = msg
        self.server = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)

    def render(self):
        self.email_content = self.template.render(self.template_data)
        return self.email_content

    def send(self):
        if not hasattr(self, "email_content"):
            self.email_content = self.template.render(self.template_data)
        # Attach the email content as HTML
        self.msg.attach(MIMEText(self.email_content, "html"))
        try:
            self.server.starttls()
            self.server.login(self.config.email_address, self.config.email_password)
            self.server.sendmail(self.msg["From"], self.msg["To"], self.msg.as_string())
            self.server.quit()
            _logger.info(f"Email to {self.msg['To']} sent successfully!")
            return True
        except Exception as e:
            _logger.error("Failed to send the email:", str(e))
            return False


class TokenEmail(Email):
    def __init__(self, *args, **kwargs):
        super().__init__("token_email", *args, **kwargs)
        start_callback_data = ["activate", self.template_data["token"]]
        self.template_data["start_callback"] = encode(start_callback_data)


def is_valid_email(email, check_mx_domain_record=True):
    # https://stackoverflow.com/questions/8022530/how-to-check-for-valid-email-address
    _, addr = parseaddr(email)
    if not addr or "@" not in addr:
        return False

    try:
        private_addr, domain = addr.rsplit("@", 1)
    except ValueError:
        return False

    if not private_addr or "." not in domain:
        return False

    if check_mx_domain_record:
        try:
            resolve(domain, "MX")
        except (NXDOMAIN, EmptyLabel):
            return False

    return True
