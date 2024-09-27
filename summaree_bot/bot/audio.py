import asyncio
import datetime
import os
import tempfile
from pathlib import Path
from typing import Any, Coroutine, Generator, cast

import magic
from sqlalchemy import and_, extract, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, MessageLimit, ParseMode, ReactionEmoji
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown
from telethon.sync import TelegramClient as TelethonClient
from tqdm.asyncio import tqdm

from ..integrations import (
    _check_existing_transcript,
    _extract_file_name,
    _summarize,
    _translate_topic,
    transcribe_file,
)
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
from .constants import LANG_TO_RECEIVED_AUDIO_MESSAGE
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
                if voice_or_audio.file_size > 20 * 1024 * 1024:
                    await download_large_file(update.effective_chat.id, update.message.message_id, file_path)
                else:
                    file = await voice_or_audio.get_file()
                    await file.download_to_drive(file_path)

                if not file_name.suffix:
                    mime = magic.from_file(file_path, mime=True)
                    _, suffix = mime.split("/")
                    file_path.rename(file_path.with_suffix(f".{suffix}"))

                transcript = await transcribe_file(update, context, file_path, voice_or_audio)

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
            [bot_msg] = list(_full_transcript_callback(update, context, summary.transcript_id, translate=False))
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
    client = TelethonClient("bot", os.environ["TELEGRAM_API_ID"], os.environ["TELEGRAM_API_HASH"])
    try:
        await client.start(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])
        message = await client.get_messages(chat_id, ids=message_id)
        if message.file:
            _logger.info("Downloading large file")
            with open(filepath, "wb") as fp:
                async for chunk in tqdm(client.iter_download(message)):
                    fp.write(chunk)
            print(f"File saved to {filepath}")
        else:
            print("This message does not contain a file")
    finally:
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
        prefix_lines = lang_to_lang_prefix.get(update.effective_user.language_code, lang_to_lang_prefix["en"])
        prefix = "\n".join(prefix_lines)
        text = f"{hashtags}{prefix}\n\n{msg}"
    else:
        text = f"{hashtags}{msg}"

    return BotMessage(chat_id=update.effective_chat.id, text=text)


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
        heading_text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
        text = heading_text + f"_{escape_markdown(transcript_text, version=2)}_"
        yield BotMessage(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    return


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
        user = session.get(TelegramUser, update.effective_user.id)
        n_summaries = len(user.summaries) if user else 0
        file_size = cast(int, voice.file_size if voice else audio.file_size if audio else 0)
        subscription_keyboard = get_subscription_keyboard(update, context)
        if file_size > 10 * 1024 * 1024 and not chat.is_premium_active:
            lang_to_text = {
                "en": r"⚠️ Maximum file size for non\-premium is 10MB\. "
                r"Please send a smaller file or upgrade to `/premium`\.",
                "de": r"⚠️ Die maximale Dateigröße für Nicht\-Premium\-Nutzer beträgt 10MB\. "
                r"Bitte sende eine kleinere Datei oder aktualisiere `/premium`\.",
                "es": r"⚠️ El tamaño máximo de archivo para no\-premium es de 10MB\. "
                r"Envíe un archivo más pequeño o actualice a `/premium`\.",
                "ru": r"⚠️ Максимальный размер файла для не\-премиум составляет 10MB\. "
                r"Отправьте меньший файл или обновитесь до `/premium`\.",
            }
            text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
            await update.message.reply_markdown_v2(
                text,
                reply_markup=subscription_keyboard,
            )
            admin_msg = AdminChannelMessage(
                text=(
                    f"User {update.effective_user.mention_markdown_v2()} tried to send "
                    f"a file than was {escape_markdown(f'{file_size / 1024 / 1024:.2f} MB', version=2)}"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await admin_msg.send(context.bot)
            return

        current_month = datetime.datetime.now(tz=datetime.UTC).month
        summaries_this_month = (
            session.query(Summary)
            .filter(
                extract("month", Summary.created_at) == current_month, Summary.tg_chat_id == update.effective_chat.id
            )
            .all()
        )
        if len(summaries_this_month) >= 5 and not chat.is_premium_active:
            lang_to_text = {
                "en": r"⚠️ Sorry, you have reached the limit of 5 summaries per month\. "
                r"Please consider upgrading to `/premium` to get unlimited summaries\.",
                "de": r"⚠️ Sorry, du hast die Grenze von 5 Zusammenfassungen pro Monat erreicht\. "
                r"Mit Premium erhälts du eine unbegrenzte Anzahl an Zusammenfassungen\.",
                "es": r"⚠️ Lo sentimos, has alcanzado el límite de 5 resúmenes al mes\. "
                r"Considere actualizar a `/premium` para obtener resúmenes ilimitados\.",
                "ru": r"⚠️ Извините, вы достигли ограничения в 5 резюме в месяц\. "
                r"Пожалуйста, рассмотрите возможность обновления до `/premium` для получения неограниченных резюме\.",
            }
            text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
            await update.effective_message.reply_markdown_v2(
                text,
                reply_markup=subscription_keyboard,
            )
            text = f"User {update.effective_user.mention_markdown_v2()} reached the limit of 5 summaries per month\."
            admin_msg = AdminChannelMessage(
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await admin_msg.send(context.bot)
            return

    _logger.info(f"Transcribing and summarizing message: {update.message}")
    text = LANG_TO_RECEIVED_AUDIO_MESSAGE.get(update.effective_user.language_code, LANG_TO_RECEIVED_AUDIO_MESSAGE["en"])
    async with asyncio.TaskGroup() as tg:
        start_msg_task = tg.create_task(update.message.reply_text(text))
        bot_response_msg_task = tg.create_task(get_summary_msg(update, context))
        tg.create_task(context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING))

    start_message = start_msg_task.result()
    bot_response_msg, total_cost = bot_response_msg_task.result()

    try:
        text = (
            f"📝 Summary \#{n_summaries + 1} created in chat {update.effective_chat.mention_markdown_v2()}"
            f" by user {update.effective_user.mention_markdown_v2()}"
        )
    except TypeError:
        text = (
            f"📝 Summary \#{n_summaries + 1} created by user "
            f"{update.effective_user.mention_markdown_v2()} \(in private chat\)"
        )
    text += escape_markdown(f"\n💰 Cost: $ {total_cost:.6f}" if total_cost else "", version=2)
    new_summary_msg = AdminChannelMessage(
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(start_message.delete())
        tg.create_task(bot_response_msg.send(context.bot))
        tg.create_task(new_summary_msg.send(context.bot))
