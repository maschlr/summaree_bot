from datetime import datetime

from typing import List, Optional
from sqlalchemy import ForeignKey, Table, Column, select
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, Session
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

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
    username: Mapped[str]
    language_code: Mapped[Optional[str]]
    is_premium: Mapped[Optional[bool]]

    # use str of Model here to avoid linter warning
    chats: Mapped[set["TelegramChat"]] = relationship(
        secondary=chats_to_users_rel, back_populates="users"
    )

class TelegramChat(Base):
    __tablename__ = "telegram_chat"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]

    language_id: Mapped[int] = mapped_column(ForeignKey("language.id"))
    language: Mapped["Language"] = relationship(back_populates="chats")
    messages: Mapped[List["BotMessage"]] = relationship(back_populates="chat")

    users: Mapped[set[TelegramUser]] = relationship(
        secondary=chats_to_users_rel, back_populates="chats"
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
    sha256_hash: Mapped[str] = mapped_column(unique=True) #hex
    duration: Mapped[int]
    mime_type: Mapped[str]
    file_size: Mapped[int]
    result: Mapped[str]

    # when creating transcript, the input language is unknown
    input_language_id: Mapped[Optional[int]] = mapped_column(ForeignKey("language.id"))
    input_language: Mapped[Optional["Language"]] = relationship(back_populates="transcripts") # ietf language tag (e.g. en)

    # summary is created after transcribing
    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="transcript")

class Summary(Base):
    __tablename__ = "summary"
    id: Mapped[int] = mapped_column(primary_key=True)
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
