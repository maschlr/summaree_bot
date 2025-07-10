import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Coroutine, Generator, Optional, Union, cast

import magic
import telegram
from sqlalchemy import and_, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, MessageLimit, ParseMode, ReactionEmoji
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown
from telethon.sync import TelegramClient as TelethonClient
from tqdm.asyncio import tqdm

from ..bot import ensure_chat
from ..integrations import _summarize, _translate_topic, transcribe_file
from ..integrations.deepl import _translate_text
from ..logging import getLogger
from ..models import (
    Summary,
    TelegramChat,
    TelegramUser,
    Topic,
    TopicTranslation,
    Transcript,
)
from ..models.session import DbSessionContext, Session, session_context
from . import AdminChannelMessage, BotDocument, BotMessage
from .constants import LANG_TO_RECEIVED_MESSAGE
from .exceptions import EmptyTranscription, NoActivePremium
from .premium import check_premium_features

# Enable logging
_logger = getLogger(__name__)

mimetype_pattern = re.compile(r"(?P<type>\w+)/(?P<subtype>\w+)")


async def process_transcription_request_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await check_premium_features(update, context)
    except NoActivePremium:
        return

    _logger.info(f"Transcribing and summarizing message: {update.message}")

    with Session.begin() as session:
        chat = session.get(TelegramChat, update.effective_chat.id)
        if chat is None:
            raise ValueError(f"Could not find chat with id {update.effective_chat.id}")
        chat_language_code = chat.language.code if chat.language else "en"

    text = LANG_TO_RECEIVED_MESSAGE.get(chat_language_code, LANG_TO_RECEIVED_MESSAGE["en"])

    async with asyncio.TaskGroup() as tg:
        start_msg_task = tg.create_task(update.message.reply_text(text))
        bot_response_msg_task = tg.create_task(get_summary_msg(update, context))
        tg.create_task(context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING))

    start_message = start_msg_task.result()
    admin_text = None
    try:
        bot_response_msg, total_cost = bot_response_msg_task.result()
    except EmptyTranscription:
        lang_to_text = {
            "en": "⚠️ Sorry, I could not transcribe the audio. Please try a different file.",
            "de": "⚠️ Entschuldigung, Audiodatei konnte nicht transkribiert werden. Bitte versuche eine andere Datei",
            "es": "⚠️ Lo siento, no pude transcribir el audio. Por favor, inténtalo de nuevo con otro archivo.",
            "ru": "⚠️ Извините, я не смог расшифровать аудиозапись. Пожалуйста, попробуйте другой файл.",
        }
        bot_response_msg = BotMessage(
            chat_id=update.effective_chat.id, text=lang_to_text.get(chat_language_code, lang_to_text["en"])
        )
        if update.effective_message.is_topic_message:
            bot_response_msg.reply_to_message_id = update.effective_message.message_thread_id
        else:
            bot_response_msg.reply_to_message_id = update.effective_message.id
        total_cost = None
        admin_text = (
            f"⚠️ Empty transcript: \nUser {update.effective_user.mention_markdown_v2()}\n"
            f"`/get_file {update.effective_message.effective_attachment.file_id}`"
        )

    if admin_text is None:
        with Session.begin() as session:
            user = session.get(TelegramUser, update.effective_user.id)
            n_summaries = len(user.summaries) if user else 0
        try:
            admin_text = (
                f"📝 Summary \#{n_summaries + 1} created in chat {update.effective_chat.mention_markdown_v2()}"
                f" by user {update.effective_user.mention_markdown_v2()}"
            )
        except TypeError:
            admin_text = (
                f"📝 Summary \#{n_summaries + 1} created by user "
                f"{update.effective_user.mention_markdown_v2()} \(in private chat\)"
            )
        admin_text += escape_markdown(f"\n💰 Cost: $ {total_cost:.6f}" if total_cost else "", version=2)

    admin_channel_msg = AdminChannelMessage(
        text=admin_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message.delete())
        tg.create_task(bot_response_msg.send(context.bot))
        tg.create_task(admin_channel_msg.send(context.bot))


