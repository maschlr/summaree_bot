from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..models import Language, TelegramChat, TelegramUser
from ..models.session import Session, session_context
from .helpers import AdminChannelMessage

__all__ = ["session_context", "ensure_chat", "Session", "chat_migration"]


def ensure_chat(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        # update is either in kwargs or first arg
        update = kwargs.get("update", args[0])
        context = kwargs.get("context", args[1])
        session = context.db_session

        if not (tg_user := session.get(TelegramUser, update.effective_user.id)):
            attrs = [
                "id",
                "first_name",
                "last_name",
                "username",
                "language_code",
                "is_premium",
                "is_bot",
            ]
            tg_user_kwargs = {attr: getattr(update.effective_user, attr, None) for attr in attrs}

            tg_user = TelegramUser(**tg_user_kwargs)
            session.add(tg_user)

            context.bot_data["message_queue"].appendleft(
                AdminChannelMessage(text=f"New user: {tg_user.md_link}", parse_mode=ParseMode.MARKDOWN)
            )

        if not (chat := session.get(TelegramChat, update.effective_chat.id)):
            # standard is english language
            en_lang = Language.get_default_language(session)
            if en_lang is None:
                raise ValueError("English language not found in database.")

            chat = TelegramChat(
                id=update.effective_chat.id,
                type=update.effective_chat.type,
                language=en_lang,
                users={tg_user},
            )
            session.add(chat)
            if chat.type != "private":
                context.bot_data["message_queue"].appendleft(AdminChannelMessage(text=f"New chat: {chat.title}"))
            # TODO: emit welcome message
        elif tg_user not in chat.users:
            chat.users.add(tg_user)

        # update chat data
        chat.title = update.effective_chat.title
        chat.username = update.effective_chat.username
        session.flush()

        return fnc(*args, **kwargs)

    return wrapper


async def chat_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for Superchat migration"""
    message = update.message
    application = context.application
    application.migrate_chat_data(message=message)

    # Get old and new chat ids
    old_id = message.migrate_from_chat_id or message.chat_id
    new_id = message.migrate_to_chat_id or message.chat_id

    with Session.begin() as session:
        chat = session.get(TelegramChat, old_id)
        if chat is None:
            return

        chat.id = new_id
    return
