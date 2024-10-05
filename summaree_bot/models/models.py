import datetime as dt
import enum
import json
import os
import re
import secrets
from datetime import datetime
from typing import List, Optional

import deepl
import sqlalchemy
from sqlalchemy import (
    Column,
    Date,
    ForeignKey,
    MetaData,
    String,
    Table,
    TypeDecorator,
    cast,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.types import BigInteger
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..utils import url
from .session import Session as SessionContext

deepl_token: Optional[str] = os.getenv("DEEPL_TOKEN")
translator = deepl.Translator(deepl_token)


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
    i18n: Mapped[List["Translation"]] = relationship(back_populates="target_lang")

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

    # use str of Model here to avoid linter warning
    chats: Mapped[set["TelegramChat"]] = relationship(secondary=chats_to_users_rel, back_populates="users")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="tg_user")
    summaries: Mapped[List["Summary"]] = relationship(back_populates="tg_user")
    subscriptions: Mapped[List["Subscription"]] = relationship(back_populates="tg_user")

    referral_token: Mapped[str] = mapped_column(default=lambda: secrets.token_urlsafe(4), unique=True)
    referral_token_active: Mapped[bool] = mapped_column(default=False)
    # https://docs.sqlalchemy.org/en/20/orm/self_referential.html#self-referential
    referred_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("telegram_user.id"))
    referred_by: Mapped["TelegramUser"] = relationship("TelegramUser", back_populates="referrals", remote_side=[id])

    referrals: Mapped[List["TelegramUser"]] = relationship("TelegramUser", back_populates="referred_by")

    @property
    def referral_url(self) -> str:
        """Generate a referral URL for a user."""
        callback_data = url.encode(["ref", self.referral_token])
        bot_link = os.getenv("BOT_LINK")
        return f"{bot_link}?start={callback_data.decode('ascii')}"

    @property
    def tg_url(self) -> str:
        """
        Generate a Telegram URL for a user.
        https://core.telegram.org/bots/api#formatting-options
        """
        return f"tg://user?id={self.id}"

    @property
    def md_link(self) -> str:
        """
        Generate a markdown link for a user.
        https://core.telegram.org/bots/api#markdownv2-style
        """
        return f"[{escape_markdown(self.username or self.first_name, version=2)}]({self.tg_url})"

    @classmethod
    def get_by_id_or_username(cls, session: Session, user_id_or_username: str) -> Optional["TelegramUser"]:
        try:
            user_id = int(user_id_or_username)
        except ValueError:
            user_id = None
            username = user_id_or_username

        if user_id is not None:
            stmt = select(cls).where(cls.id == user_id)
        else:
            stmt = select(cls).where(cls.username == username)
        return session.execute(stmt).scalar_one_or_none()

    @property
    def is_summaree_premium(self) -> bool:
        """Check if the user has a summaree premium subscription"""
        return any(
            subscription.status in {SubscriptionStatus.active, SubscriptionStatus.extended}
            for subscription in self.subscriptions
        )


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


class JsonList(TypeDecorator):
    impl = String

    def process_bind_param(self, value, dialect):
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value:
            return json.loads(value)
        return []


class Transcript(Base):
    __tablename__ = "transcript"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str]
    file_unique_id: Mapped[str] = mapped_column(unique=True)
    sha256_hash: Mapped[str] = mapped_column(unique=True)  # hex
    duration: Mapped[Optional[int]]
    mime_type: Mapped[str]
    file_size: Mapped[int]
    result: Mapped[str]
    total_seconds: Mapped[Optional[int]]

    finished_at: Mapped[Optional[datetime]]

    # when creating transcript, the input language is unknown
    input_language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    input_language: Mapped[Optional["Language"]] = relationship(back_populates="transcripts")

    reaction_emoji: Mapped[Optional[str]]
    hashtags: Mapped[Optional[List[str]]] = mapped_column(JsonList)
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

    # openai Data to track usage/costs
    openai_id: Mapped[Optional[str]]
    openai_model: Mapped[Optional[str]]
    completion_tokens: Mapped[Optional[int]]
    prompt_tokens: Mapped[Optional[int]]

    messages: Mapped[List["BotMessage"]] = relationship(back_populates="summary")
    topics: Mapped[List["Topic"]] = relationship(back_populates="summary")

    @classmethod
    def get_usage_stats(cls, session: Session) -> dict:
        """Get summary usage statistics: daily count and unique users"""
        query = (
            session.query(
                cast(cls.created_at, Date).label("date"),
                func.count(cls.id).label("summary_count"),
                func.count(func.distinct(cls.tg_user_id)).label("user_count"),
            )
            .group_by(cast(cls.created_at, Date))
            .order_by(cast(cls.created_at, Date))
        )

        return query.all()

    @property
    def total_cost(self) -> Optional[float]:
        """
        Calculate the total cost of the summary.
        """
        # gpt-4o: $0.015 per 1M tokens
        # gpt-4: $0.015 per 1M tokens
        # gpt-3.5-turbo: $0.0015 per 1M tokens
        match = re.match(r"gpt-4o-mini.*?", self.openai_model)
        if not match:
            # raise NotImplementedError(f"Cost for model {self.openai_model} not implemented")
            return None

        total_cost = (
            self.completion_tokens / 1e6 * 0.6
            + self.prompt_tokens / 1e6 * 0.15
            + self.transcript.total_seconds / 60 * 0.006
        )
        return total_cost


class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = mapped_column(primary_key=True)

    text: Mapped[str]
    order: Mapped[int]

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
    paid_at: Mapped[Optional[datetime]]


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


class Translation(Base):
    __tablename__ = "translation_i18n"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_text: Mapped[str]
    target_text: Mapped[str]
    target_lang_id: Mapped[int] = mapped_column(ForeignKey("language.id"))
    target_lang: Mapped["Language"] = relationship(back_populates="i18n")

    @classmethod
    def get(cls, session: Session, source_lang_texts: set[str], ietf_lang_code: str) -> dict[str, str]:
        lang_stmt = select(Language).where(Language.ietf_tag == ietf_lang_code)
        lang = session.execute(lang_stmt).scalar_one()

        filtered_i18n = filter(lambda i18n: i18n.source_text in (source_lang_texts), lang.i18n)
        result = {translation.source_text: translation.target_text for translation in filtered_i18n}
        missing_source_lang_texts = source_lang_texts - result.keys()
        if missing_source_lang_texts:
            # create new translations for the missing texts
            new_translations = {
                source_text: translator.translate_text(source_text, source_lang="EN", target_lang=lang.code).text
                for source_text in missing_source_lang_texts
            }
            result.update(new_translations)

            # save new translations to the database
            session.add_all(
                [
                    cls(
                        source_text=source_text,
                        target_text=target_text,
                        target_lang=lang,
                    )
                    for source_text, target_text in new_translations.items()
                ]
            )

        return result
