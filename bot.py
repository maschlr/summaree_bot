import os
import logging

from dotenv import load_dotenv

from sqlalchemy import select
from sqlalchemy.orm import Session

from telegram import ForceReply, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from summaree_bot.integrations import transcribe, translate, summarize, available_target_languages
from summaree_bot.models import TelegramChat, Translation, add_session, ensure_chat


# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
_logger = logging.getLogger(__name__)


load_dotenv()

MSG = (
    "Send me a voice message and I will summarize it for you. "
    "You can forward messages from other chats to me, even if they are in other apps."            
)

# Define a few command handlers. These usually take the two arguments update and
# context.
@add_session
@ensure_chat
async def set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE, session: Session) -> None:
    """Set the target language when /lang {language_code} is issued."""
    if update.message is None or update.effective_chat is None:
        raise ValueError("The update must contain a message.")
    
    def msg(prefix, target_languages=available_target_languages):
        return f"{prefix}:\n" + "".join(str(lang) for lang in target_languages)

    try:
        if context.args is None:
            raise IndexError
        target_language_code = context.args[0].lower()
        if target_language := available_target_languages.ietf_tag_to_language.get(target_language_code):
            stmt = select(TelegramChat).where(TelegramChat.id == update.effective_chat.id)
            chat = session.scalars(stmt).one()
            if chat.target_language_code != target_language.ietf_language_tag:
                chat.target_language_code = target_language_code
                session.add(chat)
                await update.message.reply_html(
                    f"Target language successfully set to: {target_language_code} ({target_language.name})",
                    reply_markup=ForceReply(selective=True),
                )
            else:
                other_available_languages = [lang for lang in available_target_languages if lang.ietf_language_tag != target_language_code]
                answer = (
                    f"This language is already configured as the target language :{target_language_code} ({target_language.name})\n"
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
    
    stmt = select(TelegramChat).where(TelegramChat.id == update.effective_chat.id)
    chat = session.scalars(stmt).one()

    # set the target language to the language of the user that sent the message
    _logger.info(f"Summarizing voice message {update.message.id} from {update.effective_user.name}")
    await update.message.reply_chat_action(action="typing")

    transcript = await transcribe(update, session=session)
    # this automatically backpopulates the transcript
    translate(transcript=transcript, session=session)
    summary = summarize(transcript=transcript, session=session)
    if (target_lang_ietf_tag := chat.target_language_code) is None or target_lang_ietf_tag == "en":
        text = summary.text
    else:
        language = available_target_languages.ietf_tag_to_language[target_lang_ietf_tag]
        translation_out = translate(target_language=language, summary=summary, session=session)
        summary.translation = translation_out
        session.add(summary)
        text = translation_out.target_text

    await update.message.reply_text(text)

@add_session
@ensure_chat
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        raise ValueError("The update must contain a message and a user.")
    else:
        await update.message.reply_text(MSG)

def main() -> None:
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

