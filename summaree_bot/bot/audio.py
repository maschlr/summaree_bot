import asyncio
import datetime
import tempfile
from pathlib import Path
from typing import Any, Coroutine, cast

import magic
from sqlalchemy import and_, extract, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

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
from ..models import (
    Language,
    Summary,
    TelegramChat,
    Topic,
    TopicTranslation,
    Transcript,
)
from ..models.session import DbSessionContext, Session, session_context
from . import AdminChannelMessage, BotMessage
from .constants import RECEIVED_AUDIO_MESSAGE
from .premium import get_subscription_keyboard

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
        lang_to_button_text = {
            "en": ["📖 Full transcript", "🪄 Give me more"],
            "de": ["📖 Volles Transcript", "🪄 Mehr Kontext"],
            "es": ["📖 Transcripción completa", "🪄 Más contexto"],
            "ru": ["📖 Полный транскрипт", "🪄 Больше контекста"],
        }
        button_texts = lang_to_button_text.get(update.effective_user.language_code, lang_to_button_text["en"])
        buttons = [
            InlineKeyboardButton(
                button_texts[0],
                callback_data={
                    "fnc": "elaborate",
                    "kwargs": {"transcript_id": summary.transcript_id},
                },
            ),
            InlineKeyboardButton(
                button_texts[1],
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
        prefix_lines = lang_to_lang_prefix.get(update.effective_user.language_code, lang_to_lang_prefix["en"])
        prefix = "\n".join(prefix_lines)
        text = f"{prefix}\n\n{msg}"
    else:
        text = msg

    return BotMessage(chat_id=update.effective_chat.id, text=text)


async def elaborate(update: Update, context: ContextTypes.DEFAULT_TYPE, **kwargs) -> None:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    wait_msg = await context.bot.send_message(
        update.effective_chat.id,
        "📥 Received your request and processing it....⏳\n Please wait a moment. ☕",
    )
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    await wait_msg.delete()
    for bot_msg in _elaborate(update, context, **kwargs):
        await bot_msg.send(context.bot)


async def transcribe_and_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if (
        update.message is None
        or update.effective_chat is None
        or update.effective_user is None
        or ((voice := update.message.voice) is None and (audio := update.message.audio) is None)
    ):
        raise ValueError("The update must contain chat/user/voice/audio message.")

    with Session.begin() as session:
        # check how many transcripts/summaries have already been created in the current month
        chat = session.get(TelegramChat, update.effective_chat.id)

        file_size = cast(int, voice.file_size if voice else audio.file_size if audio else 0)
        subscription_keyboard = get_subscription_keyboard(update, context)
        if file_size > 10 * 1024 * 1024 and not chat.is_premium_active:
            lang_to_text = {
                "en": "⚠️ Maximum file size for non-premium is 10MB. "
                "Please send a smaller file or upgrade to `/premium`.",
                "de": "⚠️ Die maximale Dateigröße für Nicht-Premium-Nutzer beträgt 10MB. "
                "Bitte senden Sie eine kleinere Datei oder aktualisieren Sie Ihre Premium-Lizenz.",
                "es": "⚠️ El tamaño máximo de archivo para no-premium es de 10MB. "
                "Envíe un archivo más pequeño o actualice a `/premium`.",
                "ru": "⚠️ Максимальный размер файла для не-премиум составляет 10MB. "
                "Отправьте меньший файл или обновитесь до `/premium`.",
            }
            text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
            await update.message.reply_markdown_v2(
                escape_markdown(
                    text,
                    2,
                ),
                reply_markup=subscription_keyboard,
            )
            return
        elif file_size > 25 * 1024 * 1024:
            # TODO: openai whisper docs mention possible splitting of files >25MB -> look into/inplement
            # implement using pydub -> split audio into chunks of 25MB and process each chunk
            # split using silence
            lang_to_text = {
                "en": "⚠️ Sorry, the file is too big to be processed (max. 25MB). " "Please send a smaller file.",
                "de": "⚠️ Sorry, die Datei ist zu groß, um zu verarbeiten (max. 25MB). "
                "Bitte senden Sie eine kleinere Datei.",
                "es": "⚠️ Lo sentimos, el archivo es demasiado grande para ser procesado (máximo 25MB). "
                "Envíe un archivo más pequeño.",
                "ru": "⚠️ Извините, файл слишком большой, чтобы быть обработанным (максимум 25MB). "
                "Отправьте меньший файл.",
            }
            text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
            await update.message.reply_text(text)
            return
        current_month = datetime.datetime.now(tz=datetime.UTC).month
        summaries_this_month = (
            session.query(Summary)
            .filter(
                extract("month", Summary.created_at) == current_month, Summary.tg_chat_id == update.effective_chat.id
            )
            .all()
        )
        if len(summaries_this_month) >= 10 and not chat.is_premium_active:
            lang_to_text = {
                "en": "⚠️ Sorry, you have reached the limit of 10 summaries per month. "
                "Please consider upgrading to `/premium` to get unlimited summaries.",
                "de": "⚠️ Sorry, du hast die Grenze von 10 Zusammenfassungen pro Monat erreicht. "
                "Mit Premium erhälts du eine unbegrenzte Anzahl an Zusammenfassungen",
                "es": "⚠️ Lo sentimos, has alcanzado el límite de 10 resúmenes al mes. "
                "Considere actualizar a `/premium` para obtener resúmenes ilimitados.",
                "ru": "⚠️ Извините, вы достигли ограничения в 10 резюме в месяц. "
                "Пожалуйста, рассмотрите возможность обновления до `/premium` для получения неограниченных резюме.",
            }
            text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
            await update.effective_message.reply_markdown_v2(
                escape_markdown(
                    text,
                    2,
                ),
                reply_markup=subscription_keyboard,
            )
            return

    _logger.info(f"Transcribing and summarizing message: {update.message}")
    text = RECEIVED_AUDIO_MESSAGE.get(update.effective_user.language_code, RECEIVED_AUDIO_MESSAGE["en"])
    async with asyncio.TaskGroup() as tg:
        start_msg_task = tg.create_task(update.message.reply_text(text))
        bot_response_msg_task = tg.create_task(get_summary_msg(update, context))
        tg.create_task(context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING))

    start_message = start_msg_task.result()
    bot_response_msg = bot_response_msg_task.result()

    try:
        text = (
            f"📝 New summary created in chat {update.effective_chat.mention_markdown_v2()}"
            f" by user {update.effective_user.mention_markdown_v2()}"
        )
    except TypeError:
        text = f"📝 New summary created by user {update.effective_user.mention_markdown_v2()} \(in private chat\)"
    new_summary_msg = AdminChannelMessage(
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message.delete())
        tg.create_task(bot_response_msg.send(context.bot))
        tg.create_task(new_summary_msg.send(context.bot))


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
