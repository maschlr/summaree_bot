from telegram import Update

from ..integrations import summarize, transcribe_audio, transcribe_voice, translate
from ..logging import getLogger
from ..models import Language, TelegramChat
from ..models.session import DbSessionContext
from .helpers import add_session, ensure_chat

# Enable logging
_logger = getLogger(__name__)


@add_session
@ensure_chat
async def transcribe_and_summarize(update: Update, context: DbSessionContext) -> None:
    if update.message is None or update.effective_chat is None or update.effective_user is None:
        _logger.warning("The update must contain a voice message.")
        return

    _logger.info(f"Summarizing voice message {update.message.id} from {update.effective_user.name}")
    await update.message.reply_chat_action(action="typing")
    session = context.db_session
    if update.message.voice is not None:
        transcript = transcribe_voice(update, session)
    elif update.message.audio is not None:
        transcript = transcribe_audio(update, session)
    else:
        raise ValueError("The update must contain a voice or audio message.")

    summary = summarize(transcript=await transcript, session=session)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        return
    en_lang = Language.get_default_language(session)
    if chat.language != en_lang:
        translations = [
            translate(session=session, target_language=chat.language, topic=topic) for topic in summary.topics
        ]
        session.add_all(translations)
        msg = "\n".join(f"- {translation.target_text}" for translation in translations)
    else:
        msg = "\n".join(f"- {topic.text}" for topic in summary.topics)

    await update.message.reply_text(msg)
