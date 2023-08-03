from functools import wraps

from ..models import Language, TelegramChat, TelegramUser
from ..models.session import add_session

__all__ = ["add_session", "ensure_chat"]


def ensure_chat(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        # update is either in kwargs or first arg
        update = kwargs.get("update", args[0])

        context = kwargs.get("context", args[1])
        session = context.db_session

        if not (user := session.get(TelegramUser, update.effective_user.id)):
            attrs = [
                "id",
                "first_name",
                "last_name",
                "username",
                "language_code",
                "is_premium",
                "is_bot",
            ]
            user_kwargs = {attr: getattr(update.effective_user, attr, None) for attr in attrs}

            user = TelegramUser(**user_kwargs)
            session.add(user)

        if not (chat := session.get(TelegramChat, update.effective_chat.id)):
            # standard is english language
            en_lang = Language.get_default_language(session)
            if en_lang is None:
                raise ValueError("English language not found in database.")

            chat = TelegramChat(
                id=update.effective_chat.id,
                type=update.effective_chat.type,
                language=en_lang,
                users={user},
            )
            session.add(chat)
            # TODO: emit welcome message
        elif user not in chat.users:
            chat.users.append(user)

        return fnc(*args, **kwargs)

    return wrapper