async def get_summary_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Coroutine[Any, Any, BotMessage]:
    context = cast(DbSessionContext, context)
    with Session.begin() as session:
        context.db_session = session
        # check existing transcript via file_unique_id,
        transcript, voice_or_audio_or_document_or_video = _check_existing_transcript(update, context)
        #  if not exist, download audio (async) to tempdir and transcribe
        if transcript is None:
            file_name = _extract_file_name(voice_or_audio_or_document_or_video)
            with tempfile.TemporaryDirectory() as tempdir_path_str:
                # download the file to the folder
                tempdir_path = Path(tempdir_path_str)
                file_path = tempdir_path / file_name
                if voice_or_audio_or_document_or_video.file_size > 20 * 1024 * 1024:
                    await download_large_file(update.effective_chat.id, update.message.message_id, file_path)
                else:
                    file = await voice_or_audio_or_document_or_video.get_file()
                    await file.download_to_drive(file_path)

                if not file_name.suffix:
                    mime = magic.from_file(file_path, mime=True)
                    _, suffix = mime.split("/")
                    file_path.rename(file_path.with_suffix(f".{suffix}"))

                transcript = await transcribe_file(update, context, file_path, voice_or_audio_or_document_or_video)

        session.add(transcript)

        summary = _summarize(update, context, transcript)
        total_cost = summary.total_cost
        bot_msg = _get_summary_message(update, context, summary)
        chat = session.get(TelegramChat, update.effective_chat.id)

        if transcript.reaction_emoji:
            await update.message.set_reaction(ReactionEmoji[transcript.reaction_emoji])

        lang_to_transcript = {
            "en": "Transcript",
            "de": "Transkript",
            "es": "Transcripción",
            "ru": "Транскрипт",
        }
        transcript_button_text = lang_to_transcript.get(update.effective_user.language_code, lang_to_transcript["en"])
        emoji = "📝" if summary.transcript.input_language is None else summary.transcript.input_language.flag_emoji
        # if transcript language is None or chat language, show only one button
        if not bot_msg.text:
            [bot_msg] = list(_full_transcript_callback(update, context, summary.transcript_id))
        elif summary.transcript.input_language is None or summary.transcript.input_language == chat.language:
            button = [
                InlineKeyboardButton(
                    f"{emoji} {transcript_button_text}",
                    callback_data={
                        "fnc": "full_transcript",
                        "kwargs": {"transcript_id": summary.transcript_id},
                    },
                ),
            ]
            bot_msg.reply_markup = InlineKeyboardMarkup([button])
        else:
            buttons = [
                InlineKeyboardButton(
                    f"{emoji} {transcript_button_text}",
                    callback_data={
                        "fnc": "full_transcript",
                        "kwargs": {"transcript_id": summary.transcript_id},
                    },
                ),
                InlineKeyboardButton(
                    f"{chat.language.flag_emoji} {transcript_button_text}",
                    callback_data={
                        "fnc": "full_transcript",
                        "kwargs": {"transcript_id": summary.transcript_id, "translate": True},
                    },
                ),
            ]
            bot_msg.reply_markup = InlineKeyboardMarkup([buttons])

    return bot_msg, total_cost


async def download_large_file(chat_id: int, message_id: int, filepath: Path):
    client = TelethonClient(
        session=None, api_id=os.environ["TELEGRAM_API_ID"], api_hash=os.environ["TELEGRAM_API_HASH"]
    )
    try:
        await client.start(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])
        message = await client.get_messages(chat_id, ids=message_id)
        if message.file:
            _logger.info("Downloading large file")
            with open(filepath, "wb") as fp:
                async for chunk in tqdm(client.iter_download(message)):
                    fp.write(chunk)
            _logger.info(f"File saved to {filepath}")
        else:
            _logger.warning("This message does not contain a file")
    finally:
        await client.log_out()
        await client.disconnect()


@session_context
def _get_summary_message(update: Update, context: DbSessionContext, summary: Summary) -> BotMessage:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    session = context.db_session
    session.add(summary)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

    if chat.language != summary.transcript.input_language:
        stmt = (
            select(TopicTranslation)
            .join(Topic, and_(TopicTranslation.topic_id == Topic.id, Topic.summary == summary))
            .where(TopicTranslation.target_lang == chat.language)
        )

        translations = session.scalars(stmt).all()
        if not translations:
            translations = [
                _translate_topic(update, context, target_language=chat.language, topic=topic)
                for topic in summary.topics
            ]
            session.add_all(translations)
        msg = "\n".join(
            f"- {translation.target_text}" for translation in sorted(translations, key=lambda t: t.topic.order)
        )
    else:
        msg = "\n".join(f"- {topic.text}" for topic in sorted(summary.topics, key=lambda t: t.order))

    hashtags = " ".join(summary.transcript.hashtags) + "\n\n" if summary.transcript.hashtags else ""
    if (summary_language := summary.transcript.input_language) and summary_language != chat.language:
        # add language info if different
        lang_to_lang_prefix = {
            "en": [
                f"Voice message/audio language: {summary.transcript.input_language.flag_emoji}",
                f"Summary language: {chat.language.flag_emoji}",
            ],
            "de": [
                f"Sprachnachricht/Audio-Sprache: {summary.transcript.input_language.flag_emoji}",
                f"Zusammenfassungssprache: {chat.language.flag_emoji}",
            ],
            "es": [
                f"Lenguaje del mensaje de voz/audio: {summary.transcript.input_language.flag_emoji}",
                f"Lenguaje de la resumen: {chat.language.flag_emoji}",
            ],
            "ru": [
                f"Язык аудиосообщения/аудио: {summary.transcript.input_language.flag_emoji}",
                f"Язык резюме: {chat.language.flag_emoji}",
            ],
        }
        prefix_lines = lang_to_lang_prefix.get(chat.language.code, lang_to_lang_prefix["en"])
        prefix = "\n".join(prefix_lines)
        text = f"{hashtags}{prefix}\n\n{msg}"
    else:
        text = f"{hashtags}{msg}"

    return BotMessage(
        chat_id=update.effective_chat.id,
        text=text,
        reply_to_message_id=update.effective_message.message_thread_id
        if update.effective_message.is_topic_message
        else update.effective_message.id,
    )


