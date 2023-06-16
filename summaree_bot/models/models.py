import os
from datetime import datetime
from functools import wraps

from typing import List, Optional
from sqlalchemy import ForeignKey
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)

class TelegramUser(Base):
    __tablename__ = "telegram_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    is_bot: Mapped[bool]
    first_name: Mapped[str]
    last_name: Mapped[Optional[str]]
    username: Mapped[Optional[str]]
    language_code: Mapped[Optional[str]]
    is_premium: Mapped[Optional[bool]]

class TelegramChat(Base):
    __tablename__ = "telegram_chat"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    language_code: Mapped[str]
    target_language_id: Mapped[int] = mapped_column(ForeignKey("language.id"))
    target_language: Mapped["Language"] = relationship("Language", back_populates="telegram_chats")
    messages: Mapped[List["BotMessage"]] = relationship(back_populates="chat")

class BotMessage(Base):
    __tablename__ = "bot_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[Optional[str]]

    chat_id: Mapped[int] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="messages")
    
    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="messages")
    
    translations: Mapped[List["Translation"]] = relationship(back_populates="message")

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

    messages: Mapped[List["BotMessage"]] = relationship(back_populates="translations")


class Language(Base):
    # DeepL supported target languages
    __tablename__ = "language"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    ietf_tag: Mapped[str]
    code: Mapped[str]

    transcripts: Mapped[List["Transcript"]] = relationship(back_populates="input_language")
    translations: Mapped[List["Translation"]] = relationship(back_populates="target_lang")
    telegram_chats: Mapped[List["TelegramChat"]] = relationship(back_populates="target_language_code")


if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")

# use this decorator for functions in bot.py
def add_session(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        with sessionmaker(engine).begin() as session:
            kwargs["session"] = session
            return fnc(*args, **kwargs)
    return wrapper

