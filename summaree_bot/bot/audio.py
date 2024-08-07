import asyncio
import datetime
import tempfile
from pathlib import Path
from typing import Any, Coroutine, cast

import magic
from sqlalchemy import extract
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from ..integrations import (
    _check_existing_transcript,
    _elaborate,
    _extract_file_name,
    _summarize,
    _transcribe_file,
    _translate_topic,
)
from ..integrations.deepl import _translate_text
from ..logging import getLogger
from ..models import Language, Summary, TelegramChat, Transcript
from ..models.session import DbSessionContext, Session, session_context
from . import BotMessage

# Enable logging
_logger = getLogger(__name__)


async def get_summary_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Coroutine[Any, Any, BotMessage]:
    context = cast(DbSessionContext, context)
    with Session.begin() as session:
        context.db_session = session
        # check existing transcript via file_unique_id,
        transcript, voice_or_audio = _check_existing_transcript(update, context)
        #   if not exist, download audio (async) to tempdir and transcribe
        if transcript is None:
            file_name = _extract_file_name(voice_or_audio)
            with tempfile.TemporaryDirectory() as tempdir_path_str:
                # download the file to the folder
                tempdir_path = Path(tempdir_path_str)
                file_path = tempdir_path / file_name
                file = await voice_or_audio.get_file()
                await file.download_to_drive(file_path)

                if not file_name.suffix:
                    mime = magic.from_file(file_path, mime=True)
                    _, suffix = mime.split("/")
                    file_path.rename(file_path.with_suffix(f".{suffix}"))

                transcript = _transcribe_file(update, context, file_path, voice_or_audio)

        summary = _summarize(update, context, transcript)

        bot_msg = _get_summary_message(update, context, summary)

        # add button for elaboration
        buttons = [
            InlineKeyboardButton(
                "📖 Full transcript",
                callback_data={
                    "fnc": "elaborate",
                    "kwargs": {"transcript_id": summary.transcript_id},
                },
            ),
            InlineKeyboardButton(
                "🪄 Give me more",
                callback_data={
                    "fnc": "elaborate",
                    "kwargs": {"summary_id": summary.id},
                },
            ),
        ]
        bot_msg.reply_markup = InlineKeyboardMarkup([buttons])

    return bot_msg


@session_context
def _get_summary_message(update: Update, context: DbSessionContext, summary: Summary) -> BotMessage:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    session = context.db_session
    session.add(summary)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

    en_lang = Language.get_default_language(session)
    if chat.language != en_lang:
        translations = [
            _translate_topic(update, context, target_language=chat.language, topic=topic) for topic in summary.topics
        ]
        session.add_all(translations)
        msg = "\n".join(f"- {translation.target_text}" for translation in translations)
    else:
        msg = "\n".join(f"- {topic.text}" for topic in summary.topics)

    return BotMessage(chat_id=update.effective_chat.id, text=msg)


async def elaborate(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    wait_msg = await context.bot.send_message(
        update.effective_chat.id,
        "📥 Received your request and processing it....⏳\n Please wait a moment. ☕",
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    bot_msg = _elaborate(update, context, **kwargs)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(wait_msg.delete())
        tg.create_task(bot_msg.send(context.bot))


async def transcribe_and_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (
        update.message is None
        or update.effective_chat is None
        or update.effective_user is None
        or ((voice := update.message.voice) is None and (audio := update.message.audio) is None)
    ):
        raise ValueError("The update must contain chat/user/voice/audio message.")

    # TODO: restrict file size to 10MB for free users
    # TODO: openai whisper docs mention possible splitting of files >20MB -> look into/inplement
    file_size = cast(int, voice.file_size if voice else audio.file_size if audio else 0)
    if file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "🚫 Sorry, the file is too big to be processed (max. 20MB). Please send a smaller file."
        )
        return

    with Session.begin() as session:
        # check how many transcripts/summaries have already been created in the current month
        chat = session.get(TelegramChat, update.effective_chat.id)
        current_month = datetime.datetime.now(tz=datetime.UTC).month
        summaries_this_month = (
            session.query(Summary)
            .filter(
                extract("month", Summary.created_at) == current_month, Summary.tg_chat_id == update.effective_chat.id
            )
            .all()
        )
        if len(summaries_this_month) >= 10 and not chat.is_premium:
            msg = BotMessage(
                chat_id=update.effective_chat.id,
                text=(
                    "🚫 Sorry, you have reached the limit of 10 summaries per month. "
                    "Please consider upgrading to `/premium` to get unlimited summaries."
                ),
                reply_to_message_id=update.effective_message.id,
            )
            await msg.send(context.bot)
            return

    _logger.info(f"Transcribing and summarizing message: {update.message}")
    async with asyncio.TaskGroup() as tg:
        start_msg_task = tg.create_task(
            update.message.reply_text(
                "🎧 Received your voice/audio message.\n☕ Transcribing and summarizing...\n⏳ Please wait a moment.",
            )
        )
        bot_response_msg_task = tg.create_task(get_summary_msg(update, context))
        tg.create_task(context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING))

    start_message = start_msg_task.result()
    bot_response_msg = bot_response_msg_task.result()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message.delete())
        tg.create_task(bot_response_msg.send(context.bot))


@session_context
def _translate_transcript(update: Update, context: DbSessionContext, transcript_id: int) -> BotMessage:
    """Find transscript in the database and return a BotMessage with the translation"""
    session = context.db_session

    transcript = session.get(Transcript, transcript_id)
    if transcript is None:
        raise ValueError(f"Transcript with ID {transcript_id} not found.")

    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Chat with ID {update.effective_chat.id} not found.")
    target_language = chat.language

    # TODO: create DB model for text translated transcripts to avoid calling the API
    # Before: see if this is indeed called repeatedly
    translation = _translate_text(transcript.result, target_language)

    bot_msg = BotMessage(
        chat_id=update.effective_chat.id,
        text=translation,
    )

    return bot_msg


async def translate_transcript(update: Update, context: ContextTypes.DEFAULT_TYPE, transcript_id: int) -> None:
    """Callback function to translate a transcript when button is clicked"""
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    async with asyncio.TaskGroup() as tg:
        process_msg_task = tg.create_task(
            update.effective_message.reply_text(
                "📝 Received your request.\n☕ Translating your transcript...\n⏳ Please wait a moment.",
            )
        )
        tg.create_task(context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING))

    process_msg = process_msg_task.result()
    bot_msg = _translate_transcript(update, context, transcript_id=transcript_id)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(update.effective_message.edit_reply_markup(reply_markup=None))
        tg.create_task(process_msg.delete())
        tg.create_task(bot_msg.send(context.bot))
