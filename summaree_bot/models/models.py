import datetime as dt
import enum
import secrets
from datetime import datetime
from typing import List, Optional

import sqlalchemy
from sqlalchemy import Column, ForeignKey, MetaData, Table, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.types import BigInteger
from telegram.ext import ContextTypes

from .session import Session as SessionContext


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(dt.UTC))
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(dt.UTC), onupdate=lambda: datetime.now(dt.UTC)
    )

    # https://alembic.sqlalchemy.org/en/latest/naming.html#integration-of-naming-conventions-into-operations-autogenerate
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_`%(constraint_name)s`",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


chats_to_users_rel = Table(
    "chats_to_users_rel",
    Base.metadata,
    Column("user_id", ForeignKey("telegram_user.id"), primary_key=True),
    Column("chat_id", ForeignKey("telegram_chat.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"
    # TODO:
    # - merge User into TelegramUser
    # - find all occurences of User in code and migrate them

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        "telegram_user_id",
        BigInteger,
        ForeignKey("telegram_user.id", ondelete="CASCADE"),
    )
    telegram_user: Mapped["TelegramUser"] = relationship("TelegramUser", back_populates="user")
    email: Mapped[Optional[str]]
    email_token: Mapped["EmailToken"] = relationship(back_populates="user")

    referral_token: Mapped[str] = mapped_column(default=lambda: secrets.token_urlsafe(4), unique=True)

    # https://docs.sqlalchemy.org/en/20/orm/self_referential.html#self-referential
    referrer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    referrals: Mapped[List["User"]] = relationship("User", back_populates="referrer")
    referrer: Mapped["User"] = relationship("User", back_populates="referrals", remote_side=[id])


class EmailToken(Base):
    __tablename__ = "token"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(default=lambda: secrets.token_urlsafe(4))
    active: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[Optional[datetime]]

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user: Mapped["User"] = relationship(back_populates="email_token")


class Language(Base):
    # DeepL supported target languages
    __tablename__ = "language"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    ietf_tag: Mapped[str]
    code: Mapped[str]

    transcripts: Mapped[List["Transcript"]] = relationship(back_populates="input_language")
    translations: Mapped[List["TopicTranslation"]] = relationship(back_populates="target_lang")
    chats: Mapped[List["TelegramChat"]] = relationship(back_populates="language")

    @classmethod
    def get_default_language(cls, session: Session) -> "Language":
        stmt = select(cls).where(cls.ietf_tag == "en")
        return session.execute(stmt).scalar_one()

    @property
    def flag_emoji(self) -> str:
        exceptions = {
            "zh": "cn",
            "cs": "cz",
            "el": "gr",
            "ja": "jp",
            "ko": "kr",
            "nb": "no",
            "da": "dk",
            "uk": "ua",
        }
        _country_code = self.code[-2:]
        country_code = exceptions.get(_country_code.lower(), _country_code)
        sequence = map(lambda c: ord(c) + 127397, country_code.upper())
        return "".join(chr(i) for i in sequence)

    def ietf_tag_from_emoji(self, flag_emoji: str) -> str:
        # TODO create mapping for exceptions
        sequence = map(lambda c: ord(c) - 127397, flag_emoji)
        upper_case_code = "".join(chr(i) for i in sequence)
        return upper_case_code.lower()


class TelegramUser(Base):
    __tablename__ = "telegram_user"
    id: Mapped[int] = mapped_column("id", BigInteger, primary_key=True)  # same as TG user ID
    is_bot: Mapped[bool] = mapped_column(default=False)
    first_name: Mapped[str]
    last_name: Mapped[Optional[str]]
    username: Mapped[Optional[str]]
    language_code: Mapped[Optional[str]]
    is_premium: Mapped[Optional[bool]]

    user: Mapped[Optional["User"]] = relationship("User", back_populates="telegram_user")

    # use str of Model here to avoid linter warning
    chats: Mapped[set["TelegramChat"]] = relationship(secondary=chats_to_users_rel, back_populates="users")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="tg_user")
    summaries: Mapped[List["Summary"]] = relationship(back_populates="tg_user")
    subscriptions: Mapped[List["Subscription"]] = relationship(back_populates="tg_user")


class TelegramChat(Base):
    __tablename__ = "telegram_chat"
    id: Mapped[int] = mapped_column("id", BigInteger, primary_key=True)
    type: Mapped[str]

    title: Mapped[Optional[str]]
    username: Mapped[Optional[str]]

    # language = None means translate to language of transcript
    language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    language: Mapped["Language"] = relationship(back_populates="chats")
    messages: Mapped[List["BotMessage"]] = relationship(back_populates="chat")

    users: Mapped[set["TelegramUser"]] = relationship(secondary=chats_to_users_rel, back_populates="chats")
    subscriptions: Mapped[List["Subscription"]] = relationship(back_populates="chat")
    invoices: Mapped[List["Invoice"]] = relationship("Invoice", back_populates="chat")
    summaries: Mapped[List["Summary"]] = relationship(back_populates="tg_chat")

    @property
    def is_premium_active(self) -> bool:
        """check if any subscription is active or extended"""
        # apparently, self can't be scalars but only a single record
        return any(
            subscription.status in {SubscriptionStatus.active, SubscriptionStatus.extended}
            for subscription in self.subscriptions
        )


class BotMessage(Base):
    __tablename__ = "bot_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[Optional[str]]

    chat_id: Mapped[int] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="messages")

    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="messages")


