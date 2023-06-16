import os
from datetime import datetime
from functools import wraps

from typing import List, Optional
from sqlalchemy import select, ForeignKey
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy.orm import sessionmaker

from telegram import Update

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
    language_code: Mapped[Optional[str]]
    target_language_code: Mapped[str] # ietf language tag
    messages: Mapped[List["BotMessage"]] = relationship(back_populates="chat")

class BotMessage(Base):
    __tablename__ = "bot_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[Optional[str]]

    chat_id: Mapped[int] = mapped_column(ForeignKey("telegram_chat.id"))
    chat: Mapped["TelegramChat"] = relationship(back_populates="messages")
    
    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="messages")
    
    translation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("translation.id"))
    translation: Mapped[Optional["Translation"]] = relationship(back_populates="messages")

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
    language_code: Mapped[Optional[str]] # ietf language tag of input language

    translation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("translation.id"))
    translation: Mapped["Translation"] = relationship(back_populates="transcript")

    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="transcript")

class Summary(Base):
    __tablename__ = "summary"
    id: Mapped[int] = mapped_column(primary_key=True)
    transcript: Mapped["Transcript"] = relationship(back_populates="summary")
    translation: Mapped[Optional["Translation"]] = relationship(back_populates="summary")
    messages: Mapped[Optional[List["BotMessage"]]] = relationship(back_populates="summary")
    topics: Mapped[List["Topic"]] = relationship(back_populates="summary")


class Topic(Base):
    __tablename__ = "topic"
    id: Mapped[int] = mapped_column(primary_key=True)

    text: Mapped[str]
    summary_id: Mapped[int] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped["Summary"] = relationship(back_populates="topics")


class Translation(Base):
    __tablename__ = "translation"
    # translation has either transcript or summary as input
    id: Mapped[int] = mapped_column(primary_key=True)
    source_lang: Mapped[str]
    source_text: Mapped[str]
    target_lang: Mapped[str]
    target_text: Mapped[str]

    transcript_id: Mapped[Optional[int]] = mapped_column(ForeignKey("translation.id"))
    transcript: Mapped[Optional["Transcript"]] = relationship(back_populates="translation")    
    # can be translation_in or translation_out so we don't define back_populates kwarg
    # TODO fixme
    summary_id: Mapped[Optional[int]] = mapped_column(ForeignKey("summary.id"))
    summary: Mapped[Optional["Summary"]] = relationship(back_populates="translation")

    messages: Mapped[List["BotMessage"]] = relationship(back_populates="translation")


if db_url := os.getenv("DB_URL"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
else:
    raise ValueError("DB_URL environment variable not set. Cannot initialize database engine.")

def add_session(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        with sessionmaker(engine).begin() as session:
            kwargs["session"] = session
            return fnc(*args, **kwargs)
    return wrapper

# is this a better fit for bot.py?
def ensure_chat(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        # update is either in kwargs or first arg
        update = kwargs.get("update", args[0])
        # session is in kwargs
        session = kwargs.get("session")
        stmt = select(TelegramChat).where(TelegramChat.id == update.effective_chat.id)
        if not session.scalars(stmt).one_or_none():
            chat = TelegramChat(
                id=update.effective_chat.id,
                type=update.effective_chat.type,
                target_language_code=update.effective_user.language_code if update.effective_chat.type == "private" else "en"
            )
            session.add(chat)
            # TODO: emit welcome message
        return fnc(*args, **kwargs)
    return wrapper
