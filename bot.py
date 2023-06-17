import os
import logging
from functools import wraps

from sqlalchemy import select
from sqlalchemy.orm import Session

from telegram import ForceReply, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from summaree_bot.integrations import transcribe, translate, summarize, check_database_languages
from summaree_bot.models import TelegramChat, Language
from summaree_bot.models.session import add_session


# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
fh = logging.FileHandler("summaree_bot.log")
_logger = logging.getLogger(__name__)
_logger.addHandler(fh)


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
        if not (chat := session.get(TelegramChat, update.effective_chat.id)):
            # standard is english language
            en_lang_stmt = select(Language).where(Language.ietf_tag == "en")
            en_lang = session.scalars(en_lang_stmt).one_or_none()
            if en_lang is None:
                raise ValueError("English language not found in database.")
            
            language_code = None
            attr = "language_code"
            for obj in (update.effective_chat, update.effective_user):
                if hasattr(obj, attr):
                    language_code = getattr(obj, attr)
            if language_code is None:
                language_code = "en"
                
            chat = TelegramChat(
                id=update.effective_chat.id,
                type=update.effective_chat.type,
                target_language=en_lang,
                user_language=language_code,
            )
            session.add(chat)
            # TODO: emit welcome message
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
        return f"{prefix}:" + "\n\t".join(f"{lang.ietf_tag} ({lang.name})" for lang in target_languages)

    try:
        if context.args is None:
            raise IndexError
        target_language_ietf_tag = context.args[0].lower()
        stmt = select(Language).where(Language.ietf_tag == target_language_ietf_tag)
        if target_language := session.scalars(stmt).one_or_none():
            chat = session.get(TelegramChat, update.effective_chat.id)
            if chat is None:
                return
            elif chat.target_language != target_language:
                chat.target_language = target_language
                session.add(chat)
                await update.message.reply_html(
                    f"Target language successfully set to: {target_language_ietf_tag} ({target_language.name})",
                    reply_markup=ForceReply(selective=True),
                )
            else:
                other_available_languages_stmt = select(Language).where(Language.ietf_tag != target_language_ietf_tag)  
                other_available_languages = session.scalars(other_available_languages_stmt).all()
                answer = (
                    f"This language is already configured as the target language :{chat.target_language.ietf_tag} ({chat.target_language.name})\n"
                    "Other available languages are"
                )
                
                await update.message.reply_html(
                    msg(answer, other_available_languages),
                    reply_markup=ForceReply(selective=True),
                )
        else:
            await update.message.reply_html(
                msg("Available target languages"),
                reply_markup=ForceReply(selective=True),
            )
    except IndexError:
        await update.message.reply_html(
            msg("Please type your target language"),
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
    en_lang_stmt = select(Language).where(Language.ietf_tag == "en")
    en_lang = session.scalars(en_lang_stmt).one()
    if chat.target_language != en_lang:
        translations = [translate(session=session, target_language=chat.target_language, topic=topic) for topic in summary.topics]
        for translation in translations:
            session.add(translation)
        msg = "\n".join(translation.target_text for translation in translations)
    else:
        msg = "\n".join(topic.text for topic in summary.topics)

    await update.message.reply_text(msg)

@add_session
@ensure_chat
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("lang", set_lang))

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.VOICE, raison_d_etre))
    application.add_handler(MessageHandler(filters.ALL & ~filters.VOICE & ~filters.COMMAND, catch_all))

    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()