class Transcript(Base):
    __tablename__ = "transcript"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str]
    file_unique_id: Mapped[str] = mapped_column(unique=True)
    sha256_hash: Mapped[str] = mapped_column(unique=True)  # hex
    duration: Mapped[int]
    mime_type: Mapped[str]
    file_size: Mapped[int]
    result: Mapped[str]

    finished_at: Mapped[Optional[datetime]]

    # when creating transcript, the input language is unknown
    input_language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    input_language: Mapped[Optional["Language"]] = relationship(back_populates="transcripts")

    # summary is created after transcribing
    # one2one relationship Transcript (Parent) -> Summary (Child)
    # https://docs.sqlalchemy.org/en/20/orm/basic_relationships.html#one-to-one
    summary: Mapped["Summary"] = relationship(back_populates="transcript")


class Summary(Base):
    __tablename__ = "summary"
    id: Mapped[int] = mapped_column(primary_key=True)
    finished_at: Mapped[Optional[datetime]]

    transcript_id = mapped_column(ForeignKey("transcript.id", ondelete="CASCADE"))
    transcript: Mapped["Transcript"] = relationship(back_populates="summary")

    tg_user_id: Mapped[Optional[BigInteger]] = mapped_column(ForeignKey("telegram_user.id"))
    tg_user: Mapped["TelegramUser"] = relationship(back_populates="summaries")

    tg_chat_id: Mapped[Optional[BigInteger]] = mapped_column(ForeignKey("telegram_chat.id"))
    tg_chat: Mapped["TelegramChat"] = relationship(back_populates="summaries")

    messages: Mapped[List["BotMessage"]] = relationship(back_populates="summary")
    topics: Mapped[List["Topic"]] = relationship(back_populates="summary")


class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = mapped_column(primary_key=True)

    text: Mapped[str]
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("summary.id", ondelete="CASCADE"),
    )
    summary: Mapped["Summary"] = relationship(back_populates="topics")

    # one topic can be translated to multiple languages
    translations: Mapped[List["TopicTranslation"]] = relationship(back_populates="topic")


class TopicTranslation(Base):
    __tablename__ = "translation"
    # translation always has topic as input
    id: Mapped[int] = mapped_column(primary_key=True)

    finished_at: Mapped[Optional[datetime]]

    # source_text = topic.text
    target_lang_id: Mapped[int] = mapped_column(ForeignKey("language.id"))
    target_lang: Mapped["Language"] = relationship(back_populates="translations")
    target_text: Mapped[str]

    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id", ondelete="CASCADE"))
    topic: Mapped["Topic"] = relationship(back_populates="translations")


class SubscriptionStatus(enum.Enum):
    pending = 0
    active = 1
    expired = 2
    canceled = 3
    extended = 4


class SubscriptionType(enum.Enum):
    onboarding = 0  # trial
    referral = 1
    reffered = 2
    paid = 3


class Subscription(Base):
    __tablename__ = "subscription"
    id: Mapped[int] = mapped_column(primary_key=True)

    tg_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("telegram_user.id"))
    tg_user: Mapped["TelegramUser"] = relationship(back_populates="subscriptions")

    chat_id: Mapped[Optional[int]] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="subscriptions")

    active: Mapped[bool] = mapped_column(default=True)

    start_date: Mapped[Optional[datetime]]
    end_date: Mapped[Optional[datetime]]
    status: Mapped[SubscriptionStatus] = mapped_column(
        sqlalchemy.Enum(SubscriptionStatus), default=SubscriptionStatus.pending
    )
    type: Mapped[SubscriptionType] = mapped_column(sqlalchemy.Enum(SubscriptionType))

    invoices: Mapped[List["Invoice"]] = relationship(back_populates="subscription")

    @staticmethod
    async def update_subscription_status(context: ContextTypes.DEFAULT_TYPE):
        """Async function to update subscription status (if expired)"""
        stmt = (
            select(Subscription)
            .where(Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.extended]))
            .where(Subscription.end_date < dt.datetime.now(dt.UTC))
        )
        with SessionContext.begin() as session:
            for subscription in session.execute(stmt).scalars():
                subscription.status = SubscriptionStatus.expired
                subscription.chat.language = Language.get_default_language(session)


class PaymentProvider(enum.Enum):
    STRIPE = 0
    TG_STARS = 1


class InvoiceStatus(enum.Enum):
    draft = 0
    paid = 1
    canceled = 2


class Invoice(Base):
    __tablename__ = "invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[InvoiceStatus] = mapped_column(sqlalchemy.Enum(InvoiceStatus), default=InvoiceStatus.draft)
    title: Mapped[str] = mapped_column(default="summar.ee bot Subscription")
    description: Mapped[str] = mapped_column(default="Premium Features: Unlimited summaries, unlimited translations")
    tg_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("telegram_user.id"))
    tg_user: Mapped["TelegramUser"] = relationship(back_populates="invoices")
    # TODO map with user email, send token and activate
    email: Mapped[Optional[str]]

    chat_id: Mapped[Optional[int]] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="invoices")

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    product: Mapped["Product"] = relationship(back_populates="invoices")

    subscription_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subscription.id"))
    subscription: Mapped["Subscription"] = relationship(back_populates="invoices")

    payment_provider: Mapped[PaymentProvider] = mapped_column(
        sqlalchemy.Enum(PaymentProvider), default=PaymentProvider.TG_STARS
    )
    provider_payment_charge_id: Mapped[Optional[str]]
    telegram_payment_charge_id: Mapped[Optional[str]]
    currency: Mapped[Optional[str]]
    total_amount: Mapped[Optional[int]]


class PremiumPeriod(enum.Enum):
    MONTH = 31
    QUARTER = 92
    YEAR = 366


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    premium_period: Mapped[PremiumPeriod] = mapped_column(sqlalchemy.Enum(PremiumPeriod))
    description: Mapped[str]
    price: Mapped[int]
    discounted_price: Mapped[Optional[int]]
    currency: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="product")
