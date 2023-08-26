import asyncio
import tempfile
from pathlib import Path
from typing import Any, Coroutine, cast

import magic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from ..integrations import (
    _check_existing_transcript,
    _elaborate,
    _extract_file_name,
    _get_summary_message,
    _summarize,
    _transcribe_file,
)
from ..logging import getLogger
from ..models.session import DbSessionContext, Session
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
                callback_data={"fnc": "elaborate", "kwargs": {"transcript_id": summary.transcript_id}},
            ),
            InlineKeyboardButton(
                "🪄 Give me more", callback_data={"fnc": "elaborate", "kwargs": {"summary_id": summary.id}}
            ),
        ]
        bot_msg.reply_markup = InlineKeyboardMarkup([buttons])

    return bot_msg


async def elaborate(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    wait_msg = await context.bot.send_message(
        update.effective_chat.id, "📥 Received your request and processing it....⏳\n Please wait a moment. ☕"
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

    file_size = cast(int, voice.file_size if voice else audio.file_size if audio else 0)
    if file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "🚫 Sorry, the file is too big to be processed (max. 20MB). Please send a smaller file."
        )
        return

    _logger.info(f"Transcribing and summarizing message: {update.message}")
    async with asyncio.TaskGroup() as tg:
        start_msg_task = tg.create_task(
            update.message.reply_text(
                "🎧 Received your voice/audio message.\n☕ Transcribing and summarizing...\n⏳ Please wait a moment.",
                reply_to_message_id=update.message.id,
            )
        )
        bot_response_msg_task = tg.create_task(get_summary_msg(update, context))

    start_message = start_msg_task.result()
    bot_response_msg = bot_response_msg_task.result()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message.delete())
        tg.create_task(bot_response_msg.send(context.bot))
