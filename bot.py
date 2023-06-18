import os
import logging
from functools import wraps

from sqlalchemy import select
from sqlalchemy.orm import Session

from telegram import ForceReply, Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import summaree_bot.logging
from summaree_bot.integrations import transcribe, translate, summarize, check_database_languages
from summaree_bot.models import TelegramChat, TelegramUser, Language
from summaree_bot.models.session import add_session


# Enable logging
_logger = logging.getLogger(__name__)

MSG = (
    "Send me a voice message and I will summarize it for you. "
    "You can forward messages from other chats to me, even if they are in other apps."            
)

def ensure_chat(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        # update is either in kwargs or first arg
        update = kwargs.get("update", args[0])
        # session is in kwargs
        session = kwargs.get("session")
        
        if not (user := session.get(TelegramUser, update.effective_user.id)):
            attrs = ["id", "first_name", "last_name", "username", "language_code", "is_premium", "is_bot"]
            user_kwargs = {attr: getattr(update.effective_user, attr, None) for attr in attrs}
            
            user = TelegramUser(
                **user_kwargs
            )
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
                users={user}
            )
            session.add(chat)
            # TODO: emit welcome message
        elif user not in chat.users:
            chat.users.append(user)

        return fnc(*args, **kwargs)
    return wrapper


# Define a few command handlers. These usually take the two arguments update and
# context.
@add_session
@ensure_chat
async def set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    """Set the target language when /lang {language_code} is issued."""
    if update.message is None or update.effective_chat is None:
        raise ValueError("The update must contain a message.")
    
    stmt = select(Language)
    languages = session.scalars(stmt).all()

    def msg(prefix, target_languages=languages):
        return prefix + "\n\t".join(f"{lang.ietf_tag} ({lang.name})" for lang in target_languages)

    try:
        if context.args is None:
            raise IndexError
        target_language_ietf_tag = context.args[0].lower()
        stmt = select(Language).where(Language.ietf_tag == target_language_ietf_tag)
        if target_language := session.scalar(stmt):
            if not (chat := session.get(TelegramChat, update.effective_chat.id)):
                return
            if chat.language != target_language:
                chat.language = target_language
                session.commit()
                await update.message.reply_html(
                    f"Target language successfully set to: {target_language_ietf_tag} ({target_language.name})",
                    reply_markup=ForceReply(selective=True),
                )
            else:
                other_available_languages_stmt = select(Language).where(Language.ietf_tag != target_language_ietf_tag)  
                other_available_languages = session.scalars(other_available_languages_stmt).all()
                answer = (
                    f"This language is already configured as the target language :{chat.language.ietf_tag} ({chat.language.name})\n"
                    "Other available languages are:\n\n"
                )
                
                await update.message.reply_html(
                    msg(answer, other_available_languages),
                    reply_markup=ForceReply(selective=True),
                )
        else:
            prefix = (
                "Unknown target language. Set your target language with `/lang language`.\n"
                "Available laguages are:\n\n"
            )
            await update.message.reply_html(
                msg(prefix),
                reply_markup=ForceReply(selective=True),
            )
    except IndexError:
        await update.message.reply_html(
            msg("Set your target language with `/lang language`. Available languages are: \n\n"),
            reply_markup=ForceReply(selective=True),
        )

@add_session
@ensure_chat
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    if update.message is None or update.effective_user is None:
        raise ValueError("The update must contain a message and a user.")
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! " + MSG,
        reply_markup=ForceReply(selective=True),
    )

@add_session
@ensure_chat
async def raison_d_etre(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    if update.message is None or update.message.voice is None or update.effective_chat is None or update.effective_user is None:
        _logger.warning("The update must contain a voice message.")
        return
    
    _logger.info(f"Summarizing voice message {update.message.id} from {update.effective_user.name}")
    await update.message.reply_chat_action(action="typing")

    transcript = await transcribe(update, session=session)
    summary = summarize(transcript=transcript, session=session)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None: 
        return
    en_lang = Language.get_default_language(session)
    if chat.language != en_lang:
        translations = [translate(session=session, target_language=chat.language, topic=topic) for topic in summary.topics]
        for translation in translations:
            session.add(translation)
        msg = "\n".join(f"- {translation.target_text}" for translation in translations)
    else:
        msg = "\n".join(f"- {topic.text}" for topic in summary.topics)

    await update.message.reply_text(msg)

@add_session
@ensure_chat
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    if update.message is None:
        raise ValueError("The update must contain a message.")
    else:
        await update.message.reply_text(MSG)

def main() -> None:
    check_database_languages()

    """Start the bot."""
    # Create the Application and pass it your bot's token.
    if telegram_bot_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        application = Application.builder().token(telegram_bot_token).build()
    else:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    # on different commands - answer in Telegram
    commands_to_set = []
    for func, command, description in (
        (start, "start", "Say hi to the bot"),
        (set_lang, "lang", "Set default language for summaries (default is English)"),
        (start, "help", "Show help message")
    ):
        application.add_handler(CommandHandler("start", start))
        bot_command: BotCommand = BotCommand(command, description)
        commands_to_set.append(bot_command)
        
        application.add_handler(CommandHandler("lang", set_lang))

    async def post_init(self):
        await self.bot.delete_my_commands()
        await self.bot.set_my_commands(commands_to_set)
    
    application.post_init = post_init
    
    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.VOICE, raison_d_etre))
    application.add_handler(MessageHandler(filters.ALL & ~filters.VOICE & ~filters.COMMAND, catch_all))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()
    # TODO: set environment variable to run with webhook (callback url)


if __name__ == "__main__":
    main()