async def full_transcript_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    wait_msg = await context.bot.send_message(
        update.effective_chat.id,
        "📥 Received your request and processing it....⏳\n Please wait a moment. ☕",
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    await wait_msg.delete()
    for bot_msg in _full_transcript_callback(update, context, **kwargs):
        await bot_msg.send(context.bot)


@session_context
def _full_transcript_callback(
    update: Update, context: DbSessionContext, transcript_id: int, translate: bool = False
) -> Generator[BotMessage, None, None]:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    session = context.db_session
    if transcript_id is None:
        raise ValueError("transcript_id must be given in kwargs.")

    transcript = session.get(Transcript, transcript_id)
    if transcript is None:
        raise ValueError(f"Could not find transcript with id {transcript_id}")
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

    if translate:
        transcript_text = _translate_text(transcript.result, chat.language)
    else:
        transcript_text = transcript.result

    if len(transcript_text) >= MessageLimit.MAX_TEXT_LENGTH:
        yield BotDocument(
            chat_id=update.effective_chat.id,
            reply_to_message_id=update.effective_message.id,
            filename="transcript.txt",
            document=transcript_text.encode("utf-8"),
        )
    else:
        emoji = (
            chat.language.flag_emoji
            if translate
            else transcript.input_language.flag_emoji
            if transcript.input_language
            else "❔"
        )
        lang_to_text = {
            "en": f"*📜 Full transcript in {emoji}:*\n\n",
            "de": f"*📜 Vollständiges Transcript in {emoji}:*\n\n",
            "es": f"*📜 Transcripción completa en {emoji}:*\n\n",
            "ru": f"*📜 Полный транскрипт на {emoji}:*\n\n",
        }
        heading_text = lang_to_text.get(chat.language.code, lang_to_text["en"])
        text = heading_text + f"_{escape_markdown(transcript_text, version=2)}_"
        yield BotMessage(
            chat_id=chat.id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=update.effective_message.message_thread_id
            if update.effective_message.is_topic_message
            else update.effective_message.id,
        )
    return


@session_context
@ensure_chat
def _check_existing_transcript(
    update: telegram.Update, context: DbSessionContext
) -> tuple[
    Optional[Transcript], Union[telegram.Voice, telegram.Audio, telegram.Document, telegram.Video, telegram.VideoNote]
]:
    if update.message is None or (
        update.message.voice is None
        and update.message.audio is None
        and update.message.document is None
        and update.message.video is None
        and update.message.video_note is None
    ):
        raise ValueError("The message must contain a voice or audio or (audio) document or video(note).")

    session = context.db_session
    if session is None:
        raise ValueError("There should be a session attached to context")

    voice_or_audio_or_document_or_video = cast(
        Union[telegram.Voice, telegram.Audio, telegram.Document, telegram.Video, telegram.VideoNote],
        (
            update.message.voice
            or update.message.audio
            or update.message.document
            or update.message.video
            or update.message.video_note
        ),
    )
    file_unique_id = voice_or_audio_or_document_or_video.file_unique_id

    stmt = select(Transcript).where(Transcript.file_unique_id == file_unique_id)
    if transcript := session.scalars(stmt).one_or_none():
        _logger.info(f"Using already existing transcript: {transcript} with file_unique_id: {file_unique_id}")

    return transcript, voice_or_audio_or_document_or_video


def _extract_file_name(
    voice_or_audio_or_document_or_video: Union[
        telegram.Voice, telegram.Audio, telegram.Document, telegram.Video, telegram.VideoNote
    ]
) -> Path:
    if (
        hasattr(voice_or_audio_or_document_or_video, "file_name")
        and voice_or_audio_or_document_or_video.file_name is not None
    ):
        file_name = Path(voice_or_audio_or_document_or_video.file_name)
        sanitized_file_name = voice_or_audio_or_document_or_video.file_unique_id + file_name.suffix
        return Path(sanitized_file_name)

    # else try to extract the suffix via the mime type or use file_name without suffic
    match = None

    if mime_type := voice_or_audio_or_document_or_video.mime_type:
        match = mimetype_pattern.match(mime_type)

    if match is None:
        file_name = voice_or_audio_or_document_or_video.file_unique_id
    else:
        file_name = f"{voice_or_audio_or_document_or_video.file_unique_id}.{match.group('subtype')}"

    return Path(file_name)
