import enum
import secrets
from datetime import datetime
from typing import List, Optional

import sqlalchemy
from sqlalchemy import Column, ForeignKey, MetaData, Table, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

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
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(ForeignKey("telegram_user.id"))
    telegram_user: Mapped["TelegramUser"] = relationship("TelegramUser", back_populates="user")
    email: Mapped[Optional[str]]
    email_token: Mapped["EmailToken"] = relationship(back_populates="user")
    subscriptions: Mapped[List["Subscription"]] = relationship(back_populates="user")

    referral_token: Mapped[str] = mapped_column(default=lambda: secrets.token_urlsafe(4), unique=True)

    # https://docs.sqlalchemy.org/en/20/orm/self_referential.html#self-referential
    referrer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    referrals: Mapped[List["User"]] = relationship("User", back_populates="referrer")
    referrer: Mapped["User"] = relationship("User", back_populates="referrals", remote_side=[id])

    invoices: Mapped["Invoice"] = relationship("Invoice", back_populates="user")


class EmailToken(Base):
    __tablename__ = "token"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(default=lambda: secrets.token_urlsafe(4))
    active: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[Optional[datetime]]

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped["User"] = relationship(back_populates="email_token")


class Language(Base):
    # DeepL supported target languages
    __tablename__ = "language"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    ietf_tag: Mapped[str]
    code: Mapped[str]

    transcripts: Mapped[List["Transcript"]] = relationship(back_populates="input_language")
    translations: Mapped[List["Translation"]] = relationship(back_populates="target_lang")
    chats: Mapped[List["TelegramChat"]] = relationship(back_populates="language")

    @classmethod
    def get_default_language(cls, session: Session) -> "Language":
        stmt = select(cls).where(cls.ietf_tag == "en")
        return session.execute(stmt).scalar_one()


class TelegramUser(Base):
    __tablename__ = "telegram_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    is_bot: Mapped[bool] = mapped_column(default=False)
    first_name: Mapped[str]
    last_name: Mapped[Optional[str]]
    username: Mapped[Optional[str]]
    language_code: Mapped[Optional[str]]
    is_premium: Mapped[Optional[bool]]

    user: Mapped[Optional["User"]] = relationship("User", back_populates="telegram_user")

    # use str of Model here to avoid linter warning
    chats: Mapped[set["TelegramChat"]] = relationship(secondary=chats_to_users_rel, back_populates="users")


class TelegramChat(Base):
    __tablename__ = "telegram_chat"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]

    title: Mapped[Optional[str]]
    username: Mapped[Optional[str]]

    # language = None means translate to language of transcript
    # TODO: make translation premium feature
    language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    language: Mapped["Language"] = relationship(back_populates="chats")
    messages: Mapped[List["BotMessage"]] = relationship(back_populates="chat")

    users: Mapped[set["TelegramUser"]] = relationship(secondary=chats_to_users_rel, back_populates="chats")
    subscriptions: Mapped[List["Subscription"]] = relationship(back_populates="chat")
    invoices: Mapped["Invoice"] = relationship("Invoice", back_populates="chat")


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

    # when creating transcript, the input language is unknown
    input_language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    input_language: Mapped[Optional["Language"]] = relationship(back_populates="transcripts")

    # summary is created after transcribing
    summary: Mapped["Summary"] = relationship(back_populates="transcript")


class Summary(Base):
    __tablename__ = "summary"
    id: Mapped[int] = mapped_column(primary_key=True)

    transcript_id = mapped_column(ForeignKey("transcript.id"))
    transcript: Mapped["Transcript"] = relationship(back_populates="summary")

    messages: Mapped[List["BotMessage"]] = relationship(back_populates="summary")
    topics: Mapped[List["Topic"]] = relationship(back_populates="summary")


class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = mapped_column(primary_key=True)

    text: Mapped[str]
    summary_id: Mapped[int] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped["Summary"] = relationship(back_populates="topics")

    # one topic can be translated to multiple languages
    translations: Mapped[List["Translation"]] = relationship(back_populates="topic")


class Translation(Base):
    __tablename__ = "translation"
    # translation always has topic as input
    id: Mapped[int] = mapped_column(primary_key=True)

    # input lang is always english
    # source_text = topic.text
    target_lang_id: Mapped[int] = mapped_column(ForeignKey("language.id"))
    target_lang: Mapped["Language"] = relationship(back_populates="translations")
    target_text: Mapped[str]

    topic_id: Mapped[int] = mapped_column(ForeignKey("topic.id"))
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

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped["User"] = relationship(back_populates="subscriptions")

    chat_id: Mapped[int] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="subscriptions")

    active: Mapped[bool] = mapped_column(default=True)

    start_date: Mapped[Optional[datetime]]
    end_date: Mapped[Optional[datetime]]
    status: Mapped[SubscriptionStatus] = mapped_column(
        sqlalchemy.Enum(SubscriptionStatus), default=SubscriptionStatus.pending
    )
    type: Mapped[SubscriptionType] = mapped_column(sqlalchemy.Enum(SubscriptionType))

    invoices: Mapped[List["Invoice"]] = relationship(back_populates="subscription")
    # TODO: implement worker that periodically checks for expired subscriptions
    # TODO: implement check for subscription status on every summarization request


class PaymentProvider(enum.Enum):
    STRIPE = 0


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
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped["User"] = relationship(back_populates="invoices")
    # TODO map with user email, send token and activate
    email: Mapped[Optional[str]]

    chat_id: Mapped[int] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="invoices")

    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"))
    product: Mapped["Product"] = relationship(back_populates="invoices")

    subscription_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subscription.id"))
    subscription: Mapped["Subscription"] = relationship(back_populates="invoices")

    payment_provider: Mapped[PaymentProvider] = mapped_column(
        sqlalchemy.Enum(PaymentProvider), default=PaymentProvider.STRIPE
    )
    provider_payment_charge_id: Mapped[Optional[str]]
    telegram_payment_charge_id: Mapped[Optional[str]]
    currency: Mapped[Optional[str]]
    total_amount: Mapped[Optional[int]]


class PremiumPeriod(enum.Enum):
    MONTH = 31
    THREE_MONTHS = 92
    YEAR = 366


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    premium_period: Mapped[PremiumPeriod] = mapped_column(sqlalchemy.Enum(PremiumPeriod))
    description: Mapped[str]
    price: Mapped[int]
    currency: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="product")
